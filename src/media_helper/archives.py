"""Inspect archives and extract once into a new, isolated library directory.

Extraction is a staging operation, never a completed library import. Passwords
and multipart parameters have no proven API contract and are not accepted.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
import hashlib
import json
import re

from .providers import AuthenticationError, ProviderError, RiskControlError


_FORMATS = ("tar.gz", "tar.bz2", "tar.xz", "tar.zst", "tar", "zip", "rar", "7z", "gz", "bz2", "xz")
_UNSUPPORTED = ("ace", "arj", "cab", "lz", "lzma", "lzh", "zst", "iso", "zipx")


class _Stop(Exception):
    def __init__(self, status, reason):
        self.status, self.reason = status, reason


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value):
        raise ValueError("valid_cloud_identifier_required")
    return value


def _account(storage):
    value = getattr(storage, "account_scope", None)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("stable_account_scope_required")
    return hashlib.sha256(value.encode()).hexdigest()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _listing(storage, parent):
    rows = storage.list_files(parent)
    if not isinstance(rows, list):
        raise ValueError("complete_listing_required")
    cleaned, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("valid_file_record_required")
        fid, name = _id(row.get("fid")), row.get("name", row.get("file_name"))
        directory = row.get("is_dir", row.get("dir"))
        size = row.get("size")
        if (fid in seen or fid == parent or row.get("parent_id") != parent or type(directory) is not bool
                or not isinstance(name, str) or not name or name in {".", ".."}
                or any(char in name for char in "/\\") or any(ord(char) < 32 for char in name)
                or (not directory and (type(size) is not int or size < 0))):
            raise ValueError("complete_unambiguous_file_listing_required")
        seen.add(fid)
        cleaned.append({"fid": fid, "name": name, "parent_id": parent,
                        "is_dir": directory, "size": 0 if directory else size})
    return cleaned


def _within(storage, root, selected):
    root, selected = _id(root), _id(selected)
    if root == selected:
        return
    queue, seen = deque([root]), {root}
    while queue:
        parent = queue.popleft()
        for row in _listing(storage, parent):
            if not row["is_dir"]:
                continue
            if row["fid"] in seen or len(seen) >= 10000:
                raise ValueError("directory_scope_cycle_or_limit")
            if row["fid"] == selected:
                return
            seen.add(row["fid"])
            queue.append(row["fid"])
    raise ValueError("source_must_be_inside_selected_library")


def _inventory(storage, root, *, max_entries, max_depth):
    """A complete bounded snapshot, including files below every old directory."""
    if type(max_entries) is not int or not 1 <= max_entries <= 1000000:
        raise ValueError("invalid_archive_inventory_limit")
    if type(max_depth) is not int or not 0 <= max_depth <= 100:
        raise ValueError("invalid_archive_inventory_depth")
    root = _id(root)
    seen, entries, queue = {root}, [], deque([(root, 0)])
    while queue:
        parent, depth = queue.popleft()
        for row in _listing(storage, parent):
            if row["fid"] in seen:
                raise _Stop("NEEDS_INPUT", "archive_inventory_cycle_or_repeated_identifier")
            if len(entries) >= max_entries:
                raise _Stop("NEEDS_INPUT", "archive_inventory_entry_limit")
            seen.add(row["fid"])
            entries.append(row)
            if row["is_dir"]:
                if depth >= max_depth:
                    raise _Stop("NEEDS_INPUT", "archive_inventory_depth_limit")
                queue.append((row["fid"], depth + 1))
    return {"complete": True, "root_id": root, "fids": sorted(seen), "entries": entries,
            "limits": {"max_entries": max_entries, "max_depth": max_depth}}


def classify_archive(name):
    """Filename-only hint for shared resources; never proves extractability."""
    if not isinstance(name, str) or not name:
        return None
    lowered = name.casefold()
    part = re.fullmatch(r"(.+)\.part(\d+)\.rar", lowered)
    split = re.fullmatch(r"(.+\.(?:zip|7z|rar|tar|tar\.gz|tar\.bz2|tar\.xz))\.(\d{3,})", lowered)
    old = re.fullmatch(r"(.+)\.r(\d{2,3})", lowered)
    old_zip = re.fullmatch(r"(.+)\.(zx?)(\d{2,3})", lowered)
    scheme, volume, multipart, format_name = "single", None, False, None
    if part:
        scheme, volume, multipart, format_name = "part_rar", int(part[2]), True, "rar"
    elif split:
        scheme, volume, multipart = "numeric", int(split[2]), True
        format_name = next((value for value in _FORMATS if split[1].endswith("." + value)), None)
    elif old:
        scheme, volume, multipart, format_name = "legacy_rar", int(old[2]) + 1, True, "rar"
    elif old_zip:
        format_name = "zipx" if old_zip[2] == "zx" else "zip"
        scheme, volume, multipart = "legacy_" + format_name, int(old_zip[3]), True
    else:
        format_name = next((value for value in _FORMATS + _UNSUPPORTED if lowered.endswith("." + value)), None)
        if format_name is None and re.search(r"\.\d{3,}$", lowered):
            format_name, multipart, scheme = "unknown_split", True, "unknown"
    if format_name is None:
        return None
    return {"format": format_name, "multipart": multipart, "scheme": scheme, "volume_number": volume,
            "supported_format": format_name in _FORMATS, "basis": "filename_only", "handling": "manual"}


def classify_archives(rows):
    """Group observed volumes without claiming their final volume is known."""
    files = [row for row in rows if not row["is_dir"]]
    old_heads = {re.sub(r"\.r\d{2,3}$", "", row["name"].casefold()) for row in files
                 if re.search(r"\.r\d{2,3}$", row["name"], re.I)}
    zip_heads = {re.sub(r"\.z\d{2,3}$", "", row["name"].casefold()) for row in files
                 if re.search(r"\.z\d{2,3}$", row["name"], re.I)}
    zipx_heads = {re.sub(r"\.zx\d{2,3}$", "", row["name"].casefold()) for row in files
                  if re.search(r"\.zx\d{2,3}$", row["name"], re.I)}
    groups = {}
    for row in files:
        name = row["name"].casefold()
        part = re.fullmatch(r"(.+)\.part(\d+)\.rar", name)
        split = re.fullmatch(r"(.+\.(?:zip|7z|rar|tar|tar\.gz|tar\.bz2|tar\.xz))\.(\d{3,})", name)
        old = re.fullmatch(r"(.+)\.r(\d{2,3})", name)
        old_zip = re.fullmatch(r"(.+)\.(zx?)(\d{2,3})", name)
        number, scheme, format_name = None, "single", None
        if part:
            key, number, scheme, format_name = part[1] + ".rar", int(part[2]), "part_rar", "rar"
        elif split:
            key, number, scheme = split[1], int(split[2]), "numeric"
            format_name = next((value for value in _FORMATS if key.endswith("." + value)), None)
        elif old:
            key, number, scheme, format_name = old[1] + ".rar", int(old[2]) + 1, "legacy_rar", "rar"
        elif old_zip:
            format_name = "zipx" if old_zip[2] == "zx" else "zip"
            key, number, scheme = old_zip[1] + "." + format_name, int(old_zip[3]), "legacy_" + format_name
        elif name.endswith(".rar") and name[:-4] in old_heads:
            key, number, scheme, format_name = name, 0, "legacy_rar", "rar"
        elif name.endswith(".zip") and name[:-4] in zip_heads:
            key, number, scheme, format_name = name, 0, "legacy_zip", "zip"
        elif name.endswith(".zipx") and name[:-5] in zipx_heads:
            key, number, scheme, format_name = name, 0, "legacy_zipx", "zipx"
        else:
            format_name = next((value for value in _FORMATS + _UNSUPPORTED if name.endswith("." + value)), None)
            if format_name is None:
                if re.search(r"\.\d{3,}$", name):
                    format_name = "unknown_split"
                else:
                    continue
            key = name
        grouping = (key, scheme)
        group = groups.setdefault(grouping, {"name": key, "format": format_name, "multipart": scheme != "single",
                                             "scheme": scheme, "members": [], "numbers": []})
        group["members"].append(deepcopy(row))
        if number is not None:
            group["numbers"].append(number)
    multipart_names = {key for (key, scheme) in groups if scheme != "single"}
    result = []
    for group in groups.values():
        group["members"].sort(key=lambda row: row["name"].casefold())
        numbers = sorted(group.pop("numbers"))
        reasons = []
        if any(row["size"] <= 0 for row in group["members"]):
            reasons.append("archive_size_must_be_positive")
        if group["multipart"]:
            first = 0 if group["scheme"] in {"legacy_rar", "legacy_zip", "legacy_zipx"} else 1
            missing = ([value for value in range(first, max(numbers) + 1) if value not in numbers]
                       if numbers and max(numbers) <= 10000 else [])
            group["volume_check"] = {"observed_numbers": numbers, "missing_numbers": missing,
                                     "starts_at_expected_first": bool(numbers and numbers[0] == first),
                                     "duplicate_numbers": len(set(numbers)) != len(numbers),
                                     "terminal_volume_known": False, "complete": False}
            reasons.append("multipart_api_not_verified")
            if missing or not numbers or numbers[0] != first:
                reasons.append("missing_archive_volume")
            if len(set(numbers)) != len(numbers):
                reasons.append("duplicate_archive_volume")
            if numbers and max(numbers) > 10000:
                reasons.append("archive_volume_limit")
        elif len(group["members"]) != 1:
            reasons.append("ambiguous_archive_name")
        if not group["multipart"] and group["name"] in multipart_names:
            reasons.append("related_multipart_archive_requires_review")
        if group["format"] not in _FORMATS:
            reasons.append("unsupported_archive")
        group.update(status="NEEDS_INPUT" if reasons else "READY", reasons=reasons,
                     archive_id=_digest([(row["fid"], row["name"], row["size"]) for row in group["members"]]),
                     format_basis="filename_only", encryption_state="unknown_not_requested")
        result.append(group)
    return result


def inspect(storage, *, folder_id, library_root_id):
    _account(storage)
    _within(storage, library_root_id, folder_id)
    rows = _listing(storage, folder_id)
    return {"status": "INSPECTED", "folder_id": folder_id, "library_root_id": library_root_id,
            "account_scope_hash": _account(storage), "archives": classify_archives(rows),
            "writes_attempted": False, "writes_performed": False, "imported": False}


def extract(storage, *, fid, parent_id, library_root_id, journal, job_key,
            apply=False, reconcile=False, max_entries=10000, max_depth=12):
    report = {"status": "PLAN", "source_fid": fid, "parent_id": parent_id,
              "library_root_id": library_root_id, "writes_attempted": False,
              "writes_performed": False, "imported": False, "source_preserved": True,
              "new_files": [], "reasons": [], "read_only": not bool(apply and not reconcile)}
    state = None
    can_write = bool(apply and not reconcile)

    def save():
        journal.put(job_key, state)

    def finish(status, reason=None):
        report["status"] = status
        if reason:
            report["reasons"].append(reason)
        if state:
            report["target"] = deepcopy(state.get("target"))
            if state.get("task_id"):
                report["task_id"] = state["task_id"]
        return report

    def verify_target(*, empty=False):
        target = state["target"]
        known = target.get("folder_id")
        if not known:
            raise _Stop("UNKNOWN", "target_creation_unknown_without_identifier")
        for index in range(3):
            matches = [row for row in _listing(storage, library_root_id) if row["name"] == target["name"] or row["fid"] == known]
            if len(matches) == 1 and matches[0]["fid"] == known and matches[0]["name"] == target["name"] and matches[0]["is_dir"]:
                if empty and _listing(storage, known):
                    raise _Stop("NEEDS_INPUT", "isolated_target_is_not_empty")
                return
            if matches:
                raise _Stop("NEEDS_INPUT", "isolated_target_identity_changed")
            if index < 2:
                getattr(storage, "_sleep", lambda delay: None)(0.3)
        raise _Stop("UNKNOWN", "isolated_target_not_observed")

    def verify_source_scope():
        try:
            _within(storage, library_root_id, parent_id)
        except ValueError:
            raise _Stop("NEEDS_INPUT", "source_archive_scope_changed") from None
        current = [row for row in _listing(storage, parent_id) if row["fid"] == fid]
        if current != [source]:
            raise _Stop("NEEDS_INPUT", "source_archive_identity_changed")

    def task_result(result):
        if not isinstance(result, dict) or result.get("task_id") != state.get("task_id"):
            raise _Stop("UNKNOWN", "archive_task_identifier_mismatch")
        if result.get("status") == "failed":
            reason = result.get("reason")
            allowed = {"password_required", "multipart_requires_review", "membership_required", "unsupported_archive", "archive_task_failed"}
            state["status"] = "FAILED"
            state["reason"] = reason if reason in allowed else "archive_task_failed"
            save()
            return finish("NEEDS_INPUT", state["reason"])
        if result.get("status") != "done":
            raise _Stop("UNKNOWN", "archive_task_pending_or_unknown")
        fids = result.get("extracted_fids")
        if not isinstance(fids, list) or not fids or len(set(fids)) != len(fids):
            raise _Stop("UNKNOWN", "archive_task_has_no_unique_extracted_identifiers")
        for value in fids:
            _id(value)
        if state.get("extracted_fids") is not None and set(state["extracted_fids"]) != set(fids):
            raise _Stop("NEEDS_INPUT", "completed_archive_task_identifiers_changed")
        if any(value in state["before_fids"] or value in {fid, library_root_id, state["target"]["folder_id"]} for value in fids):
            raise _Stop("NEEDS_INPUT", "extracted_identifier_is_not_new")
        verify_target()
        for index in range(3):
            actual = _listing(storage, state["target"]["folder_id"])
            by_id = {row["fid"]: row for row in actual}
            if all(value in by_id for value in fids):
                if set(by_id) != set(fids):
                    raise _Stop("NEEDS_INPUT", "unexpected_files_in_isolated_target")
                output = _inventory(storage, state["target"]["folder_id"],
                                    max_entries=max_entries, max_depth=max_depth)
                output_ids = set(output["fids"]) - {state["target"]["folder_id"]}
                if output_ids.intersection(state["before_fids"]) or output_ids.intersection({fid, library_root_id}):
                    raise _Stop("NEEDS_INPUT", "extracted_nested_identifier_is_not_new")
                current_source = [row for row in _listing(storage, parent_id) if row["fid"] == fid]
                if current_source != [state["bound"]["source"]]:
                    report["source_preserved"] = False
                    raise _Stop("NEEDS_INPUT", "original_archive_no_longer_matches_verified_source")
                verify_source_scope()
                state.update(status="EXTRACTED", extracted_fids=fids)
                save()
                report["new_files"] = [by_id[value] for value in fids]
                report["verified_output_count"] = len(output_ids)
                return finish("EXTRACTED")
            if index < 2:
                getattr(storage, "_sleep", lambda delay: None)(0.3)
        raise _Stop("UNKNOWN", "extracted_files_not_observed")

    try:
        fid, parent_id, library_root_id = _id(fid), _id(parent_id), _id(library_root_id)
        if not isinstance(job_key, str) or not re.fullmatch(r"archive:[0-9a-f]{64}", job_key):
            raise ValueError("stable_archive_job_key_required")
        account = _account(storage)
        _within(storage, library_root_id, parent_id)
        rows = _listing(storage, parent_id)
        sources = [row for row in rows if row["fid"] == fid]
        if len(sources) != 1 or sources[0]["is_dir"] or sources[0]["size"] <= 0:
            return finish("NEEDS_INPUT", "source_archive_identity_or_size_not_verified")
        source = sources[0]
        bound = {"account_scope_hash": account, "library_root_id": library_root_id, "source": source}
        old = journal.get(job_key)
        if old is not None:
            if not isinstance(old, dict) or old.get("kind") != "archive_extract" or old.get("bound") != bound:
                return finish("NEEDS_INPUT", "archive_source_or_account_scope_changed")
            state = deepcopy(old)
            if not isinstance(state.get("target"), dict) or not isinstance(state.get("before_fids"), list):
                return finish("NEEDS_INPUT", "invalid_archive_job_evidence")
            inventory = state.get("before_inventory")
            if (not isinstance(inventory, dict) or inventory.get("complete") is not True
                    or inventory.get("root_id") != library_root_id
                    or inventory.get("fids") != sorted(state["before_fids"])
                    or library_root_id not in state["before_fids"]):
                return finish("NEEDS_INPUT", "complete_preexisting_library_inventory_required")
            target = state["target"]
            if (target.get("parent_id") != library_root_id or target.get("name") != "解压待整理_" + _digest(bound)[:16]
                    or len(set(state["before_fids"])) != len(state["before_fids"])
                    or (target.get("folder_id") and target["folder_id"] in state["before_fids"])):
                return finish("NEEDS_INPUT", "invalid_archive_target_creation_evidence")
            if state.get("status") not in {"TARGET_SUBMITTING", "TARGET_CREATED", "TARGET_READY", "SUBMITTING",
                                           "SUBMITTED", "EXTRACTED", "FAILED", "REJECTED", "UNKNOWN"}:
                return finish("NEEDS_INPUT", "invalid_archive_job_state")
            if state.get("status") in {"SUBMITTED", "EXTRACTED", "FAILED"} and not state.get("task_id"):
                return finish("UNKNOWN", "archive_submission_unknown_without_task_identifier")
            verify_target(empty=not bool(state.get("task_id")) and state.get("status") == "TARGET_READY")
            if state.get("status") == "REJECTED":
                return finish("NEEDS_INPUT", "membership_required")
            if state.get("task_id"):
                if not storage.capabilities.get("archive_extract"):
                    return finish("NEEDS_INPUT", "archive_api_not_available")
                return task_result(storage.archive_task_status(state["task_id"]))
            if state.get("status") in {"SUBMITTING", "UNKNOWN"}:
                return finish("UNKNOWN", "archive_submission_unknown_without_task_identifier")
            if not can_write:
                return finish("READY_TO_EXTRACT")
        groups = classify_archives(rows)
        selected = next((group for group in groups if any(row["fid"] == fid for row in group["members"])), None)
        report["archive"] = selected
        if selected is None:
            return finish("NEEDS_INPUT", "unsupported_archive")
        if selected["status"] != "READY":
            return finish("NEEDS_INPUT", selected["reasons"][0])
        if not getattr(storage, "capabilities", {}).get("archive_extract"):
            return finish("NEEDS_INPUT", "archive_api_not_available")
        if state is None:
            target_name = "解压待整理_" + _digest(bound)[:16]
            root_rows = _listing(storage, library_root_id)
            if any(row["name"] == target_name for row in root_rows):
                return finish("NEEDS_INPUT", "isolated_target_name_already_exists_without_ownership")
            before = _inventory(storage, library_root_id, max_entries=max_entries, max_depth=max_depth)
            if [row for row in before["entries"] if row["fid"] == fid] != [source]:
                return finish("NEEDS_INPUT", "source_archive_changed_during_inventory")
            report["target"] = {"parent_id": library_root_id, "name": target_name, "folder_id": None}
            report["preexisting_inventory_count"] = len(before["fids"])
            if not can_write:
                return finish("PLAN")
            verify_source_scope()
            state = {"kind": "archive_extract", "bound": bound, "status": "TARGET_SUBMITTING",
                     "target": deepcopy(report["target"]), "before_fids": before["fids"],
                     "before_inventory": {key: deepcopy(before[key]) for key in ("complete", "root_id", "fids", "limits")}}
            save()
            report["writes_attempted"] = True
            created = storage.mkdir(library_root_id, target_name)
            new_id = _id(created.get("fid") if isinstance(created, dict) else None)
            if new_id in state["before_fids"]:
                return finish("NEEDS_INPUT", "isolated_target_is_not_a_new_identifier")
            state["target"]["folder_id"] = new_id
            state["status"] = "TARGET_CREATED"
            save()
            report["writes_performed"] = True
            verify_target(empty=True)
            state["status"] = "TARGET_READY"
            save()
        if not can_write:
            return finish("READY_TO_EXTRACT")
        verify_target(empty=True)
        # Snapshot every old fid again immediately before submission. Keep the
        # union so a disappeared/moved old file can never become a new output.
        latest = _inventory(storage, library_root_id, max_entries=max_entries, max_depth=max_depth)
        if [row for row in latest["entries"] if row["fid"] == fid] != [source]:
            return finish("NEEDS_INPUT", "source_archive_changed_during_inventory")
        before_fids = sorted((set(state["before_fids"]) | set(latest["fids"])) - {state["target"]["folder_id"]})
        state["before_fids"] = before_fids
        state["before_inventory"] = {"complete": True, "root_id": library_root_id, "fids": before_fids,
                                     "limits": latest["limits"]}
        save()
        verify_target(empty=True)
        # This must be the final cloud validation before the submission intent:
        # target observations must not leave the source scope/identity stale.
        verify_source_scope()
        state["status"] = "SUBMITTING"
        save()
        report["writes_attempted"] = True
        def submitted(evidence):
            task_id = _id(evidence.get("task_id"))
            if evidence.get("source_fid") != fid or evidence.get("target_parent_id") != state["target"]["folder_id"]:
                raise ValueError("archive_submission_scope_mismatch")
            state.update(task_id=task_id, status="SUBMITTED")
            save()
        result = storage.extract_archive(fid, state["target"]["folder_id"], on_submitted=submitted)
        report["writes_performed"] = True
        if not state.get("task_id"):
            raise _Stop("UNKNOWN", "archive_provider_did_not_persist_submission_identifier")
        return task_result(result)
    except _Stop as exc:
        return finish(exc.status, exc.reason)
    except AuthenticationError:
        return finish("NEEDS_AUTH", "cloud_authentication_required")
    except RiskControlError:
        return finish("RISK_CONTROL", "cloud_risk_control_stop")
    except ProviderError as exc:
        if exc.code == 31001:
            if state is not None:
                state.update(status="REJECTED", reason="membership_required")
                save()
            return finish("NEEDS_INPUT", "membership_required")
        if state is not None and getattr(exc, "task_id", None) and not state.get("task_id"):
            try:
                state["task_id"] = _id(exc.task_id)
                state["status"] = "SUBMITTED"
                save()
            except Exception:
                pass
        report["error_type"] = type(exc).__name__
        return finish("UNKNOWN" if state else "NEEDS_INPUT", "archive_operation_not_completed")
    except Exception as exc:
        # A provider task identifier is safe; raw exception text/evidence is not.
        if state is not None and getattr(exc, "task_id", None) and not state.get("task_id"):
            try:
                state["task_id"] = _id(exc.task_id)
                state["status"] = "SUBMITTED"
                save()
            except Exception:
                pass
        report["error_type"] = type(exc).__name__
        return finish("UNKNOWN" if state else "NEEDS_INPUT", "archive_operation_not_completed")
