"""Unattended archive episodes for subscriptions that opted into ``archive_auto``.

One selected single-file archive per missing episode goes through the explicit
archive pipeline: ``archive_flow.fetch`` into its own staging folder, then the
isolated ``archives.extract``. The single extracted video (and subtitles named
after it) is renamed inside that extraction folder and moved into the existing
season folder. Every write intent is journaled before its request; a pending
intent is only observed, never resent. Leftovers follow ``archive_cleanup``.
"""
from __future__ import annotations

from copy import deepcopy
import posixpath
import re

from .archive_cli import CLEANUP_KEY, _fetch_staging, _fetched_archive_recycled, _job_key
from .archive_flow import fetch
from .archives import SOURCE_ACTIONS, _Stop, _account, _inventory, _listing, cleanup_extraction, extract
from .catalog import build_episode_inventory, parse_file
from .journal import JobJournal
from .naming import episode_filename, media_info
from .planner import SUBTITLE_EXTENSIONS, _subtitle_suffix
from .providers import AuthenticationError, RiskControlError


_PENDING = {"rename_intent", "rename_returned", "move_intent", "move_returned"}


def _fold(value):
    return value.casefold()


def _observe(storage, parent, fid):
    rows = [row for row in _listing(storage, parent) if row["fid"] == fid]
    return rows[0] if len(rows) == 1 else None


def _settled(storage, check, sent):
    """Read back a just-sent write a few times; a recorded one only once."""
    for index in range(3 if sent else 1):
        if check():
            return True
        if sent and index < 2:
            getattr(storage, "_sleep", lambda delay: None)(0.3)
    return False


def _plan(storage, *, folder, archive, episode_key, prefix, include_media_tags):
    """The one extracted video and its same-named subtitles, with their target names."""
    season, episode = (int(value) for value in re.fullmatch(r"S(\d{2})E(\d{2,3})", episode_key).groups())
    entries = _inventory(storage, folder, max_entries=1000, max_depth=8)["entries"]
    by_id = {row["fid"]: row for row in entries}

    def relative(row):
        parts, parent = [], row["parent_id"]
        while parent != folder:
            parts.append(by_id[parent]["name"])
            parent = by_id[parent]["parent_id"]
        return "/".join(reversed(parts))

    videos = [row for row in entries if not row["is_dir"] and parse_file(row["name"])["kind"] == "video"]
    if len(videos) != 1:
        raise _Stop("NEEDS_INPUT", "archive_output_requires_single_video")
    video = videos[0]
    if video["size"] <= 0:
        raise _Stop("NEEDS_INPUT", "archive_output_video_size_unverified")
    parsed = parse_file(video["name"], season=season)
    if ((parsed["episode_key"] and parsed["episode_key"] != episode_key)
            or (parsed["reasons"] and "episode_unknown_no_sort_order_inference" not in parsed["reasons"])
            or (re.fullmatch(r"\d{1,3}", parsed["stem"] or "") and int(parsed["stem"]) != episode)):
        raise _Stop("NEEDS_INPUT", "archive_output_episode_disagrees_with_archive")
    name, extension = archive["name"], (archive.get("classification") or {}).get("format") or ""
    stem = name[:-len(extension) - 1] if extension and name.casefold().endswith("." + extension) else name
    # Labels nearest first: the video, its folders, the archive name, the share folders.
    labels = "/".join(part for part in (posixpath.dirname(archive.get("path", "")), stem, relative(video)) if part)
    target = episode_filename(prefix, season, episode, parsed["extension"],
                              media_info(video["name"], labels), include_media_tags)
    files = [{"kind": "video", "source": video, "target_name": target, "state": "planned"}]
    names = {_fold(target)}
    video_stem = posixpath.splitext(video["name"])[0]
    for row in entries:
        subtitle_stem, subtitle_extension = posixpath.splitext(row["name"])
        if (row["is_dir"] or row["parent_id"] != video["parent_id"] or row["size"] <= 0
                or subtitle_extension.casefold() not in SUBTITLE_EXTENSIONS):
            continue
        suffix = _subtitle_suffix(subtitle_stem, video_stem)
        if suffix is None:
            continue
        subtitle = posixpath.splitext(target)[0] + suffix + subtitle_extension
        if _fold(subtitle) not in names:
            names.add(_fold(subtitle))
            files.append({"kind": "subtitle", "source": row, "target_name": subtitle, "state": "planned"})
    return files


def _place(storage, journal, key, state, *, destination, episode_key, report):
    """Rename inside the isolated folder, then move into the season folder."""
    season = int(episode_key[1:3])

    def save():
        journal.put(key, state)

    def check_destination(item):
        rows = _listing(storage, destination)
        source = item["source"]
        if any(row["fid"] != source["fid"] and _fold(row["name"]) == _fold(item["target_name"]) for row in rows):
            raise _Stop("NEEDS_INPUT", "archive_target_name_conflict")
        if item["kind"] == "video":
            present = build_episode_inventory([row for row in rows if not row["is_dir"]], season=season)
            if episode_key in present["existing"]:
                raise _Stop("NEEDS_INPUT", "episode_appeared_before_save")

    for item in state["files"]:
        source, sent = item["source"], False
        while item["state"] != "complete":
            if item["state"] in {"rename_intent", "rename_returned"}:
                def renamed():
                    current = _observe(storage, source["parent_id"], source["fid"])
                    return bool(current and current["name"] == item["target_name"] and current["size"] == source["size"])
                if not _settled(storage, renamed, sent):
                    raise _Stop("UNKNOWN", "archive_output_rename_outcome_unknown")
                item["state"], sent = "renamed", False
                save()
            elif item["state"] in {"move_intent", "move_returned"}:
                def moved():
                    current = _observe(storage, destination, source["fid"])
                    return bool(current and current["name"] == item["target_name"] and current["size"] == source["size"]
                                and _observe(storage, source["parent_id"], source["fid"]) is None)
                if not _settled(storage, moved, sent):
                    raise _Stop("UNKNOWN", "archive_output_move_outcome_unknown")
                item["state"], sent = "complete", False
                item["verified"] = _observe(storage, destination, source["fid"])
                save()
            elif item["state"] == "planned":
                if _observe(storage, source["parent_id"], source["fid"]) != source:
                    raise _Stop("NEEDS_INPUT", "archive_output_changed")
                if source["name"] == item["target_name"]:
                    item["state"] = "renamed"
                    save()
                    continue
                if any(row["fid"] != source["fid"] and _fold(row["name"]) == _fold(item["target_name"])
                       for row in _listing(storage, source["parent_id"])):
                    raise _Stop("NEEDS_INPUT", "archive_output_name_conflict")
                check_destination(item)
                item["state"] = "rename_intent"
                save()
                report["writes_attempted"] = True
                storage.rename(source["fid"], item["target_name"])
                item["state"], sent = "rename_returned", True
                save()
                report["writes_performed"] = True
            elif item["state"] == "renamed":
                current = _observe(storage, source["parent_id"], source["fid"])
                if not current or current["name"] != item["target_name"] or current["size"] != source["size"]:
                    raise _Stop("NEEDS_INPUT", "archive_output_changed")
                check_destination(item)
                item["state"] = "move_intent"
                save()
                report["writes_attempted"] = True
                storage.move(source["fid"], destination)
                item["state"], sent = "move_returned", True
                save()
                report["writes_performed"] = True
            else:
                raise _Stop("NEEDS_INPUT", "invalid_archive_placement_state")


def save_archive_episode(storage, root, config, *, candidate, source, episode_key, prefix, destination,
                         include_media_tags=True):
    """Fetch, extract and place one archive episode; the caller already authorized writes."""
    library_root = config["library_root_id"]
    action = config.get(CLEANUP_KEY, "keep")
    if action not in SOURCE_ACTIONS:
        raise ValueError("config/headless.json 的 " + CLEANUP_KEY + " 只能是 keep、recycle 或 ask")
    report = {"status": "PLAN", "episode_key": episode_key, "candidate_id": candidate.get("candidate_id"),
              "writes_attempted": False, "writes_performed": False, "new_files": [], "reasons": []}

    def merge(step, name):
        report[name] = {field: deepcopy(step[field]) for field in
                        ("status", "reasons", "staging", "target", "archive_fid", "save_task_id", "task_id", "cleanup")
                        if field in step}
        for flag in ("writes_attempted", "writes_performed"):
            report[flag] = report[flag] or bool(step.get(flag))
        if step["status"] not in {"FETCHED", "EXTRACTED"}:
            raise _Stop(step["status"], (step.get("reasons") or [name + "_not_completed"])[-1])

    journal = JobJournal(root / "runtime/archive-jobs.sqlite3")
    state = None
    try:
        fetch_key = _job_key("archive-fetch", storage, candidate["candidate_id"])
        fetched = journal.get(fetch_key) or {}
        archive_fid = fetched.get("archive_fid") if fetched.get("status") == "FETCHED" else None
        place_key = _job_key("archive-place", storage, archive_fid) if archive_fid else None
        state = journal.get(place_key) if place_key else None
        if state is None:
            # A placed output has left the extraction folder, so only an
            # unplaced archive repeats the fetch and extraction readbacks.
            step = fetch(storage, candidate=candidate, source=source, library_root_id=library_root,
                         journal=journal, job_key=fetch_key, apply=True,
                         archive_recycled=_fetched_archive_recycled(storage, journal, journal.get(fetch_key)))
            merge(step, "fetch")
            archive_fid, staging = step["archive_fid"], step["staging"]["folder_id"]
            step = extract(storage, fid=archive_fid, parent_id=staging, library_root_id=library_root,
                           journal=journal, job_key=_job_key("archive", storage, archive_fid), apply=True,
                           source_action=action,
                           staging=_fetch_staging(storage, journal, library_root, archive_fid, staging))
            merge(step, "extraction")
            place_key = _job_key("archive-place", storage, archive_fid)
        extract_key = _job_key("archive", storage, archive_fid)
        extraction = journal.get(extract_key)
        if not isinstance(extraction, dict) or extraction.get("status") != "EXTRACTED":
            raise _Stop("NEEDS_INPUT", "verified_extraction_required")
        folder = extraction["target"]["folder_id"]
        bound = {"account_scope_hash": _account(storage), "library_root_id": library_root, "folder_id": folder,
                 "destination": destination, "episode_key": episode_key, "archive_fid": archive_fid}
        if state is None:
            state = {"kind": "archive_place", "bound": bound,
                     "files": _plan(storage, folder=folder, archive=candidate, episode_key=episode_key,
                                    prefix=prefix, include_media_tags=include_media_tags)}
            journal.put(place_key, state)
        elif state.get("kind") != "archive_place" or state.get("bound") != bound:
            raise _Stop("NEEDS_INPUT", "archive_placement_scope_changed")
        _place(storage, journal, place_key, state, destination=destination, episode_key=episode_key, report=report)
        report["new_files"] = [{"fid": item["source"]["fid"], "saved_fid": item["source"]["fid"],
                                "name": item["target_name"], "target_name": item["target_name"],
                                "episode_key": episode_key, "verified": True} for item in state["files"]]
        report["status"] = "COMPLETE"
        if action in {"recycle", "ask"}:
            parent = extraction["bound"]["source"]["parent_id"]
            cleanup = cleanup_extraction(storage, journal=journal, job_key=extract_key, library_root_id=library_root,
                                         staging=_fetch_staging(storage, journal, library_root, archive_fid, parent),
                                         apply=action == "recycle")
            report["cleanup"] = {field: cleanup[field] for field in ("status", "cleanup", "reasons") if field in cleanup}
            report["cleanup"]["mode"] = action
            for flag in ("writes_attempted", "writes_performed"):
                report[flag] = report[flag] or bool(cleanup.get(flag))
            if cleanup["status"] in {"NEEDS_AUTH", "RISK_CONTROL"}:
                report["status"] = cleanup["status"]
            elif action == "ask" and cleanup["status"] == "PLAN":
                report["cleanup"]["choices"] = {"recycle": "python3 scripts/media.py archive cleanup --fid "
                                                          + archive_fid + " --apply"}
                report["reasons"].append("archive_cleanup_awaiting_choice")
            elif cleanup["status"] not in {"CLEAN", "PLAN"}:
                report["reasons"].append("archive_cleanup_incomplete_see_archive_cleanup")
        return report
    except _Stop as exc:
        report.update(status=exc.status)
        report["reasons"].append(exc.reason)
    except AuthenticationError:
        report.update(status="NEEDS_AUTH")
        report["reasons"].append("cloud_authentication_required")
    except RiskControlError:
        report.update(status="RISK_CONTROL")
        report["reasons"].append("cloud_risk_control_stop")
    except Exception as exc:
        pending = isinstance(state, dict) and any(item.get("state") in _PENDING for item in state.get("files", []))
        report.update(status="UNKNOWN" if pending else "NEEDS_INPUT", error_type=type(exc).__name__)
        report["reasons"].append("archive_placement_not_completed")
    finally:
        journal.close()
    return report
