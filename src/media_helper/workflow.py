"""Headless, dependency-injected episode reconciliation and guarded execution.

Providers perform HTTP; this module has no browser, credential, or container API.
Only files returned by a completed save task, absent from its before-snapshot,
and subsequently observed in its destination may be renamed.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import hashlib
import json
import posixpath
import re
import time
import unicodedata

from . import catalog
from .naming import episode_filename, media_info, quality_score, series_prefix
from .planner import _subtitle_suffix
from .providers import AuthenticationError, RiskControlError
from .sources import safe_source, source_ref


_TV_CATEGORY_NAMES = {"电视剧", "剧集", "電視劇", "劇集", "tv", "tvshows", "tvseries"}
_TV_SUBCATEGORY_NAMES = {"国产剧", "国产电视剧", "国产", "国剧", "陆剧", "内地剧", "内地电视剧",
                         "大陆剧", "大陆电视剧", "华语剧", "港台剧", "港剧", "台剧", "欧美剧",
                         "美剧", "英剧", "日剧", "韩剧", "日韩剧", "日韩", "泰剧", "海外剧",
                         "其他剧", "其他", "完结剧", "已完结", "连载剧", "连载中", "正在更新"}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _name(file):
    return file.get("name", file.get("file_name", ""))


def _fid(file):
    return str(file.get("fid", file.get("id", "")))


def _directory(file):
    return bool(file.get("is_dir", file.get("dir", False)))


def _normal(value):
    return "".join(c for c in unicodedata.normalize("NFKC", str(value)).casefold()
                   if c.isalnum())


def _safe_file(file):
    """Whitelist persistent fields; share access tokens never leave memory."""
    return {"fid": _fid(file), "name": _name(file),
            "size": file.get("size"), "parent_id": str(file.get("parent_id", ""))}


def _error(exc, step):
    result = {"step": step, "error_type": type(exc).__name__}
    # Provider exception messages and evidence may contain request credentials.
    if getattr(exc, "task_id", None):
        result["task_id"] = str(exc.task_id)
    return result


def _needs(report, reason, **details):
    report.update(status="NEEDS_INPUT", **details)
    if reason not in report["reasons"]:
        report["reasons"].append(reason)
    return report


def _list(storage, parent):
    files = storage.list_files(str(parent))
    if not isinstance(files, list):
        raise ValueError("provider_inventory_must_be_a_list")
    identifiers = [_fid(file) for file in files]
    if any(not fid for fid in identifiers) or len(set(identifiers)) != len(identifiers):
        raise ValueError("inventory_requires_unique_stable_fids")
    return files


def _walk(storage, parent, *, directories_only=False, max_depth=8, max_entries=10000):
    result, seen, stack = [], set(), [(str(parent), "", 0)]
    while stack:
        fid, path, depth = stack.pop()
        if fid in seen:
            raise ValueError("directory_cycle")
        seen.add(fid)
        for item in _list(storage, fid):
            row = dict(item, parent_id=fid, path=posixpath.join(path, _name(item)))
            if not directories_only or _directory(row):
                result.append(row)
            if len(result) > max_entries:
                raise ValueError("inventory_limit_reached")
            if _directory(row):
                if directories_only and depth + 1 >= max_depth:
                    row["scan_depth_limit"] = True
                    continue
                if depth >= max_depth:
                    raise ValueError("inventory_depth_limit_reached")
                stack.append((_fid(row), row["path"], depth + 1))
    return result


def _request_errors(request):
    errors = []
    for field in ("title", "year", "season", "series_id", "metadata_source", "library_root_id"):
        if request.get(field) in (None, ""):
            errors.append("confirmed_" + field + "_required")
    title = request.get("title", "")
    if not isinstance(title, str) or not title.strip() or any(
        c in title for c in '/\\<>:"|?*'
    ) or any(unicodedata.category(c) in {"Cc", "Cs"} for c in str(title)):
        errors.append("safe_title_required")
    for field, lower, upper in (("year", 1800, 2999), ("season", 1, 99)):
        value = request.get(field)
        if isinstance(value, bool) or not str(value).isdigit() or not lower <= int(value) <= upper:
            errors.append("valid_" + field + "_required")
    aliases = request.get("aliases", [])
    if not isinstance(aliases, list) or any(not isinstance(value, str) or not value.strip() for value in aliases):
        errors.append("aliases_must_be_a_list_of_titles")
    preferences = request.get("quality_preference", ["2160p", "1080p", "720p"])
    if not isinstance(preferences, list) or not preferences or any(not isinstance(value, str) or not value.strip() for value in preferences):
        errors.append("quality_preference_must_be_a_nonempty_list")
    if not isinstance(request.get("source_selection", {}), dict):
        errors.append("source_selection_must_be_an_episode_mapping")
    if type(request.get("include_media_tags", True)) is not bool:
        errors.append("include_media_tags_must_be_boolean")
    mapping = request.get("source_episode_mapping", {})
    if not isinstance(mapping, dict):
        errors.append("source_episode_mapping_must_be_a_candidate_mapping")
    else:
        for identifier, entry in mapping.items():
            valid = (isinstance(identifier, str) and re.fullmatch(r"[0-9a-f]{64}", identifier)
                     and isinstance(entry, dict) and set(entry) == {"season", "episode", "name", "size"})
            if valid:
                valid = (all(type(entry[field]) is int and lower <= entry[field] <= upper
                             for field, lower, upper in (("season", 1, 99), ("episode", 1, 999)))
                         and str(request.get("season")).isdigit()
                         and entry["season"] == int(request["season"])
                         and isinstance(entry["name"], str) and bool(entry["name"])
                         and type(entry["size"]) is int and entry["size"] > 0)
            if not valid:
                errors.append("source_episode_mapping_requires_valid_candidate_name_size_and_requested_season")
                break
    return errors


def _job_identity(request, storage):
    """Cloud scope and metadata identity survive aliases and destination edits."""
    root = str(request["library_root_id"]).strip()
    identifier = str(request["series_id"]).strip()
    if identifier.isdecimal():
        identifier = str(int(identifier))
    return {"provider": str(getattr(storage, "provider", "quark")).strip().casefold(),
            "account_scope": str(getattr(storage, "account_scope", None) or root),
            "metadata_source": str(request["metadata_source"]).strip().casefold(),
            "series_id": identifier, "season": int(request["season"]),
            "library_root_id": root}


def _execution_config(request):
    return {"title": unicodedata.normalize("NFC", request["title"]),
            "year": int(request["year"]),
            "target_folder_id": str(request["target_folder_id"]) if request.get("target_folder_id") else None,
            "category_folder_id": str(request["category_folder_id"]) if request.get("category_folder_id") else None,
            "library_root_is_series_category": request.get("library_root_is_series_category") is True,
            "explicit_target_is_series_directory": request.get("explicit_target_is_series_directory") is True}


def _configuration_conflicts(request, record):
    previous = record.get("configuration")
    if not isinstance(previous, dict):
        # Unfinished records lacking execution configuration cannot safely be
        # reinterpreted using a newer title or destination supplied by a caller.
        return ["unrecorded_execution_configuration"] if record.get("state") != "complete" else []
    current = _execution_config(request)
    conflicts = []
    for field, value in current.items():
        if value == previous.get(field):
            continue
        # Making an already observed destination explicit does not create a
        # new operation identity or redirect a pending save/rename.
        if field == "target_folder_id" and previous.get(field) is None:
            observed = {str(operation["target_parent_id"]) for operation in record.get("operations", [])
                        if operation.get("target_parent_id")}
            if record.get("target_folder_id"):
                observed.add(str(record["target_folder_id"]))
            if value in observed:
                continue
        conflicts.append(field)
    return conflicts


def _series_identity(request, metadata, report):
    errors = _request_errors(request)
    if errors:
        if request.get("title") and not request.get("series_id"):
            report["metadata_candidates"] = metadata.search_tv(request["title"], request.get("year"))
        report["reasons"].extend(errors)
        report["status"] = "NEEDS_INPUT"
        return None
    series = metadata.get_series(request["series_id"])
    if not isinstance(series, dict):
        _needs(report, "metadata_series_response_invalid")
        return None
    actual_id = series.get("source_id", series.get("id"))
    if actual_id is not None and str(actual_id) != str(request["series_id"]):
        _needs(report, "metadata_identity_id_mismatch")
        return None
    if series.get("source") and str(series["source"]).casefold() != str(request["metadata_source"]).casefold():
        _needs(report, "metadata_identity_source_mismatch")
        return None
    if series.get("year") is not None and str(series["year"]) != str(request["year"]):
        _needs(report, "metadata_identity_year_mismatch")
        return None
    titles = [series.get("title"), series.get("name"), series.get("original_title"),
              series.get("original_name"), *series.get("aliases", [])]
    titles = [str(title) for title in titles if isinstance(title, str) and title]
    if titles and _normal(request["title"]) not in {_normal(title) for title in titles}:
        _needs(report, "metadata_title_requires_confirmation", metadata_candidates=[series])
        return None
    return series


def _directory_identity(name):
    name = unicodedata.normalize("NFKC", name).strip().casefold()
    year = re.search(r"(?:\s*\((\d{4})\)|\s*\[(\d{4})\]|\s+(\d{4}))$", name)
    return (name[:year.start()].strip(), int(next(value for value in year.groups() if value))) if year else (name, None)


def _library_directories(request, storage, root):
    """Locate a show by directory names without opening unrelated shows."""
    titles = {_directory_identity(title)[0] for title in request.get("_confirmed_titles", [request["title"]])}

    def related(row):
        return _directory_identity(_name(row))[0] in titles

    def subcategory(row):
        name = _normal(_name(row))
        return name in _TV_CATEGORY_NAMES | _TV_SUBCATEGORY_NAMES or bool(
            re.fullmatch(r"(?:19|20)\d{2}(?:年)?(?:剧集|电视剧|新剧|更新)?", name))

    def scan_category(parent, path, depth, stop_after_listing=False):
        rows = [dict(item, parent_id=parent, path=posixpath.join(path, _name(item)), discovery_depth=depth + 1)
                for item in _list(storage, parent) if _directory(item)]
        # All same-level identities remain in the result for conflict checks.
        # Existing title evidence takes us straight to that show, not its peers.
        if stop_after_listing or any(related(row) for row in rows):
            return rows
        result = list(rows)
        for row in rows:
            if not subcategory(row):
                continue
            if depth + 1 >= 5:
                row["scan_depth_limit"] = True
                continue
            result.extend(scan_category(_fid(row), row["path"], depth + 1))
            if len(result) > 10000:
                raise ValueError("inventory_limit_reached")
        return result

    direct = [dict(item, parent_id=root, path=_name(item))
              for item in _list(storage, root) if _directory(item)]
    if request.get("library_root_is_series_category") is True or str(request.get("category_folder_id", "")) == root:
        directories = scan_category(root, "", 0)
    else:
        categories = [item for item in direct if _normal(_name(item)) in _TV_CATEGORY_NAMES]
        explicit_category = str(request.get("category_folder_id", ""))
        category = next((item for item in direct if _fid(item) == explicit_category), None)
        if category is None and len(categories) == 1:
            category = categories[0]
        directories = list(direct)
        if category is not None:
            directories.extend(scan_category(_fid(category), _name(category), 1,
                                               stop_after_listing=any(related(row) for row in direct)))
    # An explicit destination outside the default TV scope requires observed
    # ancestry. This broader lookup is only entered for that explicit request.
    requested = {str(request[field]) for field in ("target_folder_id", "category_folder_id")
                 if request.get(field) and str(request[field]) != root}
    if requested - {_fid(item) for item in directories}:
        for candidate in list(directories):
            if related(candidate):
                directories.extend(_walk(storage, _fid(candidate), directories_only=True,
                                         max_depth=max(1, 5 - candidate.get("discovery_depth", 1))))
        if not requested - {_fid(item) for item in directories}:
            return directories
        return _walk(storage, root, directories_only=True, max_depth=5)
    return directories


def _bare_series_match(request, storage, directories, report):
    """Accept one bare title inside TV only with existing named episode proof."""
    root = str(request["library_root_id"])
    categories = [row for row in directories if str(row.get("parent_id")) == root
                  and _normal(_name(row)) in _TV_CATEGORY_NAMES]
    if len(categories) != 1:
        return None
    category_id = _fid(categories[0])
    parents = {_fid(row): str(row.get("parent_id", "")) for row in directories}

    def in_category(row):
        parent, seen = str(row.get("parent_id", "")), set()
        while parent and parent not in seen:
            if parent == category_id:
                return True
            seen.add(parent)
            parent = parents.get(parent, "")
        return False

    exact = lambda text: unicodedata.normalize("NFKC", text).strip().casefold()
    titles = request.get("_confirmed_titles", [request["title"]])
    title_keys = {exact(title) for title in titles}
    bare, related = [], []
    for row in directories:
        in_tv = in_category(row)
        if not in_tv and str(row.get("parent_id")) != root:
            continue
        name = exact(_name(row))
        year_marker = re.search(r"(?:\s*\((\d{4})\)|\s*\[(\d{4})\]|\s+(\d{4}))$", name)
        core = name[:year_marker.start()].strip() if year_marker else name
        if core in title_keys:
            related.append(row)
            if not year_marker and in_tv:
                bare.append(row)
    if not bare:
        return None
    if len(bare) != 1 or len(related) != 1:
        _needs(report, "bare_series_directory_has_competing_identity_or_year",
               directory_candidates=[_safe_file(row) for row in related])
        return None
    folder = bare[0]
    files = _walk(storage, _fid(folder))
    inventory = catalog.build_episode_inventory([row for row in files if not _directory(row)])
    evidence = []
    valid = not inventory["review"]
    for key, rows in inventory["existing"].items():
        for row in rows:
            stem = exact(posixpath.splitext(_name(row))[0])
            named = any(stem.startswith(title) and
                        (stem[len(title):].startswith((".", " ", "_", "-", "第")) or
                         re.match(r"(?i)s\d+e\d+", stem[len(title):]))
                        for title in title_keys)
            if not named:
                valid = False
            if named and key.startswith(f'S{int(request["season"]):02d}E'):
                evidence.append(_safe_file(row))
    if not valid or not evidence:
        _needs(report, "bare_series_directory_lacks_unambiguous_named_episode_evidence",
               directory_candidates=[_safe_file(folder)])
        return None
    report["directory_match_basis"] = {"method": "unique_bare_title_with_existing_episode_evidence",
                                       "directory": _safe_file(folder), "evidence": evidence}
    return folder, files


def _destination(request, storage, report):
    root, season = str(request["library_root_id"]), int(request["season"])
    directories = _library_directories(request, storage, root)
    explicit = request.get("target_folder_id")
    if explicit:
        explicit = str(explicit)
        if explicit == root and request.get("explicit_target_is_series_directory") is not True:
            _needs(report, "library_root_is_not_a_confirmed_series_destination")
            return None, [], []
        if explicit != root and explicit not in {_fid(row) for row in directories}:
            _needs(report, "target_folder_not_observed_under_library_root")
            return None, [], []
        # Explicit season destinations keep the observed show ancestor as the
        # title prefix. A directory named Season 01 can never become the title.
        rows = {_fid(row): row for row in directories}
        current, observed = explicit, None
        while current in rows:
            row = rows[current]
            if not re.fullmatch(r"(?:season[ ._-]*\d+|第\s*\d+\s*季)", _name(row), re.I):
                observed = _name(row)
                break
            current = str(row.get("parent_id", ""))
        request["_naming_prefix"] = series_prefix(request["title"], request["year"], observed)
        report["naming_prefix"] = request["_naming_prefix"]
        report["series_directory_name"] = request["_naming_prefix"]
        files = _walk(storage, explicit)
        return explicit, files, []
    bare_match = _bare_series_match(request, storage, directories, report)
    if report["status"] == "NEEDS_INPUT":
        return None, [], []
    if bare_match:
        matches = {"matches": [bare_match[0]], "review": []}
    else:
        confirmed = request.get("_confirmed_titles", [request["title"]])
        title_keys = {_directory_identity(title)[0] for title in confirmed}
        related = [row for row in directories if _directory_identity(_name(row))[0] in title_keys]
        if len({_fid(row) for row in related}) > 1:
            _needs(report, "series_directory_has_competing_identity_or_year",
                   directory_candidates=[_safe_file(row) for row in related])
            return None, [], []
        found, review = {}, {}
        for title in confirmed:
            result = catalog.match_series_dirs(title, directories, year=int(request["year"]))
            found.update({_fid(row): row for row in result["matches"]})
            review.update({_fid(row): row for row in result["review"]})
        matches = {"matches": list(found.values()), "review": list(review.values())}
    if len(matches["matches"]) > 1 or (not matches["matches"] and matches.get("review")):
        _needs(report, "library_series_directory_ambiguous",
               directory_candidates=[_safe_file(row) for row in matches["matches"] + matches.get("review", [])])
        return None, [], []
    if not matches["matches"]:
        if any(row.get("scan_depth_limit") for row in directories):
            _needs(report, "library_directory_scan_incomplete_no_new_series_created")
            return None, [], []
        category_id = request.get("category_folder_id")
        if category_id:
            category_id = str(category_id)
            if category_id != root and category_id not in {_fid(row) for row in directories}:
                _needs(report, "category_folder_not_observed_under_library_root")
                return None, [], []
        elif request.get("library_root_is_series_category") is True:
            category_id = root
        else:
            categories = [row for row in directories if str(row.get("parent_id")) == root
                          and _normal(_name(row)) in _TV_CATEGORY_NAMES]
            if len(categories) != 1:
                _needs(report, "unique_existing_series_category_required",
                       directory_candidates=[_safe_file(row) for row in categories])
                return None, [], []
            category_id = _fid(categories[0])
        request["_naming_prefix"] = series_prefix(request["title"], request["year"])
        report["naming_prefix"] = request["_naming_prefix"]
        report["series_directory_name"] = request["_naming_prefix"]
        return None, [], [{"parent_id": category_id, "name": request["_naming_prefix"]},
                          {"parent_step": 0, "name": f"Season {season:02d}"}]
    series_folder = _fid(matches["matches"][0])
    request["_naming_prefix"] = series_prefix(request["title"], request["year"], _name(matches["matches"][0]))
    report["naming_prefix"] = request["_naming_prefix"]
    report["series_directory_name"] = request["_naming_prefix"]
    files = bare_match[1] if bare_match else _walk(storage, series_folder)
    season_names = {f"season{season:02d}", f"season{season}", f"第{season}季"}
    season_dirs = [row for row in files if _directory(row) and _normal(_name(row)) in season_names]
    if len(season_dirs) > 1:
        _needs(report, "multiple_existing_season_directories")
        return None, [], []
    if season_dirs:
        return _fid(season_dirs[0]), files, []
    # An established flat series directory remains flat. Do not reorganize it.
    direct_videos = [row for row in files if not _directory(row) and row["parent_id"] == series_folder
                     and catalog.parse_file(_name(row), season=season)["kind"] == "video"]
    if direct_videos:
        return series_folder, files, []
    return None, files, [{"parent_id": series_folder, "name": f"Season {season:02d}"}]


def _share_tree(storage, hit, max_entries=5000):
    url = hit.get("url", hit.get("link"))
    if not url:
        return []
    parent, path = str(hit.get("parent_id", "0")), str(hit.get("path", ""))
    # A remembered directory path also supplies media labels (for example 4K).
    # Re-read its real ancestry before using those labels: a share owner can
    # rename or replace a directory while retaining the selected parent fid.
    if parent != "0" or path:
        parts = path.split("/")
        if (parent == "0" or not path or len(parts) > 8
                or any(part in {"", ".", ".."} for part in parts)):
            raise ValueError("preferred_share_directory_path_requires_reconfirmation")
        observed, ancestors = "0", {"0"}
        for part in parts:
            listing = storage.share_files(url, password=hit.get("password"), parent_id=observed)
            matches = [item for item in listing["files"]
                       if _directory(item) and _name(item) == part]
            if len(matches) != 1 or not _fid(matches[0]) or _fid(matches[0]) in ancestors:
                raise ValueError("preferred_share_directory_path_not_observed")
            observed = _fid(matches[0])
            ancestors.add(observed)
        if observed != parent:
            raise ValueError("preferred_share_directory_identity_changed")
    result, stack, seen = [], [(parent, path, 0)], set()
    while stack:
        parent, path, depth = stack.pop()
        if parent in seen:
            raise ValueError("share_directory_cycle")
        seen.add(parent)
        listing = storage.share_files(url, password=hit.get("password"), parent_id=parent)
        share_ref = listing["share_ref"]
        for file in listing["files"]:
            item = dict(file)
            item["parent_id"] = str(parent)
            item["path"] = posixpath.join(path, _name(item))
            if not _fid(item):
                raise ValueError("share_file_without_fid")
            if _directory(item):
                if depth >= 8:
                    raise ValueError("share_depth_limit_reached")
                stack.append((_fid(item), item["path"], depth + 1))
            else:
                result.append({"file": item, "share_ref": share_ref, "hit": hit})
            if len(result) + len(stack) > max_entries:
                raise ValueError("share_inventory_limit_reached")
    return result


def _select(request, storage, search, missing, report):
    needed = {row["episode_key"] if row.get("episode_key") else
              f'S{int(row["season"]):02d}E{int(row["episode"]):02d}' for row in missing}
    candidates, all_files, source_review = defaultdict(list), [], []
    report["source_candidates"] = []
    report["source_refs"] = []
    report['archive_candidates'] = []
    mapping = request.get("source_episode_mapping", {})
    mapping_seen, mapping_review = set(), []
    hits = search.search(request["title"])
    titles = [_normal(request["title"]), *[_normal(t) for t in request.get("aliases", [])]]
    seen = set()
    for hit in hits:
        if hit.get("provider", "quark") != "quark":
            continue
        try:
            entries = _share_tree(storage, hit)
        except (AuthenticationError, RiskControlError):
            raise
        except Exception as exc:
            report["resource_checks"].append({"source": _digest(hit.get("url", "")),
                                               "status": "unavailable", **_error(exc, "share_inventory")})
            continue
        report["resource_checks"].append({"source": _digest(hit.get("url", "")),
                                           "status": "enumerated", "file_count": len(entries)})
        try:
            reference = source_ref(hit)
        except (ValueError, TypeError):
            reference = None
        if reference and not any(value["source_hash"] == reference["source_hash"] for value in report["source_refs"]):
            report["source_refs"].append(reference)
        all_files.extend(entries)
        for entry in entries:
            file = entry["file"]
            parsed = catalog.parse_file(_name(file), season=int(request["season"]))
            source_key = (_digest(hit.get("url", "")), _fid(file))
            if source_key in seen:
                continue
            seen.add(source_key)
            context = _normal(str(hit.get("title", "")) + " " + file["path"])
            if not any(title and title in context for title in titles):
                continue
            candidate_id = _digest(source_key)
            from .archives import classify_archive
            archive = classify_archive(_name(file))
            if archive is not None:
                report['archive_candidates'].append({'candidate_id': candidate_id, **_safe_file(file),
                    'path': file['path'], 'share_source': {'source_hash': source_key[0]},
                    'classification': archive,
                    'policy_action': (request.get('version_policy') or {}).get('archives', 'skip'),
                    'automatically_saved': False, 'handling': 'explicit_selection_required'})
            info = media_info(_name(file), posixpath.dirname(file["path"]))
            if reference:
                try:
                    entry["source_ref"] = safe_source(reference, parent_id=str(file["parent_id"]),
                        path=posixpath.dirname(file["path"]),
                        quality_tags=[info[field] for field in ("resolution", "codec", "hdr") if info.get(field)])
                except (ValueError, TypeError):
                    # Unsafe directory text must not enter a reusable source.
                    pass
            score = quality_score(info, request.get("quality_preference", ["2160p", "1080p", "720p"]))
            decision = None
            if request.get('version_policy') is not None and parsed['kind'] == 'video':
                from .version_policy import assess
                decision = assess(request['version_policy'], info, size=file.get('size'),
                                  name=_name(file), path=file['path'])
                score = decision['score']
            candidate = None
            if parsed["kind"] == "video":
                candidate = {"candidate_id": candidate_id, **_safe_file(file), "path": file["path"],
                             "parent": posixpath.dirname(file["path"]), "media_info": info,
                             "share_source": {"source_hash": source_key[0]}, "score": score,
                             "reasons": list(parsed["reasons"])}
                if decision is not None:
                    candidate['version_decision'] = decision
                if parsed.get("episode_key") and not parsed["ambiguous"]:
                    candidate["episode_key"] = parsed["episode_key"]
                elif (parsed["reasons"] == ["episode_unknown_no_sort_order_inference"]
                      and re.fullmatch(r"[0-9]+", parsed.get("stem") or "")
                      and 1 <= int(parsed["stem"]) <= 999):
                    candidate["suggested_episode"] = int(parsed["stem"])
                report["source_candidates"].append(candidate)
            confirmed = mapping.get(candidate_id)
            if confirmed is not None:
                mapping_seen.add(candidate_id)
                reason = None
                if (confirmed["name"] != _name(file) or type(file.get("size")) is not int
                        or confirmed["size"] != file["size"]):
                    reason = "confirmed_source_name_or_size_changed"
                elif (parsed["kind"] != "video"
                      or parsed["reasons"] != ["episode_unknown_no_sort_order_inference"]
                      or not re.fullmatch(r"[0-9]+", parsed.get("stem") or "")):
                    reason = "mapping_requires_a_bare_numeric_video_without_other_ambiguity"
                elif int(parsed["stem"]) != confirmed["episode"]:
                    reason = "confirmed_episode_disagrees_with_numeric_filename"
                if reason:
                    mapping_review.append({"candidate_id": candidate_id, **_safe_file(file), "reason": reason})
                    continue
                parsed = dict(parsed, season=confirmed["season"], episode=confirmed["episode"],
                              episode_key=f'S{confirmed["season"]:02d}E{confirmed["episode"]:02d}',
                              ambiguous=False, reasons=[])
                candidate.update(episode_key=parsed["episode_key"], reasons=[],
                                 episode_basis="explicit_source_episode_mapping")
                entry["source_episode_mapping"] = {
                    "candidate_id": candidate_id, **_safe_file(file),
                    "season": confirmed["season"], "episode": confirmed["episode"],
                    "episode_key": parsed["episode_key"], "basis": "explicit_source_episode_mapping"}
            if parsed["kind"] != "video":
                continue
            if decision is not None and not decision['allowed']:
                continue
            if parsed["ambiguous"] or file.get("size") == 0:
                source_review.append({**_safe_file(file), "reasons": parsed["reasons"]})
                continue
            key = parsed.get("episode_key")
            if key in needed:
                entry.update(episode_key=key, candidate_id=candidate_id,
                             media_info=info,
                             target_name=episode_filename(request["_naming_prefix"], parsed["season"],
                                 parsed["episode"], parsed["extension"], info, request.get("include_media_tags", True)),
                             score=score)
                candidates[key].append(entry)
    for identifier in sorted(set(mapping) - mapping_seen):
        mapping_review.append({"candidate_id": identifier, "reason": "confirmed_source_not_observed"})
    if mapping_review:
        _needs(report, "source_episode_mapping_requires_reconfirmation", source_episode_mapping_review=mapping_review)
        return []
    selected, alternatives = [], {}
    for key in sorted(needed):
        values = candidates[key]
        choice = request.get("source_selection", {}).get(key)
        if choice:
            values = [entry for entry in values if entry["candidate_id"] == choice or
                      isinstance(choice, dict) and _fid(entry["file"]) == str(choice.get("fid"))
                      and entry["hit"].get("url") == choice.get("url")]
        if values:
            best = max(entry["score"] for entry in values)
            values = [entry for entry in values if entry["score"] == best]
        if len(values) != 1:
            alternatives[key] = [{"candidate_id": entry["candidate_id"],
                                  **_safe_file(entry["file"]), "score": entry["score"],
                                  "media_info": entry["media_info"]} for entry in values]
            continue
        video = values[0]
        selected.append(video)
        stem = posixpath.splitext(_name(video["file"]))[0]
        subtitle_targets = set()
        subtitle_sources = set()
        for subtitle in all_files:
            if subtitle["hit"].get("url") != video["hit"].get("url"):
                continue
            file = subtitle["file"]
            if _fid(file) in subtitle_sources:
                continue
            if posixpath.dirname(file["path"]) != posixpath.dirname(video["file"]["path"]):
                continue
            parsed = catalog.parse_file(_name(file), season=int(request["season"]))
            if parsed["kind"] != "subtitle":
                continue
            suffix = _subtitle_suffix(posixpath.splitext(_name(file))[0], stem)
            if suffix is None:
                continue
            target = posixpath.splitext(video["target_name"])[0] + suffix + parsed["extension"]
            if target.casefold() in subtitle_targets:
                alternatives[key] = [{"reason": "duplicate_associated_subtitle_target"}]
                continue
            subtitle_targets.add(target.casefold())
            subtitle_sources.add(_fid(file))
            selected.append(dict(subtitle, episode_key=key, target_name=target,
                                 **({"source_ref": deepcopy(video["source_ref"])} if "source_ref" in video else {}),
                                 candidate_id=_digest((_digest(subtitle["hit"].get("url", "")), _fid(file)))))
    if alternatives:
        report["unresolved_episodes"] = alternatives
        if source_review:
            report["source_review"] = source_review
        _needs(report, "resource_versions_or_episode_numbering_require_selection" if any(alternatives.values()) or source_review
               else "missing_episodes_have_no_verified_share_candidates")
        return []
    applied = [entry["source_episode_mapping"] for entry in selected if "source_episode_mapping" in entry]
    if applied:
        report["source_episode_mapping_applied"] = applied
    return selected


def _collision(files, selected):
    names = {_name(file).casefold(): _fid(file) for file in files}
    targets = set()
    source_names = set()
    for item in selected:
        target, source = item["target_name"].casefold(), _name(item["file"]).casefold()
        if target in names or source in names or target in targets or source in source_names:
            return True
        targets.add(target)
        source_names.add(source)
    return False


def _put(journal, key, record):
    journal.put(key, deepcopy(record))


def _observe_created_directory(storage, directory):
    returned = directory.get("returned", {})
    if (not isinstance(returned, dict) or not returned.get("fid")
            or returned.get("name") != directory.get("name")
            or returned.get("parent_id") != directory.get("parent_id")
            or returned.get("is_dir") is not True):
        return None, 0
    for attempt in range(1, 4):
        observed = next((row for row in _list(storage, directory["parent_id"])
                         if _fid(row) == returned["fid"] and _name(row) == directory["name"]
                         and str(row.get("parent_id", "")) == directory["parent_id"] and _directory(row)), None)
        if observed is not None:
            return observed, attempt
        if attempt < 3:
            time.sleep(0.3)
    return None, 3


def _record_directory_verification(journal, key, record, observed, attempts, phase, report):
    evidence = {**_safe_file(observed), "method": "returned_fid_in_complete_parent_listing",
                "read_attempts": attempts, "phase": phase}
    record.setdefault("directory_verifications", []).append(evidence)
    record.update(state="ready", target_folder_id=_fid(observed))
    _put(journal, key, record)
    report["directory_verifications"] = deepcopy(record["directory_verifications"])


def _verify_saved(storage, operation):
    files = _list(storage, operation["target_parent_id"])
    current = {_fid(file): file for file in files}
    ids = operation.get("saved_fids", [])
    if len(ids) != len(operation["files"]) or len(set(ids)) != len(ids):
        return None
    if set(ids) & set(operation["before_fids"]):
        return None
    selected = operation["files"]
    verified = []
    for fid in ids:
        actual = current.get(str(fid))
        if not actual or _directory(actual):
            return None
        matches = [item for item in selected if _name(actual) in {item["name"], item["target_name"]}
                   and (item.get("size") is None or actual.get("size") == item["size"])]
        if len(matches) != 1:
            return None
        verified.append(dict(matches[0], saved_fid=str(fid)))
    if len({item["source_fid"] for item in verified}) != len(selected):
        return None
    return verified


def _rename_verified(storage, journal, key, record, operation, report=None):
    verified = _verify_saved(storage, operation)
    if verified is None:
        operation["state"], record["state"] = "unknown", "unknown"
        operation["reason"] = "saved_fids_not_uniquely_verified_in_destination"
        _put(journal, key, record)
        return False
    operation["verified_files"] = verified
    operation["state"], record["state"] = "rename_pending", "rename_pending"
    _put(journal, key, record)
    for item in verified:
        current = _list(storage, operation["target_parent_id"])
        actual = next((file for file in current if _fid(file) == item["saved_fid"]), None)
        if not actual or _directory(actual):
            operation["reason"] = "new_file_disappeared_before_rename"
            operation["state"], record["state"] = "partial", "partial"
            _put(journal, key, record)
            return False
        conflicts = [file for file in current if _name(file).casefold() == item["target_name"].casefold()
                     and _fid(file) != item["saved_fid"]]
        if conflicts:
            operation["reason"] = "target_name_appeared_before_rename"
            operation["state"], record["state"] = "partial", "partial"
            _put(journal, key, record)
            return False
        if _name(actual) != item["target_name"]:
            if _name(actual) != item["name"]:
                operation["reason"] = "new_file_name_changed_externally"
                operation["state"], record["state"] = "partial", "partial"
                _put(journal, key, record)
                return False
            try:
                if report is not None:
                    report["writes_attempted"] = True
                storage.rename(item["saved_fid"], item["target_name"])
            except Exception as exc:
                operation["error"] = _error(exc, "rename")
                operation["state"], record["state"] = "partial", "partial"
                _put(journal, key, record)
                return False
        final = next((file for file in _list(storage, operation["target_parent_id"])
                      if _fid(file) == item["saved_fid"]), None)
        if not final or _name(final) != item["target_name"]:
            operation["reason"] = "rename_not_observed"
            operation["state"], record["state"] = "partial", "partial"
            _put(journal, key, record)
            return False
        item["verified"] = True
        if report is not None and _name(actual) != item["target_name"]:
            report["writes_performed"] = True
        _put(journal, key, record)
    operation["state"] = "complete"
    _put(journal, key, record)
    return True


def _resume(storage, journal, key, record, report):
    """Recover only durable known saves. Never repeat a save POST."""
    if record.get("state") == "mkdir_pending":
        observed, attempts = _observe_created_directory(storage, record.get("directory", {}))
        if observed is None:
            report.update(status="UNKNOWN", reasons=["directory_creation_outcome_requires_reconciliation"], operations=record.get("operations", []))
            return report
        _record_directory_verification(journal, key, record, observed, attempts, "resume", report)
    for operation in record.get("operations", []):
        if operation["state"] == "complete":
            continue
        if operation["state"] == "ready":
            continue  # No write was sent. A fresh missing-episode plan may replace it.
        if operation["state"] in {"save_pending", "unknown"} and operation.get("task_id") and hasattr(storage, "task_status"):
            try:
                task = storage.task_status(operation["task_id"])
                if task.get("status") == "done" and task.get("saved_fids"):
                    operation["saved_fids"] = [str(fid) for fid in task["saved_fids"]]
                    operation["state"] = "saved"
                    _put(journal, key, record)
            except Exception as exc:
                if isinstance(exc, (AuthenticationError, RiskControlError)):
                    raise
                operation["error"] = _error(exc, "task_recovery")
                _put(journal, key, record)
        if operation["state"] not in {"saved", "rename_pending", "partial"} or not operation.get("saved_fids"):
            report.update(status="UNKNOWN", reasons=["previous_save_outcome_unknown_no_resubmission"], operations=record.get("operations", []))
            return report
        if not _rename_verified(storage, journal, key, record, operation, report):
            report.update(status=record["state"].upper(), reasons=[operation.get("reason", "resume_incomplete")], operations=record["operations"])
            return report
    record["state"] = "complete"
    _put(journal, key, record)
    report["operations"] = record.get("operations", [])
    report["reasons"].append("previous_execution_reconciled")
    return None


def run(request, storage, metadata, search, journal, apply=False, *, resume_only=False):
    """Reconcile an explicitly identified season, optionally saving missing files.

    Required identity fields: title, year, season, series_id, metadata_source,
    library_root_id. target_folder_id can explicitly select an observed root
    descendant. source_selection maps episode keys to candidate IDs (or url/fid).
    ``apply=True`` is the caller's already-established user authorization.
    """
    request = deepcopy(request)
    report = {"schema_version": 1, "mode": "apply" if apply else "plan", "status": "READY",
              "writes_performed": False, "writes_attempted": False, "reasons": [], "existing": [], "missing": [],
              "future": [], "unknown_schedule": [], "resource_checks": [], "plan": [], "operations": []}
    report["source_candidates"] = []
    report["source_refs"] = []
    try:
        if request.get('version_policy') is not None:
            from .version_policy import normalize
            request['version_policy'] = normalize(request['version_policy'])
            report['version_policy'] = deepcopy(request['version_policy'])
        if _request_errors(request):
            _series_identity(request, metadata, report)
            return report
        identity = _job_identity(request, storage)
        request.update(series_id=identity["series_id"], season=identity["season"],
                       metadata_source=identity["metadata_source"], library_root_id=identity["library_root_id"],
                       year=int(request["year"]))
        key = "season:" + _digest(identity)
        report["job_key"] = key
        prior = journal.get(key)
        if prior:
            conflicts = _configuration_conflicts(request, prior)
            if conflicts:
                return _needs(report, "execution_configuration_changed_requires_reconciliation",
                              conflicting_fields=conflicts, operations=prior.get("operations", []))
        if prior and prior.get("state") != "complete":
            if not apply:
                report.update(status="UNKNOWN", reasons=["unfinished_execution_requires_read_only_reconciliation"], operations=prior.get("operations", []))
                return report
            resumed = _resume(storage, journal, key, prior, report)
            if resumed is not None:
                return resumed
        if resume_only:
            # A subscription recovery may finish durable submitted operations,
            # but new missing episodes must pass its source policy separately.
            report.update(status="COMPLETE" if prior else "NO_RECORD",
                          operations=prior.get("operations", []) if prior else [])
            return report
        series = _series_identity(request, metadata, report)
        if series is None:
            return report
        request["_confirmed_titles"] = [request["title"], *[value for key in
            ("title", "name", "original_title", "original_name")
            if isinstance((value := series.get(key)), str) and value],
            *[value for value in series.get("aliases", []) if isinstance(value, str) and value]]
        destination, existing_files, directories = _destination(request, storage, report)
        if report["status"] == "NEEDS_INPUT":
            return report
        inventory = catalog.build_episode_inventory([file for file in existing_files if not _directory(file)], season=int(request["season"]))
        if inventory["review"]:
            return _needs(report, "existing_library_episode_numbering_ambiguous", inventory_review=inventory["review"])
        episodes = metadata.get_season(request["series_id"], int(request["season"]))
        if isinstance(episodes, dict):
            episodes = episodes.get("episodes", [])
        if not isinstance(episodes, list) or not episodes:
            return _needs(report, "metadata_season_episode_schedule_missing")
        as_of = request.get("as_of") or datetime.now(timezone(timedelta(hours=8))).isoformat()
        difference = catalog.calculate_missing(episodes, inventory, as_of=as_of)
        report.update(existing=difference["existing"], missing=difference["missing"], future=difference["future"],
                      unknown_schedule=difference["unknown"], metadata_basis=difference.get("basis", "expected_schedule_only"))
        if not report["missing"]:
            report["status"] = ("COMPLETE" if report["operations"] else "UP_TO_DATE") if not report["unknown_schedule"] else "NEEDS_INPUT"
            if report["unknown_schedule"]:
                report["reasons"].append("unknown_air_dates_cannot_establish_latest_episode")
            return report
        selected = _select(request, storage, search, report["missing"], report)
        if report["status"] == "NEEDS_INPUT":
            return report
        if _collision(_list(storage, destination) if destination else [], selected):
            return _needs(report, "source_or_target_filename_conflict")
        report["directories"] = directories
        report["target_folder_id"] = destination
        report["plan"] = [{"episode_key": item["episode_key"], "candidate_id": item["candidate_id"],
                           "source": {**_safe_file(item["file"]), "path": item["file"].get("path", "")},
                           "share_source": {"source_hash": _digest(item["hit"].get("url", ""))},
                           **({"source_ref": deepcopy(item["source_ref"])} if "source_ref" in item else {}),
                           "target_name": item["target_name"],
                           **({"media_info": deepcopy(item["media_info"])} if "media_info" in item else {})}
                          for item in selected]
        if not apply:
            return report
        record = {"schema_version": 2, "state": "ready", "operations": [], "plan": report["plan"],
                  "identity": identity, "configuration": _execution_config(request),
                  "target_folder_id": destination}
        if prior and prior.get("directory_verifications"):
            record["directory_verifications"] = deepcopy(prior["directory_verifications"])
        _put(journal, key, record)
        created = []
        for step in directories:
            parent = step.get("parent_id") or created[step["parent_step"]]
            same = [file for file in _list(storage, parent) if _name(file).casefold() == step["name"].casefold()]
            if same:
                if len(same) != 1 or not _directory(same[0]):
                    return _needs(report, "destination_directory_conflict")
                result = same[0]
            else:
                record.update(state="mkdir_pending", directory={"parent_id": parent, "name": step["name"]})
                _put(journal, key, record)
                try:
                    report["writes_attempted"] = True
                    result = storage.mkdir(parent, step["name"])
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
                result = observed
            created.append(_fid(result))
            record.update(state="ready", target_folder_id=created[-1])
            _put(journal, key, record)
        destination = destination or created[-1]
        report["target_folder_id"] = destination
        groups = defaultdict(list)
        for item in selected:
            groups[_digest(item["share_ref"])].append(item)
        for items in groups.values():
            before = _list(storage, destination)
            if _collision(before, items):
                record["state"] = "partial" if record["operations"] else "complete"
                _put(journal, key, record)
                return _needs(report, "source_or_target_filename_conflict_before_save", operations=record["operations"])
            current_inventory = catalog.build_episode_inventory(before, season=int(request["season"]))
            if set(current_inventory["existing"]) & {item["episode_key"] for item in items}:
                record["state"] = "complete"
                _put(journal, key, record)
                return _needs(report, "episode_appeared_before_save", operations=record["operations"])
            operation = {"state": "save_pending", "target_parent_id": destination,
                         "before_fids": [_fid(file) for file in before],
                         "files": [{"source_fid": _fid(item["file"]), "name": _name(item["file"]),
                                    "size": item["file"].get("size"), "episode_key": item["episode_key"],
                                    "target_name": item["target_name"]} for item in items]}
            record["operations"].append(operation)
            record["state"] = "save_pending"
            _put(journal, key, record)
            try:
                report["writes_attempted"] = True
                def submitted(evidence):
                    expected = [item["source_fid"] for item in operation["files"]]
                    task_id = evidence.get("task_id")
                    if (not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", task_id)
                            or evidence.get("source_fids") != expected
                            or evidence.get("target_parent_id") != destination):
                        raise ValueError("submitted_task_does_not_match_persisted_intent")
                    operation["task_id"] = task_id
                    _put(journal, key, record)

                options = ({"on_submitted": submitted} if
                           getattr(storage, "capabilities", {}).get("save_submission_callback") else {})
                result = storage.save_files(items[0]["share_ref"], [item["file"] for item in items], destination, **options)
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
                report.update(status=status, reasons=["save_outcome_unknown_no_automatic_resubmission"], operations=record["operations"])
                return report
            if not _rename_verified(storage, journal, key, record, operation, report):
                report.update(status=record["state"].upper(), reasons=[operation.get("reason", "rename_incomplete")], operations=record["operations"])
                return report
        record["state"] = "complete"
        _put(journal, key, record)
        report.update(status="COMPLETE", operations=record["operations"])
        return report
    except Exception as exc:
        # Only the provider exception class and step are exposed, never raw HTTP.
        status = "NEEDS_AUTH" if isinstance(exc, AuthenticationError) else "RISK_CONTROL" if isinstance(exc, RiskControlError) else "FAILED"
        report.update(status=status, error=_error(exc, "workflow"))
        return report


def reconcile(request, storage, metadata, journal):
    """Read-only cloud reconciliation; only local journal evidence may change."""
    from .recovery import reconcile as readback
    return readback(request, storage, metadata, journal)
