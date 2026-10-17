"""Conversation-facing per-series release policy settings."""
from .journal import run_lock
from .profiles import ProfileStore
from .tasks import TaskStore
from .version_policy import VersionPolicyStore


def add_parser(commands):
    parser = commands.add_parser('policy', help='set release rules for one verified series; existing files stay in place')
    actions = parser.add_subparsers(dest='policy_action', required=True)
    actions.add_parser('list')
    for action in ('show', 'set', 'reset'):
        p = actions.add_parser(action)
        source = p.add_mutually_exclusive_group(required=True)
        source.add_argument('--task-id')
        source.add_argument('--profile-key')
        p.add_argument('--library-id')
        if action == 'set':
            p.add_argument('--resolution', action='append', choices=('2160p', '1080p', '720p', '480p'))
            fallback = p.add_mutually_exclusive_group()
            fallback.add_argument('--allow-fallback', dest='allow_fallback', action='store_true', default=None)
            fallback.add_argument('--no-fallback', dest='allow_fallback', action='store_false')
            p.add_argument('--codec', action='append', choices=('HEVC', 'H.264', 'AV1'))
            codec = p.add_mutually_exclusive_group()
            codec.add_argument('--require-codec', dest='require_codec', action='store_true', default=None)
            codec.add_argument('--prefer-codec', dest='require_codec', action='store_false')
            p.add_argument('--hdr', choices=('any', 'prefer', 'exclude', 'require', 'dolby-vision'))
            p.add_argument('--keyword', action='append')
            p.add_argument('--min-size-mb', type=float)
            p.add_argument('--max-size-gb', type=float)
            p.add_argument('--clear-max-size', action='store_true')
            p.add_argument('--archives', choices=('skip', 'manual'))
            p.add_argument('--prefer-size', choices=('any', 'smaller', 'larger'),
                           help='break ties between equal versions by file size')


def _request(args, config, root):
    if args.library_id:
        from .library_registry import LibraryRegistry
        config = LibraryRegistry(root / 'runtime/libraries.json').config_for(args.library_id, config)
    if args.task_id:
        return TaskStore(root / 'runtime/tasks', config['library_root_id']).load(args.task_id)['resolved_request']
    item = ProfileStore(root / 'runtime/series-profiles.json').all().get(args.profile_key)
    if not item:
        raise ValueError('未找到已确认的作品身份')
    return {**item, 'library_root_id': config['library_root_id']}


def run_command(args, config, root):
    store = VersionPolicyStore(root / 'runtime/version-policies.json')
    if args.policy_action == 'list':
        return {'status': 'VERSION_POLICIES', 'series': store.all(), 'cloud_writes_performed': False, 'browser_opened': False}
    with run_lock(root / 'runtime/workflow.lock'):
        request = _request(args, config, root)
        previous = store.get(request)
        if args.policy_action == 'show':
            return {'status': 'VERSION_POLICY', 'record': previous, 'legacy_quality_preference': request.get('quality_preference'),
                    'cloud_writes_performed': False, 'browser_opened': False}
        if args.policy_action == 'reset':
            store.reset(request)
            return {'status': 'POLICY_RESET', 'cloud_writes_performed': False, 'browser_opened': False}
        policy = dict(previous['policy']) if previous else {'resolution_order': request.get('quality_preference') or ['2160p', '1080p', '720p']}
        fields = {'resolution': 'resolution_order', 'allow_fallback': 'allow_fallback', 'codec': 'preferred_codecs',
                  'require_codec': 'require_codec', 'hdr': 'hdr', 'keyword': 'preferred_keywords',
                  'min_size_mb': 'min_size_mb', 'max_size_gb': 'max_size_gb', 'archives': 'archives',
                  'prefer_size': 'prefer_size'}
        for option, field in fields.items():
            value = getattr(args, option)
            if value is not None:
                policy[field] = value
        if args.clear_max_size:
            if args.max_size_gb is not None:
                raise ValueError('不能同时指定文件大小上限和清除上限')
            policy['max_size_gb'] = None
        record = store.set(request, policy)
        return {'status': 'POLICY_SAVED', 'record': record, 'existing_files_action': 'keep',
                'cloud_writes_performed': False, 'browser_opened': False}
