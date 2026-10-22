"""Headless subscription commands; the existing account lock covers every run."""
from __future__ import annotations

from datetime import datetime, timezone
import posixpath
import re

from .journal import run_lock, safe_record
from .profiles import private_json
from .sources import extract_selected_sources, safe_source
from .subscriptions import SubscriptionStore
from .tasks import TaskStore
from .workflow import _digest


# (subscription field, enabling flag, disabling flag, help) for the boolean policies.
SWITCH_FLAGS = (
    ('archive_pick_largest', '--archive-pick-largest', '--no-archive-pick-largest',
     "several archives of one episode in one share: take the largest instead of waiting"),
    ('archive_tiebreak_labels', '--archive-tiebreak-labels', '--no-archive-tiebreak-labels',
     'within 3%% of the largest size, rank DV > HDR > SDR, then 高码 > 低码 from folder labels'),
    ('archive_video_fallback', '--archive-video-fallback', '--no-archive-video-fallback',
     "judge archives apart from videos: an uncertain or unusable archive keeps the episode's video"),
    ('upgrade_existing', '--upgrade-existing', '--no-upgrade-existing',
     'replace an existing episode by a higher-resolution or clearly larger version, recycling the old file'),
)


def add_parser(commands):
    watch = commands.add_parser('watch', help='manage approved TV subscriptions without a browser')
    actions = watch.add_subparsers(dest='watch_action', required=True)
    add = actions.add_parser('add', help='subscribe to the confirmed series in an existing task')
    add.add_argument('--task-id', required=True)
    add.add_argument('--interval-hours', type=float, default=None)
    authority = add.add_mutually_exclusive_group()
    authority.add_argument('--auto-save', action='store_true', default=None)
    authority.add_argument('--no-auto-save', action='store_false', dest='auto_save')
    add.add_argument('--source-url', help='import a canonical source proven by the selected task receipt')
    add.add_argument('--append-source', action='store_true',
                     help='add the --source-url source after the recorded ones instead of replacing them')
    add.add_argument('--numeric-source', action='store_true', help='reuse the prior confirmed bare numeric episode mapping in that source directory')
    add.add_argument('--secondary-source', choices=('tmdb', 'tvmaze'))
    add.add_argument('--secondary-id')
    trust = add.add_mutually_exclusive_group()
    trust.add_argument('--trust-share-title', action='store_true', default=None,
                       help="accept a new source's SxxEyy or season-folder file when the share title names the work")
    trust.add_argument('--no-trust-share-title', action='store_false', dest='trust_share_title')
    uncertain = add.add_mutually_exclusive_group()
    uncertain.add_argument('--wait-on-uncertain', action='store_true', default=None, dest='wait_on_uncertain',
                           help='an uncertain source waits for the next check instead of blocking the subscription')
    uncertain.add_argument('--block-on-uncertain', action='store_false', dest='wait_on_uncertain')
    add.add_argument('--archive-auto', choices=('largest', 'off'),
                     help='largest: fetch, extract and place the largest single-file archive of a missing episode')
    for field, on, off, text in SWITCH_FLAGS:
        group = add.add_mutually_exclusive_group()
        group.add_argument(on, action='store_true', default=None, dest=field, help=text)
        group.add_argument(off, action='store_false', dest=field)
    add.add_argument('--archive-cleanup', choices=('recycle', 'keep', 'ask'),
                     help='what archive_auto leaves after extraction (default recycle; never emptied or deleted)')
    add.add_argument('--prefer-hdr-format', choices=('dv', 'hdr', 'none'),
                     help='within 3%% of the largest size, prefer this HDR format')
    add.add_argument('--upgrade-min-ratio', type=float,
                     help='same-resolution upgrades need this many times the old size (default 1.5)')
    add.add_argument('--upgrade-min-ratio-higher-tier', type=float,
                     help='higher-resolution upgrades need this many times the old size (default 1.0)')
    actions.add_parser('list')
    status = actions.add_parser('status')
    status.add_argument('subscription_id')
    run = actions.add_parser('run')
    run.add_argument('--subscription-id')
    run.add_argument('--due', action='store_true', help='run only enabled subscriptions whose interval has elapsed')
    run.add_argument('--force', action='store_true', help='check now, still respecting pause, blocks and write authority')
    run.add_argument('--apply', action='store_true')
    run.add_argument('--plan', action='store_true', help='always read-only, including when --apply is present')
    for name in ('pause', 'resume'):
        action = actions.add_parser(name)
        action.add_argument('subscription_id')
        if name == 'resume':
            action.add_argument('--clear-block', action='store_true', help='clear only after this command completes a successful read-only reconciliation')


def _receipt_sources(task, source_url=None):
    receipt = task.get('transfer_receipt') or task.get('report') or {}
    sources = extract_selected_sources(receipt)
    if source_url:
        explicit = safe_source(source_url)
        selected = [row for row in receipt.get('plan', []) if row.get('source', {}).get('fid')
                    and row.get('candidate_id') == _digest((explicit['source_hash'], row['source']['fid']))]
        if not selected:
            raise ValueError('该分享链接与此任务已选来源的候选标识不符')
        imported = []
        for row in selected:
            observed, info = row['source'], row.get('media_info') or {}
            imported.append(safe_source(explicit, title=task['resolved_request']['title'],
                parent_id=observed.get('parent_id'), path=posixpath.dirname(observed.get('path', '')),
                quality_tags=[info[key] for key in ('resolution', 'codec', 'hdr') if info.get(key)]))
        sources = list({(s['source_hash'], s.get('parent_id'), s.get('path')): s for s in imported}.values())
    return receipt, sources


def _numeric_policy(receipt, sources, request):
    if len(sources) != 1 or not sources[0].get('parent_id'):
        raise ValueError('纯数字集号策略需要唯一的已确认分享目录')
    source = sources[0]
    quality = (request.get('quality_preference') or [None])[0]
    if not quality or quality not in source.get('quality_tags', []):
        raise ValueError('纯数字来源缺少与作品偏好一致的画质证据')
    mapping = {row.get('candidate_id'): row for row in receipt.get('source_episode_mapping_applied', [])}
    matched = []
    for row in receipt.get('plan', []):
        file = row.get('source', {})
        identifier = row.get('candidate_id')
        if (file.get('parent_id') != source['parent_id'] or not file.get('fid')
                or identifier != _digest((source['source_hash'], file['fid']))):
            continue
        proof = mapping.get(identifier, {})
        stem = posixpath.splitext(file.get('name', ''))[0]
        if (not re.fullmatch(r'\d+', stem) or not 1 <= int(stem) <= 999
                or proof.get('season') != request['season'] or proof.get('episode') != int(stem)
                or proof.get('name') != file.get('name') or proof.get('size') != file.get('size')
                or proof.get('fid') != file['fid']):
            raise ValueError('该来源没有与真实文件对应的既有纯数字季集确认')
        matched.append(identifier)
    if not matched:
        raise ValueError('此任务不包含可沿用的纯数字季集确认')
    return {'source_hash': source['source_hash'], 'parent_id': source['parent_id'],
            'season': request['season'], 'quality': quality}


def _add(args, config, root, store):
    from .headless_cli import _prepare_identity
    task = TaskStore(root / 'runtime/tasks', config['library_root_id']).load(args.task_id)
    request, issue, _, _ = _prepare_identity(task['resolved_request'], config)
    if issue:
        return issue
    if args.append_source and not args.source_url:
        raise ValueError('--append-source 需要同时指定 --source-url')
    receipt, sources = _receipt_sources(task, args.source_url)
    options = {}
    if sources:
        options['preferred_sources'] = sources
        options['append_sources'] = bool(args.append_source)
    if args.trust_share_title is not None:
        options['trust_share_title'] = args.trust_share_title
    if args.wait_on_uncertain is not None:
        options['wait_on_uncertain_source'] = args.wait_on_uncertain
    if args.archive_auto is not None:
        options['archive_auto'] = None if args.archive_auto == 'off' else args.archive_auto
    for field, *_ in SWITCH_FLAGS:
        if getattr(args, field) is not None:
            options[field] = getattr(args, field)
    if args.archive_cleanup is not None:
        options['archive_cleanup'] = args.archive_cleanup
    if args.prefer_hdr_format is not None:
        options['prefer_hdr_format'] = None if args.prefer_hdr_format == 'none' else args.prefer_hdr_format
    if args.upgrade_min_ratio is not None:
        options['upgrade_min_ratio'] = args.upgrade_min_ratio
    if args.upgrade_min_ratio_higher_tier is not None:
        options['upgrade_min_ratio_higher_tier'] = args.upgrade_min_ratio_higher_tier
    if args.interval_hours is not None:
        options['interval_hours'] = args.interval_hours
    if args.auto_save is not None:
        options['auto_save'] = args.auto_save
    if args.numeric_source:
        options['numeric_policy'] = _numeric_policy(receipt, sources, request)
    if bool(args.secondary_source) != bool(args.secondary_id):
        raise ValueError('补充元数据需要同时指定来源和作品 ID')
    if args.secondary_source:
        options['secondary'] = {'source': args.secondary_source, 'series_id': args.secondary_id}
    record = store.add(request, **options)
    return {'status': 'SUBSCRIBED', 'subscription': record, 'browser_opened': False,
            'writes_attempted': False, 'writes_performed': False}


def _run(args, config, root, store):
    from .watching import run_subscription
    current = datetime.now(timezone.utc)
    records = store.list()
    if args.subscription_id:
        selected = store.get(args.subscription_id)
        records = [selected]
    if args.due and not args.force:
        due = {row['subscription_id'] for row in store.due(current)}
        records = [row for row in records if row['subscription_id'] in due]
    records = [row for row in records if row['enabled']]
    outcomes = []
    for record in records:
        report = run_subscription(record, config, root, apply=bool(args.apply and not args.plan), as_of=current)
        if report.get('preferred_sources'):
            store.update_sources(record['subscription_id'], report['preferred_sources'])
        outcome = store.record_run(record['subscription_id'], report, checked_at=current)
        outcomes.append({'subscription_id': record['subscription_id'], 'title': record['request']['title'],
                         'task_id': report.get('task_id'), 'blocked': outcome['record']['blocked'],
                         'next_check_at': outcome['record']['next_check_at'],
                         **{key: outcome[key] for key in ('summary', 'meaningful_change', 'new_files')}})
        if report.get('status') in {'NEEDS_AUTH', 'RISK_CONTROL'}:
            break
    needs_attention = any(row['blocked'] for row in outcomes)
    result = {'status': 'NEEDS_INPUT' if needs_attention else 'COMPLETE',
              'checked_count': len(outcomes), 'meaningful_change': any(row['meaningful_change'] for row in outcomes),
              'new_files': [file for row in outcomes for file in row['new_files']], 'subscriptions': outcomes,
              'browser_opened': False, 'checked_at': current.isoformat()}
    private_json(root / 'runtime/reports/watch-latest.json', safe_record(result))
    return result


def run_command(args, config, root):
    store = SubscriptionStore(root / 'runtime/subscriptions.json', config['library_root_id'])
    if args.watch_action == 'list':
        return {'status': 'SUBSCRIPTIONS', 'subscriptions': store.list(), 'browser_opened': False}
    if args.watch_action == 'status':
        return {'status': 'SUBSCRIPTION_STATUS', 'subscription': store.get(args.subscription_id), 'browser_opened': False}
    try:
        with run_lock(root / 'runtime/workflow.lock'):
            if args.watch_action == 'add':
                return _add(args, config, root, store)
            if args.watch_action == 'run':
                return _run(args, config, root, store)
            if args.watch_action == 'pause':
                return {'status': 'PAUSED', 'subscription': store.set_enabled(args.subscription_id, False)}
            if args.clear_block:
                from .watching import run_subscription
                record = store.get(args.subscription_id)
                report = run_subscription(record, config, root, apply=False, reconcile_pending=True)
                if report.get('subscription_id') != record['subscription_id']:
                    raise ValueError('只读对账的订阅身份不一致')
                store.clear_block(record['subscription_id'], report)
            return {'status': 'ENABLED', 'subscription': store.set_enabled(args.subscription_id, True), 'browser_opened': False}
    except RuntimeError as exc:
        if str(exc) == '另一个影库任务正在运行；本次没有开始云盘操作':
            return {'status': 'BUSY', 'meaningful_change': False, 'writes_attempted': False,
                    'writes_performed': False, 'browser_opened': False}
        raise
