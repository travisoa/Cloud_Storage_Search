"""In-place naming cleanup for one existing work folder inside the library.

The plan only renames files inside the selected folder so that every video
uses the real folder name as its prefix (``剧名 (2026)S01E01_1080P.mkv`` or
``片名 (2024)_2160P.mkv``) and subtitles follow their video. Folders are
never renamed, files are never moved, deleted or overwritten, and anything
ambiguous (unknown numbering, duplicate versions, name conflicts) stays as is.
"""
from __future__ import annotations

from collections import defaultdict, deque
from copy import deepcopy
import hashlib
import json
import posixpath
import re

from . import catalog
from .archives import _id, _listing
from .movie_workflow import _EXTRA
from .naming import episode_filename, media_info, movie_filename, series_prefix
from .planner import _SE, _subtitle_suffix
from .library_identity import _normal, _title_year

_SEASON = re.compile(r"(?:season[ ._-]*(\d{1,2})|第\s*(\d{1,2})\s*季|s(\d{1,2}))", re.I)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _fold(value):
    return str(value).casefold()


def ancestry(storage, root, folder, *, limit=10000):
    """Return the observed directory chain from below the root down to ``folder``."""
    root, folder = _id(root), _id(folder)
    if folder == root:
        raise ValueError("请选择具体作品目录，而不是影库根目录")
    parents, queue, seen = {}, deque([root]), {root}
    while queue:
        parent = queue.popleft()
        for row in _listing(storage, parent):
            if not row["is_dir"]:
                continue
            if row["fid"] in seen or len(seen) >= limit:
                raise ValueError("目录范围核验存在循环或超出上限")
            seen.add(row["fid"])
            parents[row["fid"]] = row
            if row["fid"] == folder:
                chain, cursor = [], row
                while True:
                    chain.append(cursor)
                    if cursor["parent_id"] == root:
                        return list(reversed(chain))
                    cursor = parents[cursor["parent_id"]]
            queue.append(row["fid"])
    raise ValueError("所选目录不在当前影库范围内")


def _tree(storage, folder, *, max_entries, max_depth):
    rows, queue, seen = [], deque([(folder, "", 0)]), {folder}
    while queue:
        parent, path, depth = queue.popleft()
        for row in _listing(storage, parent):
            if row["fid"] in seen:
                raise ValueError("目录存在循环或重复 fid")
            seen.add(row["fid"])
            if len(rows) >= max_entries:
                raise ValueError("作品目录条目超过上限，未生成计划")
            row = dict(row, parent_path=path)
            rows.append(row)
            if row["is_dir"]:
                if depth >= max_depth:
                    raise ValueError("作品目录层级超过上限，未生成计划")
                queue.append((row["fid"], posixpath.join(path, row["name"]), depth + 1))
    return rows


def _season_of(parent_path):
    for part in reversed(parent_path.split("/")):
        match = _SEASON.fullmatch(part.strip()) if part else None
        if match:
            return int(next(value for value in match.groups() if value))
    return None


def _legacy_subtitle(row, prefix, kind):
    """Recognize a work title and language suffix without guessing an alias."""
    if _EXTRA.search(posixpath.join(row["parent_path"], row["name"])):
        return None
    stem = posixpath.splitext(row["name"])[0]
    expected_title, expected_year = _title_year(prefix)
    parsed = catalog.parse_file(row["name"], season=_season_of(row["parent_path"]))
    if kind == "tv":
        marker = _SE.search(stem)
        if marker is None or parsed["ambiguous"] or not parsed["episode_key"]:
            return None
        suffix = _subtitle_suffix(stem, stem[:marker.end()])
        choices = [(stem[:marker.start()].strip(" ._-"), suffix)] if suffix is not None else []
    else:
        if parsed["episode"] is not None:
            return None
        choices = [(stem[:index], suffix) for index in range(1, len(stem) + 1)
                   if (suffix := _subtitle_suffix(stem, stem[:index])) is not None]
    for title, suffix in choices:
        title, year = _title_year(title)
        if _normal(title) == _normal(expected_title) and (year is None or year == expected_year):
            return {"suffix": suffix, "episode_key": parsed["episode_key"] if kind == "tv" else None}
    return None


def _snapshot_payload(report):
    return {key: report[key] for key in ("library_root_id", "chain", "kind", "prefix", "entries")}


def _plan_payload(report):
    return {key: report[key] for key in ("version", "snapshot_hash", "operations")}


def plan_tidy(storage, *, folder_id, library_root_id, categories, kind=None, include_media_tags=True,
              max_entries=5000, max_depth=4):
    """Read-only proposal for one folder; nothing is written to cloud or journal."""
    chain = ancestry(storage, library_root_id, folder_id)
    folder = chain[-1]
    if len(chain) < 2:
        raise ValueError("所选目录是分类目录；请选择分类下的具体作品目录")
    if _SEASON.fullmatch(folder["name"].strip()):
        raise ValueError("请选择作品目录，而不是季目录")
    prefix = series_prefix(folder["name"], 2000, folder["name"])
    category_kind = {str(value.get("folder_id") if isinstance(value, dict) else value): key
                     for key, value in (categories or {}).items()}.get(chain[0]["fid"])
    from .workflow import _TV_CATEGORY_NAMES, _TV_SUBCATEGORY_NAMES, _directory_identity, _normal
    if _normal(folder["name"]) in _TV_CATEGORY_NAMES | _TV_SUBCATEGORY_NAMES | {"电影", "movie", "movies"}:
        raise ValueError("所选目录看起来是分类目录；请选择具体作品目录")
    entries = _tree(storage, folder["fid"], max_entries=max_entries, max_depth=max_depth)
    works = [row for row in entries if row["is_dir"] and not row["parent_path"]
             and _directory_identity(row["name"])[1] is not None]
    if len(works) >= 2:
        raise ValueError("所选目录包含多个带年份的作品子目录，看起来是分类目录；请选择具体作品目录")
    videos = [row for row in entries if not row["is_dir"] and catalog.parse_file(row["name"])["kind"] == "video"]
    if kind is None:
        kind = category_kind if category_kind in {"tv", "movie"} else None
    if kind is None:
        marked = [row for row in videos if catalog.parse_file(row["name"], season=_season_of(row["parent_path"]))["episode_key"]]
        kind = "tv" if marked else None
    report = {"version": 1, "status": "READY", "library_root_id": library_root_id,
              "chain": [{"fid": row["fid"], "name": row["name"], "parent_id": row["parent_id"]} for row in chain],
              "folder": {"fid": folder["fid"], "name": folder["name"]}, "kind": kind, "prefix": prefix,
              "entries": [{key: row[key] for key in ("fid", "name", "parent_id", "size", "is_dir", "parent_path")}
                          for row in entries],
              "operations": [], "review": [], "compliant": 0, "skipped": [],
              "writes_attempted": False, "writes_performed": False}
    report["snapshot_hash"] = _digest(_snapshot_payload(report))
    if kind not in {"tv", "movie"}:
        report.update(status="NEEDS_INPUT", reason="media_kind_requires_confirmation")
        report["plan_hash"] = _digest(_plan_payload(report))
        return report

    def review(row, reason):
        report["review"].append({**{key: row[key] for key in ("fid", "name", "parent_id", "size")}, "reason": reason})

    proposals = []
    features = [row for row in videos if not _EXTRA.search(posixpath.join(row["parent_path"], row["name"]))]
    report["skipped"] = [{"fid": row["fid"], "name": row["name"], "reason": "extra_or_trailer_left_unchanged"}
                         for row in videos if row not in features]
    top = [row for row in features if not row["parent_path"]]
    if kind == "movie" and len(top) > 1:
        for row in top:
            review(row, "multiple_movie_videos_require_review")
        features = [row for row in features if row["parent_path"]]
    for row in features:
        parts = [part for part in row["parent_path"].split("/") if part]
        if kind == "tv" and any(not _SEASON.fullmatch(part.strip()) for part in parts):
            review(row, "video_outside_season_structure")
            continue
        if kind == "movie" and parts:
            review(row, "movie_video_not_at_folder_top_level")
            continue
        if type(row["size"]) is not int or row["size"] <= 0:
            review(row, "video_size_unknown_or_zero")
            continue
        extension = posixpath.splitext(row["name"])[1]
        info = media_info(row["name"], row["parent_path"])
        try:
            if kind == "movie":
                target = movie_filename(prefix, extension, info, include_media_tags)
            else:
                season = _season_of(row["parent_path"])
                parsed = catalog.parse_file(row["name"], season=season)
                if parsed["ambiguous"] or not parsed["episode_key"]:
                    review(row, "episode_number_unrecognized")
                    continue
                if season is not None and parsed["season"] != season:
                    review(row, "season_directory_conflict")
                    continue
                target = episode_filename(prefix, parsed["season"], parsed["episode"], extension, info, include_media_tags)
        except ValueError:
            review(row, "unsafe_or_unsupported_name")
            continue
        proposals.append({"source": row, "target_name": target})
    groups = defaultdict(list)
    for item in proposals:
        groups[(item["source"]["parent_id"], _fold(item["target_name"]))].append(item)
    accepted = []
    for items in groups.values():
        if len(items) > 1:
            for item in items:
                review(item["source"], "duplicate_versions_same_target")
            continue
        accepted.append(items[0])
    by_parent = defaultdict(list)
    for row in entries:
        by_parent[row["parent_id"]].append(row)
    associated_videos = {}
    for item in accepted:
        row = item["source"]
        if row["name"] == item["target_name"]:
            report["compliant"] += 1
            associated_videos[row["fid"]] = item
            continue
        if any(other["fid"] != row["fid"] and _fold(other["name"]) == _fold(item["target_name"])
               for other in by_parent[row["parent_id"]]):
            review(row, "target_name_already_used")
            continue
        associated_videos[row["fid"]] = item
        report["operations"].append({"fid": row["fid"], "name": row["name"], "parent_id": row["parent_id"],
                                     "size": row["size"], "target_name": item["target_name"], "kind": "video",
                                     "media_info": info_summary(row)})
    planned = {(op["parent_id"], _fold(op["target_name"])) for op in report["operations"]}
    for row in entries:
        if row["is_dir"] or catalog.parse_file(row["name"])["kind"] != "subtitle":
            continue
        if type(row["size"]) is not int or row["size"] <= 0:
            review(row, "subtitle_size_unknown_or_zero")
            continue
        stem, extension = posixpath.splitext(row["name"])
        matches = []
        for item in associated_videos.values():
            video = item["source"]
            if video["parent_id"] != row["parent_id"]:
                continue
            suffix = _subtitle_suffix(stem, posixpath.splitext(video["name"])[0])
            if suffix is not None:
                matches.append((item, suffix, "source_stem"))
        legacy = None
        if not matches and (legacy := _legacy_subtitle(row, prefix, kind)) is not None:
            candidates = [video for video in features if video["parent_id"] == row["parent_id"]
                          and (kind == "movie" or catalog.parse_file(video["name"], season=_season_of(video["parent_path"]))["episode_key"] == legacy["episode_key"])]
            if len(candidates) != 1 or candidates[0]["fid"] not in associated_videos:
                review(row, "subtitle_has_no_unique_video")
                continue
            matches.append((associated_videos[candidates[0]["fid"]], legacy["suffix"], "work_identity"))
        if not matches:
            continue
        if len(matches) != 1:
            review(row, "subtitle_has_no_unique_video")
            continue
        item, suffix, basis = matches[0]
        target = posixpath.splitext(item["target_name"])[0] + suffix + extension
        if target == row["name"]:
            continue
        if (row["parent_id"], _fold(target)) in planned or any(
                other["fid"] != row["fid"] and _fold(other["name"]) == _fold(target) for other in by_parent[row["parent_id"]]):
            review(row, "target_name_already_used")
            continue
        planned.add((row["parent_id"], _fold(target)))
        video = item["source"]
        report["operations"].append({"fid": row["fid"], "name": row["name"], "parent_id": row["parent_id"],
                                     "size": row["size"], "target_name": target, "kind": "subtitle",
                                     "associated_video": {"fid": video["fid"], "name": video["name"],
                                                          "target_name": item["target_name"], "size": video["size"],
                                                          "basis": basis, "kind": kind,
                                                          "season": _season_of(video["parent_path"]),
                                                          "episode_key": legacy["episode_key"] if legacy else None}})
    report["status"] = "READY" if report["operations"] else "NEEDS_INPUT" if report["review"] else "UP_TO_DATE"
    report["plan_hash"] = _digest(_plan_payload(report))
    return report


def info_summary(row):
    info = media_info(row["name"], row["parent_path"])
    return {key: info[key] for key in ("resolution", "basis", "codec", "hdr", "audio", "inspection")}


def apply_tidy(plan, storage, *, journal, job_key, apply=False):
    """Rename planned fids after rechecking scope, identity and conflicts each time."""
    if plan.get("version") != 1 or plan.get("plan_hash") != _digest(_plan_payload(plan)) \
            or plan.get("snapshot_hash") != _digest(_snapshot_payload(plan)):
        raise ValueError("整理计划已变化，请重新生成")
    report = {"status": "READY", "plan_hash": plan["plan_hash"], "operations": [], "review": deepcopy(plan["review"]),
              "writes_attempted": False, "writes_performed": False}
    state = journal.get(job_key) or {"version": 1, "plan_hash": plan["plan_hash"], "files": {}}
    if state.get("plan_hash") != plan["plan_hash"]:
        raise ValueError("执行记录属于其他整理计划")

    def persist():
        journal.put(job_key, state)

    chain = ancestry(storage, plan["library_root_id"], plan["folder"]["fid"])
    if [{"fid": row["fid"], "name": row["name"], "parent_id": row["parent_id"]} for row in chain] != plan["chain"]:
        raise ValueError("作品目录位置或名称已变化，请重新生成计划")
    pending = {fid: record for fid, record in state["files"].items() if record.get("state") in {"intent", "returned"}}
    if pending:
        # Unknown rename results are only observed in this run.
        resolved = True
        for fid, record in pending.items():
            current = [row for row in _listing(storage, record["parent_id"]) if row["fid"] == fid]
            if len(current) == 1 and current[0]["name"] == record["target_name"]:
                record["state"] = "complete"
            elif len(current) == 1 and current[0]["name"] == record["name"]:
                record["state"] = "not_applied"
            else:
                resolved = False
        persist()
        report["status"] = "READY_TO_CONTINUE" if resolved else "UNKNOWN_OUTCOME"
        report["operations"] = deepcopy(list(state["files"].values()))
        return report
    for operation in plan["operations"]:
        record = state["files"].get(operation["fid"])
        if record and record.get("state") == "complete":
            report["operations"].append(deepcopy(record))
            continue
        rows = _listing(storage, operation["parent_id"])
        own = [row for row in rows if row["fid"] == operation["fid"]]
        if len(own) != 1 or own[0]["is_dir"] or own[0]["size"] != operation["size"]:
            report.update(status="NEEDS_INPUT", reason="source_file_changed")
            report["operations"].append({**operation, "state": "source_changed"})
            break
        if own[0]["name"] == operation["target_name"]:
            state["files"][operation["fid"]] = {**operation, "state": "complete"}
            if apply:
                persist()
            report["operations"].append({**operation, "state": "complete"})
            continue
        if own[0]["name"] != operation["name"]:
            report.update(status="NEEDS_INPUT", reason="source_file_renamed_elsewhere")
            report["operations"].append({**operation, "state": "source_changed"})
            break
        association = operation.get("associated_video")
        if association:
            expected_names = {association["target_name"]} if apply else {association["name"], association["target_name"]}
            videos = [row for row in rows if not row["is_dir"] and catalog.parse_file(row["name"])["kind"] == "video"
                      and not _EXTRA.search(row["name"])]
            own_video = [row for row in videos if row["fid"] == association["fid"] and row["name"] in expected_names
                         and row["size"] == association["size"]]
            unique = True
            if association["basis"] == "work_identity":
                candidates = [row for row in videos if association["kind"] == "movie" or
                              catalog.parse_file(row["name"], season=association["season"])["episode_key"] == association["episode_key"]]
                unique = len(candidates) == 1
            if len(own_video) != 1 or not unique:
                report.update(status="NEEDS_INPUT", reason="subtitle_video_identity_changed")
                report["operations"].append({**operation, "state": "source_changed"})
                break
        if any(row["fid"] != operation["fid"] and _fold(row["name"]) == _fold(operation["target_name"]) for row in rows):
            report.update(status="NEEDS_INPUT", reason="target_name_conflict")
            report["operations"].append({**operation, "state": "conflict"})
            break
        if not apply:
            report["operations"].append({**operation, "state": "planned"})
            continue
        record = {**operation, "state": "intent"}
        state["files"][operation["fid"]] = record
        persist()
        report["writes_attempted"] = True
        try:
            storage.rename(operation["fid"], operation["target_name"])
        except Exception as exc:
            record["error_type"] = type(exc).__name__
            persist()
            report.update(status="UNKNOWN_OUTCOME", reason="rename_outcome_unknown")
            report["operations"] = deepcopy(list(state["files"].values()))
            return report
        record["state"] = "returned"
        persist()
        report["writes_performed"] = True
        observed = [row for row in _listing(storage, operation["parent_id"]) if row["fid"] == operation["fid"]]
        if len(observed) != 1 or observed[0]["name"] != operation["target_name"]:
            report.update(status="UNKNOWN_OUTCOME", reason="rename_not_observed")
            report["operations"] = deepcopy(list(state["files"].values()))
            return report
        record["state"] = "complete"
        persist()
        report["operations"].append(deepcopy(record))
    if report["status"] == "READY" and apply:
        report["status"] = "COMPLETE"
    return report
