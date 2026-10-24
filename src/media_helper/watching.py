"""Subscription cycles: expected schedule, actual shares, then missing inventory."""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import posixpath
import re

from .catalog import parse_file
from .journal import JobJournal
from .naming import episode_filename, media_info
from .planner import _chinese_integer, _episode_metadata
from .schedules import ScheduleCache, obtain_schedule, schedule_timing
from .search import PanSouSearch, PreferredSourceSearch
from .sources import extract_selected_sources, safe_source
from .subscriptions import POLICY_DEFAULTS
from .tasks import TaskStore
from .updates import UpdatesMetadata, collect_updates
from . import workflow


_SEASON_DIRECTORY = re.compile(r"(?:season[ ._-]*(\d{1,2})|s(\d{1,2})|第\s*([零〇一二两三四五六七八九十\d]{1,3})\s*季)", re.I)
UNCERTAIN_SOURCE_REASON = 'subscription_source_uncertain_retry_next_cycle'
# Within this share of the largest size, opted-in label preferences rank first.
NEAR_SIZE_PERCENT = 3
_HDR_FORMATS = frozenset({'HDR10+', 'HDR10', 'HDR', 'HLG'})
_TIERS = ('480p', '576p', '720p', '1080p', '1440p', '2160p', '4320p')
_HARD_STOPS = frozenset({'NEEDS_AUTH', 'RISK_CONTROL', 'UNKNOWN', 'FAILED', 'PARTIAL'})


def _needed(report):
    return {row['episode_key'] for row in report.get('missing', []) if row.get('episode_key')}


def _key(season, episode):
    return f'S{season:02d}E{episode:02d}'


def _directory_seasons(path):
    """Seasons named by whole directory components (Season 1, S01, 第一季)."""
    seasons = set()
    for part in posixpath.dirname(path or '').split('/'):
        match = _SEASON_DIRECTORY.fullmatch(part.strip())
        if match:
            seasons.add(_chinese_integer(next(group for group in match.groups() if group is not None)))
    return seasons


def _bare_number(stem):
    return int(stem) if re.fullmatch(r'\d{1,3}', stem or '') and 1 <= int(stem) <= 999 else None


def _filters(request):
    """The subscription's version policy or exact quality, as one predicate."""
    version_policy = None
    if request.get('version_policy') is not None:
        from .version_policy import normalize
        version_policy = normalize(request['version_policy'])
    quality = (request.get('quality_preference') or [None])[0]

    def check(info, size, name, path):
        """None when filtered out, otherwise the policy score (0 without a policy)."""
        if version_policy is not None:
            from .version_policy import assess
            decision = assess(version_policy, info, size=size, name=name, path=path)
            return decision['score'] if decision['allowed'] else None
        if quality and info.get('resolution') != quality:
            return None
        return 0
    return version_policy, check


def _share_titled(request, report, candidate):
    """The search-card title names the work. The share's root directory is
    already part of the candidate path, which the ordinary check reads."""
    names = [workflow._normal(name) for name in
             (request['title'], request.get('canonical_title', ''), *request.get('aliases', []))]
    source_hash = candidate.get('share_source', {}).get('source_hash')
    titles = [row.get('title', '') for row in report.get('source_refs', [])
              if isinstance(row, dict) and row.get('source_hash') == source_hash]
    return any(name and name in workflow._normal(title) for title in titles for name in names)


def _trusted_episode(candidate, season):
    """An explicit SxxEyy file name, or one season directory plus the file's own episode."""
    seasons = _directory_seasons(candidate.get('path', ''))
    if len(seasons) > 1 or (seasons and seasons != {season}):
        return None, None
    explicit = parse_file(candidate.get('name', ''))
    if explicit['episode_key'] and not explicit['ambiguous']:
        return (explicit['episode_key'], None) if explicit['season'] == season else (None, None)
    if not seasons:
        return None, None
    if candidate.get('episode_key'):
        return candidate['episode_key'], None
    number = _bare_number(posixpath.splitext(candidate.get('name', ''))[0])
    if number is not None and candidate.get('suggested_episode') == number:
        return _key(season, number), {'season': season, 'episode': number,
                                      'name': candidate['name'], 'size': candidate['size']}
    return None, None


def _preferred_rank(subscription, row):
    preferred = subscription.get('preferred_sources', [])
    return next((index for index, source in enumerate(preferred)
                 if source['source_hash'] == row.get('share_source', {}).get('source_hash')
                 and (not source.get('parent_id') or source['parent_id'] == row.get('parent_id'))), len(preferred))


def _authorized(request, report, subscription):
    """Per missing episode, the candidates the source and episode rules accept.

    Numerical episodes are authorized only inside one confirmed source dir.
    ``trust_share_title`` also accepts a file whose share title (search card or
    share root directory) names the work, whose episode is an explicit SxxEyy
    or follows from one season directory, and which is that share's only
    candidate for the episode. Returns ({key: {candidate_id: (candidate,
    mapping)}}, episodes whose files could not be authorized).
    """
    needed = _needed(report)
    season = int(request['season'])
    version_policy, check = _filters(request)
    policy = subscription.get('numeric_policy') or {}
    trust = subscription.get('trust_share_title') is True
    preferred = subscription.get('preferred_sources', [])

    def preferred_rank(row):
        return _preferred_rank(subscription, row)
    grouped, unknown, trusted, per_share = {}, set(), [], Counter()

    def hold(candidate):
        # A trusted candidate waits for the per-share uniqueness count below.
        key, mapping = _trusted_episode(candidate, season)
        if key is None or not _share_titled(request, report, candidate):
            return False
        trusted.append((key, candidate, mapping))
        return True

    for candidate in report.get('source_candidates', []):
        if type(candidate.get('size')) is not int or candidate['size'] <= 0:
            continue
        score = check(candidate.get('media_info', {}), candidate['size'],
                      candidate.get('name', ''), candidate.get('path', ''))
        if score is None:
            continue
        if version_policy is not None:
            candidate = {**candidate, 'score': score}
        key = candidate.get('episode_key')
        number = candidate.get('suggested_episode') if not key else None
        share = candidate.get('share_source', {}).get('source_hash')
        per_share[(share, key or (_key(season, number) if type(number) is int else None))] += 1
        if key in needed and preferred_rank(candidate) == len(preferred):
            context = workflow._normal(candidate.get('path') or candidate.get('name', ''))
            names = [request['title'], request.get('canonical_title', ''), *request.get('aliases', [])]
            if not any(workflow._normal(name) and workflow._normal(name) in context for name in names):
                # A matching search-card title alone cannot identify a video
                # inside a collection belonging to several different shows.
                if not (trust and hold(candidate)):
                    unknown.add(key)
                continue
        mapping = None
        if not key and type(candidate.get('suggested_episode')) is int:
            number = candidate['suggested_episode']
            stem = posixpath.splitext(candidate.get('name', ''))[0]
            if not (1 <= number <= 999 and re.fullmatch(r'\d+', stem) and int(stem) == number):
                continue
            key = _key(season, number)
            source_hash = candidate.get('share_source', {}).get('source_hash')
            if (source_hash != policy.get('source_hash')
                    or candidate.get('parent_id') != policy.get('parent_id')
                    or policy.get('season') != season
                    or candidate.get('media_info', {}).get('resolution') != policy.get('quality')):
                if key in needed and not (trust and hold(candidate)):
                    unknown.add(key)
                continue
            mapping = {'season': season, 'episode': number,
                       'name': candidate['name'], 'size': candidate['size']}
        if key in needed:
            grouped.setdefault(key, {})[candidate['candidate_id']] = (candidate, mapping)
    for key, candidate, mapping in trusted:
        if key not in needed:
            continue
        if per_share[(candidate.get('share_source', {}).get('source_hash'), key)] != 1:
            unknown.add(key)
            continue
        grouped.setdefault(key, {})[candidate['candidate_id']] = (candidate, mapping)
    return grouped, unknown


def select_ready(request, report, subscription):
    """One authorized video per missing episode: the policy's best resolution,
    then the first preferred source, then the highest score; ties stay open."""
    version_policy, _ = _filters(request)
    grouped, unknown = _authorized(request, report, subscription)

    def preferred_rank(row):
        return _preferred_rank(subscription, row)
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


def _episode_text(text, season, *, bare):
    """(episode key or None, whether ``text`` carried any episode marker)."""
    explicit, episode, reasons = _episode_metadata(text, {}, {})
    if not reasons:
        return (_key(season, episode) if explicit == season else None), True
    _, episode, reasons = _episode_metadata(text, {}, {'season': season})
    if not reasons:
        return _key(season, episode), True
    if set(reasons) - {'episode_unknown_no_sort_order_inference'}:
        return None, True  # Specials, other seasons or several markers.
    number = _bare_number(text.strip()) if bare else None
    return (_key(season, number), True) if number is not None else (None, False)


def archive_episode(candidate, season):
    """An archive's episode from its own name, else its directory name; never from order."""
    seasons = _directory_seasons(candidate.get('path', ''))
    if len(seasons) > 1 or (seasons and seasons != {season}):
        return None
    name = candidate.get('name', '')
    extension = (candidate.get('classification') or {}).get('format') or ''
    stem = name[:-len(extension) - 1] if extension and name.casefold().endswith('.' + extension) else name
    key, marked = _episode_text(stem, season, bare=True)
    if marked:
        return key
    parent = posixpath.basename(posixpath.dirname(candidate.get('path', '')))
    return _episode_text(parent, season, bare=False)[0] if parent else None


def _info(row):
    """A candidate's release labels: a video's recorded ones, else its name and folders."""
    return row.get('media_info') or media_info(row.get('name', ''), posixpath.dirname(row.get('path', '')))


def _labelled(subscription):
    return bool(subscription.get('prefer_hdr_format')) or subscription.get('archive_tiebreak_labels') is True


def _preference(subscription, row):
    """Near-size rank: the preferred HDR format, then DV > HDR > SDR and 高码 > 低码 when opted in."""
    hdr, rank = _info(row).get('hdr'), []
    prefer = subscription.get('prefer_hdr_format')
    if prefer:
        rank.append(hdr == 'Dolby Vision' if prefer == 'dv' else hdr in _HDR_FORMATS)
    if subscription.get('archive_tiebreak_labels') is True:
        text = row.get('path') or row.get('name', '')
        rank += [3 if hdr == 'Dolby Vision' else 2 if hdr in _HDR_FORMATS else 1,
                 2 if '高码' in text else 0 if '低码' in text else 1]
    return rank


def _top(rows, subscription):
    """The largest rows, ties included. Within NEAR_SIZE_PERCENT of the largest,
    opted-in label preferences rank before size; they never lift a smaller one past it."""
    largest = max(row['size'] for row in rows)
    if _labelled(subscription):
        rows = [row for row in rows if row['size'] * 100 >= largest * (100 - NEAR_SIZE_PERCENT)]

    def rank(row):
        return (*_preference(subscription, row), row['size'])
    best = max(map(rank, rows))
    return [row for row in rows if rank(row) == best]


def select_archives(request, report, subscription):
    """``archive_auto: largest`` picks one single-file archive per missing episode.

    The episode follows from the archive's name or directory, and the archive
    must outrank every other archive and every eligible video for that episode.
    By default it is its share's only one for that episode and strictly the
    largest. ``archive_pick_largest`` lets several archives of one share compete;
    ``archive_tiebreak_labels`` and ``prefer_hdr_format`` decide within 3% of
    the largest size. Returns the selection and the episodes left uncertain.
    """
    if subscription.get('archive_auto') != 'largest':
        return {}, []
    needed, season = _needed(report), int(request['season'])
    _, check = _filters(request)
    pick = subscription.get('archive_pick_largest') is True
    per_share, archives, videos = Counter(), {}, {}
    for item in report.get('archive_candidates', []):
        classification = item.get('classification') or {}
        if (classification.get('multipart') is not False or classification.get('supported_format') is not True
                or type(item.get('size')) is not int or item['size'] <= 0):
            continue
        key = archive_episode(item, season)
        if key not in needed:
            continue
        info = media_info(item.get('name', ''), posixpath.dirname(item.get('path', '')))
        if check(info, item['size'], item.get('name', ''), item.get('path', '')) is None:
            continue
        per_share[(item.get('share_source', {}).get('source_hash'), key)] += 1
        archives.setdefault(key, []).append(item)
    for candidate in report.get('source_candidates', []):
        key = candidate.get('episode_key')
        if not key and type(candidate.get('suggested_episode')) is int:
            key = _key(season, candidate['suggested_episode'])
        if (key in archives and type(candidate.get('size')) is int and candidate['size'] > 0
                and check(candidate.get('media_info', {}), candidate['size'],
                          candidate.get('name', ''), candidate.get('path', '')) is not None
                and candidate['size'] > videos.get(key, {}).get('size', 0)):
            videos[key] = candidate
    selected, uncertain = {}, []
    for key, items in sorted(archives.items()):
        video = videos.get(key)
        top = _top([*items, *([video] if video else [])], subscription)
        if any(row is video for row in top):
            continue  # A video at least as good keeps the ordinary path.
        winner = top[0]
        if len(top) == 1 and (pick or per_share[(winner.get('share_source', {}).get('source_hash'), key)] == 1):
            selected[key] = winner
        else:
            uncertain.append(key)  # Tied, or a duplicate within its share.
    return selected, uncertain


def _archive_source(report, item):
    """The selected archive's share, recorded like a saved video's source."""
    for reference in report.get('source_refs', []):
        if isinstance(reference, dict) and reference.get('source_hash') == item.get('share_source', {}).get('source_hash'):
            info = media_info(item.get('name', ''), posixpath.dirname(item.get('path', '')))
            try:
                return safe_source(reference, parent_id=item.get('parent_id'),
                                   path=posixpath.dirname(item.get('path', '')),
                                   quality_tags=[info[field] for field in ('resolution', 'codec', 'hdr') if info.get(field)])
            except (ValueError, TypeError):
                return None
    return None


def _tier(info, order):
    """Higher is better, by the version policy's resolution order when there is one.
    A known resolution outside that order ranks below it; no label is None."""
    resolution = (info or {}).get('resolution')
    if resolution not in _TIERS:
        return None
    return len(order) - order.index(resolution) if resolution in order else 0


def select_upgrades(request, report, subscription, *, exclude=()):
    """``upgrade_existing``: existing episodes that have a clearly better version.

    An episode qualifies when the season folder holds exactly one sized video for
    it. A candidate replaces the old file when its resolution tier is higher and
    its size at least ``upgrade_min_ratio_higher_tier`` times the old one, or the
    tier is the same (or the old file unlabelled) with at least
    ``upgrade_min_ratio`` times the size. A candidate without a resolution label,
    or with a lower one, never replaces anything. The archive ``archive_auto``
    would pick is the episode's only candidate; otherwise the qualifying
    authorized video with the highest tier and then the largest size (near-size
    label preferences apply). Preferred sources give no priority here: a better
    version may come from any authorized share.
    """
    destination = report.get('target_folder_id')
    if subscription.get('upgrade_existing') is not True or not destination:
        return {}
    ratio = subscription.get('upgrade_min_ratio', POLICY_DEFAULTS['upgrade_min_ratio'])
    higher_ratio = subscription.get('upgrade_min_ratio_higher_tier', POLICY_DEFAULTS['upgrade_min_ratio_higher_tier'])
    version_policy, _ = _filters(request)
    order = version_policy['resolution_order'] if version_policy else list(reversed(_TIERS))
    olds = {}
    for row in report.get('existing', []):
        files = row.get('files') or []
        if (row.get('episode_key') and row['episode_key'] not in exclude and len(files) == 1
                and str(files[0].get('parent_id', '')) == str(destination)
                and type(files[0].get('size')) is int and files[0]['size'] > 0):
            olds[row['episode_key']] = files[0]
    if not olds:
        return {}
    view = {**report, 'missing': [{'episode_key': key} for key in olds]}
    grouped, _ = _authorized(request, view, subscription)
    archives, _ = select_archives(request, view, subscription)
    plan = {}
    for key, old in sorted(olds.items()):
        current = _tier(media_info(workflow._name(old), posixpath.dirname(old.get('path', ''))), order)

        def basis(row):
            """Why ``row`` replaces the old file, or None when it does not."""
            new = _tier(_info(row), order)
            if new is None or (current is not None and new < current):
                return None
            higher = current is not None and new > current
            if row['size'] < (higher_ratio if higher else ratio) * old['size']:
                return None
            return 'higher_resolution' if higher else 'larger_size'
        mapping = None
        if key in archives:
            kind, item = 'archive', archives[key]
        else:
            rows = {candidate['candidate_id']: (candidate, value) for candidate, value in grouped.get(key, {}).values()
                    if basis(candidate)}
            if not rows:
                continue
            best = max(_tier(_info(candidate), order) for candidate, _ in rows.values())
            top = _top([candidate for candidate, _ in rows.values() if _tier(_info(candidate), order) == best],
                       subscription)
            if len(top) != 1:
                continue
            kind, (item, mapping) = 'video', rows[top[0]['candidate_id']]
        reason = basis(item)
        if not reason:
            continue
        plan[key] = {'kind': kind, 'candidate': item, 'mapping': mapping,
                     'old': {'fid': workflow._fid(old), 'name': workflow._name(old), 'size': old['size'],
                             'parent_id': str(old['parent_id'])},
                     'basis': reason}
    return plan


def _upgrade_row(key, entry, **extra):
    item = entry['candidate']
    return {'episode_key': key, 'kind': entry['kind'], 'basis': entry['basis'], 'candidate_id': item['candidate_id'],
            'name': item['name'], 'size': item['size'], 'path': item.get('path', ''),
            'old': deepcopy(entry['old']), **extra}


def _apply_upgrades(subscription, request, survey, *, storage, root, config, journal, search, metadata_for,
                    exclude=(), allow_write=False, record=None):
    """Plan, and with authority save, this cycle's upgrades of existing episodes.

    Only the season folder is read. An upgrade whose target name is already
    taken, or that stops before sending any write, is skipped and never blocks
    the subscription. Once a write was sent, a result short of COMPLETE (old
    version not yet recycled included) becomes the cycle's status.
    """
    outcome = {'status': 'COMPLETE', 'reasons': [], 'upgrade_plan': [], 'upgrade_skipped': [], 'upgrades': [],
               'new_files': [], 'sources': [], 'writes_attempted': False, 'writes_performed': False}
    plan = select_upgrades(request, survey, subscription, exclude=exclude)
    if not plan:
        return outcome
    destination = str(survey['target_folder_id'])
    names = [workflow._name(row).casefold() for row in workflow._list(storage, destination)]
    season, tags = int(request['season']), request.get('include_media_tags', True)
    for key, entry in sorted(plan.items()):
        item = entry['candidate']
        if entry['kind'] == 'video':
            target = episode_filename(survey['naming_prefix'], season, int(key[4:]),
                                      posixpath.splitext(item['name'])[1], _info(item), tags)
            taken = target.casefold() in names or item['name'].casefold() in names
        else:
            # The extracted video's extension is unknown yet: compare stems.
            extension, stem = (item.get('classification') or {}).get('format') or '', item['name']
            if extension and stem.casefold().endswith('.' + extension):
                stem = stem[:-len(extension) - 1]
            target = posixpath.splitext(episode_filename(survey['naming_prefix'], season, int(key[4:]), '.mkv',
                                        media_info(stem, posixpath.dirname(item.get('path', ''))), tags))[0]
            taken = any(posixpath.splitext(name)[0] == target.casefold() for name in names)
        if taken:
            outcome['upgrade_skipped'].append(_upgrade_row(key, plan.pop(key), reason='upgrade_target_name_exists'))
        else:
            outcome['upgrade_plan'].append(_upgrade_row(key, entry, target_name=target))
    if not allow_write or not plan:
        return outcome
    if record is not None:
        record(deepcopy(outcome['upgrade_plan']))

    def merge(step):
        for flag in ('writes_attempted', 'writes_performed'):
            outcome[flag] = outcome[flag] or bool(step.get(flag))

    def stop(step, keys):
        if not step.get('writes_attempted'):
            # Nothing was sent: the old version stays and the next cycle looks again.
            reason = (step.get('reasons') or ['upgrade_not_started'])[-1]
            outcome['upgrade_skipped'] += [_upgrade_row(key, plan[key], reason=reason) for key in keys]
            return False
        outcome.update(status=step['status'], failure=step)
        return True

    videos = {key: entry for key, entry in plan.items() if entry['kind'] == 'video'}
    if videos:
        upgrade_request = deepcopy(request)
        upgrade_request.update(
            source_selection={key: entry['candidate']['candidate_id'] for key, entry in videos.items()},
            source_episode_mapping={entry['candidate']['candidate_id']: entry['mapping']
                                    for entry in videos.values() if entry['mapping']},
            replace_episodes=sorted(videos), remove_replaced=True)
        saved = workflow.run(upgrade_request, storage, metadata_for(videos), search, journal, apply=True)
        merge(saved)
        if saved['status'] == 'COMPLETE':
            states = {row['episode_key']: row.get('state') for row in saved.get('replacements', [])}
            outcome['upgrades'] += [_upgrade_row(key, entry, old_state=states.get(key)) for key, entry in sorted(videos.items())]
            outcome['new_files'] += _new_files(saved)
            outcome['sources'] += extract_selected_sources(saved)
        elif stop(saved, sorted(videos)):
            return outcome
    from .archive_episodes import save_archive_episode
    cleanup = subscription.get('archive_cleanup', POLICY_DEFAULTS['archive_cleanup'])
    for key, entry in sorted(plan.items()):
        if entry['kind'] != 'archive':
            continue
        source = _archive_source(survey, entry['candidate'])
        if source is None:
            outcome['upgrade_skipped'].append(_upgrade_row(key, entry, reason='archive_source_unavailable'))
            continue
        result = save_archive_episode(storage, root, config, candidate=entry['candidate'], source=source,
                                      episode_key=key, prefix=survey['naming_prefix'], destination=destination,
                                      include_media_tags=tags, cleanup=cleanup, replacing=entry['old'])
        merge(result)
        if result['status'] == 'COMPLETE':
            outcome['upgrades'].append(_upgrade_row(key, entry, old_state=result['replaced']['state'],
                                                    archive=result))
            outcome['new_files'] += result['new_files']
            outcome['sources'].append(source)
            outcome['reasons'] += result.get('reasons', [])
        elif stop(result, [key]):
            outcome['upgrades'].append(_upgrade_row(key, entry, archive=result))
            return outcome
    return outcome


class _NoSearch:
    def search(self, _):
        return []


def _new_files(report):
    return [{**row, 'fid': row['saved_fid']} for operation in report.get('operations', [])
            if operation.get('state') == 'complete'
            for row in operation.get('verified_files', []) if row.get('verified') is True]


def run_subscription(subscription, config, root, *, apply=False, as_of=None, reconcile_pending=False,
                     refresh_schedule=False):
    """Caller holds the existing account lock. No interval or permission bypass."""
    from .headless_cli import _prepare_identity, _metadata, _storage, _safe_failure, _trakt
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
    schedules = ScheduleCache(root)
    zone = config.get('timezone', 'Asia/Shanghai')

    def finish(report):
        report['subscription_id'] = subscription['subscription_id']
        report['automatic_write_enabled'] = bool(apply and subscription['auto_save'] and not subscription.get('blocked'))
        report['blocked_before_check'] = bool(subscription.get('blocked'))
        report.setdefault('writes_attempted', False)
        report.setdefault('writes_performed', False)
        report.setdefault('new_files', [])
        if update_report is not None:
            report['metadata_updates'] = update_report
            cached = schedules.load(subscription['subscription_id'])
            timing = schedule_timing(update_report, report, interval_hours=subscription.get('interval_hours', 6),
                                     now=current, timezone_name=zone, previous_plan=(cached or {}).get('wake_plan'))
            report['next_check_at'] = timing['next_check_at'].isoformat()
            report['wake_plan'] = timing['wake_plan']
            if cached is not None:
                cached['wake_plan'] = timing['wake_plan']
                schedules.save(subscription['subscription_id'], cached)
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
        try:
            trakt = _trakt(config)
        except ValueError:
            trakt = None  # An unreadable Trakt credential only loses the schedule fill.
        update_report = obtain_schedule(
            subscription['subscription_id'], request,
            fetch=lambda: collect_updates(request, primary, secondary, secondary_id, current),
            primary=primary, trakt=trakt, cache=schedules, now=current, refresh=refresh_schedule,
            timezone_name=zone, secondary=secondary_spec)
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
        if pre['status'] in _HARD_STOPS:
            return finish(pre)
        cleanup = subscription.get('archive_cleanup', POLICY_DEFAULTS['archive_cleanup'])
        wide = PreferredSourceSearch(preferred, fallback, include_fallback=True)

        def metadata_for(keys):
            rows = [row for row in update_report['episodes'] if row.get('episode_key') in keys]
            return UpdatesMetadata(primary, rows, update_report['series'])

        def remember(field, value):
            # The task records each decision before the writes that follow it.
            task.setdefault('subscription_decision', {'basis': 'persisted_subscription_source_and_episode_policy'})
            task['subscription_decision'][field] = value
            store.save(task)

        def close(report, *, survey=None, saved=()):
            """After a healthy missing-episode phase, upgrade existing episodes, then finish."""
            if (subscription.get('upgrade_existing') is not True
                    or report['status'] not in {'UP_TO_DATE', 'WAITING', 'READY', 'COMPLETE'}):
                return finish(report)
            if survey is None:
                survey = workflow.run(request, storage, metadata, wide, journal, apply=False, survey_existing=True)
            if survey['status'] in _HARD_STOPS:
                if survey['status'] in {'NEEDS_AUTH', 'RISK_CONTROL'}:
                    return finish({**report, **{key: survey[key] for key in ('status', 'reasons', 'error') if key in survey}})
                return finish({**report, 'reasons': [*report.get('reasons', []), 'upgrade_check_unavailable']})
            outcome = _apply_upgrades(subscription, request, survey, storage=storage, root=root, config=config,
                                      journal=journal, search=wide, metadata_for=metadata_for, exclude=set(saved),
                                      allow_write=allow_write,
                                      record=lambda rows: remember('upgrade_selection', rows))
            result = {**report, 'new_files': [*report.get('new_files', []), *outcome['new_files']]}
            for flag in ('writes_attempted', 'writes_performed'):
                result[flag] = bool(report.get(flag)) or outcome[flag]
            for field in ('upgrade_plan', 'upgrade_skipped', 'upgrades'):
                if outcome[field]:
                    result[field] = outcome[field]
            reasons = [*report.get('reasons', []), *outcome['reasons']]
            if outcome['upgrade_skipped']:
                reasons.append('existing_episode_upgrade_skipped')
            if 'failure' in outcome:
                failure = outcome['failure']
                result['status'] = failure['status']
                reasons += failure.get('reasons', [])
                for key in ('error', 'operations', 'replacements'):
                    if key in failure:
                        result[key] = failure[key]
            elif outcome['upgrades']:
                result['status'] = 'COMPLETE' if result['status'] == 'UP_TO_DATE' else result['status']
                reasons.append('existing_episodes_upgraded')
                combined = result.get('preferred_sources', preferred) + outcome['sources']
                result['preferred_sources'] = list({(row['source_hash'], row.get('parent_id'), row.get('path')): row
                                                     for row in combined}.values())
            if reasons:
                result['reasons'] = list(dict.fromkeys(reasons))
            return finish(result)

        if pre['status'] == 'UP_TO_DATE':
            return close({**pre, 'source_strategy': 'no_missing_episodes'})
        if not _needed(pre):
            return close(pre)
        chosen, mappings, ambiguous = select_ready(request, pre, subscription)
        used_fallback = not bool(preferred)
        if preferred and len(chosen) < len(_needed(pre)) and not ambiguous:
            search = wide
            pre = workflow.run(request, storage, metadata, search, journal, apply=False)
            if pre['status'] in _HARD_STOPS:
                return finish(pre)
            chosen, mappings, ambiguous = select_ready(request, pre, subscription)
            used_fallback = True
        pre['source_strategy'] = 'preferred_then_search' if used_fallback else 'preferred_source'
        # A survey that already included the wider search is reused by the upgrade check.
        listed = pre if used_fallback else None
        archives, uncertain = select_archives(request, pre, subscription)
        apart = subscription.get('archive_video_fallback') is True
        reserve = {}
        for key in archives:
            # A winning archive outranks the video, which stays in reserve when kept apart.
            identifier = chosen.pop(key, None)
            if identifier:
                reserve[key] = (identifier, mappings.pop(identifier, None))
        for key in () if apart else uncertain:
            # By default an uncertain archive also leaves the episode's video open.
            mappings.pop(chosen.pop(key, None), None)
        ambiguous = sorted((set(ambiguous) | set(uncertain)) - set(archives) - set(chosen))
        if ambiguous and subscription.get('wait_on_uncertain_source') is not True:
            pre.update(status='NEEDS_INPUT', reasons=['subscription_source_or_episode_confirmation_required'],
                       ambiguous_episodes=ambiguous)
            return finish(pre)
        if archives and not chosen and not pre.get('target_folder_id'):
            pre.update(status='WAITING', reasons=['archive_auto_requires_existing_season_directory'],
                       archive_pending_episodes=sorted(archives))
            return finish(pre)
        if not chosen and not archives:
            # An opted-in uncertain source only defers this cycle; the next due
            # check searches again without a manual unblock.
            pre.update(status='WAITING', reasons=[UNCERTAIN_SOURCE_REASON] if ambiguous else
                       ['expected_episode_has_no_confirmed_matching_resource'])
            if ambiguous:
                pre['ambiguous_episodes'] = ambiguous
            return close(pre, survey=listed)
        ready_request = deepcopy(request)
        ready_request.update(source_selection=chosen, source_episode_mapping=mappings)
        # Missing episodes without files remain pending in the final report.
        # A verified available subset may be saved without calling it complete.
        ready_metadata = metadata_for(chosen)
        if chosen:
            planned = workflow.run(ready_request, storage, ready_metadata, search, journal, apply=False)
        else:
            planned = {**pre, 'status': 'READY', 'reasons': [], 'plan': []}
        planned['pending_resource_episodes'] = sorted(_needed(pre) - set(chosen) - set(archives))
        planned['source_strategy'] = pre['source_strategy']
        if archives:
            planned['archive_plan'] = [{'episode_key': key, 'candidate_id': item['candidate_id'], 'name': item['name'],
                                        'size': item['size'], 'path': item.get('path', '')}
                                       for key, item in sorted(archives.items())]
        if ambiguous:
            planned['ambiguous_episodes'] = ambiguous
            planned['reasons'] = [*planned.get('reasons', []), UNCERTAIN_SOURCE_REASON]
        if planned['status'] != 'READY':
            return finish(planned)
        if not allow_write:
            return close(planned, survey=listed)
        task['resolved_request'] = ready_request
        task['execution_request'] = ready_request
        task['subscription_decision'] = {'basis': 'persisted_subscription_source_and_episode_policy',
                                         'source_selection': chosen, 'source_episode_mapping': mappings}
        if archives:
            task['subscription_decision']['archive_selection'] = {key: item['candidate_id'] for key, item in archives.items()}
        store.save(task)
        saved, new_files, sources, archive_results = None, [], [], []
        writes = {'writes_attempted': False, 'writes_performed': False}
        if chosen:
            saved = workflow.run(ready_request, storage, ready_metadata, search, journal, apply=True)
            if saved['status'] not in {'COMPLETE', 'UP_TO_DATE'}:
                return finish(saved)
            new_files, sources = _new_files(saved), extract_selected_sources(saved)
            writes = {flag: saved.get(flag, False) for flag in writes}
        retry = {}
        if archives:
            from .archive_episodes import save_archive_episode
            destination = (saved or {}).get('target_folder_id') or pre.get('target_folder_id')
            for key, item in sorted(archives.items()):
                source = _archive_source(pre, item)
                result = (save_archive_episode(storage, root, config, candidate=item, source=source, episode_key=key,
                                               prefix=pre['naming_prefix'], destination=destination,
                                               include_media_tags=request.get('include_media_tags', True),
                                               cleanup=cleanup)
                          if source and destination else
                          {'status': 'NEEDS_INPUT', 'episode_key': key, 'new_files': [],
                           'reasons': ['archive_source_or_destination_unavailable']})
                archive_results.append(result)
                writes = {flag: writes[flag] or bool(result.get(flag)) for flag in writes}
                if result['status'] != 'COMPLETE':
                    if apart and result['status'] == 'NEEDS_INPUT' and key in reserve:
                        retry[key] = reserve[key]  # A known, unusable archive leaves its video.
                        continue
                    return finish({**(saved or pre), **writes, 'status': result['status'],
                                   'reasons': result.get('reasons', []), 'new_files': new_files,
                                   'archive_episodes': archive_results, 'source_strategy': pre['source_strategy']})
                new_files = [*new_files, *result['new_files']]
                sources.append(source)
        if retry:
            remember('video_fallback_selection', {key: value[0] for key, value in retry.items()})
            fallback_request = deepcopy(request)
            fallback_request.update(source_selection={key: value[0] for key, value in retry.items()},
                                    source_episode_mapping={value[0]: value[1] for value in retry.values() if value[1]})
            second = workflow.run(fallback_request, storage, metadata_for(retry), search, journal, apply=True)
            writes = {flag: writes[flag] or bool(second.get(flag)) for flag in writes}
            if second['status'] not in {'COMPLETE', 'UP_TO_DATE'}:
                return finish({**second, **writes, 'new_files': [*new_files, *_new_files(second)],
                               'archive_episodes': archive_results, 'source_strategy': pre['source_strategy']})
            saved = saved or second
            new_files, sources = [*new_files, *_new_files(second)], [*sources, *extract_selected_sources(second)]
        # Read the full season inventory without another resource search.
        after = workflow.run(request, storage, metadata, _NoSearch(), journal, apply=False)
        pending_only = (after['status'] == 'NEEDS_INPUT' and set(after.get('reasons', [])) ==
                        {'missing_episodes_have_no_verified_share_candidates'})
        if after['status'] not in {'READY', 'UP_TO_DATE', 'COMPLETE'} and not pending_only:
            return finish({**after, **writes, 'new_files': new_files, 'operations': (saved or {}).get('operations', []),
                           **({'archive_episodes': archive_results} if archive_results else {})})
        base = saved or {**pre, 'status': 'COMPLETE', 'reasons': [], 'operations': []}
        result = {**base, **{key: after.get(key, []) for key in ('existing', 'missing', 'future', 'unknown_schedule')},
                  **writes, 'source_strategy': pre['source_strategy'], 'new_files': new_files,
                  'pending_resource_episodes': sorted(_needed(after))}
        reasons = ['available_missing_episodes_saved_others_pending'] if after.get('missing') else []
        if archive_results:
            result['archive_episodes'] = archive_results
            reasons += [reason for row in archive_results for reason in row.get('reasons', [])]
        if retry:
            result['video_fallback_episodes'] = sorted(retry)
            reasons.append('archive_unusable_saved_video_instead')
        if ambiguous:
            result['ambiguous_episodes'] = ambiguous
            reasons.append(UNCERTAIN_SOURCE_REASON)
        if reasons:
            result['reasons'] = list(dict.fromkeys(reasons))
        # Every source that saved a file this cycle is appended, never swapped in.
        combined = preferred + sources
        result['preferred_sources'] = list({(row['source_hash'], row.get('parent_id'), row.get('path')): row
                                             for row in combined}.values())
        return close(result, saved={*chosen, *archives, *retry})
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
