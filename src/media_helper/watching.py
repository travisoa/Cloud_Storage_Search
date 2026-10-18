"""Subscription cycles: expected schedule, actual shares, then missing inventory."""
from copy import deepcopy
from datetime import datetime, timezone
import posixpath
import re

from .journal import JobJournal
from .search import PanSouSearch, PreferredSourceSearch
from .sources import extract_selected_sources
from .tasks import TaskStore
from .updates import UpdatesMetadata, collect_updates
from . import workflow


def _needed(report):
    return {row['episode_key'] for row in report.get('missing', []) if row.get('episode_key')}


def select_ready(request, report, subscription):
    """Numerical episodes are authorized only inside one confirmed source dir."""
    needed = _needed(report)
    version_policy = None
    if request.get('version_policy') is not None:
        from .version_policy import normalize
        version_policy = normalize(request['version_policy'])
    policy = subscription.get('numeric_policy') or {}
    quality = (request.get('quality_preference') or [None])[0]
    preferred = subscription.get('preferred_sources', [])
    def preferred_rank(row):
        return next((index for index, source in enumerate(preferred)
                     if source['source_hash'] == row.get('share_source', {}).get('source_hash')
                     and (not source.get('parent_id') or source['parent_id'] == row.get('parent_id'))), len(preferred))
    grouped, unknown = {}, set()
    for candidate in report.get('source_candidates', []):
        if type(candidate.get('size')) is not int or candidate['size'] <= 0:
            continue
        if version_policy is not None:
            from .version_policy import assess
            decision = assess(version_policy, candidate.get('media_info', {}), size=candidate['size'],
                              name=candidate.get('name', ''), path=candidate.get('path', ''))
            if not decision['allowed']:
                continue
            candidate = {**candidate, 'score': decision['score']}
        elif quality and candidate.get('media_info', {}).get('resolution') != quality:
            continue
        key = candidate.get('episode_key')
        if key in needed and preferred_rank(candidate) == len(preferred):
            context = workflow._normal(candidate.get('path') or candidate.get('name', ''))
            names = [request['title'], request.get('canonical_title', ''), *request.get('aliases', [])]
            if not any(workflow._normal(name) and workflow._normal(name) in context for name in names):
                # A matching search-card title alone cannot identify a video
                # inside a collection belonging to several different shows.
                unknown.add(key)
                continue
        mapping = None
        if not key and type(candidate.get('suggested_episode')) is int:
            number = candidate['suggested_episode']
            stem = posixpath.splitext(candidate.get('name', ''))[0]
            if not (1 <= number <= 999 and re.fullmatch(r'\d+', stem) and int(stem) == number):
                continue
            key = f'S{int(request["season"]):02d}E{number:02d}'
            source_hash = candidate.get('share_source', {}).get('source_hash')
            if (source_hash != policy.get('source_hash')
                    or candidate.get('parent_id') != policy.get('parent_id')
                    or policy.get('season') != int(request['season'])
                    or candidate.get('media_info', {}).get('resolution') != policy.get('quality')):
                if key in needed:
                    unknown.add(key)
                continue
            mapping = {'season': int(request['season']), 'episode': number,
                       'name': candidate['name'], 'size': candidate['size']}
        if key in needed:
            grouped.setdefault(key, {})[candidate['candidate_id']] = (candidate, mapping)
    chosen, mappings, ambiguous = {}, {}, []
    for key, values in grouped.items():
        if version_policy is not None:
            order = version_policy['resolution_order']
            best_resolution = min(order.index(item[0]['media_info']['resolution']) for item in values.values())
            values = {identifier: item for identifier, item in values.items()
                      if order.index(item[0]['media_info']['resolution']) == best_resolution}
        def priority(item):
            return preferred_rank(item[0])
        rank = min(priority(item) for item in values.values())
        eligible = [item for item in values.values() if priority(item) == rank]
        best = max(item[0].get('score', 0) for item in eligible)
        top = [item for item in eligible if item[0].get('score', 0) == best]
        if len(top) != 1:
            ambiguous.append(key)
            continue
        candidate, mapping = top[0]
        chosen[key] = candidate['candidate_id']
        if mapping:
            mappings[candidate['candidate_id']] = mapping
    return chosen, mappings, sorted(set(ambiguous) | (unknown - set(chosen)))


class _NoSearch:
    def search(self, _):
        return []


def _new_files(report):
    return [{**row, 'fid': row['saved_fid']} for operation in report.get('operations', [])
            if operation.get('state') == 'complete'
            for row in operation.get('verified_files', []) if row.get('verified') is True]


def run_subscription(subscription, config, root, *, apply=False, as_of=None, reconcile_pending=False):
    """Caller holds the existing account lock. No interval or permission bypass."""
    from .headless_cli import _prepare_identity, _metadata, _storage, _safe_failure
    current = as_of or datetime.now(timezone.utc)
    request = deepcopy(subscription['request'])
    request['as_of'] = current.isoformat()
    for transient in ('source_selection', 'source_episode_mapping', '_naming_prefix'):
        request.pop(transient, None)
    store = TaskStore(root / 'runtime/tasks', config['library_root_id'])
    from .storage_connection import account_scope
    task = store.create(request, apply_authorized=bool(apply and subscription['auto_save']),
                        account_scope=account_scope(config))
    journal = None
    update_report = None
    preferred = subscription.get('preferred_sources', [])

    def finish(report):
        report['subscription_id'] = subscription['subscription_id']
        report['automatic_write_enabled'] = bool(apply and subscription['auto_save'] and not subscription.get('blocked'))
        report['blocked_before_check'] = bool(subscription.get('blocked'))
        report.setdefault('writes_attempted', False)
        report.setdefault('writes_performed', False)
        report.setdefault('new_files', [])
        if update_report is not None:
            report['metadata_updates'] = update_report
        return store.finish(task, report, resolved_request=task.get('execution_request', request))

    try:
        resolved, issue, primary, profile = _prepare_identity(request, config)
        if issue:
            return finish(issue)
        request = resolved
        task['resolved_request'] = request
        store.save(task)
        secondary_spec = subscription.get('secondary')
        if not secondary_spec and profile and profile.get('tmdb_link') and request['metadata_source'] != 'tmdb':
            secondary_spec = {'source': 'tmdb', 'series_id': profile['tmdb_link']['series_id']}
        secondary, secondary_id = None, None
        if secondary_spec:
            secondary, _ = _metadata(secondary_spec['source'])
            secondary_id = secondary_spec.get('series_id')
        update_report = collect_updates(request, primary, secondary, secondary_id, current)
        if not update_report['automatic_transfer_allowed']:
            hard = next((row.get('http_status') for row in update_report.get('errors', [])
                         if row.get('http_status') in {401, 403, 429}), None)
            status = ('NEEDS_AUTH' if hard in {401, 403} else 'RISK_CONTROL' if hard == 429 else
                      'NEEDS_INPUT' if update_report.get('conflicts') else 'WAITING')
            return finish({'status': status, 'reasons': ['metadata_conflict' if update_report.get('conflicts')
                                                       else 'metadata_temporarily_unavailable']})
        metadata = UpdatesMetadata(primary, update_report['episodes'], update_report['series'])
        storage = _storage(config)
        journal = JobJournal(root / 'runtime/jobs.sqlite')
        fallback = PanSouSearch(config['pansou_url'])
        search = PreferredSourceSearch(preferred, fallback, include_fallback=not bool(preferred))
        pre = workflow.run(request, storage, metadata, search, journal, apply=False)
        allow_write = bool(apply and subscription['auto_save'] and not subscription.get('blocked'))
        if pre['status'] == 'UNKNOWN' and reconcile_pending:
            checked = workflow.reconcile(request, storage, metadata, journal)
            if checked['status'] != 'COMPLETE':
                return finish(checked)
            pre = workflow.run(request, storage, metadata, search, journal, apply=False)
        if pre['status'] == 'UNKNOWN' and allow_write:
            checked = workflow.reconcile(request, storage, metadata, journal)
            if checked['status'] in {'COMPLETE', 'READY_TO_CONTINUE', 'READY_TO_RENAME'}:
                # A known submitted task has been read back. run() resumes its
                # durable new fids, and never repeats an unknown save POST.
                pre = workflow.run(request, storage, metadata, search, journal, apply=True, resume_only=True)
                if pre['status'] not in {'COMPLETE', 'UP_TO_DATE', 'READY'}:
                    return finish(pre)
                if _new_files(pre):
                    return finish({**pre, 'new_files': _new_files(pre), 'source_strategy': 'reconciled_submission'})
                # Recovery of an empty/ready intent did not inspect this
                # cycle's missing episodes. Apply the subscription policy to
                # a fresh read-only plan before allowing any new save.
                pre = workflow.run(request, storage, metadata, search, journal, apply=False)
            else:
                return finish(checked)
        if pre['status'] in {'NEEDS_AUTH', 'RISK_CONTROL', 'UNKNOWN', 'FAILED', 'PARTIAL'}:
            return finish(pre)
        if pre['status'] == 'UP_TO_DATE':
            return finish({**pre, 'source_strategy': 'no_missing_episodes'})
        if not _needed(pre):
            return finish(pre)
        chosen, mappings, ambiguous = select_ready(request, pre, subscription)
        used_fallback = not bool(preferred)
        if preferred and len(chosen) < len(_needed(pre)) and not ambiguous:
            search = PreferredSourceSearch(preferred, fallback, include_fallback=True)
            pre = workflow.run(request, storage, metadata, search, journal, apply=False)
            if pre['status'] in {'NEEDS_AUTH', 'RISK_CONTROL', 'UNKNOWN', 'FAILED', 'PARTIAL'}:
                return finish(pre)
            chosen, mappings, ambiguous = select_ready(request, pre, subscription)
            used_fallback = True
        pre['source_strategy'] = 'preferred_then_search' if used_fallback else 'preferred_source'
        if ambiguous:
            pre.update(status='NEEDS_INPUT', reasons=['subscription_source_or_episode_confirmation_required'],
                       ambiguous_episodes=ambiguous)
            return finish(pre)
        if not chosen:
            pre.update(status='WAITING', reasons=['expected_episode_has_no_confirmed_matching_resource'])
            return finish(pre)
        ready_request = deepcopy(request)
        ready_request.update(source_selection=chosen, source_episode_mapping=mappings)
        # Missing episodes without files remain pending in the final report.
        # A verified available subset may be saved without calling it complete.
        ready_rows = [row for row in update_report['episodes'] if row.get('episode_key') in chosen]
        ready_metadata = UpdatesMetadata(primary, ready_rows, update_report['series'])
        planned = workflow.run(ready_request, storage, ready_metadata, search, journal, apply=False)
        planned['pending_resource_episodes'] = sorted(_needed(pre) - set(chosen))
        planned['source_strategy'] = pre['source_strategy']
        if planned['status'] != 'READY':
            return finish(planned)
        if not allow_write:
            return finish(planned)
        task['resolved_request'] = ready_request
        task['execution_request'] = ready_request
        task['subscription_decision'] = {'basis': 'persisted_subscription_source_and_episode_policy',
                                         'source_selection': chosen, 'source_episode_mapping': mappings}
        store.save(task)
        saved = workflow.run(ready_request, storage, ready_metadata, search, journal, apply=True)
        if saved['status'] not in {'COMPLETE', 'UP_TO_DATE'}:
            return finish(saved)
        # Read the full season inventory without another resource search.
        after = workflow.run(request, storage, metadata, _NoSearch(), journal, apply=False)
        pending_only = (after['status'] == 'NEEDS_INPUT' and set(after.get('reasons', [])) ==
                        {'missing_episodes_have_no_verified_share_candidates'})
        if after['status'] not in {'READY', 'UP_TO_DATE', 'COMPLETE'} and not pending_only:
            return finish({**after, 'new_files': _new_files(saved), 'operations': saved.get('operations', []),
                           'writes_attempted': saved.get('writes_attempted', False),
                           'writes_performed': saved.get('writes_performed', False)})
        result = {**saved, **{key: after.get(key, []) for key in ('existing', 'missing', 'future', 'unknown_schedule')},
                  'source_strategy': pre['source_strategy'], 'new_files': _new_files(saved),
                  'pending_resource_episodes': sorted(_needed(after))}
        if after.get('missing'):
            result['reasons'] = ['available_missing_episodes_saved_others_pending']
        combined = preferred + extract_selected_sources(saved)
        result['preferred_sources'] = list({(row['source_hash'], row.get('parent_id'), row.get('path')): row
                                             for row in combined}.values())
        return finish(result)
    except Exception as exc:
        from .providers import AuthenticationError, RiskControlError, UnknownOutcome
        failure = _safe_failure(exc)
        if isinstance(exc, AuthenticationError):
            failure['status'] = 'NEEDS_AUTH'
        elif isinstance(exc, RiskControlError):
            failure['status'] = 'RISK_CONTROL'
        elif isinstance(exc, UnknownOutcome):
            failure['status'] = 'UNKNOWN'
        return finish(failure)
    finally:
        if journal is not None:
            journal.close()
