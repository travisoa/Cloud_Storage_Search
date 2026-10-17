"""Durable, credential-free request snapshots and explicit candidate decisions."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import posixpath
import re
import uuid

from .journal import safe_record
from .profiles import private_json, quality_preferences
from .version_policy import override_preset


def _now():
    return datetime.now(timezone.utc).isoformat()


class TaskStore:
    """Callers hold workflow.run_lock across reads, decisions, and execution.

    create(request, apply_authorized=False, report=None) returns the persisted
    task. finish(task, report, resolved_request=None) saves the next snapshot.
    Imported receipts must be independently verified by their caller; this
    store never upgrades receipt evidence into authority to rename cloud files.
    """
    def __init__(self, path, library_root_id):
        self.path = Path(path)
        self.library_root_id = str(library_root_id)
        if not self.library_root_id:
            raise ValueError("影库根目录尚未配置")

    def _path(self, task_id):
        if not isinstance(task_id, str) or not re.fullmatch(r"[0-9a-f]{32}", task_id):
            raise ValueError("任务 ID 格式无效")
        if self.path.is_symlink():
            raise ValueError("任务目录不能是符号链接")
        path = self.path / (task_id + ".json")
        if path.is_symlink():
            raise ValueError("任务记录不能是符号链接")
        return path

    def create(self, request, *, apply_authorized=False, report=None):
        request = deepcopy(request)
        # Freeform text is unnecessary for replay and may contain pasted URLs.
        request.pop("request_text", None)
        if request.get("library_root_id") not in {None, self.library_root_id}:
            raise ValueError("任务根目录与当前配置不一致")
        request["library_root_id"] = self.library_root_id
        task = {"schema_version": 1, "task_id": uuid.uuid4().hex,
                "library_root_id": self.library_root_id, "created_at": _now(),
                "updated_at": _now(), "apply_authorized": bool(apply_authorized),
                "resolved_request": request, "status": "RESOLVING", "decisions": [],
                "report": report or {"status": "RESOLVING", "browser_opened": False}}
        if report:
            task["status"] = report.get("status", "UNKNOWN")
        self.save(task)
        return task

    def save(self, task):
        if (task.get("schema_version") != 1 or str(task.get("library_root_id")) != self.library_root_id
                or str(task.get("resolved_request", {}).get("library_root_id")) != self.library_root_id):
            raise ValueError("任务根目录与当前配置不一致")
        task["updated_at"] = _now()
        private_json(self._path(task["task_id"]), safe_record(task))

    def load(self, task_id):
        path = self._path(task_id)
        if not path.is_file():
            raise ValueError("没有找到此本地任务")
        task = json.loads(path.read_text())
        if (task.get("schema_version") != 1 or task.get("task_id") != task_id
                or str(task.get("library_root_id")) != self.library_root_id
                or str(task.get("resolved_request", {}).get("library_root_id")) != self.library_root_id):
            raise ValueError("任务身份或影库根目录与当前配置不一致")
        return safe_record(task)

    def finish(self, task, report, *, resolved_request=None):
        if resolved_request is not None:
            task["resolved_request"] = deepcopy(resolved_request)
            task["resolved_request"].pop("request_text", None)
        report = deepcopy(report)
        report["task_id"] = task["task_id"]
        report["browser_opened"] = False
        task["report"], task["status"] = report, report.get("status", "UNKNOWN")
        self.save(task)
        return report


def confirmation_request(task, *, quality=None, numeric_episodes=False, selection=None):
    """Use only observed snapshots. The workflow re-enumerates every source."""
    if task.get("status") not in {"NEEDS_INPUT", "READY"}:
        raise ValueError("此任务当前无需候选确认；可先查询状态或只读对账")
    request = deepcopy(task["resolved_request"])
    if quality or selection:
        override_preset(request)
    if not all(request.get(key) for key in ("series_id", "metadata_source", "year", "season")):
        raise ValueError("需先使用 bind-series 确认作品身份，再发起新的查询任务")
    candidates = task.get("report", {}).get("source_candidates") or []
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("任务没有可验证的视频候选快照，请重新查询")
    selection = selection or {}
    if (not isinstance(selection, dict) or any(not re.fullmatch(r"S\d{2}E\d{2,3}", str(key))
            or not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
            for key, value in selection.items())):
        raise ValueError("候选选择必须为集号到候选 ID 的 JSON 对象")
    preference = quality_preferences([quality]) if quality else []
    needed = set(task.get("report", {}).get("unresolved_episodes", {}))
    for item in task.get("report", {}).get("missing", []):
        key = item.get("episode_key")
        if key:
            needed.add(key)
        elif type(item.get("season")) is int and type(item.get("episode")) is int:
            needed.add(f'S{item["season"]:02d}E{item["episode"]:02d}')
    if not needed or set(selection) - needed:
        raise ValueError("选择中的集数不属于本任务缺集快照")
    grouped, mapped = {}, {}
    prior_mapping = request.get("source_episode_mapping", {})
    for candidate in candidates:
        identifier = candidate.get("candidate_id")
        if (not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{64}", identifier)
                or not isinstance(candidate.get("fid"), str) or not candidate["fid"]
                or not isinstance(candidate.get("name"), str)
                or type(candidate.get("size")) is not int or candidate["size"] <= 0):
            continue
        key = candidate.get("episode_key")
        needs_mapping = False
        confirmed = prior_mapping.get(identifier)
        if (isinstance(confirmed, dict) and confirmed.get("name") == candidate["name"]
                and confirmed.get("size") == candidate["size"]
                and confirmed.get("season") == request["season"]
                and type(confirmed.get("episode")) is int):
            confirmed_key = f'S{confirmed["season"]:02d}E{confirmed["episode"]:02d}'
            if key in {None, confirmed_key}:
                key = confirmed_key
                mapped[identifier] = deepcopy(confirmed)
        if not key and numeric_episodes:
            number = candidate.get("suggested_episode")
            stem, _ = posixpath.splitext(candidate["name"])
            if (type(number) is int and 1 <= number <= 999 and re.fullmatch(r"\d+", stem)
                    and int(stem) == number):
                key = f'S{int(request["season"]):02d}E{number:02d}'
                needs_mapping = True
        if key not in needed:
            continue
        if selection.get(key) and selection[key] != identifier:
            continue
        if preference and candidate.get("media_info", {}).get("resolution") != preference[0]:
            continue
        grouped.setdefault(key, {})[identifier] = candidate
        if needs_mapping:
            mapped[identifier] = {"season": int(request["season"]), "episode": candidate["suggested_episode"],
                                  "name": candidate["name"], "size": candidate["size"]}
    ambiguous = [key for key in sorted(needed) if len(grouped.get(key, {})) != 1]
    if ambiguous:
        raise ValueError("以下缺集仍没有唯一候选，请明确候选 ID 或季集映射：" + "、".join(ambiguous))
    source_selection, mapping, chosen = {}, {}, []
    for key in sorted(needed):
        identifier, candidate = next(iter(grouped[key].items()))
        source_selection[key] = identifier
        if identifier in mapped:
            mapping[identifier] = mapped[identifier]
        chosen.append({"episode_key": key, "candidate_id": identifier, "fid": candidate["fid"],
                       "name": candidate["name"], "size": candidate["size"]})
    request["source_selection"] = source_selection
    request["source_episode_mapping"] = mapping
    if preference:
        request["quality_preference"] = preference
    decision = {"confirmed_at": _now(), "numeric_episodes": bool(numeric_episodes),
                "quality_preference": preference, "candidates": chosen}
    return request, decision


def movie_confirmation_request(task, *, quality=None, selection=None):
    """Choose one observed movie candidate; the workflow re-enumerates it."""
    if task.get("status") not in {"NEEDS_INPUT", "READY"}:
        raise ValueError("此任务当前无需候选确认；可先查询状态或只读对账")
    request = deepcopy(task["resolved_request"])
    if quality or selection:
        override_preset(request)
    if not all(request.get(key) for key in ("movie_id", "year")):
        raise ValueError("电影身份尚未确认，请补充年份或 TMDB ID 后重新查询")
    candidates = [row for row in task.get("report", {}).get("source_candidates") or []
                  if isinstance(row, dict) and (selection or not row.get("excluded_by_version_policy"))
                  and isinstance(row.get("candidate_id"), str) and re.fullmatch(r"[0-9a-f]{64}", row["candidate_id"])]
    if not candidates:
        raise ValueError("任务没有可验证的电影候选快照，请重新查询")
    selection = selection or {}
    if (not isinstance(selection, dict) or set(selection) - {"movie"}
            or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in selection.values())):
        raise ValueError('电影候选选择格式为 {"movie":"候选 ID"}')
    preference = quality_preferences([quality]) if quality else []
    if selection:
        candidates = [row for row in candidates if row["candidate_id"] == selection["movie"]]
    if preference:
        candidates = [row for row in candidates if row.get("media_info", {}).get("resolution") == preference[0]]
    if len(candidates) != 1:
        raise ValueError("电影候选仍不唯一或不存在，请用 --selection 指定候选 ID")
    chosen = candidates[0]
    request["source_selection"] = {"movie": chosen["candidate_id"]}
    if preference:
        request["quality_preference"] = preference
    decision = {"confirmed_at": _now(), "quality_preference": preference,
                "candidates": [{"episode_key": "movie", "candidate_id": chosen["candidate_id"], "fid": chosen.get("fid"),
                                "name": chosen.get("name"), "size": chosen.get("size")}]}
    return request, decision
