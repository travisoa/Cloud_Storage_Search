"""Adopt a completed root-scoped journal record for the current connection.

Records written before connection scopes were keyed by the library root only,
so they do not say which account made their saves. Quark fids belong to one
account: a recorded saved fid that the current connection reads back in its
recorded folder with the recorded size shows that the record's files are in
this account. Only then is a copy written under the current key. The original
record stays in place, nothing in the cloud is written, and unfinished records
are never adopted.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

from .providers import AuthenticationError, RiskControlError
from .workflow import _digest, _directory, _error, _fid, _list, _name

_PREFIXES = {"season", "movie"}


def _stop(report, reason, **details):
    report.update(status="NEEDS_INPUT", **details)
    report["reasons"].append(reason)
    return report


def adopt(storage, journal, legacy_key, *, apply=False):
    """Check (and with ``apply`` copy) one legacy record; never writes to the cloud."""
    report = {"status": "READY", "mode": "apply" if apply else "plan", "legacy_job_key": legacy_key,
              "reasons": [], "writes_attempted": False, "writes_performed": False, "journal_updated": False}
    prefix = legacy_key.partition(":")[0] if isinstance(legacy_key, str) else ""
    record = journal.get(legacy_key) if prefix in _PREFIXES else None
    if not isinstance(record, dict) or not isinstance(record.get("identity"), dict):
        return _stop(report, "legacy_record_not_found")
    identity = record["identity"]
    if prefix + ":" + _digest(identity) != legacy_key:
        return _stop(report, "legacy_record_identity_mismatch")
    scope = getattr(storage, "account_scope", None)
    if (not isinstance(scope, str) or not scope or scope == identity.get("account_scope")
            or identity.get("account_scope") not in getattr(storage, "legacy_account_scopes", ())):
        return _stop(report, "legacy_scope_not_owned_by_current_connection")
    if record.get("state") != "complete":
        return _stop(report, "legacy_record_unfinished_requires_manual_reconciliation",
                     legacy_state=record.get("state"))
    adopted_identity = dict(identity, account_scope=scope)
    key = prefix + ":" + _digest(adopted_identity)
    report["job_key"] = key
    if journal.get(key) is not None:
        return _stop(report, "current_scope_record_already_exists")
    files = [(str(operation.get("target_parent_id", "")), item)
             for operation in record.get("operations", []) if isinstance(operation, dict)
             for item in operation.get("verified_files", []) if isinstance(item, dict) and item.get("verified") is True]
    if not files:
        return _stop(report, "legacy_record_has_no_verified_files")
    listings, matched, missing, conflicts = {}, [], [], []
    for parent, item in files:
        if parent not in listings:
            try:
                listings[parent] = {_fid(row): row for row in _list(storage, parent)}
            except (AuthenticationError, RiskControlError) as exc:
                report.update(status="NEEDS_AUTH" if isinstance(exc, AuthenticationError) else "RISK_CONTROL",
                              error=_error(exc, "legacy_scope_check"))
                return report
            except Exception as exc:
                listings[parent] = None
                report.setdefault("listing_errors", []).append({"parent_id": parent, **_error(exc, "legacy_scope_check")})
        summary = {"saved_fid": str(item.get("saved_fid", "")), "parent_id": parent,
                   "size": item.get("size"), "target_name": item.get("target_name")}
        row = (listings[parent] or {}).get(summary["saved_fid"])
        if row is None:
            missing.append(summary)
        elif _directory(row) or type(item.get("size")) is not int or row.get("size") != item["size"]:
            conflicts.append(summary)
        else:
            # An authorized tidy may have renamed the file since; fid and size
            # are the account evidence, the name is reported for review.
            matched.append({**summary, "name_matches": _name(row) == item.get("target_name")})
    report.update(matched_files=matched, missing_files=missing, conflicting_files=conflicts)
    if conflicts:
        return _stop(report, "legacy_file_identity_conflict")
    if not matched:
        return _stop(report, "legacy_files_not_observed_in_current_connection")
    if not apply:
        report["reasons"].append("current_connection_reads_recorded_saved_fids")
        return report
    adopted = deepcopy(record)
    adopted["identity"] = adopted_identity
    adopted["adopted_from"] = {"job_key": legacy_key, "account_scope": identity["account_scope"],
                               "adopted_at": datetime.now(timezone.utc).isoformat(),
                               "method": "recorded_saved_fid_and_size_in_current_connection",
                               "matched_files": len(matched), "missing_files": len(missing)}
    journal.put(key, adopted)
    report.update(status="ADOPTED", journal_updated=True)
    return report
