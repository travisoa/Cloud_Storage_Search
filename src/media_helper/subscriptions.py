"""Private, root-bound series subscriptions and quiet, durable run summaries.

This store carries user authority, not credentials. Callers must still recheck
metadata, cloud scope and candidate evidence before any cloud write.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import unicodedata

from .profiles import identity_key, private_json, quality_preferences
from .sources import safe_source


HEALTHY_STATUSES = frozenset({"UP_TO_DATE", "WAITING", "READY", "COMPLETE"})
ATTENTION_STATUSES = frozenset({"NEEDS_INPUT", "NEEDS_AUTH", "RISK_CONTROL", "UNKNOWN", "FAILED",
                                "PARTIAL", "PENDING", "READY_TO_CONTINUE", "READY_TO_RENAME", "NO_RECORD", "METADATA_CONFLICT"})
SUPPORTED_STATUSES = HEALTHY_STATUSES | ATTENTION_STATUSES
# Opt-in source policies; an absent field means the strict default.
SOURCE_SWITCHES = ("trust_share_title", "wait_on_uncertain_source")
ARCHIVE_AUTO = (None, "largest")
_UNSET = object()
_SECRET = re.compile(r"cookie|auth|token|password|passcode|secret|credential|api[-_]?key|signature|(?:^|_)(?:pwd|sig)(?:$|_)", re.I)
_ID = re.compile(r"[A-Za-z0-9_-]{1,240}")
_EPISODE = re.compile(r"S\d{2}E\d{2,3}")
_REASON = re.compile(r"[A-Za-z0-9_:.\-]{1,180}")


def _now():
    return datetime.now(timezone.utc)


def _time(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("订阅时间必须是带时区的 ISO 时间") from None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("订阅时间必须包含时区")
    return value.astimezone(timezone.utc)


def _identifier(value, label):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(label + "格式无效")
    return value


def _no_secrets(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if _SECRET.search(str(key)):
                raise ValueError("凭据字段不得进入订阅记录")
            _no_secrets(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            _no_secrets(item)


def _text(value, label, *, filename=False):
    if (not isinstance(value, str) or not value.strip() or len(value) > 512
            or any(unicodedata.category(c) in {"Cc", "Cs"} for c in value)
            or (filename and any(c in value for c in '/\\<>:"|?*'))
            or re.search(r"https?://|(?:cookie|authorization|token|password|passcode|密码|提取码)\s*[:：=]", value, re.I)):
        raise ValueError(label + "必须是无凭据的有效名称")
    return unicodedata.normalize("NFC", value.strip())


def _request(value, root):
    if not isinstance(value, dict):
        raise ValueError("订阅需要已经确认的作品请求")
    _no_secrets(value)
    if value.get("library_root_id") not in {None, root}:
        raise ValueError("订阅影库根目录与当前配置不一致")
    source, identifier, year = value.get("metadata_source"), value.get("series_id"), value.get("year")
    identity_key(source, identifier, year)
    season = value.get("season")
    if type(season) is not int or not 1 <= season <= 99:
        raise ValueError("订阅季号必须为 1–99")
    result = {"title": _text(value.get("title"), "作品名", filename=True), "metadata_source": source,
              "series_id": str(int(str(identifier))), "year": year, "season": season, "library_root_id": root}
    if "aliases" in value:
        if not isinstance(value["aliases"], list):
            raise ValueError("作品别名必须为列表")
        result["aliases"] = list(dict.fromkeys(_text(item, "作品别名") for item in value["aliases"]))
    # A task's policy-derived resolution is refreshed from VersionPolicyStore
    # each cycle. Persisting it as a standing preference would revive a reset
    # policy after the transient version_policy_key has been stripped here.
    if "quality_preference" in value and value.get("version_policy_key") is None:
        result["quality_preference"] = quality_preferences(value["quality_preference"])
        if not result["quality_preference"]:
            raise ValueError("订阅画质偏好不能为空")
    for field in ("include_media_tags", "library_root_is_series_category"):
        if field in value:
            if type(value[field]) is not bool:
                raise ValueError(field + "必须是布尔值")
            result[field] = value[field]
    for field in ("target_folder_id", "category_folder_id"):
        if value.get(field) is not None:
            result[field] = _identifier(value[field], field)
    # Candidate decisions and freeform text are single-run evidence, not standing
    # authority. In particular, never persist source_selection URL dictionaries.
    return result


def _subscription_id(request):
    identity = {key: request[key] for key in ("metadata_source", "series_id", "year", "season", "library_root_id")}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _sources(values):
    if not isinstance(values, (list, tuple)):
        raise ValueError("订阅来源必须为列表")
    result = {}
    for value in values:
        source = safe_source(value)
        result[(source["source_hash"], source.get("parent_id"), source.get("path"))] = source
    return list(result.values())


def _numeric_policy(value, request, sources):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"source_hash", "parent_id", "season", "quality"}:
        raise ValueError("纯数字集号策略必须精确指定来源、目录、季号和画质")
    _no_secrets(value)
    digest = value.get("source_hash")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("纯数字集号策略的来源哈希无效")
    parent = _identifier(value.get("parent_id"), "来源目录 ID")
    if type(value.get("season")) is not int or value["season"] != request["season"]:
        raise ValueError("纯数字集号策略必须绑定本次订阅的季号")
    matching = [source for source in sources if source["source_hash"] == digest and source.get("parent_id") == parent]
    if len(matching) != 1:
        raise ValueError("纯数字集号策略必须绑定已确认来源的真实目录")
    quality = quality_preferences([value["quality"]])[0]
    return {"source_hash": digest, "parent_id": parent, "season": request["season"], "quality": quality}


def _secondary(value, request):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"source", "series_id"}:
        raise ValueError("补充元数据必须明确来源和该来源的作品 ID")
    _no_secrets(value)
    identity_key(value.get("source"), value.get("series_id"), request["year"])
    if value["source"] == request["metadata_source"]:
        raise ValueError("补充元数据必须来自不同的来源")
    return {"source": value["source"], "series_id": str(int(str(value["series_id"])))}


def _episode_keys(items):
    result = set()
    if not isinstance(items, list):
        return []
    for item in items:
        if isinstance(item, str):
            key = item
        elif isinstance(item, dict):
            key = item.get("episode_key")
            if key is None and type(item.get("season")) is int and type(item.get("episode")) is int:
                key = f'S{item["season"]:02d}E{item["episode"]:02d}'
        else:
            continue
        if isinstance(key, str) and _EPISODE.fullmatch(key):
            result.add(key)
    return sorted(result)


def _new_files(report):
    result = {}
    rows = list(report.get("new_files") or []) if isinstance(report.get("new_files"), list) else []
    for operation in report.get("operations") or []:
        if not isinstance(operation, dict):
            continue
        rows.extend(item for item in operation.get("verified_files", []) if isinstance(item, dict) and item.get("verified") is True)
    for item in rows:
        if not isinstance(item, dict) or item.get("verified") is False:
            continue
        fid, name = item.get("saved_fid", item.get("fid")), item.get("target_name", item.get("name"))
        try:
            fid = _identifier(fid, "已核验文件 ID")
            name = _text(name, "已核验文件名", filename=True)
        except ValueError:
            continue
        row = {"fid": fid, "name": name}
        keys = _episode_keys([item])
        if keys:
            row["episode_key"] = keys[0]
        result[fid] = row
    return sorted(result.values(), key=lambda item: item["fid"])


def _metadata_summary(value):
    if not isinstance(value, dict) or value.get("status") not in {"ok", "degraded", "conflict", "error"}:
        return None
    result = {"status": value["status"], "conflicts": [], "errors": []}
    conflicts = value.get("conflicts", [])
    errors = value.get("errors", [])
    for item in conflicts if isinstance(conflicts, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("code"), str) or not _REASON.fullmatch(item["code"]):
            continue
        row = {"code": item["code"]}
        if item.get("source") in {"tmdb", "tvmaze"}:
            row["source"] = item["source"]
        if isinstance(item.get("episode_key"), str) and _EPISODE.fullmatch(item["episode_key"]):
            row["episode_key"] = item["episode_key"]
        for key in ("primary_only", "secondary_only"):
            if isinstance(item.get(key), list):
                row[key] = _episode_keys(item[key])
        for key in ("primary_count", "secondary_count"):
            if type(item.get(key)) is int and 0 <= item[key] <= 9999:
                row[key] = item[key]
        result["conflicts"].append(row)
    for item in errors if isinstance(errors, list) else []:
        if (not isinstance(item, dict) or item.get("source") not in {"tmdb", "tvmaze"}
                or not isinstance(item.get("code"), str) or not _REASON.fullmatch(item["code"])):
            continue
        row = {"source": item["source"], "code": item["code"]}
        if type(item.get("http_status")) is int and 100 <= item["http_status"] <= 599:
            row["http_status"] = item["http_status"]
        result["errors"].append(row)
    for key in ("conflicts", "errors"):
        result[key] = sorted(result[key], key=lambda item: json.dumps(item, sort_keys=True))
    return result


def _summary(report):
    if not isinstance(report, dict):
        raise ValueError("订阅执行结果必须是对象")
    status = report.get("status")
    if not isinstance(status, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", status):
        raise ValueError("订阅执行结果状态无效")
    reasons = sorted({value for value in report.get("reasons", [])
                      if isinstance(value, str) and _REASON.fullmatch(value)}) if isinstance(report.get("reasons", []), list) else []
    result = {"status": status, "reason_ids": reasons, "new_files": _new_files(report),
              "existing_count": len(report["existing"]) if isinstance(report.get("existing"), list) else 0,
              "missing_count": len(report["missing"]) if isinstance(report.get("missing"), list) else 0,
              "future_count": len(report["future"]) if isinstance(report.get("future"), list) else 0,
              "missing_episodes": _episode_keys(report.get("missing")),
              "unknown_schedule_count": len(report["unknown_schedule"]) if isinstance(report.get("unknown_schedule"), list) else 0}
    task_id = report.get("task_id", report.get("last_task_id"))
    if isinstance(task_id, str) and re.fullmatch(r"[0-9a-f]{32}", task_id):
        result["last_task_id"] = task_id
    error = report.get("error")
    if isinstance(error, dict):
        result["error"] = {key: error[key] for key in ("error_type", "step", "status")
                           if isinstance(error.get(key), (str, int)) and _REASON.fullmatch(str(error[key]))}
    checks = report.get("resource_checks")
    if isinstance(checks, list):
        result["source_summary"] = [{"source_hash": item["source"], "status": item["status"]}
                                     for item in checks if isinstance(item, dict)
                                     and isinstance(item.get("source"), str) and re.fullmatch(r"[0-9a-f]{64}", item["source"])
                                     and isinstance(item.get("status"), str) and _REASON.fullmatch(item["status"])]
    metadata = _metadata_summary(report.get("metadata_updates"))
    if metadata:
        result["metadata_updates"] = metadata
    return result


def _attention_key(summary):
    if summary["status"] in HEALTHY_STATUSES:
        return None
    return {key: summary.get(key) for key in ("status", "reason_ids", "error")}


def _metadata_key(summary):
    return summary.get("metadata_updates") or {"status": "ok", "conflicts": [], "errors": []}


class SubscriptionStore:
    """Mutations are serialized independently of the cloud workflow lock.

    ``blocked`` is a latch: healthy readbacks, duplicate add and enable operations
    never clear a prior ambiguous write, risk, auth or confirmation requirement.
    The runner must inspect it and avoid automatic cloud writes while blocked.
    """
    def __init__(self, path, library_root_id):
        self.path = Path(path)
        self.library_root_id = _identifier(library_root_id, "影库根目录 ID")

    def _check_path(self):
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise ValueError("订阅记录及其目录不能是符号链接")

    @contextmanager
    def _lock(self):
        self._check_path()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.path.with_name(self.path.name + ".lock")
        if lock_path.is_symlink():
            raise ValueError("订阅锁不能是符号链接")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("订阅锁必须为普通文件")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _read(self):
        self._check_path()
        if not self.path.exists():
            return {}
        if not self.path.is_file() or self.path.stat().st_mode & 0o077:
            raise ValueError("订阅记录必须是权限为 0600 的普通文件")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or data.get("schema_version") != 1
                or data.get("library_root_id") != self.library_root_id
                or not isinstance(data.get("subscriptions"), dict)):
            raise ValueError("订阅记录格式或影库根目录与当前配置不一致")
        _no_secrets(data)
        for identifier, record in data["subscriptions"].items():
            if not isinstance(record, dict):
                raise ValueError("订阅记录无效")
            request = _request(record.get("request"), self.library_root_id)
            if (record.get("subscription_id") != identifier or _subscription_id(request) != identifier
                    or record.get("library_root_id") != self.library_root_id):
                raise ValueError("订阅身份或影库根目录与当前配置不一致")
            if request != record["request"]:
                raise ValueError("订阅请求含不受支持或非规范字段")
            sources = _sources(record.get("preferred_sources", []))
            if sources != record.get("preferred_sources"):
                raise ValueError("订阅来源记录无效")
            _numeric_policy(record.get("numeric_policy"), request, sources)
            _secondary(record.get("secondary"), request)
            for field in ("enabled", "auto_save", "blocked", "needs_attention"):
                if type(record.get(field)) is not bool:
                    raise ValueError("订阅授权或阻塞状态无效")
            for field in SOURCE_SWITCHES:
                if field in record and type(record[field]) is not bool:
                    raise ValueError("订阅来源策略开关必须为布尔值")
            if record.get("archive_auto") not in ARCHIVE_AUTO:
                raise ValueError("订阅压缩包策略无效")
            hours = record.get("interval_hours")
            if (isinstance(hours, bool) or not isinstance(hours, (int, float))
                    or not math.isfinite(hours) or not 1 <= hours <= 168):
                raise ValueError("订阅追更间隔无效")
            for field in ("created_at", "updated_at", "last_checked_at", "next_check_at"):
                if record.get(field) is not None:
                    _time(record[field])
        return data["subscriptions"]

    def _write(self, items):
        self._check_path()
        _no_secrets(items)
        private_json(self.path, {"schema_version": 1, "library_root_id": self.library_root_id, "subscriptions": items})

    def list(self):
        with self._lock():
            return deepcopy(sorted(self._read().values(), key=lambda value: value["subscription_id"]))

    def get(self, subscription_id):
        if not isinstance(subscription_id, str) or not re.fullmatch(r"[0-9a-f]{64}", subscription_id):
            raise ValueError("订阅 ID 格式无效")
        with self._lock():
            record = self._read().get(subscription_id)
            if record is None:
                raise ValueError("没有找到此订阅")
            return deepcopy(record)

    def add(self, request, *, preferred_sources=_UNSET, interval_hours=_UNSET, auto_save=_UNSET,
            numeric_policy=_UNSET, secondary=_UNSET, trust_share_title=_UNSET,
            wait_on_uncertain_source=_UNSET, archive_auto=_UNSET, append_sources=False):
        """Create with six-hour checks and no write authority by default.

        Re-adding the same identity updates only explicitly supplied options;
        omitted options retain their prior values and never clear a block.
        ``append_sources`` adds the supplied sources after the recorded ones
        instead of replacing them.
        """
        request = _request(request, self.library_root_id)
        identifier, now = _subscription_id(request), _now().isoformat()
        with self._lock():
            items = self._read()
            record = deepcopy(items.get(identifier, {}))
            hours = record.get("interval_hours", 6) if interval_hours is _UNSET else interval_hours
            authorized = record.get("auto_save", False) if auto_save is _UNSET else auto_save
            if (isinstance(hours, bool) or not isinstance(hours, (int, float))
                    or not math.isfinite(hours) or not 1 <= hours <= 168):
                raise ValueError("追更间隔必须为 1–168 小时")
            if type(authorized) is not bool:
                raise ValueError("自动转存授权必须为布尔值")
            if type(append_sources) is not bool:
                raise ValueError("追加来源选项必须为布尔值")
            recorded = list(record.get("preferred_sources", ()))
            if preferred_sources is _UNSET:
                sources = _sources(recorded)
            else:
                sources = _sources([*recorded, *preferred_sources] if append_sources else preferred_sources)
            numeric = _numeric_policy(record.get("numeric_policy") if numeric_policy is _UNSET else numeric_policy, request, sources)
            supplement = _secondary(record.get("secondary") if secondary is _UNSET else secondary, request)
            switches = {}
            for field, value in zip(SOURCE_SWITCHES, (trust_share_title, wait_on_uncertain_source)):
                switches[field] = record.get(field, False) if value is _UNSET else value
                if type(switches[field]) is not bool:
                    raise ValueError("订阅来源策略开关必须为布尔值")
            archives = record.get("archive_auto") if archive_auto is _UNSET else archive_auto
            if archives not in ARCHIVE_AUTO:
                raise ValueError("压缩包自动处理策略只能为 largest 或关闭")
            request = {**record.get("request", {}), **request}
            record.update(subscription_id=identifier, library_root_id=self.library_root_id, request=request,
                          preferred_sources=sources, interval_hours=hours, auto_save=authorized,
                          numeric_policy=numeric, secondary=supplement, archive_auto=archives,
                          updated_at=now, **switches)
            record.setdefault("created_at", now)
            record.setdefault("enabled", True)
            record.setdefault("blocked", False)
            record.setdefault("needs_attention", False)
            record.setdefault("last_checked_at", None)
            record.setdefault("next_check_at", None)
            record.setdefault("last_run", None)
            record.setdefault("observed_new_fids", [])
            items[identifier] = record
            self._write(items)
            return deepcopy(record)

    def set_enabled(self, subscription_id, enabled):
        if type(enabled) is not bool:
            raise ValueError("订阅启用状态必须为布尔值")
        return self._update(subscription_id, {"enabled": enabled})

    def update_sources(self, subscription_id, sources):
        sources = _sources(sources)
        with self._lock():
            items = self._read()
            record = self._find(items, subscription_id)
            _numeric_policy(record.get("numeric_policy"), record["request"], sources)
            record.update(preferred_sources=sources, updated_at=_now().isoformat())
            self._write(items)
            return deepcopy(record)

    def clear_block(self, subscription_id, reconciliation_report):
        """Explicitly clear a block using a successful, strictly read-only report.

        This method never runs reconciliation or submits a cloud operation. The
        caller must obtain the report for this exact subscription, in this call.
        """
        accepted = {"COMPLETE", "UP_TO_DATE", "READY", "READY_TO_CONTINUE", "READY_TO_RENAME", "NO_RECORD"}
        if (not isinstance(reconciliation_report, dict) or reconciliation_report.get("status") not in accepted
                or reconciliation_report.get("subscription_id", subscription_id) != subscription_id
                or reconciliation_report.get("writes_attempted") is not False
                or reconciliation_report.get("writes_performed") is not False):
            raise ValueError("解除阻塞需要本次成功的只读对账结果")
        summary = _summary(reconciliation_report)
        return self._update(subscription_id, {"blocked": False, "needs_attention": False,
                                              "blocked_status": None, "blocked_reason_ids": [],
                                              "reconciliation": summary})

    @staticmethod
    def _find(items, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{64}", identifier) or identifier not in items:
            raise ValueError("没有找到此订阅")
        return items[identifier]

    def _update(self, identifier, changes):
        with self._lock():
            items = self._read()
            record = self._find(items, identifier)
            record.update(changes, updated_at=_now().isoformat())
            self._write(items)
            return deepcopy(record)

    def due(self, as_of=None):
        as_of = _time(as_of if as_of is not None else _now())
        return [record for record in self.list() if record["enabled"] and
                (record.get("next_check_at") is None or _time(record["next_check_at"]) <= as_of)]

    def record_run(self, subscription_id, report, *, checked_at=None, next_check_at=None):
        checked_at = _time(checked_at if checked_at is not None else _now())
        summary = _summary(report)
        with self._lock():
            items = self._read()
            record = self._find(items, subscription_id)
            next_time = (_time(next_check_at) if next_check_at is not None
                         else checked_at + timedelta(hours=record["interval_hours"]))
            if next_time <= checked_at:
                raise ValueError("下次检查时间必须晚于本次检查")
            previous = record.get("last_run")
            if record.get("last_checked_at") and checked_at < _time(record["last_checked_at"]):
                raise ValueError("不能用较早的检查结果覆盖较新的订阅状态")
            seen = set(record.get("observed_new_fids", []))
            new_files = [item for item in summary["new_files"] if item["fid"] not in seen]
            attention = _attention_key(summary)
            missing_changed = (summary["missing_episodes"] != previous.get("missing_episodes", [])
                               if previous else bool(summary["missing_count"]))
            attention_changed = attention != (_attention_key(previous) if previous else None)
            metadata_changed = _metadata_key(summary) != _metadata_key(previous or {})
            meaningful_change = bool(new_files or missing_changed or attention_changed or metadata_changed)
            if attention is not None:
                record["blocked"] = record["needs_attention"] = True
                record["blocked_status"] = summary["status"]
                record["blocked_reason_ids"] = summary["reason_ids"]
            record.update(last_checked_at=checked_at.isoformat(), next_check_at=next_time.isoformat(),
                          last_run=summary, observed_new_fids=sorted(seen | {item["fid"] for item in summary["new_files"]}),
                          updated_at=_now().isoformat())
            self._write(items)
            return {"record": deepcopy(record), "meaningful_change": meaningful_change,
                    "summary": deepcopy(summary), "new_files": deepcopy(new_files)}
