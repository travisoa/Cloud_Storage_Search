"""Exact metadata discovery and explicit links to an existing bound series."""
from .journal import run_lock
from .profiles import ProfileStore, private_json
from .tasks import TaskStore


def add_parser(commands):
    parser = commands.add_parser('match', help='exact TMDB movie/TV identity by year, ID/URL or public external IDs')
    parser.add_argument('--kind', choices=('tv', 'movie'), default='tv')
    parser.add_argument('--title', default='')
    parser.add_argument('--year', type=int)
    parser.add_argument('--tmdb-id', help='numeric TMDB ID or an exact HTTPS themoviedb.org tv/movie URL')
    parser.add_argument('--imdb-id')
    parser.add_argument('--tvdb-id', type=int)
    parser.add_argument('--task-id', help='use a previously confirmed task identity as the cross-check')
    parser.add_argument('--library-id')
    parser.add_argument('--bind', action='store_true', help='save a verified TMDB association for this task profile; primary ID stays unchanged')


def run_command(args, config, root):
    from .headless_cli import _metadata
    from .identity_match import resolve_exact
    with run_lock(root / 'runtime/workflow.lock'):
        if args.library_id:
            from .library_registry import LibraryRegistry
            config = LibraryRegistry(root / 'runtime/libraries.json').config_for(args.library_id, config)
        request, aliases, public = None, [], {}
        title, year = args.title, args.year
        if args.task_id:
            if args.kind != 'tv':
                raise ValueError('当前任务快照是电视剧，不能绑定为电影')
            request = TaskStore(root / 'runtime/tasks', config['library_root_id']).load(args.task_id)['resolved_request']
            if args.title:
                from .profiles import normalize
                known = [request['title'], request.get('canonical_title', ''), *request.get('aliases', [])]
                if normalize(args.title) not in {normalize(name) for name in known if name}:
                    raise ValueError('指定片名不属于此任务已确认的作品名称或别名')
            title, year = title or request['title'], request.get('year') if args.year is None else args.year
            if year != request.get('year'):
                raise ValueError('指定年份与任务作品身份不一致')
            aliases = [*request.get('aliases', [])]
            if request.get('canonical_title'):
                aliases.append(request['canonical_title'])
            primary, _ = _metadata(request['metadata_source'])
            from .catalog import _external_ids
            public = {key: value for key, value in _external_ids(primary.get_external_ids(request['series_id'])).items()
                      if value is not None}
        for option, field in ((args.imdb_id, 'imdb_id'), (args.tvdb_id, 'tvdb_id')):
            if option is not None:
                from .catalog import _external_ids
                canonical = _external_ids({field: option})[field]
                if canonical is None:
                    raise ValueError('指定公共作品 ID 格式无效')
                if public.get(field) is not None and public[field] != canonical:
                    raise ValueError('指定公共作品 ID 与原任务实时身份冲突')
                public[field] = canonical
        provider, _ = _metadata('tmdb')
        result = resolve_exact(provider, title=title, year=year, kind=args.kind,
                               tmdb_id=args.tmdb_id, external_ids=public or None,
                               aliases=list(dict.fromkeys(aliases)))
        result.update(browser_opened=False, cloud_writes_performed=False)
        if args.bind:
            if request is None:
                raise ValueError('保存 TMDB 关联需要指定已确认的任务')
            if result['status'] == 'MATCHED':
                store = ProfileStore(root / 'runtime/series-profiles.json')
                profile = store.match(request)
                if profile is None:
                    raise ValueError('需先为此任务保存已核验的作品身份，才能关联 TMDB')
                result = {**store.bind_tmdb(profile, primary=primary, tmdb=provider,
                                          tmdb_id=result['identity']['id']),
                          'browser_opened': False, 'cloud_writes_performed': False}
        private_json(root / 'runtime/reports/match-latest.json', result)
        return result
