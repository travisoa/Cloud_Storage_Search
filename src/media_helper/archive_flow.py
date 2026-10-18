"""Explicit archive pipeline: a selected share archive → isolated staging → library plan.

``fetch`` saves exactly one user-selected single-file archive candidate into a
new ``压缩包待解压_…`` folder under the library root. Extraction is the existing
``archives.extract`` step, which may recycle the fetched archive afterwards;
``plan_output`` turns its verified, isolated output into an ordinary library
import plan. Nothing here deletes, overwrites or moves old library files, and
multipart or password archives stay manual.
"""
from __future__ import annotations

from copy import deepcopy
import re

from .archives import _account, _id, _listing, classify_archive
from .providers import AuthenticationError, RiskControlError
from .workflow import _digest as _candidate_digest, _fid, _name, _share_tree


class _Stop(Exception):
    def __init__(self, status, reason):
        self.status, self.reason = status, reason


def staging_name(candidate_id):
    return "压缩包待解压_" + candidate_id[:16]


def task_candidate(task, candidate_id):
    """Return the archive candidate and its credential-free share source from a task snapshot."""
    if not isinstance(candidate_id, str) or not re.fullmatch(r"[0-9a-f]{64}", candidate_id):
        raise ValueError("压缩包候选 ID 格式无效")
    report = task.get("report", {})
    rows = [row for row in report.get("archive_candidates") or []
            if isinstance(row, dict) and row.get("candidate_id") == candidate_id]
    if len(rows) != 1:
        raise ValueError("任务快照中没有此压缩包候选，请重新查询")
    candidate = deepcopy(rows[0])
    source_hash = candidate.get("share_source", {}).get("source_hash")
    sources = [row for row in report.get("source_refs") or []
               if isinstance(row, dict) and row.get("source_hash") == source_hash and isinstance(row.get("url"), str)]
    if len(sources) != 1:
        raise ValueError("压缩包来源需要无密码、可复查的分享链接；带提取码的分享仍须人工处理")
    return candidate, deepcopy(sources[0])


def fetch(storage, *, candidate, source, library_root_id, journal, job_key, apply=False, reconcile=False,
          archive_recycled=False):
    """Save one selected archive into a new empty staging folder; never resubmit unknown writes.

    ``archive_recycled`` means the verified extraction already recycled this
    archive (and possibly the staging folder); that job reports the outcome.
    """
    report = {"status": "PLAN", "candidate_id": candidate.get("candidate_id"), "writes_attempted": False,
              "writes_performed": False, "imported": False, "extracted": False, "reasons": [],
              "read_only": not bool(apply and not reconcile)}
    can_write = bool(apply and not reconcile)
    state = None

    def save():
        journal.put(job_key, state)

    def finish(status, reason=None):
        report["status"] = status
        if reason:
            report["reasons"].append(reason)
        if state:
            report["staging"] = deepcopy(state.get("target"))
            for key in ("task_id", "archive_fid"):
                if state.get(key):
                    report[key] = state[key]
            if state.get("task_id"):
                report["save_task_id"] = state["task_id"]
        return report

    def verify_target(*, empty=False):
        target = state["target"]
        for index in range(3):
            matches = [row for row in _listing(storage, library_root_id)
                       if row["name"] == target["name"] or row["fid"] == target.get("folder_id")]
            if len(matches) == 1 and matches[0]["fid"] == target.get("folder_id") and matches[0]["is_dir"]:
                if empty and _listing(storage, target["folder_id"]):
                    raise _Stop("NEEDS_INPUT", "staging_folder_is_not_empty")
                return
            if matches:
                raise _Stop("NEEDS_INPUT", "staging_folder_identity_changed")
            if index < 2:
                getattr(storage, "_sleep", lambda delay: None)(0.3)
        raise _Stop("UNKNOWN", "staging_folder_not_observed")

    def verify_saved(saved_fids):
        verify_target()
        rows = _listing(storage, state["target"]["folder_id"])
        expected = state["bound"]["source"]
        if not isinstance(saved_fids, list) or len(saved_fids) != 1:
            raise _Stop("UNKNOWN", "saved_archive_identifier_not_unique")
        matches = [row for row in rows if row["fid"] == saved_fids[0]]
        if (len(rows) != 1 or len(matches) != 1 or matches[0]["is_dir"]
                or matches[0]["name"] != expected["name"] or matches[0]["size"] != expected["size"]):
            raise _Stop("UNKNOWN", "saved_archive_not_verified_in_staging")
        state.update(status="FETCHED", archive_fid=matches[0]["fid"])
        save()
        report["archive"] = matches[0]
        return finish("FETCHED")

    try:
        library_root_id = _id(library_root_id)
        if not isinstance(job_key, str) or not re.fullmatch(r"archive-fetch:[0-9a-f]{64}", job_key):
            raise ValueError("stable_archive_fetch_job_key_required")
        candidate_id = candidate.get("candidate_id")
        if not isinstance(candidate_id, str) or not re.fullmatch(r"[0-9a-f]{64}", candidate_id):
            return finish("NEEDS_INPUT", "valid_archive_candidate_required")
        classification = classify_archive(candidate.get("name"))
        if classification is None or classification["multipart"] or not classification["supported_format"]:
            return finish("NEEDS_INPUT", "only_single_supported_archive_can_be_fetched")
        if type(candidate.get("size")) is not int or candidate["size"] <= 0:
            return finish("NEEDS_INPUT", "archive_size_must_be_positive")
        if source.get("source_hash") != candidate.get("share_source", {}).get("source_hash"):
            return finish("NEEDS_INPUT", "archive_source_mismatch")
        bound = {"account_scope_hash": _account(storage), "library_root_id": library_root_id,
                 "candidate_id": candidate_id,
                 "source": {"fid": candidate["fid"], "name": candidate["name"], "size": candidate["size"]}}
        old = journal.get(job_key)
        if old is not None:
            if not isinstance(old, dict) or old.get("kind") != "archive_fetch" or old.get("bound") != bound:
                return finish("NEEDS_INPUT", "archive_candidate_or_account_scope_changed")
            state = deepcopy(old)
            if state["target"].get("name") != staging_name(candidate_id) or state["target"].get("parent_id") != library_root_id:
                return finish("NEEDS_INPUT", "invalid_staging_evidence")
            if state.get("status") == "FETCHED":
                if archive_recycled:
                    report["archive_recycled"] = True
                    return finish("FETCHED")
                return verify_saved([state["archive_fid"]])
            if state.get("status") == "TARGET_SUBMITTING" and not state["target"].get("folder_id"):
                return finish("UNKNOWN", "staging_creation_unknown_without_identifier")
            if state.get("status") in {"SAVE_SUBMITTING", "UNKNOWN"} and not state.get("task_id"):
                return finish("UNKNOWN", "archive_save_unknown_without_task_identifier")
            if state.get("task_id"):
                task = storage.task_status(state["task_id"])
                if task.get("status") != "done" or not task.get("saved_fids"):
                    return finish("UNKNOWN", "archive_save_task_pending_or_unknown")
                return verify_saved([str(value) for value in task["saved_fids"]])
            verify_target(empty=True)
            if not can_write:
                return finish("READY_TO_FETCH")
        # Re-enumerate the share; the snapshot is only a pointer to recheck.
        entries = _share_tree(storage, {"url": source["url"], "title": source.get("title", "")})
        observed = [entry for entry in entries
                    if _candidate_digest((source["source_hash"], _fid(entry["file"]))) == candidate_id]
        if (len(observed) != 1 or _name(observed[0]["file"]) != candidate["name"]
                or observed[0]["file"].get("size") != candidate["size"]):
            return finish("NEEDS_INPUT", "archive_candidate_changed_or_not_observed")
        entry = observed[0]
        if state is None:
            name = staging_name(candidate_id)
            if any(row["name"] == name for row in _listing(storage, library_root_id)):
                return finish("NEEDS_INPUT", "staging_name_already_exists_without_ownership")
            report["staging"] = {"parent_id": library_root_id, "name": name, "folder_id": None}
            if not can_write:
                return finish("PLAN")
            state = {"kind": "archive_fetch", "bound": bound, "status": "TARGET_SUBMITTING",
                     "target": deepcopy(report["staging"])}
            save()
            report["writes_attempted"] = True
            created = storage.mkdir(library_root_id, name)
            state["target"]["folder_id"] = _id(created.get("fid") if isinstance(created, dict) else None)
            state["status"] = "TARGET_READY"
            save()
            report["writes_performed"] = True
            verify_target(empty=True)
        state["status"] = "SAVE_SUBMITTING"
        save()
        report["writes_attempted"] = True

        def submitted(evidence):
            task_id = _id(evidence.get("task_id"))
            if (evidence.get("source_fids") != [candidate["fid"]]
                    or evidence.get("target_parent_id") != state["target"]["folder_id"]):
                raise ValueError("archive_save_scope_mismatch")
            state.update(task_id=task_id, status="SAVE_SUBMITTED")
            save()

        options = {"on_submitted": submitted} if getattr(storage, "capabilities", {}).get("save_submission_callback") else {}
        result = storage.save_files(entry["share_ref"], [entry["file"]], state["target"]["folder_id"], **options)
        report["writes_performed"] = True
        if not state.get("task_id") and isinstance(result, dict) and result.get("task_id"):
            state.update(task_id=_id(result["task_id"]), status="SAVE_SUBMITTED")
            save()
        if not isinstance(result, dict) or result.get("status") != "done":
            return finish("UNKNOWN", "archive_save_task_not_complete")
        return verify_saved([str(value) for value in result.get("saved_fids", [])])
    except _Stop as exc:
        # Each write intent was persisted before its request; the stored state
        # already tells a later run whether anything may need reconciliation.
        return finish(exc.status, exc.reason)
    except AuthenticationError:
        return finish("NEEDS_AUTH", "cloud_authentication_required")
    except RiskControlError:
        return finish("RISK_CONTROL", "cloud_risk_control_stop")
    except Exception as exc:
        if state is not None and getattr(exc, "task_id", None) and not state.get("task_id"):
            try:
                state.update(task_id=_id(exc.task_id), status="SAVE_SUBMITTED")
                save()
            except ValueError:
                pass
        report["error_type"] = type(exc).__name__
        return finish("UNKNOWN" if state else "NEEDS_INPUT", "archive_fetch_not_completed")


def output_library(config_or_library):
    """Describe the target library for import planning, from config or a registered library."""
    categories = {kind: value for kind, value in (config_or_library.get("categories") or {}).items()
                  if kind in {"tv", "movie"}}
    if set(categories) != {"tv", "movie"}:
        raise ValueError("影库需要已配置的电影与电视剧分类目录")
    library = {"library_root_id": config_or_library["library_root_id"], "categories": deepcopy(categories),
               "include_media_tags": config_or_library.get("include_media_tags", True) is not False}
    for key in ("parent_id", "name"):
        if config_or_library.get(key) is not None and "init_job_key" in config_or_library:
            library[key] = config_or_library[key]
    return library


def extracted_folder(journal, job_key):
    """Only a completed, verified extraction job yields a scan source."""
    state = journal.get(job_key)
    if not isinstance(state, dict) or state.get("kind") != "archive_extract" or state.get("status") != "EXTRACTED":
        raise ValueError("该压缩包尚无已核验完成的解压记录；请先 archive extract --reconcile")
    folder = state.get("target", {}).get("folder_id")
    return _id(folder), state["bound"]["library_root_id"]
