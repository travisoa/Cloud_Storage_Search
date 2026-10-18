"""Explicit archive staging commands; credentials remain in provider memory."""
import hashlib
import json
import re
import uuid

from .archives import SOURCE_ACTIONS, cleanup_extraction, extract, inspect
from .journal import JobJournal, run_lock
from .library_registry import LibraryRegistry
from .profiles import private_json


CLEANUP_KEY = "archive_cleanup"


def _cleanup_flags(parser):
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--remove-archive", dest="remove_archive", action="store_true", default=None,
                       help="after a verified extraction, move the source archive to the recycle bin")
    group.add_argument("--keep-archive", dest="remove_archive", action="store_false",
                       help="keep the source archive for this run without asking, overriding " + CLEANUP_KEY)


def add_parser(commands):
    parser = commands.add_parser("archive", help="inspect archive volumes or stage a separately authorized extraction")
    actions = parser.add_subparsers(dest="archive_action", required=True)
    check = actions.add_parser("inspect", help="inspect one folder without cloud writes")
    check.add_argument("--folder-id", required=True)
    check.add_argument("--library-id")
    run = actions.add_parser("extract", help="extract into a new isolated library folder; the source archive follows " + CLEANUP_KEY)
    run.add_argument("--fid", required=True)
    run.add_argument("--parent-id", required=True, help="the source archive's parent directory")
    run.add_argument("--library-id")
    run.add_argument("--apply", action="store_true")
    run.add_argument("--plan", action="store_true")
    run.add_argument("--reconcile", action="store_true", help="read only task status and actual files; never resubmit")
    _cleanup_flags(run)
    fetch = actions.add_parser("fetch", help="save one selected single-file archive candidate from a task into a new staging folder")
    fetch.add_argument("task_id")
    fetch.add_argument("--candidate-id", required=True)
    fetch.add_argument("--library-id")
    fetch.add_argument("--apply", action="store_true")
    fetch.add_argument("--plan", action="store_true")
    fetch.add_argument("--reconcile", action="store_true", help="read only the recorded save task and staging folder")
    fetch.add_argument("--extract", action="store_true", help="after a verified fetch, continue with the isolated extraction")
    _cleanup_flags(fetch)
    cleanup = actions.add_parser("cleanup", help="recycle the source archive and emptied staging folders of a verified extraction")
    cleanup.add_argument("--fid", required=True, help="the extracted archive's fid")
    cleanup.add_argument("--library-id")
    cleanup.add_argument("--apply", action="store_true")
    cleanup.add_argument("--plan", action="store_true")
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
        if library["test_only"] and args.archive_action in {"fetch", "organize", "cleanup"} and args.apply and not args.plan:
            raise ValueError("隔离测试影库只允许压缩包检查与预览")
        args._library = library
        return storage, library["library_root_id"]
    from .headless_cli import _storage
    storage = _storage(config)
    return storage, config["library_root_id"]


def _job_key(prefix, storage, value, *, account_scope=None):
    scope = account_scope if account_scope is not None else getattr(storage, "account_scope", None)
    if not isinstance(scope, str) or not scope:
        raise ValueError("需要已绑定账号作用域")
    # Root/parent/name/size are immutable journal evidence, not ways to
    # mint another submission after a moved source or nested-library switch.
    bound = [hashlib.sha256(scope.encode()).hexdigest(), value]
    return prefix + ":" + hashlib.sha256(json.dumps(bound, ensure_ascii=False,
                                                    separators=(",", ":")).encode()).hexdigest()


def _legacy_record(prefix, storage, value, journal, job_key):
    # The archive CLI already used mount scopes for AList before the shared
    # connection resolver. Only the default file path had the old root scope.
    if getattr(storage, "credential_source", None) == "alist" or journal.get(job_key) is not None:
        return None
    for scope in getattr(storage, "legacy_account_scopes", ()):
        old_key = _job_key(prefix, storage, value, account_scope=scope)
        if old_key != job_key and journal.get(old_key) is not None:
            return {"status": "NEEDS_INPUT", "writes_attempted": False, "writes_performed": False,
                    "reasons": ["legacy_account_scope_requires_confirmation"], "legacy_job_key": old_key}
    return None


def _source_action(args, config):
    """The run flag wins; otherwise the project setting; absent means keep."""
    if args.remove_archive is not None:
        return ("recycle" if args.remove_archive else "keep"), "flag"
    if CLEANUP_KEY not in config:
        return "keep", "default"
    if config[CLEANUP_KEY] not in SOURCE_ACTIONS:
        raise ValueError("config/headless.json 的 " + CLEANUP_KEY + " 只能是 keep、recycle 或 ask")
    return config[CLEANUP_KEY], "config"


def _choices(args, fid):
    command = ["python3 scripts/media.py archive cleanup --fid", fid]
    if args.library_id:
        command += ["--library-id", args.library_id]
    return {"recycle": " ".join(command + ["--apply"]), "keep": "无需操作，原压缩包及暂存目录保留"}


def _cleanup_basis(result, basis, args, *, fid):
    cleanup = result.get("cleanup")
    if not isinstance(cleanup, dict):
        return
    cleanup["basis"] = basis
    source = cleanup.get("source", {}).get("state")
    if source == "awaiting_choice":
        cleanup["choices"] = _choices(args, fid)
    elif basis == "default" and result.get("status") == "EXTRACTED" and source == "kept":
        cleanup["hint"] = ("原压缩包已保留。config/headless.json 的 " + CLEANUP_KEY
                           + " 可设为 keep、recycle（解压核验后移入回收站，并回收清空的暂存目录）"
                           "或 ask（每次询问）；单次可用 --remove-archive 或 --keep-archive")


def _fetch_staging(storage, journal, library_root, fid, parent_id):
    """The staging folder that ``archive fetch`` created for exactly this archive, if any."""
    account = hashlib.sha256(storage.account_scope.encode()).hexdigest()
    for _key, record in journal.records("archive-fetch:"):
        target, bound = record.get("target") or {}, record.get("bound") or {}
        if (record.get("kind") == "archive_fetch" and record.get("status") == "FETCHED"
                and record.get("archive_fid") == fid and target.get("folder_id") == parent_id
                and target.get("parent_id") == library_root and isinstance(target.get("name"), str)
                and bound.get("account_scope_hash") == account and bound.get("library_root_id") == library_root):
            return {"folder_id": parent_id, "name": target["name"]}
    return None


def _extract(storage, root, *, fid, parent_id, library_root, apply, reconcile, source_action="keep"):
    job_key = _job_key("archive", storage, fid)
    journal = JobJournal(root / "runtime/archive-jobs.sqlite3")
    try:
        result = _legacy_record("archive", storage, fid, journal, job_key)
        if result is None:
            result = extract(storage, fid=fid, parent_id=parent_id, library_root_id=library_root,
                             journal=journal, job_key=job_key, apply=apply, reconcile=reconcile,
                             source_action=source_action,
                             staging=_fetch_staging(storage, journal, library_root, fid, parent_id))
    finally:
        journal.close()
    result["job_key"] = job_key
    return result


def _fetch(args, storage, library_root, config, root):
    from .archive_flow import fetch, task_candidate
    from .tasks import TaskStore
    # Validate the cleanup setting before the fetch can write to the cloud.
    action, basis = _source_action(args, config) if args.extract else (None, None)
    task = TaskStore(root / "runtime/tasks", library_root).load(args.task_id)
    apply = bool(args.apply and not args.plan and not args.reconcile)
    from .headless_cli import _task_scope_issue
    issue = _task_scope_issue(task, config, apply=apply, current_scope=storage.account_scope)
    if issue:
        return {**issue, "task_id": args.task_id, "source_task_id": args.task_id,
                "save_task_id": None, "browser_opened": False}
    candidate, source = task_candidate(task, args.candidate_id)
    job_key = _job_key("archive-fetch", storage, args.candidate_id)
    journal = JobJournal(root / "runtime/archive-jobs.sqlite3")
    try:
        result = _legacy_record("archive-fetch", storage, args.candidate_id, journal, job_key)
        if result is None:
            result = fetch(storage, candidate=candidate, source=source, library_root_id=library_root,
                           journal=journal, job_key=job_key, apply=apply, reconcile=args.reconcile,
                           archive_recycled=_fetched_archive_recycled(storage, journal, journal.get(job_key)))
    finally:
        journal.close()
    result.update(job_key=job_key, source_task_id=args.task_id,
                  save_task_id=result.get("save_task_id", result.get("task_id")), task_id=args.task_id)
    if result["status"] == "FETCHED":
        staging = result["staging"]["folder_id"]
        result["next_step"] = ("archive extract --fid " + result["archive_fid"] + " --parent-id " + staging)
        if args.extract:
            result["extraction"] = _extract(storage, root, fid=result["archive_fid"], parent_id=staging,
                                            library_root=library_root, apply=apply, reconcile=args.reconcile,
                                            source_action=action)
            _cleanup_basis(result["extraction"], basis, args, fid=result["archive_fid"])
    return result


def _fetched_archive_recycled(storage, journal, state):
    """A fetched archive whose own extraction job already started recycling its leftovers."""
    if not isinstance(state, dict) or state.get("status") != "FETCHED" or not state.get("archive_fid"):
        return False
    extraction = journal.get(_job_key("archive", storage, state["archive_fid"]))
    return (isinstance(extraction, dict) and extraction.get("kind") == "archive_extract"
            and any(isinstance(extraction.get(field), dict)
                    for field in ("source_cleanup", "staging_cleanup", "target_cleanup"))
            and (extraction.get("bound") or {}).get("source", {}).get("parent_id")
            == (state.get("target") or {}).get("folder_id"))


def _cleanup(args, storage, library_root, root, *, fid=None, apply=None):
    fid = fid or args.fid
    job_key = _job_key("archive", storage, fid)
    journal = JobJournal(root / "runtime/archive-jobs.sqlite3")
    try:
        result = _legacy_record("archive", storage, fid, journal, job_key)
        if result is None:
            parent = ((journal.get(job_key) or {}).get("bound") or {}).get("source", {}).get("parent_id")
            result = cleanup_extraction(storage, journal=journal, job_key=job_key, library_root_id=library_root,
                                        staging=_fetch_staging(storage, journal, library_root, fid, parent),
                                        apply=bool(args.apply and not args.plan) if apply is None else apply)
    finally:
        journal.close()
    result["job_key"] = job_key
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


def _organize(args, storage, library_root, config, root):
    from .library_import import apply_import
    if not isinstance(args.plan_id, str) or not re.fullmatch(r"[0-9a-f]{32}", args.plan_id):
        raise ValueError("整理计划 ID 格式错误")
    action = config.get(CLEANUP_KEY, "keep")
    if action not in SOURCE_ACTIONS:
        raise ValueError("config/headless.json 的 " + CLEANUP_KEY + " 只能是 keep、recycle 或 ask")
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
    result = {**result, "plan_id": args.plan_id}
    if result.get("status") == "COMPLETE" and action in {"recycle", "ask"} and isinstance(record.get("archive_fid"), str):
        # Organizing emptied the extraction folder; recycle the leftovers or offer to.
        cleanup = _cleanup(args, storage, library_root, root, fid=record["archive_fid"],
                           apply=action == "recycle" and bool(args.apply and not args.plan))
        result["cleanup"] = {key: cleanup[key] for key in ("status", "cleanup", "reasons") if key in cleanup}
        result["cleanup"].update(mode=action, basis="config")
        if action == "ask" and cleanup.get("status") == "PLAN" and any(
                item["state"] == "planned" for item in cleanup.get("cleanup", {}).values()):
            result["cleanup"]["choices"] = _choices(args, record["archive_fid"])
        result["writes_attempted"] = result.get("writes_attempted") or cleanup["writes_attempted"]
        result["writes_performed"] = result.get("writes_performed") or cleanup["writes_performed"]
    return result


def run_command(args, config, root):
    with run_lock(root / "runtime/workflow.lock"):
        storage, library_root = _selected(args, config, root)
        if args.archive_action == "inspect":
            result = inspect(storage, folder_id=args.folder_id, library_root_id=library_root)
        elif args.archive_action == "fetch":
            result = _fetch(args, storage, library_root, config, root)
        elif args.archive_action == "cleanup":
            result = _cleanup(args, storage, library_root, root)
        elif args.archive_action == "plan":
            result = _plan(args, storage, library_root, config, root)
        elif args.archive_action == "organize":
            result = _organize(args, storage, library_root, config, root)
        else:
            action, basis = _source_action(args, config)
            result = _extract(storage, root, fid=args.fid, parent_id=args.parent_id, library_root=library_root,
                              apply=bool(args.apply and not args.plan and not args.reconcile),
                              reconcile=args.reconcile, source_action=action)
            _cleanup_basis(result, basis, args, fid=args.fid)
        result.update(browser_opened=False, library_id=args.library_id)
        private_json(root / "runtime/reports/archive-latest.json", result)
        return result
