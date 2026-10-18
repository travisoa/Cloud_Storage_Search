"""Headless movie lookup, guarded save and naming.

A movie is identified by a confirmed TMDB ID and year. The workflow only reads
the library's movie category, saves one explicitly selected or uniquely best
video (plus its associated subtitles) into ``片名 (年份)`` and renames only the
new fids it verified there. Old files are never renamed, moved or replaced.
"""
from __future__ import annotations

from copy import deepcopy
import posixpath
import re
import unicodedata

from . import catalog
from .naming import media_info, movie_filename, quality_score, series_prefix
from .planner import _subtitle_suffix
from .providers import AuthenticationError, RiskControlError
from .recovery import _valid_operation, _verify_operation
from .sources import safe_source, source_digest, source_ref
from .workflow import (_digest, _directory, _directory_identity, _error, _fid, _list, _name, _needs,
                       _observe_created_directory, _put, _record_directory_verification,
                       _rename_verified, _resume, _safe_file, _share_tree, _walk, _normal, _collision,
                       legacy_scope_details, legacy_scope_record)


_MOVIE_CATEGORY_NAMES = {"电影", "電影", "movie", "movies", "film", "films"}
_EXTRA = re.compile(r"(?<![a-z])(?:sample|trailer|teaser|featurette|extras?|making[ ._-]?of)(?![a-z])|"
                    r"预告|預告|花絮|片花|彩蛋|幕后|幕後|特辑|特輯|删减片段|刪減片段", re.I)
_MULTIPART = re.compile(r"(?<![a-z0-9])(?:cd|disc|disk|part|pt)[ ._-]?\d{1,2}(?![0-9])|上部|下部|上集|下集", re.I)
_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_MIN_MOVIE_BYTES = 100 * 1024 * 1024


def _request_errors(request):
    errors = []
    for field in ("title", "year", "movie_id", "metadata_source", "library_root_id"):
        if request.get(field) in (None, ""):
            errors.append("confirmed_" + field + "_required")
    title = request.get("title", "")
    if (not isinstance(title, str) or not title.strip() or any(c in title for c in '/\\<>:"|?*')
            or any(unicodedata.category(c) in {"Cc", "Cs"} for c in str(title))):
        errors.append("safe_title_required")
    year = request.get("year")
    if isinstance(year, bool) or not str(year).isdigit() or not 1800 <= int(year) <= 2999:
        errors.append("valid_year_required")
    if str(request.get("metadata_source", "")).casefold() not in {"", "tmdb"}:
        errors.append("movie_identity_requires_tmdb")
    aliases = request.get("aliases", [])
    if not isinstance(aliases, list) or any(not isinstance(value, str) or not value.strip() for value in aliases):
        errors.append("aliases_must_be_a_list_of_titles")
    preferences = request.get("quality_preference", ["2160p", "1080p", "720p"])
    if not isinstance(preferences, list) or not preferences or any(not isinstance(v, str) for v in preferences):
        errors.append("quality_preference_must_be_a_nonempty_list")
    selection = request.get("source_selection", {})
    if not isinstance(selection, dict) or set(selection) - {"movie"} or any(
            not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in selection.values()):
        errors.append("source_selection_must_name_one_movie_candidate")
    if type(request.get("include_media_tags", True)) is not bool:
        errors.append("include_media_tags_must_be_boolean")
    return errors


def _job_identity(request, storage):
    root = str(request["library_root_id"]).strip()
    identifier = str(request["movie_id"]).strip()
    if identifier.isdecimal():
        identifier = str(int(identifier))
    return {"kind": "movie", "provider": str(getattr(storage, "provider", "quark")).strip().casefold(),
            "account_scope": str(getattr(storage, "account_scope", None) or root),
            "metadata_source": "tmdb", "movie_id": identifier, "library_root_id": root}


def _configuration(request):
    return {"title": unicodedata.normalize("NFC", request["title"]), "year": int(request["year"]),
            "category_folder_id": str(request["category_folder_id"]) if request.get("category_folder_id") else None}


def _titles(request):
    values = [request["title"], *request.get("aliases", [])]
    return [value for value in dict.fromkeys(values) if isinstance(value, str) and value.strip()]


def _category(request, storage, report):
    root = str(request["library_root_id"])
    direct = [row for row in _list(storage, root) if _directory(row)]
    explicit = request.get("category_folder_id")
    if explicit:
        matches = [row for row in direct if _fid(row) == str(explicit)]
        if len(matches) != 1:
            _needs(report, "movie_category_not_observed_under_library_root")
            return None
        return matches[0]
    matches = [row for row in direct if _normal(_name(row)) in _MOVIE_CATEGORY_NAMES]
    if len(matches) != 1:
        _needs(report, "unique_existing_movie_category_required",
               directory_candidates=[_safe_file(row) for row in matches])
        return None
    return matches[0]


def _names_title(name, title):
    """A Latin title counts only as a whole word; CJK text has no word boundary."""
    text = unicodedata.normalize("NFKC", name).casefold()
    key = unicodedata.normalize("NFKC", title).strip().casefold()

    def word(char):
        return char.isascii() and char.isalnum()

    start = text.find(key) if key else -1
    while start >= 0:
        end = start + len(key)
        if not ((word(key[0]) and start and word(text[start - 1]))
                or (word(key[-1]) and end < len(text) and word(text[end]))):
            return True
        start = text.find(key, start + 1)
    return False


def _existing(request, storage, category, report):
    """Return (destination folder, existing videos) or NEEDS_INPUT on ambiguity."""
    year = int(request["year"])
    keys = {_directory_identity(title)[0] for title in _titles(request)}
    directories = _walk(storage, _fid(category), directories_only=True, max_depth=3)
    related = []
    for row in directories:
        core, found_year = _directory_identity(_name(row))
        if core in keys and found_year in {None, year}:
            related.append(row)
    # Qualified/unrecognized names are evidence of a possible existing work,
    # not permission to create a parallel directory or reuse that directory.
    # Folders inside a work folder (a matched one, or any with a final year)
    # belong to that work, and a folder naming only other years is another work.
    related_ids = {_fid(row) for row in related}
    by_fid = {_fid(row): row for row in directories}
    works = {_fid(row) for row in directories
             if _fid(row) in related_ids or _directory_identity(_name(row))[1] is not None}

    def inside_work(row):
        parent, seen = str(row.get("parent_id", "")), set()
        while parent in by_fid and parent not in seen:
            if parent in works:
                return True
            seen.add(parent)
            parent = str(by_fid[parent].get("parent_id", ""))
        return False

    peers = [row for row in directories if _fid(row) not in related_ids and not inside_work(row)]
    uncertain = {}
    for title in _titles(request):
        try:
            reviewed = catalog.match_series_dirs(title, peers, year=year)["review"]
        except ValueError:
            continue  # An alias may contain characters disallowed in filenames.
        for row in reviewed:
            years = {int(value) for value in _YEAR.findall(_name(row))}
            if (row.get("reasons") == ["directory_year_mismatch"] or (years and year not in years)
                    or not _names_title(_name(row), title)):
                continue
            uncertain[_fid(row)] = row
    if uncertain:
        _needs(report, "movie_directory_identity_requires_confirmation",
               directory_candidates=[_safe_file(row) for row in uncertain.values()])
        return None, []
    loose = []
    for row in _list(storage, _fid(category)):
        if _directory(row) or catalog.parse_file(_name(row))["kind"] != "video":
            continue
        stem = _normal(posixpath.splitext(_name(row))[0])
        if any(_normal(title) and stem.startswith(_normal(title)) for title in _titles(request)) \
                and str(year) in _YEAR.findall(_name(row)):
            loose.append(row)
    if loose:
        report["existing"] = [_safe_file(row) for row in loose]
        report["existing_basis"] = "loose_video_in_movie_category"
        return None, loose
    if len(related) > 1:
        _needs(report, "movie_directory_has_competing_identity_or_year",
               directory_candidates=[_safe_file(row) for row in related])
        return None, []
    if not related:
        return None, []
    folder = related[0]
    files = [row for row in _walk(storage, _fid(folder), max_depth=3) if not _directory(row)]
    videos = [row for row in files if catalog.parse_file(_name(row))["kind"] == "video"]
    if _directory_identity(_name(folder))[1] is None:
        # A bare title can be another work with the same name. Reuse it only as
        # evidence that something exists; never save into it without a year.
        if videos:
            report["existing"] = [_safe_file(row) for row in videos]
            report["existing_basis"] = "bare_title_directory_contains_video"
            return folder, videos
        _needs(report, "bare_movie_directory_requires_confirmation", directory_candidates=[_safe_file(folder)])
        return None, []
    report["existing"] = [_safe_file(row) for row in videos]
    return folder, videos


def _select(request, storage, search, report):
    titles = [_normal(title) for title in _titles(request)]
    year = int(request["year"])
    report.update(source_candidates=[], source_refs=[], archive_candidates=[], source_review=[])
    entries_by_source, candidates, seen = {}, [], set()
    from .archives import classify_archive
    for hit in search.search(request["title"]):
        if hit.get("provider", "quark") != "quark":
            continue
        try:
            entries = _share_tree(storage, hit)
        except (AuthenticationError, RiskControlError):
            raise
        except Exception as exc:
            report["resource_checks"].append({"source": source_digest(hit.get("url", "")), "status": "unavailable",
                                               **_error(exc, "share_inventory")})
            continue
        source_hash = source_digest(hit.get("url", ""))
        report["resource_checks"].append({"source": source_hash, "status": "enumerated", "file_count": len(entries)})
        try:
            reference = source_ref(hit)
        except (ValueError, TypeError):
            reference = None
        if reference and all(value["source_hash"] != reference["source_hash"] for value in report["source_refs"]):
            report["source_refs"].append(reference)
        entries_by_source[source_hash] = entries
        for entry in entries:
            file = entry["file"]
            key = (source_hash, _fid(file))
            if key in seen:
                continue
            seen.add(key)
            # A multi-title share title is not evidence for each of its files;
            # the title or a confirmed alias must be in the file's own path.
            if not any(title and title in _normal(file["path"]) for title in titles):
                continue
            candidate_id = _digest(key)
            archive = classify_archive(_name(file))
            if archive is not None:
                report["archive_candidates"].append({
                    "candidate_id": candidate_id, **_safe_file(file), "path": file["path"],
                    "share_source": {"source_hash": source_hash}, "classification": archive,
                    "policy_action": (request.get("version_policy") or {}).get("archives", "skip"),
                    "automatically_saved": False, "handling": "explicit_selection_required"})
                continue
            parsed = catalog.parse_file(_name(file))
            if parsed["kind"] != "video":
                continue
            reason = None
            # The nearest text with a year decides: a file name outranks its
            # folders, which outrank the share title.
            years = set()
            for text in (_name(file), *reversed(posixpath.dirname(file["path"]).split("/")), str(hit.get("title", ""))):
                years = {int(value) for value in _YEAR.findall(unicodedata.normalize("NFKC", text))}
                if years:
                    break
            if _EXTRA.search(file["path"]):
                reason = "extra_or_trailer_not_a_feature"
            elif parsed.get("episode_key"):
                reason = "episode_marker_not_a_movie"
            elif _MULTIPART.search(_name(file)):
                reason = "multipart_movie_requires_review"
            elif type(file.get("size")) is not int or file["size"] < _MIN_MOVIE_BYTES:
                reason = "size_unknown_or_too_small_for_feature"
            elif years and year not in years:
                reason = "release_year_mismatch"
            info = media_info(_name(file), posixpath.dirname(file["path"]))
            if reason:
                report["source_review"].append({"candidate_id": candidate_id, **_safe_file(file),
                                                "path": file["path"], "reason": reason})
                continue
            score = quality_score(info, request.get("quality_preference", ["2160p", "1080p", "720p"]))
            decision = None
            if request.get("version_policy") is not None:
                from .version_policy import assess
                decision = assess(request["version_policy"], info, size=file.get("size"),
                                  name=_name(file), path=file["path"])
                score = decision["score"]
            candidate = {"candidate_id": candidate_id, **_safe_file(file), "path": file["path"],
                         "parent": posixpath.dirname(file["path"]), "media_info": info,
                         "share_source": {"source_hash": source_hash}, "score": score,
                         "year_evidence": year in years}
            if decision is not None:
                candidate["version_decision"] = decision
                if not decision["allowed"]:
                    candidate["excluded_by_version_policy"] = True
            report["source_candidates"].append(candidate)
            if decision is None or decision["allowed"]:
                if reference:
                    try:
                        entry["source_ref"] = safe_source(reference, parent_id=str(file["parent_id"]),
                            path=posixpath.dirname(file["path"]),
                            quality_tags=[info[field] for field in ("resolution", "codec", "hdr") if info.get(field)])
                    except (ValueError, TypeError):
                        pass
                candidates.append(dict(entry, candidate_id=candidate_id, media_info=info, score=score,
                                       year_evidence=year in years, source_hash=source_hash))
    choice = request.get("source_selection", {}).get("movie")
    if choice:
        values = [entry for entry in candidates if entry["candidate_id"] == choice]
        if len(values) != 1:
            _needs(report, "selected_movie_candidate_not_observed_requires_reconfirmation")
            return []
    else:
        values = list(candidates)
        if values:
            best = max(entry["score"] for entry in values)
            values = [entry for entry in values if entry["score"] == best]
        if len(values) > 1 and any(entry["year_evidence"] for entry in values):
            values = [entry for entry in values if entry["year_evidence"]]
    if len(values) != 1:
        report["unresolved_movie"] = [{"candidate_id": entry["candidate_id"], **_safe_file(entry["file"]),
                                       "path": entry["file"]["path"], "score": entry["score"],
                                       "media_info": entry["media_info"], "year_evidence": entry["year_evidence"]}
                                      for entry in values]
        _needs(report, "movie_versions_require_selection" if values else
               "movie_archive_candidates_require_explicit_handling" if report["archive_candidates"] else
               "movie_has_no_verified_share_candidates")
        return []
    video = values[0]
    prefix = request["_naming_prefix"]
    video["target_name"] = movie_filename(prefix, posixpath.splitext(_name(video["file"]))[1],
                                          video["media_info"], request.get("include_media_tags", True))
    selected = [video]
    stem = posixpath.splitext(_name(video["file"]))[0]
    targets = set()
    for subtitle in entries_by_source.get(video["source_hash"], []):
        file = subtitle["file"]
        if posixpath.dirname(file["path"]) != posixpath.dirname(video["file"]["path"]):
            continue
        parsed = catalog.parse_file(_name(file))
        if parsed["kind"] != "subtitle":
            continue
        suffix = _subtitle_suffix(posixpath.splitext(_name(file))[0], stem)
        if suffix is None:
            continue
        if type(file.get("size")) is not int or file["size"] <= 0:
            # Saved files are verified by size; an unverifiable subtitle would
            # leave the saved movie unrenamed after the write.
            report["source_review"].append({**_safe_file(file), "reason": "source_size_must_be_positive_integer"})
            _needs(report, "associated_subtitle_size_unverified")
            return []
        target = posixpath.splitext(video["target_name"])[0] + suffix + parsed["extension"]
        if target.casefold() in targets:
            _needs(report, "duplicate_associated_subtitle_target")
            return []
        targets.add(target.casefold())
        selected.append(dict(subtitle, target_name=target, episode_key="movie",
                             candidate_id=_digest((video["source_hash"], _fid(file)))))
    video["episode_key"] = "movie"
    return selected


def run(request, storage, search, journal, apply=False):
    """Plan or execute one confirmed movie; ``apply`` is the user's authorization."""
    request = deepcopy(request)
    report = {"schema_version": 1, "kind": "movie", "mode": "apply" if apply else "plan", "status": "READY",
              "writes_performed": False, "writes_attempted": False, "reasons": [], "existing": [],
              "resource_checks": [], "plan": [], "operations": [], "source_candidates": [], "source_refs": []}
    try:
        if request.get("version_policy") is not None:
            from .version_policy import normalize
            request["version_policy"] = normalize(request["version_policy"])
            report["version_policy"] = deepcopy(request["version_policy"])
        errors = _request_errors(request)
        if errors:
            report["reasons"].extend(errors)
            report["status"] = "NEEDS_INPUT"
            return report
        identity = _job_identity(request, storage)
        key = "movie:" + _digest(identity)
        report["job_key"] = key
        prior = journal.get(key)
        if prior is None:
            legacy = legacy_scope_record(identity, storage, journal, key_prefix="movie")
            if legacy:
                return _needs(report, "legacy_account_scope_requires_confirmation", **legacy_scope_details(legacy[0]))
        if prior:
            previous = prior.get("configuration")
            conflicts = [field for field, value in _configuration(request).items()
                         if not isinstance(previous, dict) or previous.get(field) != value]
            if conflicts and prior.get("state") != "complete":
                return _needs(report, "execution_configuration_changed_requires_reconciliation",
                              conflicting_fields=conflicts, operations=prior.get("operations", []))
        if prior and prior.get("state") != "complete":
            if not apply:
                report.update(status="UNKNOWN", reasons=["unfinished_execution_requires_read_only_reconciliation"],
                              operations=prior.get("operations", []))
                return report
            resumed = _resume(storage, journal, key, prior, report)
            if resumed is not None:
                return resumed
        category = _category(request, storage, report)
        if category is None:
            return report
        folder, videos = _existing(request, storage, category, report)
        if report["status"] == "NEEDS_INPUT":
            return report
        if videos:
            report["status"] = "COMPLETE" if prior else "UP_TO_DATE"
            report["reasons"].append("movie_already_in_library")
            return report
        request["_naming_prefix"] = series_prefix(request["title"], request["year"],
                                                  _name(folder) if folder else None)
        report["naming_prefix"] = request["_naming_prefix"]
        selected = _select(request, storage, search, report)
        if report["status"] == "NEEDS_INPUT":
            return report
        destination = _fid(folder) if folder else None
        if _collision(_list(storage, destination) if destination else [], selected):
            return _needs(report, "source_or_target_filename_conflict")
        directories = [] if destination else [{"parent_id": _fid(category), "name": request["_naming_prefix"]}]
        report.update(directories=directories, target_folder_id=destination,
                      category=_safe_file(category))
        report["plan"] = [{"candidate_id": item["candidate_id"],
                           "source": {**_safe_file(item["file"]), "path": item["file"].get("path", "")},
                           "share_source": {"source_hash": source_digest(item["hit"].get("url", ""))},
                           **({"source_ref": deepcopy(item["source_ref"])} if "source_ref" in item else {}),
                           "target_name": item["target_name"],
                           **({"media_info": deepcopy(item["media_info"])} if "media_info" in item else {})}
                          for item in selected]
        if not apply:
            return report
        record = {"schema_version": 2, "kind": "movie", "state": "ready", "operations": [],
                  "plan": report["plan"], "identity": identity, "configuration": _configuration(request),
                  "target_folder_id": destination}
        _put(journal, key, record)
        for step in directories:
            same = [row for row in _list(storage, step["parent_id"]) if _name(row).casefold() == step["name"].casefold()]
            if same:
                if len(same) != 1 or not _directory(same[0]):
                    return _needs(report, "destination_directory_conflict")
                destination = _fid(same[0])
                continue
            record.update(state="mkdir_pending", directory=dict(step))
            _put(journal, key, record)
            try:
                report["writes_attempted"] = True
                result = storage.mkdir(step["parent_id"], step["name"])
            except Exception as exc:
                record["error"] = _error(exc, "mkdir")
                _put(journal, key, record)
                report.update(status="UNKNOWN", reasons=["directory_creation_outcome_unknown"], error=record["error"])
                return report
            report["writes_performed"] = True
            record["directory"]["returned"] = {"fid": _fid(result), "name": _name(result),
                                               "parent_id": str(result.get("parent_id", "")),
                                               "is_dir": _directory(result)}
            _put(journal, key, record)
            observed, attempts = _observe_created_directory(storage, record["directory"])
            if observed is None:
                report.update(status="UNKNOWN", reasons=["created_directory_not_observed"])
                return report
            _record_directory_verification(journal, key, record, observed, attempts, "after_create", report)
            destination = _fid(observed)
        record.update(state="ready", target_folder_id=destination)
        _put(journal, key, record)
        report["target_folder_id"] = destination
        before = _list(storage, destination)
        if _collision(before, selected):
            record["state"] = "complete"
            _put(journal, key, record)
            return _needs(report, "source_or_target_filename_conflict_before_save")
        if any(catalog.parse_file(_name(row))["kind"] == "video" for row in before if not _directory(row)):
            record["state"] = "complete"
            _put(journal, key, record)
            return _needs(report, "movie_video_appeared_before_save")
        operation = {"state": "save_pending", "target_parent_id": destination,
                     "before_fids": [_fid(row) for row in before],
                     "files": [{"source_fid": _fid(item["file"]), "name": _name(item["file"]),
                                "size": item["file"].get("size"), "episode_key": "movie",
                                "target_name": item["target_name"]} for item in selected]}
        record["operations"].append(operation)
        record["state"] = "save_pending"
        _put(journal, key, record)
        try:
            report["writes_attempted"] = True

            def submitted(evidence):
                expected = [item["source_fid"] for item in operation["files"]]
                task_id = evidence.get("task_id")
                if (not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", task_id)
                        or evidence.get("source_fids") != expected or evidence.get("target_parent_id") != destination):
                    raise ValueError("submitted_task_does_not_match_persisted_intent")
                operation["task_id"] = task_id
                _put(journal, key, record)

            options = ({"on_submitted": submitted}
                       if getattr(storage, "capabilities", {}).get("save_submission_callback") else {})
            result = storage.save_files(selected[0]["share_ref"], [item["file"] for item in selected],
                                        destination, **options)
            operation.update(saved_fids=[str(fid) for fid in result.get("saved_fids", [])],
                             task_id=result.get("task_id"), state="saved")
            if result.get("status") != "done":
                operation["state"], record["state"] = "unknown", "unknown"
                _put(journal, key, record)
                report.update(status="UNKNOWN", reasons=["save_task_not_complete"], operations=record["operations"])
                return report
            record["state"] = "saved"
            report["writes_performed"] = True
            _put(journal, key, record)
        except Exception as exc:
            operation.update(state="unknown", **_error(exc, "save"))
            record["state"] = "unknown"
            _put(journal, key, record)
            status = ("NEEDS_AUTH" if isinstance(exc, AuthenticationError) else
                      "RISK_CONTROL" if isinstance(exc, RiskControlError) else "UNKNOWN")
            report.update(status=status, reasons=["save_outcome_unknown_no_automatic_resubmission"],
                          operations=record["operations"])
            return report
        if not _rename_verified(storage, journal, key, record, operation, report):
            report.update(status=record["state"].upper(), reasons=[operation.get("reason", "rename_incomplete")],
                          operations=record["operations"])
            return report
        record["state"] = "complete"
        _put(journal, key, record)
        report.update(status="COMPLETE", operations=record["operations"])
        return report
    except Exception as exc:
        status = ("NEEDS_AUTH" if isinstance(exc, AuthenticationError) else
                  "RISK_CONTROL" if isinstance(exc, RiskControlError) else "FAILED")
        report.update(status=status, error=_error(exc, "movie_workflow"))
        return report


def reconcile(request, storage, journal):
    """Read-only: query a known task ID and actual files; never save or rename."""
    report = {"kind": "movie", "status": "NO_RECORD", "writes_attempted": False, "writes_performed": False,
              "reasons": [], "operations": []}
    errors = _request_errors(request)
    if errors:
        report.update(status="NEEDS_INPUT", reasons=errors)
        return report
    identity = _job_identity(request, storage)
    key = "movie:" + _digest(identity)
    report["job_key"] = key
    record = journal.get(key)
    if not record:
        legacy = legacy_scope_record(identity, storage, journal, key_prefix="movie")
        if legacy:
            return _needs(report, "legacy_account_scope_requires_confirmation", **legacy_scope_details(legacy[0]))
        return report
    report["operations"] = record.get("operations", [])
    if record.get("identity") != identity:
        return _needs(report, "record_identity_requires_confirmation")
    if record.get("state") == "ready" and not record.get("operations"):
        report.update(status="READY", reasons=["no_save_was_submitted"])
        return report
    if record.get("state") == "mkdir_pending":
        observed, _ = _observe_created_directory(storage, record.get("directory", {}))
        if observed is None:
            report.update(status="UNKNOWN", reasons=["directory_creation_outcome_requires_reconciliation"])
            return report
        _record_directory_verification(journal, key, record, observed, 1, "reconcile", report)
    operations = record.get("operations", [])
    if not isinstance(operations, list) or any(not _valid_operation(item) for item in operations):
        return _needs(report, "operation_requires_exact_source_names_sizes_and_before_snapshot")
    if not operations:
        report.update(status="READY", reasons=["no_save_was_submitted"])
        return report
    category = _category(request, storage, report)
    if category is None:
        return report
    folder, _ = _existing(request, storage, category, report)
    if report["status"] == "NEEDS_INPUT":
        return report
    if folder is None or any(item["target_parent_id"] != _fid(folder) for item in operations):
        return _needs(report, "operation_target_not_in_observed_destination_scope")
    complete = True
    for operation in operations:
        if operation.get("state") in {"save_pending", "unknown"} and operation.get("task_id"):
            task = storage.task_status(operation["task_id"])
            if task.get("task_id", operation["task_id"]) != operation["task_id"]:
                report.update(status="UNKNOWN", reasons=["save_task_status_identity_invalid"])
                return report
            if task.get("status") == "done" and task.get("saved_fids"):
                operation.update(saved_fids=[str(fid) for fid in task["saved_fids"]], state="saved")
                _put(journal, key, record)
        if operation.get("state") in {"save_pending", "unknown"}:
            report.update(status="UNKNOWN", reasons=["previous_save_outcome_unknown_no_resubmission"])
            return report
        verified, reason = _verify_operation(storage, operation)
        previous = operation.get("verified_files")
        if verified is not None and previous is not None:
            old_mapping = ({item.get("source_fid"): item.get("saved_fid") for item in previous}
                           if isinstance(previous, list) and all(isinstance(item, dict) for item in previous)
                           else {})
            if (len(old_mapping) != len(operation["files"])
                    or old_mapping != {item["source_fid"]: item["saved_fid"] for item in verified}):
                verified, reason = None, "saved_file_source_mapping_changed"
        if verified is None:
            operation["state"], record["state"] = "unknown", "unknown"
            operation["reason"] = reason
            _put(journal, key, record)
            report.update(status="NEEDS_INPUT" if reason == "target_filename_conflict_requires_confirmation"
                          else "UNKNOWN", reasons=[reason])
            return report
        operation["verified_files"] = verified
        operation_complete = all(item["verified"] for item in verified)
        operation["state"] = "complete" if operation_complete else "saved"
        complete = complete and operation_complete
    record["state"] = "complete" if complete else "saved"
    _put(journal, key, record)
    report.update(status="COMPLETE" if complete else "READY_TO_RENAME", operations=record["operations"])
    if not complete:
        report["reasons"] = ["saved_files_verified_rename_requires_resume_apply"]
    return report
