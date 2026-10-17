"""Normalize only files whose new fids were verified by this project's save job.

The original receipt and operation journal are both required. No file is
adopted by title, ordering, or filename alone, and no directory is renamed.
"""

from __future__ import annotations

from copy import deepcopy
import posixpath
import re

from . import catalog
from .naming import episode_filename, media_info
from .planner import _subtitle_suffix
from .providers import AuthenticationError, RiskControlError
from .workflow import (_configuration_conflicts, _destination, _digest, _directory,
                       _error, _fid, _job_identity, _list, _name, _needs, _put,
                       _request_errors, _series_identity)


class _ValidationError(ValueError):
    pass


_SOURCE_FIELDS = ("source_fid", "name", "size", "episode_key")


def _verified_rows(operations):
    """Validate complete coverage so a receipt cannot silently omit subtitles."""
    if not isinstance(operations, list) or not operations:
        raise _ValidationError("verified_save_operations_required")
    result, global_sources = {}, set()
    for operation in operations:
        if not isinstance(operation, dict) or operation.get("state") != "complete":
            raise _ValidationError("completed_save_operation_required")
        saved = operation.get("saved_fids")
        before = operation.get("before_fids")
        verified = operation.get("verified_files")
        sources = operation.get("files")
        parent = str(operation.get("target_parent_id", ""))
        if (not parent or not isinstance(saved, list) or not saved or
                any(not isinstance(fid, str) or not fid for fid in saved) or
                len(set(saved)) != len(saved) or not isinstance(before, list) or
                set(saved) & set(before) or not isinstance(verified, list) or
                not isinstance(sources, list) or len(sources) != len(saved) or
                len(verified) != len(saved)):
            raise _ValidationError("new_saved_fids_require_complete_before_and_after_evidence")
        verified_ids = set()
        source_ids = set()
        for row in verified:
            if not isinstance(row, dict):
                raise _ValidationError("verified_source_identity_required")
            fid = row.get("saved_fid")
            if (row.get("verified") is not True or fid not in saved or fid in verified_ids or
                    fid in result or not row.get("source_fid") or
                    row.get("source_fid") in source_ids or row.get("source_fid") in global_sources or
                    type(row.get("size")) is not int or
                    row["size"] <= 0 or not isinstance(row.get("name"), str) or
                    not isinstance(row.get("target_name"), str)):
                raise _ValidationError("only_independently_verified_new_fids_can_be_normalized")
            matches = [source for source in sources if isinstance(source, dict) and
                       all(source.get(field) == row.get(field) for field in _SOURCE_FIELDS) and
                       source.get("target_name") == row["target_name"]]
            if len(matches) != 1:
                raise _ValidationError("verified_file_must_match_its_real_source")
            verified_ids.add(fid)
            source_ids.add(row["source_fid"])
            global_sources.add(row["source_fid"])
            result[fid] = {**deepcopy(row), "parent_id": parent}
        if verified_ids != set(saved):
            raise _ValidationError("saved_fid_coverage_incomplete")
    return result


def _source_tuple(row):
    return tuple(row.get(field) for field in _SOURCE_FIELDS)


def _receipt_media(plan_row):
    supplied = plan_row.get("media_info", plan_row["source"].get("media_info"))
    if supplied is None:
        return media_info(plan_row["source"]["name"])
    if not isinstance(supplied, dict) or supplied.get("inspection") != "release_labels_only":
        raise _ValidationError("media_labels_require_recorded_provenance")
    for field in ("resolution", "codec", "hdr"):
        value = supplied.get(field)
        if value is None:
            continue
        evidence = supplied.get("evidence" if field == "resolution" else field + "_evidence", [])
        basis = supplied.get("basis" if field == "resolution" else field + "_basis")
        valid = False
        for item in evidence:
            if (not isinstance(item, dict) or item.get("source") not in {"filename", "parent_directory"}
                    or not isinstance(item.get("text"), str)):
                continue
            if item["source"] == "filename" and item["text"] != plan_row["source"]["name"]:
                continue
            # A quality preference is never evidence of actual release labels.
            checked = media_info(item["text"])
            if checked.get(field) == value and item.get("value") == value and basis == item["source"]:
                valid = True
        if not valid:
            raise _ValidationError("media_labels_require_recorded_provenance")
    return deepcopy(supplied)


def _make_plan(request, receipt, base_rows, receipt_rows, prefix, destination):
    source_plans = receipt.get("plan")
    if not isinstance(source_plans, list) or len(source_plans) != len(receipt_rows):
        raise _ValidationError("receipt_plan_must_cover_all_saved_files")
    plans = {}
    videos = {}
    subtitles = []
    for fid, row in receipt_rows.items():
        base = base_rows[fid]
        if _source_tuple(base) != _source_tuple(row) or base["parent_id"] != row["parent_id"]:
            raise _ValidationError("receipt_and_journal_source_identity_disagree")
        if row["parent_id"] != destination:
            raise _ValidationError("saved_destination_outside_confirmed_series_folder")
        matching = [item for item in source_plans if isinstance(item, dict) and
                    isinstance(item.get("source"), dict) and
                    str(item["source"].get("fid", "")) == row["source_fid"] and
                    item["source"].get("name") == row["name"] and
                    item["source"].get("size") == row["size"] and
                    item.get("episode_key") == row["episode_key"] and
                    item.get("target_name") == row["target_name"]]
        episode = re.fullmatch(r"S([0-9]{2})E([0-9]{2,3})", str(row.get("episode_key")))
        if len(matching) != 1 or not episode or int(episode[1]) != int(request["season"]):
            raise _ValidationError("receipt_plan_source_and_requested_episode_must_agree")
        parsed = catalog.parse_file(row["name"], season=int(request["season"]))
        info = _receipt_media(matching[0]) if parsed["kind"] == "video" else None
        item = {"saved_fid": fid, "source_fid": row["source_fid"], "source_name": row["name"],
                "parent_id": row["parent_id"], "size": row["size"], "episode_key": row["episode_key"],
                "old_name": row["target_name"], "media_info": info}
        if parsed["kind"] == "video":
            if row["episode_key"] in videos:
                raise _ValidationError("one_verified_video_per_episode_required")
            item["target_name"] = episode_filename(prefix, int(episode[1]), int(episode[2]),
                parsed["extension"], info, request.get("include_media_tags", True))
            videos[row["episode_key"]] = item
        elif parsed["kind"] == "subtitle":
            item["extension"] = parsed["extension"]
            subtitles.append(item)
        else:
            raise _ValidationError("unsupported_saved_file_requires_explicit_handling")
        plans[fid] = item
    for item in subtitles:
        video = videos.get(item["episode_key"])
        suffix = (_subtitle_suffix(posixpath.splitext(item["source_name"])[0],
                                   posixpath.splitext(video["source_name"])[0]) if video else None)
        if suffix is None:
            raise _ValidationError("saved_subtitle_cannot_be_uniquely_associated")
        item["target_name"] = posixpath.splitext(video["target_name"])[0] + suffix + item.pop("extension")
    if not videos:
        raise _ValidationError("verified_saved_videos_required")
    return list(plans.values())


def _observe(storage, plan):
    by_parent = {parent: _list(storage, parent) for parent in {item["parent_id"] for item in plan}}
    observed = {}
    targets = set()
    for item in plan:
        target = (item["parent_id"], item["target_name"].casefold())
        if target in targets:
            raise _ValidationError("normalized_target_names_conflict")
        targets.add(target)
        rows = by_parent[item["parent_id"]]
        actual = next((row for row in rows if _fid(row) == item["saved_fid"]), None)
        if (not actual or _directory(actual) or actual.get("size") != item["size"] or
                str(actual.get("parent_id", item["parent_id"])) != item["parent_id"]):
            raise _ValidationError("verified_new_file_missing_moved_or_size_changed")
        if any(_name(row).casefold() == item["target_name"].casefold() and
               _fid(row) != item["saved_fid"] for row in rows):
            raise _ValidationError("normalized_target_name_already_exists")
        observed[item["saved_fid"]] = actual
    return observed


def _refresh_base(base, plan):
    result = deepcopy(base)
    mapping = {item["source_fid"]: item for item in plan}
    for operation in result["operations"]:
        for field in ("files", "verified_files"):
            for row in operation[field]:
                row["target_name"] = mapping[row["source_fid"]]["target_name"]
    for row in result.get("plan", []):
        source = row.get("source", {}).get("fid")
        if source in mapping:
            row["target_name"] = mapping[source]["target_name"]
    return result


def normalize_saved(request, storage, metadata, journal, receipt, apply=False):
    """Plan or apply naming fixes to exactly the receipt's verified new fids.

    Returns ``plan`` and ``naming_job_key``. ``updated_receipt`` is supplied on
    successful apply so callers can persist the normalized operation targets.
    Unknown outcomes require fresh fid/name/size reconciliation before a retry.
    """
    request = deepcopy(request)
    report = {"schema_version": 1, "mode": "apply" if apply else "plan", "status": "READY",
              "writes_attempted": False, "writes_performed": False, "reasons": [], "plan": []}
    try:
        errors = _request_errors(request)
        if errors:
            return _needs(report, "confirmed_request_identity_required", identity_errors=errors)
        identity = _job_identity(request, storage)
        request.update(series_id=identity["series_id"], season=identity["season"],
                       metadata_source=identity["metadata_source"], library_root_id=identity["library_root_id"],
                       year=int(request["year"]))
        job_key = "season:" + _digest(identity)
        report["job_key"] = job_key
        if not isinstance(receipt, dict) or receipt.get("job_key") != job_key:
            return _needs(report, "receipt_job_scope_does_not_match_request")
        base = journal.get(job_key)
        if not isinstance(base, dict) or base.get("state") != "complete" or base.get("identity") != identity:
            return _needs(report, "completed_project_save_journal_required")
        conflicts = _configuration_conflicts(request, base)
        if conflicts:
            return _needs(report, "execution_configuration_changed_requires_reconciliation", conflicting_fields=conflicts)
        series = _series_identity(request, metadata, report)
        if series is None:
            return report
        request["_confirmed_titles"] = [request["title"], *[series.get(field) for field in
            ("title", "name", "original_title", "original_name") if isinstance(series.get(field), str)],
            *[value for value in series.get("aliases", []) if isinstance(value, str)]]
        destination, _, directories = _destination(request, storage, report)
        if report["status"] == "NEEDS_INPUT":
            return report
        if not destination or directories:
            return _needs(report, "existing_series_destination_required_no_directory_changes")
        base_rows = _verified_rows(base.get("operations"))
        receipt_rows = _verified_rows(receipt.get("operations"))
        if set(base_rows) != set(receipt_rows):
            return _needs(report, "receipt_and_journal_saved_fids_disagree")
        plan = _make_plan(request, receipt, base_rows, receipt_rows, report["naming_prefix"], destination)
        naming_key = "naming:" + _digest({"job_key": job_key, "targets": [
            {field: item[field] for field in ("saved_fid", "source_fid", "parent_id", "size", "target_name")}
            for item in sorted(plan, key=lambda item: item["saved_fid"])]})
        report["naming_job_key"] = naming_key
        prior = journal.get(naming_key)
        if isinstance(prior, dict):
            previous = {item["saved_fid"]: item for item in prior.get("plan", [])}
            for item in plan:
                previous_item = previous.get(item["saved_fid"], {})
                if item["old_name"] in {previous_item.get("old_name"), previous_item.get("target_name")}:
                    # A caller may persist updated_receipt after completion.
                    # Keep the first durable intent's before-name on reruns.
                    item["old_name"] = previous_item["old_name"]
        report["plan"] = deepcopy(plan)
        if prior is not None and (prior.get("base_job_key") != job_key or
                prior.get("plan") != plan or prior.get("identity") != identity):
            return _needs(report, "naming_intent_changed_requires_reconciliation")
        observed = _observe(storage, plan)
        for item in plan:
            fid = item["saved_fid"]
            if base_rows[fid]["target_name"] not in {item["old_name"], item["target_name"]}:
                return _needs(report, "journal_target_name_changed_requires_reconciliation")
            expected = {item["old_name"], item["target_name"]} if prior else {item["old_name"]}
            if _name(observed[fid]) not in expected or (not prior and base_rows[fid]["target_name"] != item["old_name"]):
                return _needs(report, "saved_file_name_changed_externally")
        changes = [item for item in plan if _name(observed[item["saved_fid"]]) != item["target_name"]]
        if not apply:
            report["status"] = "READY" if changes or prior and prior.get("state") != "complete" else "UP_TO_DATE"
            return report
        intent = deepcopy(prior) if prior else {"schema_version": 1, "base_job_key": job_key,
            "identity": identity, "state": "ready", "plan": deepcopy(plan), "files": {}}
        for item in plan:
            fid = item["saved_fid"]
            current = _observe(storage, plan)[fid]
            if _name(current) not in {item["old_name"], item["target_name"]}:
                return _needs(report, "saved_file_name_changed_externally")
            evidence = {field: item[field] for field in
                        ("saved_fid", "source_fid", "parent_id", "size", "old_name", "target_name")}
            if _name(current) == item["target_name"]:
                intent["files"][fid] = {**evidence, "state": "complete", "verified": True}
                continue
            # Every mutation gets a durable, independent before-intent, even
            # when a previous unknown attempt has now been observed at old_name.
            intent["files"][fid] = {**evidence, "state": "rename_pending", "verified": False,
                                    "observed_name": _name(current)}
            intent["state"] = "rename_pending"
            _put(journal, naming_key, intent)
            try:
                report["writes_attempted"] = True
                storage.rename(fid, item["target_name"])
            except Exception as exc:
                intent["state"] = intent["files"][fid]["state"] = "unknown"
                intent["files"][fid]["error"] = _error(exc, "normalize_saved_rename")
                _put(journal, naming_key, intent)
                report["status"] = ("NEEDS_AUTH" if isinstance(exc, AuthenticationError) else
                                    "RISK_CONTROL" if isinstance(exc, RiskControlError) else "UNKNOWN")
                report["reasons"].append("rename_outcome_unknown_reconcile_fid_before_retry")
                return report
            report["writes_performed"] = True
            final = _observe(storage, plan)[fid]
            if _name(final) != item["target_name"]:
                intent["state"] = "unknown"
                _put(journal, naming_key, intent)
                report.update(status="UNKNOWN", reasons=["rename_not_observed_by_verified_fid"])
                return report
            intent["files"][fid].update(state="complete", verified=True)
            _put(journal, naming_key, intent)
        final = _observe(storage, plan)
        if any(_name(final[item["saved_fid"]]) != item["target_name"] for item in plan):
            report.update(status="UNKNOWN", reasons=["final_names_not_verified"])
            return report
        latest = journal.get(job_key)
        if latest != base:
            return _needs(report, "base_save_journal_changed_during_normalization")
        updated = _refresh_base(base, plan)
        _put(journal, job_key, updated)
        intent["state"] = "complete"
        _put(journal, naming_key, intent)
        updated_receipt = deepcopy(receipt)
        updated_receipt["operations"] = deepcopy(updated["operations"])
        target_mapping = {item["source_fid"]: item["target_name"] for item in plan}
        for row in updated_receipt["plan"]:
            row["target_name"] = target_mapping[row["source"]["fid"]]
        report.update(status="COMPLETE" if changes or prior and prior.get("state") != "complete" else "UP_TO_DATE",
                      updated_receipt=updated_receipt)
        return report
    except _ValidationError as exc:
        # Only local validation reasons are surfaced. Provider data stays out.
        return _needs(report, "saved_naming_validation_failed", validation_error=str(exc))
    except Exception as exc:
        status = ("NEEDS_AUTH" if isinstance(exc, AuthenticationError) else
                  "RISK_CONTROL" if isinstance(exc, RiskControlError) else "FAILED")
        report.update(status=status, error=_error(exc, "normalize_saved"))
        return report
