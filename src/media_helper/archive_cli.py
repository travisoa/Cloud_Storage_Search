"""Explicit archive staging commands; credentials remain in provider memory."""
import hashlib
import json
import re
import uuid

from .archives import extract, inspect
from .journal import JobJournal, run_lock
from .library_registry import LibraryRegistry
from .profiles import private_json


def add_parser(commands):
    parser = commands.add_parser("archive", help="inspect archive volumes or stage a separately authorized extraction")
    actions = parser.add_subparsers(dest="archive_action", required=True)
    check = actions.add_parser("inspect", help="inspect one folder without cloud writes")
    check.add_argument("--folder-id", required=True)
    check.add_argument("--library-id")
    run = actions.add_parser("extract", help="extract into a new isolated library folder, preserving the original archive")
    run.add_argument("--fid", required=True)
    run.add_argument("--parent-id", required=True, help="the source archive's parent directory")
    run.add_argument("--library-id")
    run.add_argument("--apply", action="store_true")
    run.add_argument("--plan", action="store_true")
    run.add_argument("--reconcile", action="store_true", help="read only task status and actual files; never resubmit")
    fetch = actions.add_parser("fetch", help="save one selected single-file archive candidate from a task into a new staging folder")
    fetch.add_argument("task_id")
    fetch.add_argument("--candidate-id", required=True)
    fetch.add_argument("--library-id")
    fetch.add_argument("--apply", action="store_true")
    fetch.add_argument("--plan", action="store_true")
    fetch.add_argument("--reconcile", action="store_true", help="read only the recorded save task and staging folder")
    fetch.add_argument("--extract", action="store_true", help="after a verified fetch, continue with the isolated extraction")
    plan = actions.add_parser("plan", help="scan a verified extraction output and propose a library import plan")
    plan.add_argument("--fid", required=True, help="the extracted archive's fid")
    plan.add_argument("--library-id")
    plan.add_argument("--metadata-source", choices=("auto", "tmdb", "tvmaze"), default="auto")
    organize = actions.add_parser("organize", help="execute a saved extraction import plan only with --apply")
    organize.add_argument("plan_id")
    organize.add_argument("--library-id")
    organize.add_argument("--apply", action="store_true")
    organize.add_argument("--plan", action="store_true")


def _selected(args, config, root):
    if args.library_id:
        from .library_cli import _storage, _scope
        registry = LibraryRegistry(root / "runtime/libraries.json")
        library = registry.get_library(args.library_id)
        if (library["test_only"] and args.archive_action == "extract" and args.apply
                and not args.plan and not args.reconcile):
            raise ValueError("隔离测试影库只允许压缩包检查与预览")
        drive = registry.get_drive(library["drive_id"], config)
        storage = _storage(drive)
        _scope(storage, drive, library["library_root_id"])
        rows = storage.list_files(library["parent_id"])
        if (not isinstance(rows, list) or len([row for row in rows if isinstance(row, dict)
                and row.get("fid") == library["library_root_id"] and row.get("parent_id") == library["parent_id"]
                and row.get("name", row.get("file_name")) == library["name"]
                and row.get("is_dir", row.get("dir")) is True]) != 1):
            raise ValueError("影库真实目录身份已变化，需先对账")
        if library["test_only"] and args.archive_action in {"fetch", "organize"} and args.apply and not args.plan:
            raise ValueError("隔离测试影库只允许压缩包检查与预览")
        args._library = library
        return storage, library["library_root_id"]
    from .headless_cli import _storage
    storage = _storage(config)
    source = config.get("credential_source", {})
    if source.get("type") == "alist" and type(source.get("storage_id")) is int:
        storage.account_scope = "quark:alist-storage:" + str(source["storage_id"])
    return storage, config["library_root_id"]


def _job_key(prefix, storage, value):
    scope = getattr(storage, "account_scope", None)
    if not isinstance(scope, str) or not scope:
        raise ValueError("需要已绑定账号作用域")
    # Root/parent/name/size are immutable journal evidence, not ways to
    # mint another submission after a moved source or nested-library switch.
    bound = [hashlib.sha256(scope.encode()).hexdigest(), value]
    return prefix + ":" + hashlib.sha256(json.dumps(bound, ensure_ascii=False,
                                                    separators=(",", ":")).encode()).hexdigest()


def _extract(storage, root, *, fid, parent_id, library_root, apply, reconcile):
    job_key = _job_key("archive", storage, fid)
    journal = JobJournal(root / "runtime/archive-jobs.sqlite3")
    try:
        result = extract(storage, fid=fid, parent_id=parent_id, library_root_id=library_root,
                         journal=journal, job_key=job_key, apply=apply, reconcile=reconcile)
    finally:
        journal.close()
    result["job_key"] = job_key
    return result


def _fetch(args, storage, library_root, config, root):
    from .archive_flow import fetch, task_candidate
    from .tasks import TaskStore
    task = TaskStore(root / "runtime/tasks", library_root).load(args.task_id)
    candidate, source = task_candidate(task, args.candidate_id)
    apply = bool(args.apply and not args.plan and not args.reconcile)
    job_key = _job_key("archive-fetch", storage, args.candidate_id)
    journal = JobJournal(root / "runtime/archive-jobs.sqlite3")
    try:
        result = fetch(storage, candidate=candidate, source=source, library_root_id=library_root,
                       journal=journal, job_key=job_key, apply=apply, reconcile=args.reconcile)
    finally:
        journal.close()
    result.update(job_key=job_key, task_id=args.task_id)
    if result["status"] == "FETCHED":
        staging = result["staging"]["folder_id"]
        result["next_step"] = ("archive extract --fid " + result["archive_fid"] + " --parent-id " + staging)
        if args.extract:
            result["extraction"] = _extract(storage, root, fid=result["archive_fid"], parent_id=staging,
                                            library_root=library_root, apply=apply, reconcile=args.reconcile)
    return result


def _library(args, config):
    from .archive_flow import output_library
    return output_library(getattr(args, "_library", None) or config)


def _plan(args, storage, library_root, config, root):
    from .archive_flow import extracted_folder
    from .library_cli import _media_resolver
    from .library_import import plan_import, scan
    journal = JobJournal(root / "runtime/archive-jobs.sqlite3")
    try:
        folder, bound_root = extracted_folder(journal, _job_key("archive", storage, args.fid))
    finally:
        journal.close()
    if bound_root != library_root:
        raise ValueError("解压记录属于其他影库根目录")
    snapshot = scan(storage, source_id=folder, excluded_ids=[library_root])
    account = hashlib.sha256(storage.account_scope.encode()).hexdigest()
    library = {**_library(args, config), "account_scope_hash": account}
    result = plan_import(snapshot, library, _media_resolver(args, root))
    record_id = uuid.uuid4().hex
    private_json(root / "runtime/archive-plans" / (record_id + ".json"),
                 {"record_id": record_id, "library_root_id": library_root, "archive_fid": args.fid, "report": result})
    return {**result, "plan_id": record_id, "staging_folder_id": folder}


def _organize(args, storage, library_root, root):
    from .library_import import apply_import
    if not isinstance(args.plan_id, str) or not re.fullmatch(r"[0-9a-f]{32}", args.plan_id):
        raise ValueError("整理计划 ID 格式错误")
    path = root / "runtime/archive-plans" / (args.plan_id + ".json")
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError("没有找到私有的解压整理计划")
    record = json.loads(path.read_text(encoding="utf-8"))
    plan = record.get("report", {})
    account = hashlib.sha256(storage.account_scope.encode()).hexdigest()
    if (record.get("record_id") != args.plan_id or record.get("library_root_id") != library_root
            or plan.get("library", {}).get("library_root_id") != library_root
            or plan["library"].get("account_scope_hash") != account):
        raise ValueError("整理计划与当前影库或账号不一致")
    journal = JobJournal(root / "runtime/library-jobs.sqlite")
    try:
        result = apply_import(plan, storage, journal=journal, job_key="archive-import:" + args.plan_id,
                              apply=bool(args.apply and not args.plan))
    finally:
        journal.close()
    return {**result, "plan_id": args.plan_id}


def run_command(args, config, root):
    with run_lock(root / "runtime/workflow.lock"):
        storage, library_root = _selected(args, config, root)
        if args.archive_action == "inspect":
            result = inspect(storage, folder_id=args.folder_id, library_root_id=library_root)
        elif args.archive_action == "fetch":
            result = _fetch(args, storage, library_root, config, root)
        elif args.archive_action == "plan":
            result = _plan(args, storage, library_root, config, root)
        elif args.archive_action == "organize":
            result = _organize(args, storage, library_root, root)
        else:
            result = _extract(storage, root, fid=args.fid, parent_id=args.parent_id, library_root=library_root,
                              apply=bool(args.apply and not args.plan and not args.reconcile),
                              reconcile=args.reconcile)
        result.update(browser_opened=False, library_id=args.library_id)
        private_json(root / "runtime/reports/archive-latest.json", result)
        return result
