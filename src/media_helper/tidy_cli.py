"""Plan, then separately apply, in-place naming cleanup of one library work folder."""
import json
import re
import uuid

from .journal import JobJournal, run_lock
from .profiles import private_json


def add_parser(commands):
    parser = commands.add_parser("tidy", help="rename files inside one existing work folder to the library naming rules")
    actions = parser.add_subparsers(dest="tidy_action", required=True)
    plan = actions.add_parser("plan", help="read-only proposal for one work folder")
    plan.add_argument("--folder-id", required=True)
    plan.add_argument("--kind", choices=("tv", "movie"))
    plan.add_argument("--library-id")
    apply = actions.add_parser("apply", help="recheck and execute a saved proposal; writes only with --apply")
    apply.add_argument("plan_id")
    apply.add_argument("--library-id")
    apply.add_argument("--apply", action="store_true")
    apply.add_argument("--plan", action="store_true")


def _selected(args, config, root):
    from .archive_cli import _selected as selected
    shim = type("Args", (), {"library_id": args.library_id, "archive_action": "inspect"})()
    storage, library_root = selected(shim, config, root)
    library = getattr(shim, "_library", None) or config
    if (library.get("test_only") and args.tidy_action == "apply"
            and args.apply and not args.plan):
        raise ValueError("隔离测试影库只允许整理预览")
    return storage, library_root, library


def _path(root, plan_id):
    if not isinstance(plan_id, str) or not re.fullmatch(r"[0-9a-f]{32}", plan_id):
        raise ValueError("整理计划 ID 格式错误")
    return root / "runtime/tidy-plans" / (plan_id + ".json")


def run_command(args, config, root):
    from .tidy import apply_tidy, plan_tidy
    with run_lock(root / "runtime/workflow.lock"):
        storage, library_root, library = _selected(args, config, root)
        if args.tidy_action == "plan":
            result = plan_tidy(storage, folder_id=args.folder_id, library_root_id=library_root,
                               categories=library.get("categories", {}), kind=args.kind,
                               include_media_tags=config.get("include_media_tags", True) is not False)
            plan_id = uuid.uuid4().hex
            private_json(_path(root, plan_id), {"record_id": plan_id, "library_root_id": library_root,
                                                "account_scope": storage.account_scope, "report": result})
            summary = {key: result[key] for key in ("status", "folder", "kind", "prefix", "operations", "review",
                                                     "compliant", "skipped", "writes_attempted", "writes_performed")
                       if key in result}
            result = {**summary, "plan_id": plan_id, "reason": result.get("reason")}
        else:
            path = _path(root, args.plan_id)
            if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
                raise ValueError("没有找到私有的整理计划")
            record = json.loads(path.read_text(encoding="utf-8"))
            if (record.get("record_id") != args.plan_id or record.get("library_root_id") != library_root
                    or record.get("account_scope") != storage.account_scope):
                raise ValueError("整理计划与当前影库或账号不一致")
            journal = JobJournal(root / "runtime/library-jobs.sqlite")
            try:
                result = apply_tidy(record["report"], storage, journal=journal, job_key="library-tidy:" + args.plan_id,
                                    apply=bool(args.apply and not args.plan))
            finally:
                journal.close()
            result["plan_id"] = args.plan_id
        result.update(browser_opened=False, library_id=args.library_id)
        private_json(root / "runtime/reports/tidy-latest.json", result)
        return result
