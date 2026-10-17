"""Create a cloud library with durable mkdir evidence and no file changes.

Only the selected parent and the library's direct children are listed. A lost
mkdir response never licenses another mkdir or ownership inferred from a name.
The caller supplies an account-bound storage adapter and owns the run lock.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import re
import time
import unicodedata

from .providers import AuthenticationError, RiskControlError


DEFAULT_CATEGORIES = {"movie": "电影", "tv": "电视剧", "animation": "动漫",
                      "documentary": "纪录片", "variety": "综艺"}
_VERIFY_ATTEMPTS = 3


class _Stop(Exception):
    def __init__(self, status, reason):
        self.status, self.reason = status, reason


def _identifier(value):
    if (not isinstance(value, str) or not value or len(value) > 256
            or not re.fullmatch(r"[A-Za-z0-9_.:-]+", value) or value in {".", ".."}):
        raise ValueError("invalid_directory_identifier")
    return value


def _name(value):
    if (not isinstance(value, str) or not value.strip() or value != value.strip()
            or len(value) > 128 or value in {".", ".."}
            or any(char in value for char in "/\\") or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("invalid_directory_name")
    return value


def _config(storage, parent_id, root_name, categories):
    scope = getattr(storage, "account_scope", None)
    if not isinstance(scope, str) or not scope.strip():
        raise ValueError("stable_account_scope_required")
    selected = DEFAULT_CATEGORIES if categories is None else categories
    if not isinstance(selected, dict) or not selected or len(selected) > 20:
        raise ValueError("nonempty_category_mapping_required")
    cleaned, names = {}, set()
    for key, value in selected.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", key):
            raise ValueError("invalid_category_key")
        value = _name(value)
        normalized = unicodedata.normalize("NFKC", value).casefold()
        if normalized in names:
            raise ValueError("category_names_must_be_unique")
        names.add(normalized)
        cleaned[key] = value
    return {"parent_id": _identifier(parent_id), "root_name": _name(root_name),
            "categories": cleaned,
            "account_scope_hash": hashlib.sha256(scope.encode()).hexdigest()}


def _listing(storage, parent_id):
    rows = storage.list_files(parent_id)
    if not isinstance(rows, list):
        raise _Stop("NEEDS_INPUT", "incomplete_or_invalid_directory_listing")
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise _Stop("NEEDS_INPUT", "incomplete_or_invalid_directory_listing")
        fid = row.get("fid")
        name = row.get("name", row.get("file_name"))
        directory = row.get("is_dir", row.get("dir"))
        actual_parent = row.get("parent_id")
        try:
            _identifier(fid)
            _name(name)
        except ValueError:
            raise _Stop("NEEDS_INPUT", "incomplete_or_invalid_directory_listing") from None
        if fid == parent_id or fid in seen or not isinstance(directory, bool) or str(actual_parent) != parent_id:
            raise _Stop("NEEDS_INPUT", "incomplete_or_invalid_directory_listing")
        seen.add(fid)
        result.append({"fid": fid, "name": name, "parent_id": parent_id, "is_dir": directory})
    return result


def _match(rows, name):
    matches = [row for row in rows if row["name"] == name]
    if len(matches) > 1:
        raise _Stop("NEEDS_INPUT", "duplicate_directory_name")
    if matches and not matches[0]["is_dir"]:
        raise _Stop("NEEDS_INPUT", "directory_name_is_occupied_by_file")
    return matches[0] if matches else None


def _verify(storage, operation):
    """Only a returned/persisted fid can reconcile a previous mkdir."""
    for index in range(_VERIFY_ATTEMPTS):
        rows = _listing(storage, operation["parent_id"])
        matching = _match(rows, operation["name"])
        by_id = next((row for row in rows if row["fid"] == operation["folder_id"]), None)
        if by_id is not None:
            if by_id["name"] != operation["name"] or not by_id["is_dir"] or matching != by_id:
                raise _Stop("NEEDS_INPUT", "created_directory_identity_changed")
            return by_id
        if matching is not None:
            raise _Stop("NEEDS_INPUT", "created_directory_identifier_conflict")
        if index + 1 < _VERIFY_ATTEMPTS:
            getattr(storage, "_sleep", time.sleep)(0.3)
    raise _Stop("UNKNOWN", "created_directory_not_yet_observed")


def initialize(storage, *, parent_id, root_name="影库", categories=None,
               journal, job_key, apply=False, reconcile=False):
    """Plan or create root/categories; ``reconcile`` always forbids cloud writes.

    ``storage.account_scope`` must identify the selected cloud account/connection,
    not just its root directory. Reuse the same job key to recover partial work.
    """
    report = {"status": "PLAN", "parent_id": parent_id, "root_name": root_name,
              "root_id": None, "categories": {}, "operations": [], "reasons": [],
              "writes_attempted": False, "writes_performed": False,
              "read_only": not bool(apply and not reconcile)}
    state = None
    can_write = bool(apply and not reconcile)
    persist = can_write or bool(reconcile)

    def save():
        if persist:
            durable = deepcopy(state)
            durable["operations"] = {key: row for key, row in durable["operations"].items()
                                     if row.get("status") != "PLANNED"}
            journal.put(job_key, durable)

    def finish(status, reason=None):
        report["status"] = status
        if reason:
            report["reasons"].append(reason)
        if state is not None:
            report["operations"] = [deepcopy(row) for row in state["operations"].values()]
        if status == "READY_TO_CONTINUE":
            report["next_action"] = "apply_same_job_to_create_remaining_directories"
        return report

    try:
        config = _config(storage, parent_id, root_name, categories)
        if (not isinstance(job_key, str) or not job_key or len(job_key) > 256
                or any(ord(char) < 32 for char in job_key)):
            raise ValueError("valid_initialization_job_key_required")
        report["categories"] = {key: {"folder_id": None, "name": name}
                                for key, name in config["categories"].items()}
        prior = journal.get(job_key)
        if prior is not None:
            if (not isinstance(prior, dict) or prior.get("kind") != "library_init"
                    or prior.get("config") != config or not isinstance(prior.get("operations"), dict)):
                return finish("NEEDS_INPUT", "initialization_scope_or_layout_changed")
            state = deepcopy(prior)
        else:
            state = {"kind": "library_init", "config": config, "operations": {}}

        def ensure(key, parent, name, rows=None):
            previous = state["operations"].get(key)
            if previous is not None:
                if (not isinstance(previous, dict) or previous.get("parent_id") != parent
                        or previous.get("name") != name):
                    raise _Stop("NEEDS_INPUT", "initialization_operation_scope_changed")
                if not previous.get("folder_id"):
                    raise _Stop("UNKNOWN", "mkdir_outcome_unknown_without_returned_identifier")
                _identifier(previous["folder_id"])
                verified = _verify(storage, previous)
                previous["status"] = "VERIFIED" if previous.get("action") == "create" else "REUSED"
                save()
                return verified["fid"]
            rows = _listing(storage, parent) if rows is None else rows
            existing = _match(rows, name)
            if existing:
                state["operations"][key] = {"key": key, "action": "reuse", "status": "REUSED",
                                             "parent_id": parent, "name": name, "folder_id": existing["fid"]}
                save()
                return existing["fid"]
            if not can_write:
                state["operations"][key] = {"key": key, "action": "create", "status": "PLANNED",
                                             "parent_id": parent, "name": name}
                # Planned rows are report data, never durable submission evidence.
                return None
            operation = {"key": key, "action": "create", "status": "SUBMITTING",
                         "parent_id": parent, "name": name}
            state["operations"][key] = operation
            save()  # Intent is durable before the one and only POST.
            report["writes_attempted"] = True
            try:
                created = storage.mkdir(parent, name)
            except Exception:
                operation["status"] = "UNKNOWN"
                save()
                raise
            fid = created.get("fid") if isinstance(created, dict) else None
            try:
                _identifier(fid)
            except ValueError:
                operation["status"] = "UNKNOWN"
                save()
                raise _Stop("UNKNOWN", "mkdir_outcome_unknown_without_returned_identifier") from None
            operation.update(folder_id=fid, status="CREATED")
            save()  # Do not perform any reads before persisting the returned fid.
            report["writes_performed"] = True
            _verify(storage, operation)
            operation["status"] = "VERIFIED"
            save()
            return fid

        root_id = ensure("root", config["parent_id"], config["root_name"])
        report["root_id"] = root_id
        if root_id is None:
            # The root does not exist, so its contents cannot be listed safely.
            for key, name in config["categories"].items():
                state["operations"]["category:" + key] = {"key": "category:" + key,
                    "action": "create", "status": "PLANNED", "parent_id": None, "name": name}
            return finish("READY_TO_CONTINUE" if reconcile else "PLAN")
        rows = _listing(storage, root_id)
        # Detect every known category conflict before creating any category.
        for name in config["categories"].values():
            _match(rows, name)
        complete = True
        for key, name in config["categories"].items():
            folder_id = ensure("category:" + key, root_id, name)
            report["categories"][key]["folder_id"] = folder_id
            complete = complete and folder_id is not None
        return finish("COMPLETE" if complete else "READY_TO_CONTINUE" if reconcile else "PLAN")
    except _Stop as exc:
        return finish(exc.status, exc.reason)
    except AuthenticationError:
        return finish("NEEDS_AUTH", "cloud_authentication_required")
    except RiskControlError:
        return finish("RISK_CONTROL", "cloud_risk_control_stop")
    except Exception as exc:
        # Neither provider messages nor evidence are safe to persist or display.
        report["error_type"] = type(exc).__name__
        pending = state is not None and any(row.get("status") in {"SUBMITTING", "CREATED", "UNKNOWN"}
                                            for row in state["operations"].values() if isinstance(row, dict))
        return finish("UNKNOWN" if pending else "NEEDS_INPUT", "initialization_check_failed")
