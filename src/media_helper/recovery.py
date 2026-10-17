"""Read-only cloud reconciliation of durable save and directory evidence.

Only the local journal may change. No cloud mutation method is used here.
"""
from __future__ import annotations

from copy import deepcopy

from . import workflow as wf
from .providers import AuthenticationError, RiskControlError


def _result(report, record, status, reason=None):
    report["status"] = status
    if reason and reason not in report["reasons"]:
        report["reasons"].append(reason)
    if record is not None:
        report["operations"] = deepcopy(record.get("operations", []))
    return report


def _valid_operation(operation):
    if not isinstance(operation, dict) or not isinstance(operation.get("target_parent_id"), str):
        return False
    files, before = operation.get("files"), operation.get("before_fids")
    if (not isinstance(files, list) or not files or not isinstance(before, list)
            or any(not isinstance(fid, str) or not fid for fid in before)
            or len(set(before)) != len(before)):
        return False
    for item in files:
        if (not isinstance(item, dict)
                or any(not isinstance(item.get(key), str) or not item[key]
                       for key in ("source_fid", "name", "target_name"))
                or type(item.get("size")) is not int or item["size"] <= 0):
            return False
    return (len({item["source_fid"] for item in files}) == len(files)
            and len({item["target_name"].casefold() for item in files}) == len(files))


def _directory_in_scope(request, storage, report, directory, destination, existing, planned):
    """Allow only a planned mkdir or its returned fid on the observed show path."""
    parent, name = directory.get("parent_id"), directory.get("name")
    if not isinstance(parent, str) or not isinstance(name, str):
        return False
    if any(step.get("parent_id") == parent and step.get("name") == name for step in planned):
        return True
    returned = directory.get("returned")
    if not isinstance(returned, dict) or not returned.get("fid"):
        return False
    rows = wf._library_directories(request, storage, str(request["library_root_id"]))
    rows.extend(row for row in existing if wf._directory(row))
    indexed = {wf._fid(row): row for row in rows}
    anchors = {str(step["parent_id"]) for step in planned if step.get("parent_id")}
    if destination:
        anchors.add(str(destination))
    ancestors = set()
    for anchor in anchors:
        current, visited = anchor, set()
        while current and current not in visited:
            visited.add(current)
            ancestors.add(current)
            row = indexed.get(current)
            if row is None:
                break
            current = str(row.get("parent_id", ""))
    row = indexed.get(str(returned["fid"]))
    return bool(row and wf._fid(row) in ancestors and parent in ancestors
                and str(row.get("parent_id", "")) == parent and wf._name(row) == name)


def _verify_operation(storage, operation):
    """Verify task fids and exact size/name again before changing local state."""
    ids = operation.get("saved_fids")
    if (not isinstance(ids, list) or not ids
            or any(not isinstance(fid, str) or not fid for fid in ids)):
        return None, "save_has_no_verified_new_fids"
    verified = wf._verify_saved(storage, operation)
    if verified is None:
        return None, "saved_fids_not_uniquely_verified_in_destination"
    current = {wf._fid(row): row for row in wf._list(storage, operation["target_parent_id"])}
    for item in verified:
        actual = current.get(item["saved_fid"])
        if (not actual or wf._directory(actual)
                or str(actual.get("parent_id", "")) != operation["target_parent_id"]
                or actual.get("size") != item["size"]
                or wf._name(actual) not in {item["name"], item["target_name"]}):
            return None, "new_file_changed_during_reconciliation"
        if any(wf._fid(row) != item["saved_fid"]
               and wf._name(row).casefold() == item["target_name"].casefold()
               for row in current.values()):
            return None, "target_filename_conflict_requires_confirmation"
        item["verified"] = wf._name(actual) == item["target_name"]
    return verified, None


def reconcile(request, storage, metadata, journal):
    """Read task status and destination contents, without save/mkdir/rename.

    ``READY_TO_RENAME`` records are left in ``saved`` state so a separately
    authorized apply may continue. A completed record is verified on every call.
    """
    request = deepcopy(request)
    report = {"schema_version": 1, "mode": "reconcile", "status": "READY",
              "writes_attempted": False, "writes_performed": False,
              "browser_opened": False, "reasons": [], "operations": []}
    record = None
    try:
        errors = wf._request_errors(request)
        if errors:
            report["reasons"].extend(errors)
            return _result(report, record, "NEEDS_INPUT")
        identity = wf._job_identity(request, storage)
        request.update(series_id=identity["series_id"], season=identity["season"],
                       metadata_source=identity["metadata_source"],
                       library_root_id=identity["library_root_id"], year=int(request["year"]))
        key = "season:" + wf._digest(identity)
        report["job_key"] = key
        record = journal.get(key) if journal is not None else None
        if record is None:
            return _result(report, record, "NO_RECORD", "no_durable_execution_record")
        if not isinstance(record, dict) or record.get("identity") != identity:
            return _result(report, None, "NEEDS_INPUT", "record_identity_requires_confirmation")
        conflicts = wf._configuration_conflicts(request, record)
        if conflicts:
            report["conflicting_fields"] = conflicts
            return _result(report, record, "NEEDS_INPUT", "execution_configuration_changed_requires_reconciliation")
        series = wf._series_identity(request, metadata, report)
        if series is None:
            return _result(report, record, report["status"])
        request["_confirmed_titles"] = [request["title"], *[series.get(field) for field in
            ("title", "name", "original_title", "original_name") if isinstance(series.get(field), str)],
            *[title for title in series.get("aliases", []) if isinstance(title, str) and title]]
        destination, existing, planned = wf._destination(request, storage, report)
        if report["status"] == "NEEDS_INPUT":
            return _result(report, record, "NEEDS_INPUT")
        report["target_folder_id"] = destination
        report["directories"] = planned
        allowed = {wf._fid(row) for row in existing if wf._directory(row)}
        if destination:
            allowed.add(str(destination))
        operations = record.get("operations", [])
        if not isinstance(operations, list):
            return _result(report, record, "NEEDS_INPUT", "invalid_durable_operations")
        for operation in operations:
            if not _valid_operation(operation):
                return _result(report, record, "NEEDS_INPUT", "operation_requires_exact_source_names_sizes_and_before_snapshot")
            if operation["target_parent_id"] not in allowed:
                return _result(report, record, "NEEDS_INPUT", "operation_target_not_in_observed_destination_scope")
        directory_recovered = False
        if record.get("state") == "mkdir_pending":
            directory = record.get("directory", {})
            if not isinstance(directory, dict):
                return _result(report, record, "NEEDS_INPUT", "invalid_durable_directory_evidence")
            if not isinstance(directory.get("returned"), dict) or not directory["returned"].get("fid"):
                return _result(report, record, "UNKNOWN", "directory_creation_has_no_returned_fid_no_name_only_adoption")
            if not _directory_in_scope(request, storage, report, directory, destination, existing, planned):
                return _result(report, record, "NEEDS_INPUT", "pending_directory_outside_observed_destination_scope")
            observed, attempts = wf._observe_created_directory(storage, directory)
            if observed is None:
                return _result(report, record, "UNKNOWN", "returned_directory_fid_not_observed")
            wf._record_directory_verification(journal, key, record, observed, attempts, "reconcile", report)
            directory_recovered = True
        statuses = []
        for operation in operations:
            if operation.get("state") == "ready":
                statuses.append("READY_TO_CONTINUE")
                continue
            if operation.get("state") in {"save_pending", "unknown"} and operation.get("task_id"):
                if not callable(getattr(storage, "task_status", None)):
                    return _result(report, record, "UNKNOWN", "provider_cannot_read_task_status")
                try:
                    task = storage.task_status(operation["task_id"])
                except (AuthenticationError, RiskControlError):
                    raise
                except Exception as exc:
                    operation["error"] = wf._error(exc, "task_recovery")
                    wf._put(journal, key, record)
                    return _result(report, record, "UNKNOWN", "save_task_status_unavailable_no_resubmission")
                if not isinstance(task, dict) or task.get("task_id", operation["task_id"]) != operation["task_id"]:
                    return _result(report, record, "UNKNOWN", "save_task_status_identity_invalid")
                if task.get("status") == "pending":
                    operation["state"], record["state"] = "save_pending", "save_pending"
                    wf._put(journal, key, record)
                    return _result(report, record, "PENDING", "save_task_still_pending")
                if task.get("status") != "done":
                    return _result(report, record, "UNKNOWN", "save_task_not_successful_requires_reconciliation")
                operation["saved_fids"] = deepcopy(task.get("saved_fids", []))
            if not operation.get("saved_fids"):
                return _result(report, record, "UNKNOWN", "previous_save_outcome_unknown_no_resubmission")
            verified, reason = _verify_operation(storage, operation)
            if verified is None:
                operation["state"], record["state"] = "unknown", "unknown"
                operation["reason"] = reason
                wf._put(journal, key, record)
                return _result(report, record, "NEEDS_INPUT" if reason == "target_filename_conflict_requires_confirmation"
                               else "UNKNOWN", reason)
            operation["verified_files"] = verified
            complete = all(item["verified"] for item in verified)
            operation["state"] = "complete" if complete else "saved"
            operation.pop("reason", None)
            statuses.append("COMPLETE" if complete else "READY_TO_RENAME")
            record["state"] = "saved" if not complete else record["state"]
            wf._put(journal, key, record)
        if not statuses:
            if directory_recovered:
                return _result(report, record, "READY_TO_CONTINUE", "directory_verified_no_cloud_write_performed")
            if record.get("state") == "ready":
                return _result(report, record, "READY_TO_CONTINUE", "durable_plan_has_no_submitted_operations")
            return _result(report, record, "UNKNOWN", "record_has_no_save_operations_to_verify")
        status = ("READY_TO_RENAME" if "READY_TO_RENAME" in statuses else
                  "READY_TO_CONTINUE" if "READY_TO_CONTINUE" in statuses else "COMPLETE")
        record["state"] = {"READY_TO_RENAME": "saved", "READY_TO_CONTINUE": "ready", "COMPLETE": "complete"}[status]
        wf._put(journal, key, record)
        return _result(report, record, status, "durable_execution_reconciled_read_only")
    except Exception as exc:
        status = ("NEEDS_AUTH" if isinstance(exc, AuthenticationError) else
                  "RISK_CONTROL" if isinstance(exc, RiskControlError) else "FAILED")
        report["error"] = wf._error(exc, "reconciliation")
        return _result(report, record, status)
