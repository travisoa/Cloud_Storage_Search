"""Read-only library checks; presence and expected schedules remain separate."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import posixpath
import re
import unicodedata

from .catalog import build_episode_inventory, calculate_missing, parse_file
from .library_import import _account, _error, _id, _listing, scan
from .library_identity import _QUALITY
from .planner import _SE


_YEAR = re.compile(r"(?:\s*\(((?:18|19|2\d)\d{2})\)|\s*（((?:18|19|2\d)\d{2})）|[ ._-]+((?:18|19|2\d)\d{2}))$")
_SEASON = re.compile(r"(?:Season[ ._-]*(\d{1,2})|第\s*(\d{1,2})\s*季)", re.I)
_PAIR = re.compile(r"(?<![A-Za-z0-9])(?:S\d{1,2}[ ._-]*E\d{1,3}|E[P]?\d{1,3})(?!\d)|第\s*\d+\s*[季集]", re.I)
_LABEL = re.compile(r"[ ._\-\[({]*(?<![A-Za-z0-9])(?:\d{3,4}p|[248]k|uhd|hevc|avc|[hx][ ._-]?26[45]|av1|hdr(?:10\+?)?|dovi|dolby[ ._-]*vision|blu[ ._-]*ray|web[ ._-]*(?:dl|rip)|hdtv|remux)(?![A-Za-z0-9])", re.I)
_ARCHIVE = re.compile(r"\.(?:zip|rar|7z|tar|gz|bz2|xz)(?:\.\d{3})?$|\.part\d+\.rar$|\.[rz]\d{2}$", re.I)
_LANG = re.compile(r"(?:[ ._\-](?:zh(?:[-_]?(?:cn|tw|hans|hant))?|chs|cht|chi|eng?|ja|jpn|ko|kor|forced|default|sdh|简体|繁体|中英|中文))+$", re.I)


def _fold(value):
    return re.sub(r"[\s._\-·:：()（）\[\]【】]", "", unicodedata.normalize("NFKC", str(value))).casefold()


def _prefix(name):
    stem = posixpath.splitext(name)[0]
    markers = [match for pattern in (_PAIR, _SE) if (match := pattern.search(stem))]
    marker = min(markers, key=lambda match: match.start()) if markers else None
    if marker:
        return stem[:marker.start()].strip(" ._-[({")
    labels = [match for pattern in (_LABEL, _QUALITY) if (match := pattern.search(stem))]
    label = min(labels, key=lambda match: match.start()) if labels else None
    return (stem[:label.start()] if label else stem).strip(" ._-")


def _has_year(name):
    match = _YEAR.search(name)
    # A release-year assertion needs a title before the separator. Numeric
    # titles such as 1917 or 2001 never provide their own release year.
    return bool(match and name[:match.start()].strip(' ._-()（）'))


def _stamp(value):
    value = value or datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("health_time_must_be_timezone_aware")
    return value.isoformat()


def _archive_parts(name):
    lower = name.casefold()
    for pattern, style, start in ((r"(.+)\.part(\d+)\.rar$", "part-rar", 1),
                                  (r"(.+\.(?:7z|zip|rar))\.(\d{3})$", "numeric", 1),
                                  (r"(.+)\.r(\d{2})$", "legacy-rar", 0),
                                  (r"(.+)\.z(\d{2})$", "legacy-zip", 1)):
        if match := re.fullmatch(pattern, lower):
            return match[1], style, int(match[2]), start
    return lower, "single", None, None


def _folder_snapshot(storage, root, selected, *, categories, excluded_ids, max_entries, max_depth):
    """Locate ``selected`` by directory listings, then scan only that folder.

    The ancestors become ordinary directory entries with library-relative
    paths, so the checks below see the same context as a root scan.
    """
    from .tidy import OutsideLibrary, ancestry
    preferred = [value.get("folder_id") if isinstance(value, dict) else value
                 for value in (categories.values() if isinstance(categories, dict) else ())]
    budget = [max_entries]

    def bounded(storage, parent):
        # Locating and scanning share one entry budget, like a root scan.
        rows = _listing(storage, parent)
        budget[0] -= len(rows)
        if budget[0] < 0:
            raise ValueError("scan_entry_limit_exceeded")
        return rows

    try:
        chain = ancestry(storage, root, selected, preferred=preferred, excluded=excluded_ids, listing=bounded)
        if budget[0] < 1:
            raise ValueError("scan_entry_limit_exceeded")
    except OutsideLibrary:
        chain = None
    except Exception as exc:
        status, reason = _error(exc)
        return {"status": status, "complete": False, "entries": [], "errors": [{"reason": reason}],
                "excluded_ids": sorted({_id(value) for value in excluded_ids}), "account_scope_hash": _account(storage),
                "limits": {"max_entries": max_entries, "max_depth": max_depth}}
    if chain is None:
        return {"status": "COMPLETE", "complete": True, "entries": [], "errors": [],
                "excluded_ids": sorted({_id(value) for value in excluded_ids}), "account_scope_hash": _account(storage),
                "limits": {"max_entries": max_entries, "max_depth": max_depth}}
    snapshot = scan(storage, source_id=selected, excluded_ids=excluded_ids,
                    max_entries=budget[0], max_depth=max(max_depth - len(chain), 0))
    snapshot["limits"] = {"max_entries": max_entries, "max_depth": max_depth}
    names, upper = [row["name"] for row in chain], []
    for index, row in enumerate(chain):
        upper.append({**row, "size": 0, "path": "/".join(names[:index + 1]),
                      "parent_path": "/".join(names[:index]), "kind": "directory"})
    prefix = upper[-1]["path"]
    for row in snapshot["entries"]:
        row["parent_path"] = posixpath.join(prefix, row["parent_path"]) if row["parent_path"] else prefix
        row["path"] = posixpath.join(prefix, row["path"])
    snapshot["entries"] = upper + snapshot["entries"]
    return snapshot


def inspect(storage, *, library_root_id, categories, resolver=None, metadata=None,
            excluded_ids=(), folder_id=None, max_entries=10000, max_depth=12, as_of=None):
    """Inspect a bounded authorized tree using listings only.

    A selected subfolder is accepted only after its ancestry is observed by
    directory listings; then only that folder is scanned. ``resolver(file,
    context)`` may return an exact MATCHED identity; schedule checks use its
    ``providers`` or the optional metadata provider. The report never implies
    byte integrity, playback, or a confirmed release.
    """
    root = _id(library_root_id)
    selected = _id(folder_id) if folder_id is not None else root
    checked = _stamp(as_of)
    if selected == root:
        snapshot = scan(storage, source_id=root, excluded_ids=excluded_ids,
                        max_entries=max_entries, max_depth=max_depth)
    else:
        snapshot = _folder_snapshot(storage, root, selected, categories=categories, excluded_ids=excluded_ids,
                                    max_entries=max_entries, max_depth=max_depth)
    metadata_requested = resolver is not None or metadata is not None
    if resolver is None and metadata is not None:
        from .library_identity import MediaResolver
        resolver = MediaResolver(metadata)
    report = {"schema_version": 1, "status": "HEALTHY", "checked_at": checked,
              "library_root_id": root, "folder_id": selected, "findings": [],
              "read_only": True, "writes_attempted": False, "writes_performed": False,
              "browser_opened": False, "playability_verified": False,
              "coverage": {"scan_complete": snapshot["complete"], "metadata_requested": metadata_requested,
                           "metadata_complete": not metadata_requested,
                           "excluded_ids": snapshot["excluded_ids"], "limits": snapshot["limits"],
                           "scope_verified": selected == root, "account_scope_hash": snapshot["account_scope_hash"]},
              "expected_schedule": {"basis": "expected_schedule_only", "availability_verified": False,
                                    "as_of": checked, "seasons": [], "missing": [], "future": [], "unknown": []}}

    def finding(code, severity, path="", fids=(), **details):
        item = {"code": code, "severity": severity, "path": path, "fids": sorted(set(fids)), **details}
        item["finding_id"] = hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]
        report["findings"].append(item)

    if not snapshot["complete"]:
        report["status"] = snapshot["status"]
        for error in snapshot["errors"]:
            finding("scan_incomplete", "error", reason=error["reason"])
    all_entries = snapshot["entries"]
    directories = {row["fid"]: row for row in all_entries if row["is_dir"]}
    if selected != root:
        chosen = directories.get(selected)
        if chosen is None:
            finding("scope_not_verified", "error", fids=[selected], reason="folder_not_observed_inside_library")
            report["status"] = "INCOMPLETE"
            entries = []
        else:
            report["coverage"]["scope_verified"] = True
            prefix = chosen["path"] + "/"
            entries = [row for row in all_entries if row["fid"] == selected or row["path"].startswith(prefix)]
    else:
        entries = all_entries

    if not isinstance(categories, dict):
        raise ValueError("health_categories_must_be_mapping")
    category_ids = {}
    for kind, value in sorted(categories.items()):
        fid = value.get("folder_id") if isinstance(value, dict) else value
        fid = _id(fid)
        if fid in category_ids or fid == root:
            raise ValueError("health_categories_must_be_distinct")
        category_ids[fid] = kind
        row = directories.get(fid)
        if selected == root and snapshot["complete"] and (row is None or row["parent_id"] != root):
            finding("category_missing_or_misplaced", "error", fids=[fid], category=kind)
        elif selected == root and row and isinstance(value, dict) and isinstance(value.get("name"), str) and value['name'] != row["name"]:
            finding("category_name_changed", "warning", row["path"], [fid], category=kind,
                    configured_name=value.get("name"), observed_name=row["name"])

    def ancestors(row):
        chain, seen, parent = [], set(), row["parent_id"]
        while parent != root and parent in directories and parent not in seen:
            seen.add(parent)
            chain.append(directories[parent])
            parent = directories[parent]["parent_id"]
        return list(reversed(chain))

    contexts, work_dirs, videos = {}, {}, []
    by_parent = defaultdict(list)
    actual = defaultdict(lambda: defaultdict(list))
    archive_groups = defaultdict(list)
    identity_by_work = defaultdict(dict)
    metadata_ok = snapshot['status'] not in {'NEEDS_AUTH', 'RISK_CONTROL'}
    fatal_metadata = not metadata_ok
    from .workflow import _TV_CATEGORY_NAMES, _TV_SUBCATEGORY_NAMES, _normal
    tv_groups = _TV_CATEGORY_NAMES | _TV_SUBCATEGORY_NAMES
    processed = 0
    for row in entries:
        processed += 1
        chain = ancestors(row)
        category_index = next((index for index, value in enumerate(chain) if value["fid"] in category_ids), None)
        category = category_ids[chain[category_index]["fid"]] if category_index is not None else None
        candidates = chain[category_index + 1:] if category_index is not None else []
        if row["is_dir"] and category_index is not None:
            candidates = [*candidates, row]
        # Skip only recognized category layers. Unknown folders retain their
        # identity and still need the usual title/year and metadata checks.
        work = next((item for item in candidates if category != "tv" or _normal(item["name"]) not in tv_groups), None)
        work_id = work["fid"] if work else "unclassified:" + row["parent_id"]
        if work:
            work_dirs[work_id] = work
        seasons = {int(next(group for group in match.groups() if group is not None)) for ancestor in chain
                   if (match := _SEASON.fullmatch(ancestor["name"]))}
        parsed = parse_file(row["name"], next(iter(seasons)) if len(seasons) == 1 else None) if not row["is_dir"] else None
        contexts[row["fid"]] = {"work_id": work_id, "work": work, "category": category,
                                  "ancestors": chain, "parsed": parsed, "seasons": seasons}
        by_parent[row["parent_id"]].append(row)
        if row["is_dir"]:
            continue
        if row["kind"] in {"video", "subtitle", "archive"} and (row["size"] is None or row["size"] == 0):
            finding("unknown_file_size" if row["size"] is None else "zero_byte_file", "warning", row["path"], [row["fid"]], kind=row["kind"])
        if row["kind"] == "archive" or _ARCHIVE.search(row["name"]):
            family, style, number, start = _archive_parts(row["name"])
            archive_groups[(row["parent_id"], family, style)].append((row, number, start))
            continue
        if row["kind"] not in {"video", "subtitle"}:
            continue
        if work is None:
            finding("work_directory_unknown", "warning", row["path"], [row["fid"]])
        elif _fold(_prefix(row["name"])) != _fold(work["name"]):
            finding("title_prefix_mismatch", "warning", row["path"], [row["fid"], work["fid"]],
                    directory_prefix=work["name"], file_prefix=_prefix(row["name"]))
        is_tv = category in {"tv", "variety"} or bool(seasons) or parsed["episode"] is not None
        if len(seasons) > 1 or (seasons and parsed["season"] is not None and parsed["season"] not in seasons):
            finding("season_directory_conflict", "error", row["path"], [row["fid"]])
        if row["kind"] != "video":
            continue
        videos.append(row)
        if is_tv and (parsed["ambiguous"] or not parsed["episode_key"]):
            finding("episode_unrecognized", "warning", row["path"], [row["fid"]])
        elif is_tv:
            actual[work_id][parsed["episode_key"]].append(row)
        if metadata_requested and not fatal_metadata:
            try:
                resolution = resolver(row, {"path": row["path"], "kind": "tv" if is_tv else "movie",
                                            "ancestors": chain})
                if not isinstance(resolution, dict) or resolution.get("status") != "MATCHED":
                    metadata_ok = False
                    finding("metadata_identity_unverified", "warning", row["path"], [row["fid"]],
                            reason="identity_requires_confirmation")
                else:
                    identity = {key: resolution[key] for key in ("kind", "title", "year", "metadata_source", "series_id") if key in resolution}
                    identity_by_work[work_id][json.dumps(identity, sort_keys=True)] = identity
            except Exception as exc:
                metadata_ok = False
                status, reason = _error(exc)
                finding("metadata_unavailable", "error", row["path"], [row["fid"]], reason=reason)
                if status in {"NEEDS_AUTH", "RISK_CONTROL"}:
                    report["status"] = status
                    fatal_metadata = True
                    break

    inspected_directories = {row['fid'] for row in entries if row['is_dir']}
    for work_id, work in sorted(work_dirs.items()):
        if not _has_year(work["name"]):
            finding("work_year_unrecognized", "warning", work["path"], [work_id])
        descendants = [row for row in videos if contexts[row["fid"]]["work_id"] == work_id]
        if snapshot["complete"] and not fatal_metadata and work_id in inspected_directories and not descendants:
            finding("empty_work_directory", "warning", work["path"], [work_id], basis="no_video_file_observed")
    for row in entries:
        if row["is_dir"] and _SEASON.fullmatch(row["name"]) and snapshot["complete"] and not fatal_metadata:
            if not any(video["path"].startswith(row["path"] + "/") for video in videos):
                finding("empty_season_directory", "warning", row["path"], [row["fid"]], basis="no_video_file_observed")
    for work_id, episodes in sorted(actual.items()):
        for key, files in sorted(episodes.items()):
            if len(files) > 1:
                finding("duplicate_episode_versions", "warning", work_dirs.get(work_id, {}).get("path", ""),
                        [row["fid"] for row in files], episode_key=key, count=len(files),
                        basis="multiple_video_files_not_content_hash")
    for row in entries:
        if row["kind"] != "subtitle":
            continue
        context = contexts.get(row["fid"])
        if context is None:
            continue
        parsed = context["parsed"]
        same_episode = parsed["episode_key"] and actual[context["work_id"]].get(parsed["episode_key"])
        stem = _LANG.sub("", posixpath.splitext(row["name"])[0])
        same_movie = any(_fold(stem) == _fold(posixpath.splitext(file["name"])[0])
                         for file in by_parent[row["parent_id"]] if file["kind"] == "video")
        if not same_episode and not same_movie and snapshot["complete"] and not fatal_metadata:
            finding("orphan_subtitle", "warning", row["path"], [row["fid"]], basis="no_matching_video_observed")
    for (parent, family, style), parts in sorted(archive_groups.items()):
        rows = [part[0] for part in parts]
        finding("archive_remaining", "warning", rows[0]["parent_path"], [row["fid"] for row in rows],
                archive_family=family, format=style, content_inspected=False)
        if style != "single":
            numbers = [part[1] for part in parts]
            missing = sorted(set(range(parts[0][2], max(numbers) + 1)) - set(numbers))
            anchor = family + (".rar" if style == "legacy-rar" else ".zip")
            anchor_missing = style.startswith("legacy-") and not any(row["name"].casefold() == anchor for row in by_parent[parent])
            if missing or anchor_missing or len(numbers) != len(set(numbers)):
                finding("archive_part_gap", "error", rows[0]["parent_path"], [row["fid"] for row in rows],
                        missing_parts=missing, anchor_missing=anchor_missing,
                        duplicate_part_numbers=len(numbers) != len(set(numbers)),
                        tail_completeness="unverified")

    if metadata_requested and not fatal_metadata and snapshot['complete']:
        for work_id, identities in sorted(identity_by_work.items()):
            if len(identities) != 1:
                metadata_ok = False
                finding("work_identity_conflict", "error", work_dirs.get(work_id, {}).get("path", ""),
                        fids=[work_id] if work_id in work_dirs else [])
                continue
            identity = next(iter(identities.values()))
            if identity.get("kind") != "tv":
                continue
            providers = getattr(resolver, "providers", {})
            provider = providers.get(identity.get("metadata_source")) or metadata
            if provider is None or getattr(provider, "source", None) != identity.get("metadata_source"):
                metadata_ok = False
                finding("metadata_schedule_unavailable", "warning", work_dirs.get(work_id, {}).get("path", ""))
                continue
            seasons = sorted({contexts[row["fid"]]["parsed"]["season"] for row in videos
                              if contexts[row["fid"]]["work_id"] == work_id and contexts[row["fid"]]["parsed"]["season"]})
            for season in seasons:
                try:
                    episodes = provider.get_season(identity["series_id"], season)
                    files = [row for row in videos if contexts[row["fid"]]["work_id"] == work_id]
                    changes = calculate_missing(episodes, build_episode_inventory(files), checked)
                    if changes["unknown"]:
                        metadata_ok = False
                    season_report = {"work_id": work_id, "identity": identity, "season": season,
                                     "missing": [row["episode_key"] for row in changes["missing"]],
                                     "future": [row["episode_key"] for row in changes["future"]],
                                     "unknown_count": len(changes["unknown"])}
                    report["expected_schedule"]["seasons"].append(season_report)
                    for field in ("missing", "future", "unknown"):
                        report["expected_schedule"][field].extend(
                            {"work_id": work_id, **{key: item[key] for key in ("episode_key", "air_date", "air_at", "eligibility_reason") if key in item}}
                            for item in changes[field])
                except Exception as exc:
                    metadata_ok = False
                    status, reason = _error(exc)
                    finding("metadata_schedule_unavailable", "warning", work_dirs.get(work_id, {}).get("path", ""), reason=reason)
                    if status in {"NEEDS_AUTH", "RISK_CONTROL"}:
                        report["status"] = status
                        fatal_metadata = True
                        break
            if fatal_metadata:
                break
    report["coverage"].update(metadata_complete=bool(metadata_ok and snapshot['complete']) if metadata_requested else None,
                              entries_observed=len(all_entries), entries_inspected=processed,
                              video_files=len(videos), work_directories=len(work_dirs))
    report["findings"].sort(key=lambda row: (row["path"], row["code"], row["finding_id"]))
    counts = Counter(row["severity"] for row in report["findings"])
    report["summary"] = {"errors": counts["error"], "warnings": counts["warning"], "info": counts["info"],
                         "finding_count": len(report["findings"]), "expected_missing": len(report["expected_schedule"]["missing"]),
                         "expected_future": len(report["expected_schedule"]["future"]),
                         "schedule_unknown": len(report["expected_schedule"]["unknown"])}
    if report["status"] == "HEALTHY":
        if not snapshot["complete"] or not report["coverage"]["scope_verified"] or metadata_requested and not metadata_ok:
            report["status"] = "INCOMPLETE"
        elif report["findings"]:
            report["status"] = "ISSUES_FOUND"
    report["severity"] = "error" if counts["error"] else "warning" if counts["warning"] else "info" if counts["info"] else "none"
    return report


def markdown(report):
    """A deterministic, credential-free view of an already sanitized report."""
    def escape(value):
        return str(value).replace("\\", "\\\\").replace("`", "\\`").replace("|", "\\|").replace("\n", " ").replace("\r", " ")
    coverage, summary = report["coverage"], report["summary"]
    lines = ["# 影库体检", "", f"状态：{escape(report['status'])}；检查时间：{escape(report['checked_at'])}", "",
             f"只读检查，云盘写入 0。扫描完整：{coverage['scan_complete']}；视频 {coverage['video_files']} 个；问题 {summary['finding_count']} 项。", "",
             "检查只证明目录和文件记录，不证明播放、内容完整性或实际播出。", "",
             "| 程度 | 问题 | 路径 |", "| --- | --- | --- |"]
    lines.extend(f"| {escape(row['severity'])} | {escape(row['code'])} | {escape(row['path'])} |" for row in report["findings"])
    lines.extend(["", f"预计缺集：{summary['expected_missing']}；预计未来集：{summary['expected_future']}；日程未知：{summary['schedule_unknown']}。",
                  "这些数字仅来自预计播出日程，不表示资源已发布或可搜索。", ""])
    return "\n".join(lines)
