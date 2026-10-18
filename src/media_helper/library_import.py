"""Read-only cloud inventory and explicit, journaled import into a new library.

Metadata resolution belongs to the caller. An unresolved resource stays where
it is; this module never infers episode numbers from directory order. Import
changes existing files and therefore requires a separately selected plan and
``apply=True``. Unknown writes are reconciled read-only before another call
may continue, and a write observed as not completed is never simply retried.
"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from copy import deepcopy
import hashlib
import json
import posixpath
import re
import unicodedata

from .journal import safe_record
from .catalog import MetadataError
from .naming import episode_filename, media_info, movie_filename, series_prefix
from .planner import SUBTITLE_EXTENSIONS, VIDEO_EXTENSIONS, _SE, _subtitle_suffix
from .providers import AuthenticationError, RiskControlError
from .archives import classify_archive
from .library_identity import _title_year
from .movie_workflow import _EXTRA


_EXTRA_DIRECTORY = re.compile(r"(?:trailers?|samples?|teasers?|featurettes?)", re.I)


def _fold_title(value):
    return re.sub(r"[._]+", " ", unicodedata.normalize("NFKC", value)).casefold()


def _is_extra(path):
    """Extras words count only outside the work's own title (幕后之王, Extras)."""
    parts = [part for part in path.split("/") if part]
    stem = posixpath.splitext(parts[-1])[0] if parts else ""
    titles = [title for title, year in map(_title_year, [*parts[:-1], stem]) if year and title]
    marker = _SE.search(stem)
    if marker:
        titles.append(stem[:marker.start()].strip(" ._-"))
    keys = sorted({_fold_title(title) for title in titles if title.strip()}, key=len, reverse=True)
    for index, part in enumerate(parts):
        if index < len(parts) - 1 and _EXTRA_DIRECTORY.fullmatch(part):
            return True
        text = _fold_title(part)
        for key in keys:
            text = text.replace(key, " ")
        if _EXTRA.search(text):
            return True
    return False


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _id(value):
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise ValueError("invalid_cloud_identifier")
    value = str(value)
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", value) or value in {".", ".."}:
        raise ValueError("invalid_cloud_identifier")
    return value


def _component(value):
    if not isinstance(value, str) or not value.strip() or value in {".", ".."}:
        raise ValueError("unsafe_name_component")
    series_prefix(value, 2000, value)
    return value


def _fold(value):
    return unicodedata.normalize("NFKC", value).casefold()


def _file(row, parent):
    if not isinstance(row, dict):
        raise ValueError("invalid_file_record")
    fid, parent = _id(row.get("fid")), _id(parent)
    reported = row.get("parent_id", row.get("pdir_fid"))
    if _id(reported) != parent:
        raise ValueError("listing_parent_mismatch")
    name = row.get("name", row.get("file_name"))
    if (not isinstance(name, str) or not name or name in {".", ".."}
            or any(c in name for c in "/\\") or any(ord(c) < 32 for c in name)):
        raise ValueError("invalid_source_name")
    directory = row.get("is_dir", row.get("dir"))
    if type(directory) is not bool:
        raise ValueError("unknown_file_type")
    size = 0 if directory else row.get("size")
    if size is not None and (type(size) is not int or size < 0):
        raise ValueError("unknown_file_size")
    return {"fid": fid, "parent_id": parent, "name": name,
            "size": size, "is_dir": directory}


def _listing(storage, parent):
    raw = storage.list_files(parent)
    if not isinstance(raw, list):
        raise ValueError("incomplete_listing")
    rows = [_file(row, parent) for row in raw]
    if len({row["fid"] for row in rows}) != len(rows):
        raise ValueError("duplicate_listing_identifier")
    return rows


def _error(exc):
    # Provider messages may contain HTTP payloads. Expose only stable categories.
    if isinstance(exc, AuthenticationError):
        return "NEEDS_AUTH", "authentication_required"
    if isinstance(exc, RiskControlError):
        return "RISK_CONTROL", "provider_risk_control"
    if isinstance(exc, MetadataError) and (exc.status in {401, 403, 429} or exc.code in
                                           {"authentication_required", "risk_control", "captcha_required"}):
        return (("RISK_CONTROL", "metadata_risk_control") if exc.status == 429 or exc.code in
                {"risk_control", "captcha_required"} else ("NEEDS_AUTH", "metadata_authentication_required"))
    if isinstance(exc, ValueError):
        return "INCOMPLETE", str(exc) if re.fullmatch(r"[a-z_]+", str(exc)) else "invalid_record"
    return "INCOMPLETE", "listing_unavailable"


def _account(storage):
    scope = getattr(storage, "account_scope", None)
    if not isinstance(scope, str) or not scope.strip():
        raise ValueError("stable_account_scope_required")
    return hashlib.sha256(scope.encode()).hexdigest()


def _scan_payload(report):
    keys = ("source_id", "excluded_ids", "entries", "complete", "account_scope_hash")
    payload = {key: report[key] for key in keys}
    if "source_context" in report:
        payload["source_context"] = report["source_context"]
    return payload


def _source_context(value):
    if not isinstance(value, dict) or set(value) - {"ancestors", "kind"}:
        raise ValueError("invalid_scan_source_context")
    ancestors = value.get("ancestors", [])
    if (not isinstance(ancestors, list) or len(ancestors) > 100
            or any(not isinstance(name, str) or not name or name in {".", ".."} or any(c in name for c in "/\\")
                   or any(ord(c) < 32 for c in name) for name in ancestors)
            or value.get("kind") not in {None, "tv", "movie"}):
        raise ValueError("invalid_scan_source_context")
    return deepcopy(value)


def scan(storage, *, source_id, excluded_ids=(), max_entries=10000, max_depth=12, source_context=None):
    """Inventory a complete bounded tree without changing cloud or journal state."""
    source_id = _id(source_id)
    account = _account(storage)
    excluded = sorted({_id(value) for value in excluded_ids})
    if source_id in excluded:
        raise ValueError("scan_source_is_excluded")
    if type(max_entries) is not int or not 1 <= max_entries <= 1000000:
        raise ValueError("invalid_scan_limit")
    if type(max_depth) is not int or not 0 <= max_depth <= 100:
        raise ValueError("invalid_scan_depth")
    report = {"status": "COMPLETE", "complete": False, "source_id": source_id,
              "account_scope_hash": account,
              "excluded_ids": excluded, "entries": [], "directories": [], "errors": [],
              "writes_attempted": False, "writes_performed": False,
              "limits": {"max_entries": max_entries, "max_depth": max_depth}}
    if source_context is not None:
        report["source_context"] = _source_context(source_context)
    seen = {source_id}
    queue = deque([(source_id, "", 0)])
    try:
        while queue:
            parent, path, depth = queue.popleft()
            rows = _listing(storage, parent)
            for row in rows:
                # Detect aliases/cycles even for excluded entries; never silently
                # accept two locations for the same provider identifier.
                if row["fid"] in seen:
                    raise ValueError("cycle_or_repeated_identifier")
                seen.add(row["fid"])
                if row["fid"] in excluded:
                    if not row["is_dir"]:
                        raise ValueError("excluded_root_is_not_directory")
                    continue
                if len(report["entries"]) >= max_entries:
                    raise ValueError("scan_entry_limit_exceeded")
                row.update(path=posixpath.join(path, row["name"]), parent_path=path)
                if row["is_dir"]:
                    row["kind"] = "directory"
                    report["directories"].append(row["fid"])
                    if depth >= max_depth:
                        raise ValueError("scan_depth_limit_exceeded")
                    queue.append((row["fid"], row["path"], depth + 1))
                else:
                    extension = posixpath.splitext(row["name"])[1].casefold()
                    row["kind"] = ("archive" if classify_archive(row["name"]) else
                                   "video" if extension in VIDEO_EXTENSIONS else
                                   "subtitle" if extension in SUBTITLE_EXTENSIONS else "unmatched")
                report["entries"].append(row)
        report["complete"] = True
    except Exception as exc:
        report["status"], reason = _error(exc)
        report["errors"].append({"reason": reason})
    report["counts"] = dict(Counter(row["kind"] for row in report["entries"]))
    report["snapshot_hash"] = _digest(_scan_payload(report))
    return report


def _category(library, kind):
    category = library.get("categories", {}).get(kind)
    if isinstance(category, dict):
        category = category.get("folder_id", category.get("fid", category.get("id")))
    return _id(category)


def _identity(value):
    if not isinstance(value, dict) or value.get("status", "CONFIRMED") not in {"CONFIRMED", "MATCHED", "OK", "READY"}:
        raise ValueError("metadata_requires_confirmation")
    kind = value.get("kind")
    if kind not in {"tv", "movie"}:
        raise ValueError("unsupported_media_kind")
    title = _component(value.get("title"))
    year = value.get("year")
    if type(year) is not int or not 1800 <= year <= 2199:
        raise ValueError("metadata_year_unknown")
    source = value.get("metadata_source")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("metadata_identity_unknown")
    identifier = value.get("series_id", value.get("movie_id", value.get("id")))
    identity = {"kind": kind, "title": title, "year": year,
                "metadata_source": source, "series_id": _id(identifier)}
    if kind == "tv":
        season, episode = value.get("season"), value.get("episode")
        if type(season) is not int or not 1 <= season <= 99 or type(episode) is not int or not 1 <= episode <= 999:
            raise ValueError("season_or_episode_requires_confirmation")
        identity.update(season=season, episode=episode)
    return identity


def _plan_payload(report):
    return {key: report[key] for key in ("version", "scan", "library", "operations", "review")}


def plan_import(scan_report, library, resolver, *, source_context=None):
    """Propose only uniquely resolved files; archives and ambiguity stay in place."""
    snapshot = deepcopy(scan_report)
    if not snapshot.get("complete") or snapshot.get("status") != "COMPLETE":
        return {"status": "INCOMPLETE", "operations": [], "review": [],
                "errors": [{"reason": "complete_scan_required"}], "writes_performed": False}
    if snapshot.get("snapshot_hash") != _digest(_scan_payload(snapshot)):
        raise ValueError("scan_snapshot_changed")
    if source_context is not None:
        context = _source_context(source_context)
        if "source_context" in snapshot and snapshot["source_context"] != context:
            raise ValueError("scan_source_context_mismatch")
        snapshot["source_context"] = context
        snapshot["snapshot_hash"] = _digest(_scan_payload(snapshot))
    root = _id(library.get("library_root_id"))
    if library.get("account_scope_hash", snapshot["account_scope_hash"]) != snapshot["account_scope_hash"]:
        raise ValueError("library_account_scope_mismatch")
    if root not in snapshot["excluded_ids"] or root == snapshot["source_id"]:
        raise ValueError("library_root_must_be_excluded_from_scan")
    categories = {kind: _category(library, kind) for kind in ("tv", "movie")}
    if root in categories.values() or len(set(categories.values())) != 2:
        raise ValueError("invalid_library_categories")
    if set(categories.values()) & {row["fid"] for row in snapshot["entries"]}:
        raise ValueError("target_category_is_inside_scan")
    normalized_library = {"library_root_id": root, "categories": categories,
                          "account_scope_hash": snapshot["account_scope_hash"],
                          "include_media_tags": library.get("include_media_tags", True) is not False}
    category_names = {kind: value["name"] for kind, value in library.get("categories", {}).items()
                      if kind in categories and isinstance(value, dict) and isinstance(value.get("name"), str)}
    if category_names:
        normalized_library["category_names"] = category_names
    if library.get("parent_id") is not None:
        normalized_library["parent_id"] = _id(library["parent_id"])
    if library.get("name") is not None:
        normalized_library["name"] = _component(library["name"])
    report = {"version": 1, "status": "READY", "scan": snapshot,
              "library": normalized_library, "operations": [], "review": [],
              "writes_attempted": False, "writes_performed": False}
    by_fid = {row["fid"]: row for row in snapshot["entries"]}
    source_context = snapshot.get("source_context", {})
    videos = []
    for row in snapshot["entries"]:
        if row["is_dir"] or row["kind"] == "subtitle":
            continue
        if row["kind"] != "video" or classify_archive(row["name"]):
            report["review"].append({"source": deepcopy(row), "reason":
                                     "archive_requires_extraction" if row["kind"] == "archive" or classify_archive(row["name"]) else "unsupported_file"})
            continue
        path = posixpath.join(*source_context.get("ancestors", []), row["path"])
        if _is_extra(path):
            report["review"].append({"source": deepcopy(row), "reason": "extra_or_trailer_not_a_feature"})
            continue
        if type(row["size"]) is not int or row["size"] <= 0:
            report["review"].append({"source": deepcopy(row), "reason": "video_size_unknown_or_zero"})
            continue
        try:
            context = {"source_id": snapshot["source_id"],
                       "path": row["path"],
                       "ancestors": [*source_context.get("ancestors", []),
                                     *(row["parent_path"].split("/") if row["parent_path"] else [])],
                       "parent_path": row["parent_path"]}
            if source_context.get("kind"):
                context["kind"] = source_context["kind"]
            if any(re.fullmatch(r"(?:Season[ ._-]*\d+|第\s*\d+\s*季)", part, re.I)
                   for part in context["ancestors"]):
                context.setdefault("kind", "tv")
            resolved = resolver(deepcopy(row), context)
            if isinstance(resolved, dict) and resolved.get("status") == "NEEDS_INPUT":
                reason = resolved.get("reason")
                review = {"source": deepcopy(row), "reason": reason if isinstance(reason, str) and
                          re.fullmatch(r"[a-z_]{1,100}", reason) else "metadata_requires_confirmation"}
                candidates = resolved.get("candidates")
                if isinstance(candidates, list):
                    review["candidates"] = [{key: item[key] for key in
                                             ("id", "series_id", "title", "year", "source", "metadata_source", "kind")
                                             if key in item and isinstance(item[key], (str, int)) and not isinstance(item[key], bool)}
                                            for item in candidates[:10] if isinstance(item, dict)]
                report["review"].append(review)
                continue
            identity = _identity(resolved)
            info = media_info(row["name"], row["parent_path"])
            prefix = series_prefix(identity["title"], identity["year"])
            extension = posixpath.splitext(row["name"])[1]
            if identity["kind"] == "tv":
                folders = [prefix, f"Season {identity['season']:02d}"]
                name = episode_filename(prefix, identity["season"], identity["episode"], extension,
                                        info, normalized_library["include_media_tags"])
            else:
                folders = [prefix]
                name = movie_filename(prefix, extension, info, normalized_library["include_media_tags"])
            operation = {"source": deepcopy(row), "identity": identity,
                         "target_category_id": categories[identity["kind"]],
                         "target_directories": folders, "target_name": name, "media_info": info}
            videos.append(operation)
            report["operations"].append(operation)
        except (AuthenticationError, RiskControlError, MetadataError) as exc:
            status, reason = _error(exc)
            return {**report, "status": status, "operations": [], "errors": [{"reason": reason}]}
        except Exception:
            report["review"].append({"source": deepcopy(row), "reason": "metadata_requires_confirmation"})
    for row in snapshot["entries"]:
        if row["kind"] != "subtitle":
            continue
        if type(row["size"]) is not int or row["size"] <= 0:
            report["review"].append({"source": deepcopy(row), "reason": "subtitle_size_unknown_or_zero"})
            continue
        stem, extension = posixpath.splitext(row["name"])
        associations = []
        for video in videos:
            if row["parent_id"] != video["source"]["parent_id"]:
                continue
            suffix = _subtitle_suffix(stem, posixpath.splitext(video["source"]["name"])[0])
            if suffix is not None:
                associations.append((video, suffix))
        if len(associations) != 1:
            report["review"].append({"source": deepcopy(row), "reason": "subtitle_has_no_unique_video"})
            continue
        video, suffix = associations[0]
        report["operations"].append({"source": deepcopy(row), "identity": deepcopy(video["identity"]),
                                     "associated_video_fid": video["source"]["fid"],
                                     "target_category_id": video["target_category_id"],
                                     "target_directories": list(video["target_directories"]),
                                     "target_name": posixpath.splitext(video["target_name"])[0] + suffix + extension})
    collisions = defaultdict(list)
    for operation in report["operations"]:
        key = (operation["target_category_id"], tuple(_fold(name) for name in operation["target_directories"]),
               _fold(operation["target_name"]))
        collisions[key].append(operation["source"]["fid"])
    blocked = {fid for group in collisions.values() if len(group) > 1 for fid in group}
    blocked.update(op["source"]["fid"] for op in report["operations"]
                   if op.get("associated_video_fid") in blocked)
    report["operations"] = [op for op in report["operations"] if op["source"]["fid"] not in blocked]
    report["review"].extend({"source": deepcopy(by_fid[fid]), "reason": "duplicate_target"} for fid in sorted(blocked))
    report["status"] = "READY" if report["operations"] else "NEEDS_INPUT" if report["review"] else "EMPTY"
    report["plan_hash"] = _digest(_plan_payload(report))
    return report


def _matches(actual, expected, *, name=None, parent=None):
    return (actual["fid"] == expected["fid"] and not actual["is_dir"]
            and actual["size"] == expected["size"]
            and actual["name"] == (expected["name"] if name is None else name)
            and actual["parent_id"] == (expected["parent_id"] if parent is None else parent))


def _validate_plan(plan):
    if plan.get("version") != 1 or plan.get("plan_hash") != _digest(_plan_payload(plan)):
        raise ValueError("import_plan_changed")
    snapshot = plan["scan"]
    if not snapshot.get("complete") or snapshot.get("status") != "COMPLETE":
        raise ValueError("complete_scan_required")
    if snapshot["snapshot_hash"] != _digest(_scan_payload(snapshot)):
        raise ValueError("scan_snapshot_changed")
    root = plan["library"]["library_root_id"]
    if root not in snapshot["excluded_ids"] or snapshot["source_id"] in {root, *plan["library"]["categories"].values()}:
        raise ValueError("invalid_import_scope")
    by_id = {row["fid"]: row for row in snapshot["entries"]}
    selected = set()
    for operation in plan["operations"]:
        row = operation["source"]
        if (row != by_id.get(row["fid"]) or row["is_dir"] or row["kind"] not in {"video", "subtitle"}
                or type(row["size"]) is not int or row["size"] <= 0 or row["fid"] in selected):
            raise ValueError("source_not_in_scan_snapshot")
        selected.add(row["fid"])
        identity = _identity(operation["identity"])
        if operation["target_category_id"] != plan["library"]["categories"][identity["kind"]]:
            raise ValueError("target_outside_library")
        for name in [*operation["target_directories"], operation["target_name"]]:
            _component(name)
        expected_folders = [series_prefix(identity["title"], identity["year"])]
        if identity["kind"] == "tv":
            expected_folders.append(f"Season {identity['season']:02d}")
        if operation["target_directories"] != expected_folders:
            raise ValueError("target_identity_mismatch")
    return by_id


def _verify_path(storage, source, by_id, source_root):
    parent = source["parent_id"]
    ancestors = []
    seen = set()
    while parent != source_root:
        if parent in seen or parent not in by_id or not by_id[parent]["is_dir"]:
            raise ValueError("source_parent_outside_scan")
        seen.add(parent)
        ancestors.append(by_id[parent])
        parent = by_id[parent]["parent_id"]
    for row in reversed(ancestors):
        matches = [item for item in _listing(storage, row["parent_id"])
                   if item["fid"] == row["fid"] and item["name"] == row["name"] and item["is_dir"]]
        if len(matches) != 1:
            raise ValueError("source_ancestor_changed")


def apply_import(plan, storage, *, journal, job_key, apply=False):
    """Execute a selected immutable plan; interrupted writes reconcile read-only.

    The caller must hold the project's global run lock and must bind the selected
    plan to the intended account/configuration. No deletion/overwrite is used.
    """
    by_id = _validate_plan(plan)
    if _account(storage) != plan["library"]["account_scope_hash"] or plan["scan"]["account_scope_hash"] != plan["library"]["account_scope_hash"]:
        raise ValueError("import_account_scope_mismatch")
    job_key = _id(job_key)
    report = {"status": "READY", "plan_hash": plan["plan_hash"],
              "operations": [], "review": deepcopy(plan.get("review", [])),
              "writes_attempted": False, "writes_performed": False}
    prior = journal.get(job_key)
    if prior and prior.get("plan_hash") != plan["plan_hash"]:
        raise ValueError("journal_plan_binding_mismatch")
    state = deepcopy(prior) if prior else {"version": 1, "plan_hash": plan["plan_hash"],
                                           "directories": {}, "files": {}}
    state.setdefault("anchors", {})
    root = plan["library"]["library_root_id"]

    def persist():
        journal.put(job_key, safe_record(state))

    def directory_key(parent, name):
        return _digest([parent, name])

    def returned_evidence(result):
        if not isinstance(result, dict):
            return {}
        evidence = {}
        for key in ("fid", "parent_id", "target_parent_id", "task_id"):
            if key in result:
                try:
                    evidence[key] = _id(result[key])
                except ValueError:
                    continue
        if isinstance(result.get("status"), str) and result["status"] in {"done", "submitted", "pending", "success", "unknown", "error"}:
            evidence["status"] = result["status"]
        return evidence

    def verify_root():
        parent = plan["library"].get("parent_id")
        if parent is not None:
            matches = [row for row in _listing(storage, parent) if row["fid"] == root and row["is_dir"]]
            if len(matches) != 1 or (plan["library"].get("name") is not None and matches[0]["name"] != plan["library"]["name"]):
                raise ValueError("library_root_identity_changed")

    def verify_target_parent(parent):
        """Re-establish the complete root/category/folder chain before a write."""
        verify_root()
        bindings = {row["fid"]: row for row in state["anchors"].values()}
        bindings.update({row["fid"]: row for row in state["directories"].values() if row.get("fid")})
        chain, seen = [], set()
        cursor = parent
        while cursor != root:
            if cursor in seen or cursor not in bindings:
                raise ValueError("target_parent_outside_library")
            seen.add(cursor)
            binding = bindings[cursor]
            chain.append(binding)
            cursor = binding["parent_id"]
        if not chain or chain[-1]["fid"] not in plan["library"]["categories"].values():
            raise ValueError("target_category_outside_library")
        for binding in reversed(chain):
            matches = [row for row in _listing(storage, binding["parent_id"])
                       if _fold(row["name"]) == _fold(binding["name"])]
            if (len(matches) != 1 or matches[0]["fid"] != binding["fid"]
                    or not matches[0]["is_dir"] or matches[0]["name"] != binding["name"]):
                raise ValueError("target_directory_identity_changed")

    def existing_target(operation):
        parent = operation["target_category_id"]
        verify_target_parent(parent)
        for name in operation["target_directories"]:
            matches = [row for row in _listing(storage, parent) if _fold(row["name"]) == _fold(name)]
            if not matches:
                return None
            if len(matches) != 1 or not matches[0]["is_dir"] or matches[0]["name"] != name:
                raise ValueError("target_directory_conflict")
            recorded = state["directories"].get(directory_key(parent, name))
            if recorded and recorded.get("fid") and recorded["fid"] != matches[0]["fid"]:
                raise ValueError("target_directory_identity_changed")
            parent = matches[0]["fid"]
        return parent

    def ensure_dir(parent, name):
        verify_target_parent(parent)
        key = directory_key(parent, name)
        prior_dir = state["directories"].get(key)
        listing = _listing(storage, parent)
        matches = [row for row in listing if _fold(row["name"]) == _fold(name)]
        if len(matches) > 1 or matches and (not matches[0]["is_dir"] or matches[0]["name"] != name):
            raise ValueError("target_directory_conflict")
        if prior_dir and prior_dir.get("state") == "intent":
            # An unknown mkdir is never adopted by name alone; another actor
            # might have created it. Stop until an operator resolves ownership.
            raise ValueError("directory_write_requires_reconciliation")
        if prior_dir and matches and prior_dir.get("fid") != matches[0]["fid"]:
            raise ValueError("target_directory_identity_changed")
        if matches:
            if not prior_dir:
                state["directories"][key] = {"state": "verified", "action": "reuse",
                                              "parent_id": parent, "name": name, "fid": matches[0]["fid"]}
                if apply:
                    persist()
            elif prior_dir.get("state") != "verified":
                prior_dir["state"] = "verified"
                if apply:
                    persist()
            return matches[0]["fid"]
        if prior_dir:
            raise ValueError("target_directory_disappeared")
        if not apply:
            return None
        verify_target_parent(parent)
        state["directories"][key] = {"state": "intent", "parent_id": parent, "name": name}
        persist()
        report["writes_attempted"] = True
        result = storage.mkdir(parent, name)
        # Persist the returned identifier before verification; a failed verify
        # must not allow another mkdir on resume.
        fid = _id(result.get("fid"))
        state["directories"][key].update(state="returned", fid=fid)
        persist()
        verify_target_parent(parent)
        matches = [row for row in _listing(storage, parent) if row["fid"] == fid]
        if len(matches) != 1 or not matches[0]["is_dir"] or matches[0]["name"] != name:
            raise ValueError("created_directory_not_verified")
        state["directories"][key]["state"] = "verified"
        persist()
        report["writes_performed"] = True
        return fid

    try:
        verify_root()
        root_listing = _listing(storage, root)
        for category in plan["library"]["categories"].values():
            matches = [row for row in root_listing if row["fid"] == category and row["is_dir"]]
            if len(matches) != 1:
                raise ValueError("category_outside_library")
            kind = next(kind for kind, fid in plan["library"]["categories"].items() if fid == category)
            expected_name = plan["library"].get("category_names", {}).get(kind)
            if expected_name is not None and matches[0]["name"] != expected_name:
                raise ValueError("category_identity_changed")
            anchor = {"fid": category, "parent_id": root, "name": matches[0]["name"]}
            if category in state["anchors"] and state["anchors"][category] != anchor:
                raise ValueError("category_identity_changed")
            state["anchors"][category] = anchor
        # Pending/unknown file writes are handled in a read-only pass. Even if
        # observation proves completion, this invocation never continues writes.
        pending = [record for record in state["files"].values()
                   if record.get("state") in {"move_intent", "rename_intent", "move_returned", "rename_returned"}]
        if pending:
            resolved = True
            for record in pending:
                source, target = record["source"], record["target_parent_id"]
                operation = next(op for op in plan["operations"] if op["source"]["fid"] == source["fid"])
                _verify_path(storage, source, by_id, plan["scan"]["source_id"])
                if existing_target(operation) != target:
                    raise ValueError("target_parent_outside_library")
                old = [row for row in _listing(storage, source["parent_id"]) if row["fid"] == source["fid"]]
                current = [row for row in _listing(storage, target) if row["fid"] == source["fid"]]
                if not old and len(current) == 1 and _matches(current[0], source, name=record["target_name"], parent=target):
                    record["state"] = "complete"
                    record["verified"] = current[0]
                elif (record["state"] in {"move_intent", "move_returned"} and not old
                      and len(current) == 1 and _matches(current[0], source, parent=target)):
                    record["state"] = "moved"
                    record["verified"] = current[0]
                else:
                    resolved = False
            persist()
            report["status"] = "READY_TO_CONTINUE" if resolved else "UNKNOWN_OUTCOME"
            report["operations"] = deepcopy(list(state["files"].values()))
            return report
        if any(record.get("state") == "intent" for record in state["directories"].values()):
            report["status"] = "UNKNOWN_OUTCOME"
            return report
        for operation in plan["operations"]:
            source = operation["source"]
            record = state["files"].get(source["fid"])
            _verify_path(storage, source, by_id, plan["scan"]["source_id"])
            if not record:
                original = [row for row in _listing(storage, source["parent_id"]) if row["fid"] == source["fid"]]
                if len(original) != 1 or not _matches(original[0], source):
                    raise ValueError("source_snapshot_changed")
            parent = operation["target_category_id"]
            for name in operation["target_directories"]:
                parent = ensure_dir(parent, name)
                if parent is None:
                    break
            if parent is None:
                # A preview can validate source snapshot without making folders.
                rows = [row for row in _listing(storage, source["parent_id"]) if row["fid"] == source["fid"]]
                if len(rows) != 1 or not _matches(rows[0], source):
                    raise ValueError("source_snapshot_changed")
                report["operations"].append({**deepcopy(operation), "state": "planned"})
                continue
            if record and record.get("target_parent_id") != parent:
                raise ValueError("target_parent_identity_changed")
            target_rows = _listing(storage, parent)
            if record and record.get("state") == "complete":
                matches = [row for row in target_rows if row["fid"] == source["fid"]]
                if len(matches) != 1 or not _matches(matches[0], source, name=operation["target_name"], parent=parent):
                    raise ValueError("completed_import_changed")
                report["operations"].append(deepcopy(record))
                continue
            if any(row["fid"] != source["fid"] and _fold(row["name"]) in
                   {_fold(source["name"]), _fold(operation["target_name"])} for row in target_rows):
                report["operations"].append({**deepcopy(operation), "state": "conflict"})
                report["status"] = "NEEDS_INPUT"
                # No video movement means its associated subtitle must stay too.
                break
            source_rows = _listing(storage, parent if record and record.get("state") == "moved" else source["parent_id"])
            matches = [row for row in source_rows if row["fid"] == source["fid"]]
            expected_parent = parent if record and record.get("state") == "moved" else source["parent_id"]
            if len(matches) != 1 or not _matches(matches[0], source, parent=expected_parent):
                raise ValueError("source_snapshot_changed")
            if not apply:
                report["operations"].append({**deepcopy(operation), "state": "planned"})
                continue
            if not record:
                verify_target_parent(parent)
                _verify_path(storage, source, by_id, plan["scan"]["source_id"])
                immediate = [row for row in _listing(storage, source["parent_id"]) if row["fid"] == source["fid"]]
                if len(immediate) != 1 or not _matches(immediate[0], source):
                    raise ValueError("source_snapshot_changed")
                record = {"source": deepcopy(source), "target_parent_id": parent,
                          "target_name": operation["target_name"], "state": "move_intent"}
                state["files"][source["fid"]] = record
                persist()
                report["writes_attempted"] = True
                result = storage.move(source["fid"], parent)
                record["state"] = "move_returned"
                record["move_evidence"] = returned_evidence(result)
                persist()
                verify_target_parent(parent)
                actual = [row for row in _listing(storage, parent) if row["fid"] == source["fid"]]
                old = [row for row in _listing(storage, source["parent_id"]) if row["fid"] == source["fid"]]
                if old or len(actual) != 1 or not _matches(actual[0], source, parent=parent):
                    raise ValueError("move_not_verified")
                record["state"], record["verified"] = "moved", actual[0]
                persist()
                report["writes_performed"] = True
            if source["name"] != operation["target_name"]:
                # Recheck collisions immediately before the rename write.
                verify_target_parent(parent)
                fresh = _listing(storage, parent)
                own = [row for row in fresh if row["fid"] == source["fid"]]
                if len(own) != 1 or not _matches(own[0], source, parent=parent):
                    raise ValueError("moved_source_changed_before_rename")
                if any(row["fid"] != source["fid"] and _fold(row["name"]) == _fold(operation["target_name"])
                       for row in fresh):
                    raise ValueError("rename_target_conflict")
                record["state"] = "rename_intent"
                persist()
                report["writes_attempted"] = True
                result = storage.rename(source["fid"], operation["target_name"])
                record["state"] = "rename_returned"
                record["rename_evidence"] = returned_evidence(result)
                persist()
                report["writes_performed"] = True
            verify_target_parent(parent)
            actual = [row for row in _listing(storage, parent) if row["fid"] == source["fid"]]
            if len(actual) != 1 or not _matches(actual[0], source, name=operation["target_name"], parent=parent):
                raise ValueError("renamed_file_not_verified")
            record["state"], record["verified"] = "complete", actual[0]
            persist()
            report["operations"].append(deepcopy(record))
        if report["status"] == "READY" and apply:
            report["status"] = "COMPLETE"
    except Exception as exc:
        pending_write = any(record.get("state") in {"intent", "returned"} for record in state["directories"].values()) or any(
            record.get("state") in {"move_intent", "rename_intent", "move_returned", "rename_returned"}
            for record in state["files"].values())
        status, reason = _error(exc)
        report["status"] = status if status in {"NEEDS_AUTH", "RISK_CONTROL"} else "UNKNOWN_OUTCOME" if pending_write else "NEEDS_INPUT"
        report["errors"] = [{"reason": reason}]
        if pending_write:
            # An exception can still carry an accepted asynchronous task ID.
            # Preserve it before any later, separately invoked reconciliation.
            evidence = returned_evidence({**(getattr(exc, "evidence", {}) or {}),
                                          "task_id": getattr(exc, "task_id", None)})
            if evidence:
                for record in reversed(list(state["files"].values())):
                    if record.get("state") in {"move_intent", "rename_intent", "move_returned", "rename_returned"}:
                        record["failure_evidence"] = evidence
                        break
                persist()
        if state["files"]:
            report["operations"] = deepcopy(list(state["files"].values()))
    return report
