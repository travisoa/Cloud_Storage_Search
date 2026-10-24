"""Per-subscription schedule cache: TMDB leads, Trakt fills gaps, refreshed daily.

The cache keeps expected schedules only. It never proves a release, and it
holds no credential, share token or whole API response.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, time, timedelta, timezone
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from .catalog import (EpisodeKey, MetadataError, _aware_datetime, _external_ids, _integer, _zone,
                      build_episode_inventory, calculate_missing)
from .profiles import private_json
from .subscriptions import HEALTHY_STATUSES
from .updates import _schedule, fill_schedule


SCHEMA_VERSION = 1
REFRESH_AFTER = timedelta(hours=24)
RETRY_AFTER_DEFAULT = timedelta(hours=1)
RETRY_AFTER_MAX = timedelta(hours=24)
CLOSED_SERIES = frozenset({"ended", "canceled", "cancelled"})
# Statuses whose last linked rows remain usable; errors keep the previous fill.
USABLE_SUPPLEMENT = frozenset({"linked", "error", "deferred"})
CHECK_DELAY = timedelta(minutes=30)
BASELINE = timedelta(hours=24)
SAMPLE_WINDOW = timedelta(days=14)
DEFAULT_INTERVAL = timedelta(hours=6)
WEEKDAY_NAMES = "一二三四五六日"
SOURCE_LABELS = {"tmdb": "TMDB", "tvmaze": "TVmaze", "trakt": "Trakt"}
SLOT_STEPS = (6, 4, 3, 2, 1)
CROSS_CHECK_CONFLICTS = frozenset({"external_identity_mismatch", "multiple_secondary_external_id_matches",
                                   "episode_numbering_mismatch", "episode_schedule_mismatch",
                                   "season_episode_count_mismatch"})
# A completed secondary read, linked or not; "error" is an outage that proves nothing.
SECONDARY_READ = frozenset({"linked", "unlinked"})


class ScheduleCache:
    def __init__(self, root):
        self.directory = Path(root) / "runtime/schedules"

    def path(self, subscription_id):
        if not isinstance(subscription_id, str) or not re.fullmatch(r"[0-9a-f]{64}", subscription_id):
            raise ValueError("订阅 ID 无效")
        return self.directory / f"{subscription_id}.json"

    def load(self, subscription_id):
        path = self.path(subscription_id)
        try:
            if path.is_symlink():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) and data.get("schema_version") == SCHEMA_VERSION else None

    def save(self, subscription_id, data):
        private_json(self.path(subscription_id), data)


def _identity(request, secondary):
    """A cached schedule is valid only for the sources that checked it."""
    linked = None
    if isinstance(secondary, dict) and secondary.get("source"):
        linked = {"source": secondary["source"],
                  "series_id": None if secondary.get("series_id") is None else str(secondary["series_id"])}
    return {"metadata_source": request.get("metadata_source"), "series_id": str(request.get("series_id")),
            "season": _integer(request.get("season"), 1, 99), "secondary": linked}


def _time(value):
    try:
        return _aware_datetime(value, allow_date=False)
    except (TypeError, ValueError):
        return None


def _zone_name(value):
    try:
        return value if isinstance(value, str) and value and ZoneInfo(value) else None
    except (ValueError, KeyError):
        return None


def _open_episodes(episodes, now, zone):
    """Unknown or not-yet-past dates: the only rows a supplement can still improve."""
    today = now.astimezone(zone).date()
    return [row for row in episodes if (local := _schedule(row)[0]) is None or local >= today]


def _merged(data, report):
    supplement = data.get("supplement") or {}
    if supplement.get("status") in USABLE_SUPPLEMENT and supplement.get("rows"):
        return fill_schedule(data.get("primary_episodes", []), supplement["rows"], report)
    return deepcopy(data.get("primary_episodes", []))


def _finished(series, episodes, now, zone):
    """Ended or canceled, and every episode has a date that is already past."""
    status = str((series or {}).get("status") or "").casefold()
    if status not in CLOSED_SERIES:
        return False
    today = now.astimezone(zone).date()
    for row in episodes:
        local_date, stamp = _schedule(row)
        if stamp is not None:
            if stamp > now:
                return False
        elif local_date is None or local_date > today:
            return False
    return True


def _closed(data, now, zone):
    return _finished(data.get("series"), _merged(data, {"evidence": []}), now, zone)


def _needs_refresh(data, now, refresh, trakt_wanted, zone):
    if refresh or data.get("refresh_required"):
        return True
    if trakt_wanted and (data.get("supplement") or {}).get("status") in {None, "disabled"}:
        return True
    if _closed(data, now, zone):
        return False
    refreshed = _time(data.get("refreshed_at"))
    return refreshed is None or now - refreshed >= REFRESH_AFTER


def _airs_time(supplement):
    value = ((supplement.get("airs") or {}).get("time") if supplement.get("status") in USABLE_SUPPLEMENT else None)
    return value if isinstance(value, str) and re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) else None


def _report(data, now, *, used_cache, timezone_name):
    report = {"schema_version": 1, "status": data.get("status", "ok"), "basis": "expected_schedule_only",
              "checked_at": now.isoformat(), "series": deepcopy(data.get("series")), "episodes": [],
              "source_links": deepcopy(data.get("source_links", [])), "evidence": deepcopy(data.get("evidence", [])),
              "conflicts": [], "errors": deepcopy(data.get("errors", [])), "expected_due": [], "upcoming": [],
              "unknown": deepcopy(data.get("metadata_unknown", [])), "automatic_transfer_allowed": True,
              "availability_verified": False, "next_check_at": None}
    report["episodes"] = _merged(data, report)
    difference = calculate_missing(report["episodes"], build_episode_inventory([]), now)
    report["expected_due"], report["upcoming"] = difference["missing"], difference["future"]
    report["unknown"].extend(difference["unknown"])
    supplement = data.get("supplement") or {"status": "disabled"}
    report["schedule_cache"] = {"used_cache": used_cache, "refreshed_at": data.get("refreshed_at"),
                                "supplement": supplement_summary(supplement)}
    report["schedule_hint"] = {"airs_time": _airs_time(supplement),
                               "timezone": supplement.get("timezone") or timezone_name}
    return report


def supplement_summary(supplement):
    """Trakt fill state for reports and ``watch status``; rows stay in the cache."""
    summary = {"source": "trakt", **{key: supplement.get(key) for key in ("status", "trakt_id", "reason", "http_status")}}
    if summary["http_status"] in {401, 403}:
        summary["action"] = "rerun_configure_trakt"
    return summary


def _kept(previous, **changes):
    """A failed Trakt read keeps the last linked rows instead of erasing them."""
    kept = {key: deepcopy(previous[key]) for key in ("trakt_id", "airs", "timezone", "rows", "checked_at")
            if previous and previous.get("status") in USABLE_SUPPLEMENT and key in previous}
    return {**kept, **changes}


def _supplement(previous, trakt, primary, request, episodes, now, timezone_name):
    zone = _zone(timezone_name)
    if not _open_episodes(episodes, now, zone):
        return {"status": "not_needed"}
    waiting = _time((previous or {}).get("retry_after_until"))
    if waiting is not None and now < waiting:
        return deepcopy(previous)
    try:
        tmdb_id = int(str(request["series_id"]))
        trakt_id = previous.get("trakt_id") if previous and previous.get("status") in USABLE_SUPPLEMENT else None
        if trakt_id is None:
            found = [row for row in trakt.find_by_tmdb(tmdb_id)
                     if isinstance(row, dict) and row.get("tmdb_id") == tmdb_id]
            if len(found) != 1:
                return {"status": "unlinked", "reason": "no_unique_tmdb_match"}
            trakt_id = found[0]["id"]
        series = trakt.get_series(trakt_id)
        if series.get("tmdb_id") != tmdb_id:
            return {"status": "conflict", "reason": "tmdb_id_mismatch", "trakt_id": trakt_id}
        mine, theirs = _external_ids(primary.get_external_ids(tmdb_id)), _external_ids(series)
        different = [key for key in ("imdb_id", "tvdb_id") if mine[key] and theirs[key] and mine[key] != theirs[key]]
        if different:
            return {"status": "conflict", "reason": "external_id_mismatch", "trakt_id": trakt_id,
                    "conflicting_external_ids": different}
        airs = series.get("airs") or {}
        zone_name = _zone_name(airs.get("timezone")) or timezone_name
        rows = trakt.get_season(trakt_id, request["season"], zone_name)
        keep = ("season", "episode", "air_date", "air_at", "date_precision")
        return {"status": "linked", "linked_by": "tmdb_id", "trakt_id": trakt_id, "airs": airs,
                "timezone": zone_name, "checked_at": now.isoformat(),
                "rows": [{**{key: row.get(key) for key in keep}, "source": "trakt"} for row in rows if isinstance(row, dict)]}
    except MetadataError as exc:
        if exc.status == 429:
            delay = timedelta(seconds=int(exc.retry_after)) if exc.retry_after else RETRY_AFTER_DEFAULT
            return _kept(previous, status="deferred", reason="rate_limited",
                         retry_after_until=(now + min(delay, RETRY_AFTER_MAX)).isoformat())
        return _kept(previous, status="error", reason=exc.code, http_status=exc.status)
    except (TypeError, ValueError, KeyError, AttributeError):
        return _kept(previous, status="error", reason="invalid_supplement_response")


def _cross_check_conflicts(conflicts, secondary_source):
    """Conflicts that came from the secondary source or the cross-check, not the primary alone."""
    return sorted({row.get("code") for row in conflicts if isinstance(row, dict)
                   and (row.get("code") in CROSS_CHECK_CONFLICTS or row.get("source") == secondary_source)} - {None})


def _secondary_status(report):
    return next((link.get("status") for link in report.get("source_links") or []
                 if isinstance(link, dict) and link.get("role") == "secondary"), None)


def _series_summary(series):
    return {key: deepcopy(value) for key, value in series.items() if key != "overview"} if isinstance(series, dict) else series


def obtain_schedule(subscription_id, request, *, fetch, primary, trakt, cache, now, refresh=False,
                    timezone_name="Asia/Shanghai", secondary=None):
    """Return a ``collect_updates``-shaped report, reusing a fresh cached schedule.

    ``fetch`` performs the existing live read. A blocking fetch result (auth,
    rate limit or conflict) is returned unchanged and the cache is no longer
    trusted until a healthy read; any other failed refresh keeps the last good
    schedule and retries at the next check. ``secondary`` is the effective
    cross-check source; changing it invalidates the cache.
    """
    now = _aware_datetime(now)
    zone = _zone(timezone_name)
    identity = _identity(request, secondary)
    trakt_wanted = trakt is not None and identity["metadata_source"] == "tmdb"
    cached = cache.load(subscription_id)
    if cached is not None and cached.get("identity") != identity:
        cached = None
    if cached is not None and not trakt_wanted and cached.get("supplement") != {"status": "disabled"}:
        cached["supplement"] = {"status": "disabled"}
        cache.save(subscription_id, cached)
    if cached is not None and not _needs_refresh(cached, now, refresh, trakt_wanted, zone):
        return _report(cached, now, used_cache=True, timezone_name=timezone_name)
    fetched = fetch()
    conflicted = bool(fetched.get("conflicts"))
    if not fetched.get("automatic_transfer_allowed"):
        blocked = any(isinstance(row, dict) and row.get("http_status") in {401, 403, 429}
                      for row in fetched.get("errors", []))
        if blocked or conflicted:
            # Until a healthy read, no cached schedule may vouch for this subscription. A
            # cross-check conflict is also remembered without one, for a later outage.
            record = cached if cached is not None else {"schema_version": SCHEMA_VERSION,
                                                        "subscription_id": subscription_id, "identity": identity}
            record["refresh_required"] = True
            codes = (_cross_check_conflicts(fetched["conflicts"], identity["secondary"]["source"])
                     if conflicted and identity["secondary"] is not None else [])
            if codes:
                record.setdefault("unresolved_conflict", {"codes": codes, "since": now.isoformat()})
            cache.save(subscription_id, record)
        if cached is None or blocked or conflicted or cached.get("refresh_required"):
            return fetched
        report = _report(cached, now, used_cache=True, timezone_name=timezone_name)
        report["evidence"].append({"code": "schedule_refresh_failed_using_cache",
                                   "errors": deepcopy(fetched.get("errors", []))})
        return report
    if (cached or {}).get("unresolved_conflict") and _secondary_status(fetched) not in SECONDARY_READ:
        # Only a completed read of that source resolves its known conflict; an outage does not.
        report = deepcopy(fetched)
        report.update(status="conflict", automatic_transfer_allowed=False,
                      conflicts=[*report.get("conflicts", []),
                                 {"code": "unresolved_secondary_conflict", "source": identity["secondary"]["source"]}])
        return report
    episodes = deepcopy(fetched.get("episodes", []))
    previous = (cached or {}).get("supplement")
    data = {"schema_version": SCHEMA_VERSION, "subscription_id": subscription_id, "identity": identity,
            "refreshed_at": now.isoformat(), "status": fetched.get("status", "ok"),
            "series": _series_summary(fetched.get("series")), "primary_episodes": episodes,
            "source_links": deepcopy(fetched.get("source_links", [])),
            "evidence": deepcopy(fetched.get("evidence", [])), "errors": deepcopy(fetched.get("errors", [])),
            "metadata_unknown": [deepcopy(row) for row in fetched.get("unknown", [])
                                 if isinstance(row, dict) and "episode" not in row],
            "supplement": (_supplement(previous, trakt, primary, request, episodes, now, timezone_name)
                           if trakt_wanted else {"status": "disabled"}),
            "wake_plan": (cached or {}).get("wake_plan")}
    cache.save(subscription_id, data)
    return _report(data, now, used_cache=False, timezone_name=timezone_name)


def _expected(row, hint, hint_zone):
    """A precise instant, or a date completed with the show's usual airing time."""
    local_date, stamp = _schedule(row)
    if stamp is not None or local_date is None or not hint:
        return stamp
    hour, minute = (int(part) for part in hint.split(":"))
    return datetime.combine(local_date, time(hour, minute), hint_zone)


def _key(row):
    try:
        return row.get("episode_key") or str(EpisodeKey(row.get("season"), row.get("episode")))
    except (ValueError, AttributeError):
        return None


def _obtained(run_report):
    """Healthy, and every due episode is in the library or was saved this run."""
    missing = {_key(row) for row in run_report.get("missing") or [] if isinstance(row, dict)}
    saved = {_key(row) for row in run_report.get("new_files") or [] if isinstance(row, dict)}
    return run_report.get("status") in HEALTHY_STATUSES and not (missing - saved)


def _interval_hours(interval_hours):
    return interval_hours if interval_hours and interval_hours > 0 else DEFAULT_INTERVAL.total_seconds() / 3600


def _slot_step(interval_hours):
    """Slot spacing: the largest divisor of a day not above the interval, at most 6 hours."""
    return next(step for step in SLOT_STEPS if step <= max(_interval_hours(interval_hours), 1))


def _interval_rrule(interval_hours):
    hours = min(_interval_hours(interval_hours), BASELINE.total_seconds() / 3600)
    if float(hours).is_integer():
        return f"FREQ=HOURLY;INTERVAL={int(hours)}", f"每 {int(hours)} 小时检查"
    return f"FREQ=MINUTELY;INTERVAL={round(hours * 60)}", f"每 {hours:g} 小时检查"


def _plan(rows, times, now, zone, interval_hours):
    """Wake plan plus its slots: (hours, minute, step) for fixed times, None for an interval.

    Fixed times are every day, 30 minutes after the broadcast and then every
    ``step`` hours (the interval, at most 6), so the retry and the 24-hour
    baseline always have a slot too.
    """
    sample = [(row, stamp.astimezone(zone)) for row, stamp in zip(rows, times)
              if stamp is not None and stamp >= now - SAMPLE_WINDOW]
    clocks = {(local.hour, local.minute) for _, local in sample}
    if len(clocks) != 1:
        rrule, summary = _interval_rrule(interval_hours)
        basis = "缺少一致的播出时刻" if sample else "没有具体播出时刻"
        return {"mode": "interval", "rrule": rrule, "summary": summary, "basis": basis}, None
    hour, minute = next(iter(clocks))
    step = _slot_step(interval_hours)
    check = sample[0][1] + CHECK_DELAY
    retries = [check + timedelta(hours=step * index) for index in range(1, 24 // step)]
    hours = sorted({moment.hour for moment in [check, *retries]})
    later = ("、".join(f"{moment:%H:%M}" for moment in retries) + " 补查" if len(retries) <= 3
             else f"之后每 {step} 小时补查")
    days = sorted({local.weekday() for _, local in sample})
    label = "每天" if len(days) == 7 else "每周" + "、".join(WEEKDAY_NAMES[day] for day in days)
    # A primary instant keeps its own catalog; filled rows and hint-completed dates come from Trakt.
    sources = sorted({SOURCE_LABELS.get(row.get("source"), "TMDB")
                      if row.get("schedule_basis", "primary") == "primary" and row.get("air_at") else "Trakt"
                      for row, _ in sample})
    plan = {"mode": "fixed_times",
            "rrule": "FREQ=DAILY;BYHOUR=" + ",".join(str(value) for value in hours) + f";BYMINUTE={check.minute}",
            "summary": f"每天 {check:%H:%M} 检查，{later}",
            "basis": f"{len(sample)} 集预计{label} {hour:02d}:{minute:02d} 播出（{'、'.join(sources)}）"}
    return plan, (hours, check.minute, step)


def _slot(moment, slots, zone, *, nearest):
    """The plan slot nearest ``moment`` (ties go later), or the last one at or before it.

    Rounding to the nearest slot absorbs a scheduler's start delay: a run at
    20:30:10 retries at 02:30, not 08:30.
    """
    hours, minute, step = slots
    local = moment.astimezone(zone)
    candidates = [datetime.combine(local.date() + timedelta(days=offset), time(hour, minute), zone)
                  for offset in range(-1, 3) for hour in hours]
    if nearest:
        earliest = moment - timedelta(hours=step) / 2
        return min(value for value in candidates if value >= earliest)
    return max(value for value in candidates if value <= moment)


def schedule_timing(update_report, run_report, *, interval_hours, now, timezone_name, previous_plan):
    """Next ``next_check_at`` and the wake plan an external scheduler can follow.

    Once every due episode is in the library the next check moves to 30
    minutes after the next expected broadcast, otherwise after the interval,
    always within 24 hours. With fixed times it is snapped to a plan slot, so
    a scheduler following ``wake_plan`` never misses a check.
    """
    now = _aware_datetime(now)
    zone = _zone(timezone_name)
    hint = (update_report.get("schedule_hint") or {})
    hint_zone = _zone(hint.get("timezone") or timezone_name)
    rows = [row for row in update_report.get("episodes") or [] if isinstance(row, dict)]
    times = [_expected(row, hint.get("airs_time"), hint_zone) for row in rows]
    obtained = _obtained(run_report)
    interval = timedelta(hours=interval_hours) if interval_hours and interval_hours > 0 else DEFAULT_INTERVAL
    plan, slots = _plan(rows, times, now, zone, interval_hours)
    baseline = now + BASELINE
    if slots is None:
        candidate = min(now + interval, baseline)
    else:
        upcoming = sorted(stamp for stamp in times if stamp is not None and stamp > now)
        target = upcoming[0] + CHECK_DELAY if obtained and upcoming else now + interval
        candidate = _slot(min(target, baseline), slots, zone, nearest=True)
        if candidate > baseline:
            candidate = _slot(baseline, slots, zone, nearest=False)
    if candidate <= now:
        candidate = now + DEFAULT_INTERVAL
    plan["stop"] = obtained and _finished(update_report.get("series"), rows, now, zone)
    previous = previous_plan or {}
    plan["changed"] = previous_plan is None or any(previous.get(key) != plan[key] for key in ("mode", "rrule", "stop"))
    return {"next_check_at": candidate.astimezone(timezone.utc), "wake_plan": plan}
