"""Connect a drive, initialize a library, then optionally scan/import media."""
from collections import Counter, deque
from copy import deepcopy
import hashlib
import json
import re
import uuid

from .journal import JobJournal, run_lock
from .library_registry import LibraryRegistry, drive_reference, identifier, local_id
from .profiles import private_json


def add_parser(commands):
    parser = commands.add_parser('library', help='connect drives and build a cloud library before optionally importing files')
    actions = parser.add_subparsers(dest='library_action', required=True)
    actions.add_parser('drives', help='list credential references, never secret values')
    connect = actions.add_parser('connect', help='register a drive: an existing AList Quark mount, or a private Cookie file')
    connect.add_argument('--drive-id', required=True)
    source = connect.add_mutually_exclusive_group(required=True)
    source.add_argument('--alist-storage-id', type=int)
    source.add_argument('--cookie', action='store_true',
                        help='read the Cookie from a hidden prompt or one stdin line and keep it in the private credential directory')
    credential = actions.add_parser('credential', help='replace the private Cookie of a file-based drive (same account only)')
    credential.add_argument('--drive-id', required=True)
    init = actions.add_parser('init', help='create only a library root and category folders')
    init.add_argument('--drive-id', default='current')
    init.add_argument('--library-id', required=True)
    init.add_argument('--parent-id', required=True)
    init.add_argument('--name', default='影库')
    init.add_argument('--test', action='store_true', help='isolate a test below the existing library; never activate it for transfers')
    init.add_argument('--apply', action='store_true')
    init.add_argument('--plan', action='store_true')
    init.add_argument('--reconcile', action='store_true', help='read-only reconciliation of the same initialization')
    init.add_argument('--version-preset', choices=('quality', 'balanced', 'storage'),
                      help='library-wide version choice: quality, balanced or storage')
    preset = actions.add_parser('preset', help='show or change the library-wide version preset; no cloud access')
    preset.add_argument('library_id', nargs='?', help='omit for the default library')
    choice = preset.add_mutually_exclusive_group()
    choice.add_argument('--set', dest='preset', choices=('quality', 'balanced', 'storage'))
    choice.add_argument('--clear', action='store_true')
    status = actions.add_parser('status')
    status.add_argument('library_id', nargs='?')
    scan = actions.add_parser('scan', help='optionally inventory existing files; no cloud writes')
    scan.add_argument('library_id')
    scan.add_argument('--source-id', required=True)
    scan.add_argument('--max-entries', type=int, default=10000)
    scan.add_argument('--max-depth', type=int, default=12)
    plan = actions.add_parser('plan', help='resolve a saved inventory and preview its proposed organization')
    plan.add_argument('library_id')
    plan.add_argument('scan_id')
    plan.add_argument('--metadata-source', choices=('auto', 'tmdb', 'tvmaze'), default='auto')
    organize = actions.add_parser('organize', help='execute only the saved, reviewed import plan when --apply is present')
    organize.add_argument('library_id')
    organize.add_argument('plan_id')
    organize.add_argument('--apply', action='store_true')
    organize.add_argument('--plan', action='store_true')
    request = actions.add_parser('request', help='use an initialized production library for a conversational media request')
    request.add_argument('library_id')
    request.add_argument('text')
    request.add_argument('--plan', action='store_true')
    request.add_argument('--kind', choices=('tv', 'movie'))


def _registry(root):
    return LibraryRegistry(root / 'runtime/libraries.json')


def _storage(drive, cookie=None):
    from .providers import QuarkProvider
    source = drive['credential_source']
    if cookie is None and source['type'] == 'file':
        from .credentials import read_secret
        cookie = read_secret(source['name']).get('cookie')
        if not isinstance(cookie, str) or not cookie:
            raise ValueError('私有凭据文件中没有 Cookie')
    elif cookie is None:
        from .alist_credentials import AListCredentials
        with AListCredentials() as reader:
            cookie = reader.load_cookie(source['storage_id'])
    storage = QuarkProvider(cookie=cookie)
    storage.account_scope = drive['account_scope']
    return storage


def _file_credential(args, registry, config):
    """Verify a privately entered Cookie with read-only listings before saving it."""
    from .credentials import read_input, write_secret
    from .library_registry import file_drive_reference
    drive = file_drive_reference(args.drive_id)
    existing = registry.read()['drives'].get(args.drive_id)
    if args.library_action == 'connect' and existing and existing != drive:
        raise ValueError('此网盘名称已绑定其他凭据来源，不能覆盖')
    if args.library_action == 'credential' and existing != drive:
        raise ValueError('只有通过 --cookie 连接的网盘才能更新私有 Cookie')
    cookie = read_input('夸克 Cookie（隐藏输入）：')
    storage = _storage(drive, cookie=cookie)
    root_entries = storage.list_files('0')
    # A replacement must reach the same account: each library on this drive
    # must still appear as the same folder under its recorded parent.
    for library in registry.read()['libraries'].values():
        if library['drive_id'] != args.drive_id:
            continue
        rows = storage.list_files(library['parent_id'])
        if not any(row.get('fid') == library['library_root_id'] and row.get('is_dir', row.get('dir')) is True
                   for row in rows):
            raise ValueError('新 Cookie 看不到已登记的影库目录，可能属于其他账号，未保存')
    write_secret(drive['credential_source']['name'], {'cookie': cookie})
    result = {'status': 'CONNECTED' if args.library_action == 'connect' else 'CREDENTIAL_UPDATED',
              'drive': registry.connect(drive) if args.library_action == 'connect' else drive,
              'root_entries': len(root_entries), 'credential_displayed': False,
              'browser_opened': False, 'writes_attempted': False, 'writes_performed': False}
    return result


def _reachable(storage, root_id, selected_id):
    """Prove current-drive operations stay within its already authorized root."""
    root_id, selected_id = identifier(root_id), identifier(selected_id)
    if selected_id == root_id:
        return
    queue, seen = deque([root_id]), {root_id}
    while queue:
        parent = queue.popleft()
        rows = storage.list_files(parent)
        if not isinstance(rows, list):
            raise ValueError('目录读取不完整，无法确认范围')
        local = set()
        for row in rows:
            if (not isinstance(row, dict) or row.get('parent_id') != parent
                    or type(row.get('is_dir', row.get('dir'))) is not bool):
                raise ValueError('目录读取不完整，无法确认范围')
            fid = identifier(row.get('fid'))
            if fid in local:
                raise ValueError('目录返回重复 fid')
            local.add(fid)
            if row.get('is_dir', row.get('dir')):
                if fid == selected_id:
                    return
                if fid in seen or len(seen) >= 10000:
                    raise ValueError('目录范围核验存在循环或超出上限')
                seen.add(fid)
                queue.append(fid)
    raise ValueError('所选目录不在当前已授权影库范围内')


def _scope(storage, drive, selected_id):
    identifier(selected_id, '目录 ID')
    if drive.get('boundary_root_id'):
        _reachable(storage, drive['boundary_root_id'], selected_id)


def _load_record(root, kind, record_id, library_id):
    if not isinstance(record_id, str) or not re.fullmatch(r'[0-9a-f]{32}', record_id):
        raise ValueError('扫描或整理计划 ID 格式错误')
    path = root / 'runtime' / ('library-' + kind) / (record_id + '.json')
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError('没有找到私有的扫描或整理计划记录')
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('record_id') != record_id or data.get('library_id') != library_id:
        raise ValueError('记录与所选影库不一致')
    return data['report']


def _save_record(root, kind, library_id, report):
    record_id = uuid.uuid4().hex
    private_json(root / 'runtime' / ('library-' + kind) / (record_id + '.json'),
                 {'record_id': record_id, 'library_id': library_id, 'report': report})
    return record_id


def _init(args, registry, config, root):
    from .library_init import initialize
    library_id = local_id(args.library_id)
    drive = registry.get_drive(args.drive_id, config)
    if drive.get('boundary_root_id') and not args.test:
        raise ValueError('现有网盘仅允许在当前影库内通过 --test 验证新建结构；新网盘请使用独立连接')
    storage = _storage(drive)
    _scope(storage, drive, args.parent_id)
    previous = registry.read()['libraries'].get(library_id)
    job_key = 'library-init:' + hashlib.sha256(json.dumps(
        [drive['account_scope'], library_id], ensure_ascii=False).encode()).hexdigest()
    if previous and (previous['drive_id'] != args.drive_id or previous['parent_id'] != args.parent_id
                     or previous['name'] != args.name or previous['test_only'] != args.test):
        raise ValueError('影库名称已绑定其他目录或模式，不能更换范围')
    journal = JobJournal(root / 'runtime/library-jobs.sqlite')
    try:
        result = initialize(storage, parent_id=args.parent_id, root_name=args.name,
                            journal=journal, job_key=job_key,
                            apply=bool(args.apply and not args.plan), reconcile=args.reconcile)
    finally:
        journal.close()
    result.update(library_id=library_id, drive_id=args.drive_id, init_job_key=job_key,
                  browser_opened=False, optional_next_step='scan_existing_files_or_finish')
    from .version_policy import preset_choices
    chosen = args.version_preset or (previous or {}).get('version_preset')
    if previous and args.version_preset and previous.get('version_preset') not in {None, args.version_preset}:
        raise ValueError('该影库已有版本预设；请用 library preset 修改')
    result['version_preset'] = chosen
    if not chosen:
        result['version_preset_choices'] = preset_choices()
    if result['status'] == 'COMPLETE':
        library = {'library_id': library_id, 'drive_id': args.drive_id, 'parent_id': args.parent_id,
                   'library_root_id': result['root_id'], 'name': args.name, 'categories': result['categories'],
                   'init_job_key': job_key, 'test_only': args.test,
                   **({'version_preset': chosen} if chosen else {})}
        result['library'] = registry.register(library)
    private_json(root / 'runtime/reports/library-init-latest.json', result)
    return result


def _media_resolver(args, root):
    from .headless_cli import _metadata
    from .library_identity import MediaResolver
    from .profiles import ProfileStore
    from .catalog import TVmazeProvider
    metadata, _ = _metadata(args.metadata_source)
    bindings = list(ProfileStore(root / 'runtime/series-profiles.json').all().values())
    return MediaResolver(metadata, bindings=bindings, providers={'tvmaze': TVmazeProvider()})


def _with_library(args, registry, config, root):
    from .library_import import scan, plan_import, apply_import
    library = registry.get_library(args.library_id)
    drive = registry.get_drive(library['drive_id'], config)
    storage = _storage(drive)
    _scope(storage, drive, library['library_root_id'])
    # Verify the initialized root remains the exact child of its selected parent.
    actual = storage.list_files(library['parent_id'])
    observed = [row for row in actual if row.get('fid') == library['library_root_id']
                and row.get('name', row.get('file_name')) == library['name']
                and row.get('is_dir', row.get('dir')) is True]
    if len(observed) != 1:
        raise ValueError('初始化的影库目录身份发生变化，需先对账')
    account = hashlib.sha256(drive['account_scope'].encode()).hexdigest()
    scoped_library = {**library, 'account_scope_hash': account}
    if args.library_action == 'scan':
        _scope(storage, drive, args.source_id)
        result = scan(storage, source_id=args.source_id, excluded_ids=[library['library_root_id']],
                      max_entries=args.max_entries, max_depth=args.max_depth)
        record_id = _save_record(root, 'scans', args.library_id, result)
        return {**result, 'scan_id': record_id, 'library_id': args.library_id,
                'counts': dict(Counter(row['kind'] for row in result['entries'])), 'browser_opened': False}
    if args.library_action == 'plan':
        snapshot = _load_record(root, 'scans', args.scan_id, args.library_id)
        if snapshot.get('account_scope_hash') != account:
            raise ValueError('扫描来自其他网盘账号，不能复用')
        _scope(storage, drive, snapshot['source_id'])
        result = plan_import(snapshot, scoped_library, _media_resolver(args, root))
        plan_id = _save_record(root, 'plans', args.library_id, result)
        return {**result, 'plan_id': plan_id, 'scan_id': args.scan_id,
                'library_id': args.library_id, 'browser_opened': False}
    plan = _load_record(root, 'plans', args.plan_id, args.library_id)
    if (plan.get('library', {}).get('library_root_id') != library['library_root_id']
            or plan['library'].get('account_scope_hash') != account):
        raise ValueError('整理计划与当前影库或账号不一致')
    _scope(storage, drive, plan['scan']['source_id'])
    if library['test_only'] and args.apply and not args.plan:
        raise ValueError('隔离测试影库只允许整理预览，不能迁入旧资源')
    journal = JobJournal(root / 'runtime/library-jobs.sqlite')
    try:
        result = apply_import(plan, storage, journal=journal, job_key='library-import:' + args.plan_id,
                              apply=bool(args.apply and not args.plan))
    finally:
        journal.close()
    result.update(plan_id=args.plan_id, library_id=args.library_id, browser_opened=False)
    private_json(root / 'runtime/reports/library-import-latest.json', result)
    return result


def _preset(args, registry, config, root):
    from .version_policy import PRESETS, preset_choices
    changing = bool(args.preset or args.clear)
    with run_lock(root / 'runtime/workflow.lock'):
        if args.library_id:
            library = registry.get_library(args.library_id)
            if changing:
                library = registry.set_preset(args.library_id, args.preset)
            current = library.get('version_preset')
        else:
            path = root / 'config/headless.json'
            current = config.get('version_preset')
            if changing:
                data = json.loads(path.read_text(encoding='utf-8'))
                if args.preset:
                    data['version_preset'] = args.preset
                else:
                    data.pop('version_preset', None)
                private_json(path, data)
                current = args.preset
    return {'status': 'PRESET_SAVED' if changing else 'PRESET', 'library_id': args.library_id or 'default',
            'version_preset': current, 'label': PRESETS[current]['label'] if current else None,
            'choices': preset_choices(), 'precedence': '单剧版本策略 > 本次确认的画质或候选 > 影库预设',
            'existing_files_action': 'keep', 'cloud_writes_performed': False, 'browser_opened': False}


def run_command(args, config, root):
    registry = _registry(root)
    if args.library_action == 'request':
        from .headless_cli import _run, parse_request
        selected = registry.config_for(args.library_id, config)
        request = parse_request(args.text)
        apply = request.pop('save_requested') and not args.plan
        if args.kind == 'tv':
            request.pop('kind', None)
        elif args.kind == 'movie':
            request['kind'] = 'movie'
            request.pop('season', None)
        request['library_id'] = args.library_id
        return {**_run(request, apply=apply, config=selected), 'library_id': args.library_id}
    if args.library_action == 'drives':
        data = registry.read()
        current = registry.get_drive('current', config)
        return {'status': 'DRIVES', 'drives': [current, *data['drives'].values()], 'browser_opened': False}
    if args.library_action == 'preset':
        return _preset(args, registry, config, root)
    if args.library_action == 'status':
        value = registry.get_library(args.library_id) if args.library_id else registry.read()['libraries']
        return {'status': 'LIBRARIES', 'libraries': value, 'browser_opened': False}
    with run_lock(root / 'runtime/workflow.lock'):
        if args.library_action == 'credential' or args.library_action == 'connect' and args.cookie:
            return _file_credential(args, registry, config)
        if args.library_action == 'connect':
            from .alist_credentials import AListCredentials
            drive = drive_reference(args.drive_id, args.alist_storage_id)
            source = config.get('credential_source', {})
            if source.get('type') == 'alist' and source.get('storage_id') == args.alist_storage_id:
                drive = drive_reference(args.drive_id, args.alist_storage_id, boundary_root_id=config['library_root_id'])
            with AListCredentials() as reader:
                matches = [row for row in reader.list_safe() if row['storage_id'] == args.alist_storage_id
                           and row['driver'].strip().casefold() == 'quark'
                           and row['cookie_present'] and not row['disabled']]
            if len(matches) != 1:
                raise ValueError('需要唯一、已启用且有 Cookie 的 AList 夸克挂载')
            return {'status': 'CONNECTED', 'drive': registry.connect(drive), 'credential_copied': False,
                    'browser_opened': False, 'writes_attempted': False, 'writes_performed': False}
        if args.library_action == 'init':
            return _init(args, registry, config, root)
        return _with_library(args, registry, config, root)
