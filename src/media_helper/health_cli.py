"""Formal, read-only health command with private local reports."""
from pathlib import Path
import os
import tempfile

from .health import inspect, markdown
from .journal import run_lock
from .library_registry import LibraryRegistry
from .profiles import private_json


def add_parser(commands):
    parser = commands.add_parser('health', help='read-only library structure and optional exact metadata checks')
    parser.add_argument('--library-id', help='initialized library; default is the configured current library')
    parser.add_argument('--folder-id', help='inspect a folder proven to be within this library')
    parser.add_argument('--metadata', action='store_true', help='verify exact identities and compare expected schedules')
    parser.add_argument('--metadata-source', choices=('auto', 'tmdb', 'tvmaze'), default='auto')
    parser.add_argument('--max-entries', type=int, default=10000)
    parser.add_argument('--max-depth', type=int, default=12)
    parser.add_argument('--details', action='store_true', help='include all findings and schedule details in command output; local reports are always complete')


def _selected(args, config, root):
    from .headless_cli import _storage
    from .library_cli import _storage as drive_storage, _scope
    registry = LibraryRegistry(root / 'runtime/libraries.json')
    records = registry.read()
    if args.library_id:
        library = registry.get_library(args.library_id)
        drive = registry.get_drive(library['drive_id'], config)
        storage = drive_storage(drive)
        _scope(storage, drive, library['library_root_id'])
        actual = storage.list_files(library['parent_id'])
        if not isinstance(actual, list) or len([row for row in actual if isinstance(row, dict)
                and row.get('fid') == library['library_root_id'] and row.get('parent_id') == library['parent_id']
                and row.get('name', row.get('file_name')) == library['name']
                and row.get('is_dir', row.get('dir')) is True]) != 1:
            raise ValueError('初始化影库的目录身份发生变化，需先对账')
        scope, categories = library['library_root_id'], library['categories']
    else:
        storage, scope, categories = _storage(config), config['library_root_id'], config['categories']
        drive = registry.get_drive('current', config)
    excluded = [item['library_root_id'] for item in records['libraries'].values()
                if item['test_only'] and item['library_root_id'] != scope
                and registry.get_drive(item['drive_id'], config)['credential_source'] == drive['credential_source']]
    return storage, scope, categories, excluded


def _resolver(args, root):
    from .library_cli import _media_resolver
    return _media_resolver(args, root)


def _private_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('体检报告不能是符号链接')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run_command(args, config, root):
    from .library_import import _error
    with run_lock(root / 'runtime/workflow.lock'):
        try:
            storage, scope, categories, excluded = _selected(args, config, root)
            resolver = _resolver(args, root) if args.metadata else None
            report = inspect(storage, library_root_id=scope, categories=categories,
                             resolver=resolver, excluded_ids=excluded, folder_id=args.folder_id,
                             max_entries=args.max_entries, max_depth=args.max_depth)
        except Exception as exc:
            status, reason = _error(exc)
            report = {'schema_version': 1, 'status': status, 'reason': reason,
                      'read_only': True, 'writes_attempted': False, 'writes_performed': False,
                      'browser_opened': False, 'coverage': {'scan_complete': False, 'scope_verified': False},
                      'findings': [], 'summary': {'errors': 1, 'warnings': 0, 'finding_count': 0}}
        report['library_id'] = args.library_id or 'current'
        json_path = root / 'runtime/reports/health-latest.json'
        markdown_path = root / 'runtime/reports/health-latest.md'
        report['report_files'] = {'json': str(json_path), 'markdown': str(markdown_path)}
        text = markdown(report) if 'checked_at' in report else '# 影库体检\n\n检查未完成：' + report['status'] + '。云盘写入 0。\n'
        private_json(json_path, report)
        _private_text(markdown_path, text)
        if args.details:
            return report
        compact = {**report, 'findings': report['findings'][:20],
                   'findings_total': len(report['findings']),
                   'findings_truncated': len(report['findings']) > 20}
        if 'expected_schedule' in report:
            expected = report['expected_schedule']
            compact['expected_schedule'] = {key: expected[key] for key in ('basis', 'availability_verified', 'as_of')}
            compact['expected_schedule'].update(season_count=len(expected['seasons']),
                                                missing_count=len(expected['missing']),
                                                future_count=len(expected['future']),
                                                unknown_count=len(expected['unknown']))
        return compact
