"""Use the normal task store for explicitly requested saved-file name repairs."""
from copy import deepcopy
from datetime import datetime, timezone


def run_task(task_id, apply=False, config=None):
    from .headless_cli import ROOT, _config, _prepare_identity, _storage, _quark_ready, _safe_failure
    from .journal import JobJournal, run_lock
    from .profiles import private_json
    from .saved_naming import normalize_saved
    from .tasks import TaskStore

    config = _config() if config is None else config
    with run_lock(ROOT / "runtime/workflow.lock"):
        store = TaskStore(ROOT / "runtime/tasks", config["library_root_id"])
        task = store.load(task_id)
        journal = None
        try:
            resolved, issue, metadata, _ = _prepare_identity(task["resolved_request"], config)
            if issue:
                return store.finish(task, issue)
            if not _quark_ready(config):
                return store.finish(task, {"status": "NEEDS_AUTH", "reason": "尚未配置可用夸克挂载"})
            # Keep the original verified transfer receipt across plan/apply and
            # process interruptions. A later naming report is not a save receipt.
            receipt = deepcopy(task.get("transfer_receipt", task["report"]))
            task["transfer_receipt"] = receipt
            task["resolved_request"] = resolved
            task["decisions"].append({"operation": "normalize_saved_names", "apply_authorized": bool(apply),
                                      "confirmed_at": datetime.now(timezone.utc).isoformat()})
            store.save(task)
            storage = _storage(config)
            journal = JobJournal(ROOT / "runtime/jobs.sqlite")
            result = normalize_saved(resolved, storage, metadata, journal, receipt, apply=apply)
            if result.get("updated_receipt"):
                task["transfer_receipt"] = deepcopy(result["updated_receipt"])
            result = store.finish(task, result, resolved_request=resolved)
            name = "latest-naming-apply.json" if apply else "latest-naming-plan.json"
            private_json(ROOT / "runtime/reports" / name, result)
            return result
        except Exception as exc:
            return store.finish(task, _safe_failure(exc))
        finally:
            if journal is not None:
                journal.close()
