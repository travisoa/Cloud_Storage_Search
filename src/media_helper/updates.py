"""Cross-check expected episode schedules without changing a bound show ID.

Dates describe a planned broadcast, not a release confirmation. A second
catalog can contribute only after public IMDb/TVDB identifiers prove its link.
This module performs metadata reads only; it never searches or writes a drive.
"""

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import re

from .catalog import (EpisodeKey, MetadataError, SHANGHAI, _aware_datetime,
                      _external_ids, _source_id, build_episode_inventory,
                      calculate_missing)
from .planner import _integer


def _source(provider):
    value = getattr(provider, "source", "metadata")
    return value if value in {"tmdb", "tvmaze"} else "metadata"


def _error(exc, source):
    """Copy bounded error metadata, never exception messages or request URLs."""
    result = {"source": source, "code": "invalid_metadata_response"}
    if isinstance(exc, MetadataError):
        if isinstance(exc.code, str) and re.fullmatch(r"[a-z_]{1,80}", exc.code):
            result["code"] = exc.code
        status = _integer(exc.status, 100, 599)
        if status is not None:
            result["http_status"] = status
        if isinstance(exc.retry_after, str) and re.fullmatch(r"\d{1,8}", exc.retry_after):
            result["retry_after_seconds"] = int(exc.retry_after)
    return result


def _link(source, identifier, role, status, **extra):
    urls = {"tmdb": "https://www.themoviedb.org/tv/", "tvmaze": "https://www.tvmaze.com/shows/"}
    return {"source": source, "series_id": identifier, "role": role, "status": status,
            "url": urls[source] + str(identifier) if source in urls and identifier is not None else None,
            **extra}


def _identity_relation(first, second):
    first, second = _external_ids(first), _external_ids(second)
    common = [key for key in ("imdb_id", "tvdb_id") if first[key] is not None and second[key] is not None]
    different = [key for key in common if first[key] != second[key]]
    matched = [key for key in common if first[key] == second[key]]
    return ("conflict" if different else "linked" if matched else "unlinked"), matched, different


def _series(provider, identifier):
    series = provider.get_series(identifier)
    if not isinstance(series, dict) or str(series.get("source_id", series.get("id"))) != str(identifier):
        raise MetadataError(_source(provider), "unexpected_series_identity")
    if series.get("source") and series["source"] != _source(provider):
        raise MetadataError(_source(provider), "unexpected_series_identity")
    return series


def _episodes(provider, identifier, season):
    rows = provider.get_season(identifier, season)
    if isinstance(rows, dict):
        rows = rows.get("episodes")
    if not isinstance(rows, list):
        raise MetadataError(_source(provider), "unexpected_season_response")
    return rows


def _index(rows, season, source, report):
    result, duplicates = {}, set()
    for row in rows:
        if not isinstance(row, dict) or row.get("is_special"):
            report["unknown"].append({"source": source, "reasons": ["special_or_invalid_episode_metadata"]})
            continue
        try:
            key = EpisodeKey(row.get("season", row.get("season_number")),
                             row.get("episode", row.get("episode_number")))
        except ValueError:
            report["unknown"].append({"source": source, "reasons": ["invalid_episode_key"]})
            continue
        if key.season != season:
            report["conflicts"].append({"code": "season_number_mismatch", "source": source,
                                        "episode_key": str(key)})
            continue
        if str(key) in result:
            duplicates.add(str(key))
            continue
        result[str(key)] = {**deepcopy(row), "season": key.season, "episode": key.episode,
                            "episode_key": str(key), "is_expected": True}
    for key in sorted(duplicates):
        result.pop(key, None)
        report["conflicts"].append({"code": "duplicate_episode_key", "source": source, "episode_key": key})
    return result


def _schedule(row):
    """Compare local air dates supplied by catalogs, never a UTC date slice."""
    try:
        local_date = date.fromisoformat(row.get("air_date"))
    except (ValueError, TypeError):
        local_date = None
    stamp = None
    precision = row.get("date_precision") or ("datetime" if row.get("air_at") else "date" if local_date else "unknown")
    if precision == "datetime":
        try:
            stamp = _aware_datetime(row.get("air_at"), allow_date=False)
        except (ValueError, TypeError):
            pass
    return local_date, stamp


def _evidence(row):
    # Keep only schedule attribution fields; do not persist whole API responses.
    return {key: deepcopy(row.get(key)) for key in
            ("source", "series_id", "source_id", "fetched_at", "air_date", "air_at", "date_precision")}


def _external_evidence(value, provider, identifier):
    return {"source": _source(provider), "series_id": identifier, **_external_ids(value),
            "fetched_at": value.get("fetched_at") if isinstance(value, dict) else None}


def _season_count(series, season):
    counts = [_integer(item.get("episode_count"), 1, 999) for item in series.get("seasons", [])
              if isinstance(item, dict) and _integer(item.get("season"), 1, 99) == season]
    return counts[0] if len(counts) == 1 else None


def _merge(primary, secondary, report):
    different = sorted(set(primary) ^ set(secondary))
    if different:
        report["conflicts"].append({"code": "episode_numbering_mismatch",
                                    "primary_only": sorted(set(primary) - set(secondary)),
                                    "secondary_only": sorted(set(secondary) - set(primary))})
    merged = []
    for key in sorted(set(primary) & set(secondary)):
        first, second = primary[key], secondary[key]
        first_date, first_stamp = _schedule(first)
        second_date, second_stamp = _schedule(second)
        if ((first_date and second_date and first_date != second_date) or
                (first_stamp and second_stamp and first_stamp != second_stamp)):
            report["conflicts"].append({"code": "episode_schedule_mismatch", "episode_key": key,
                                        "evidence": [_evidence(first), _evidence(second)]})
            continue
        # A timestamp refines a matching date. A valid secondary date may fill
        # an unknown primary schedule, but conflicting known dates never merge.
        chosen = second if (second_stamp and not first_stamp) or (second_date and not first_date and not first_stamp) else first
        row = deepcopy(first)
        row.update({name: deepcopy(chosen.get(name)) for name in ("air_date", "air_at", "date_precision")})
        row["schedule_evidence"] = [_evidence(first), _evidence(second)]
        row["schedule_sources"] = [first.get("source"), second.get("source")]
        row["airing_status"] = "expected"
        merged.append(row)
    return merged


def fill_schedule(episodes, supplement, report):
    """Fill gaps in a bound primary schedule: the primary leads, an earlier date wins.

    Unlike ``_merge`` a disagreement is evidence, never a conflict. A schedule
    only decides when to search; a transfer still needs real matched files.
    """
    extra = {}
    for row in supplement:
        try:
            extra[str(EpisodeKey(row.get("season"), row.get("episode")))] = row
        except (ValueError, AttributeError):
            continue
    merged, keys = [], set()
    for episode in episodes:
        row = deepcopy(episode)
        key = row.get("episode_key") or str(EpisodeKey(row.get("season"), row.get("episode")))
        keys.add(key)
        row["schedule_basis"] = "primary"
        other = extra.get(key)
        first_date, first_stamp = _schedule(row)
        second_date, second_stamp = _schedule(other) if other else (None, None)
        if second_stamp is None or second_date is None or first_stamp is not None:
            merged.append(row)
            continue
        if first_date is not None and first_date != second_date:
            chosen = "supplement" if second_date < first_date else "primary"
            report["evidence"].append({"code": "supplement_schedule_disagreement", "episode_key": key,
                                        "primary_date": first_date.isoformat(),
                                        "supplement_date": second_date.isoformat(), "chosen": chosen})
            if chosen == "primary":
                merged.append(row)
                continue
        row.update(air_date=second_date.isoformat(), air_at=other["air_at"], date_precision="datetime",
                   schedule_basis="primary+supplement_time" if first_date == second_date else "supplement")
        merged.append(row)
    primary_only, supplement_only = sorted(keys - set(extra)), sorted(set(extra) - keys)
    if primary_only or supplement_only:
        report["evidence"].append({"code": "supplement_episode_set_difference",
                                    "primary_only": primary_only, "supplement_only": supplement_only})
    return merged


def _secondary_identity(primary_ids, secondary, secondary_id, request, report):
    """Resolve one external-ID link. A fuzzy title result is never identity."""
    source = _source(secondary)
    if secondary_id is not None:
        identifier = _source_id(secondary_id)
        ids = secondary.get_external_ids(identifier)
        relation, matched, different = _identity_relation(primary_ids, ids)
        return identifier, ids, relation, matched, different
    if not any(_external_ids(primary_ids).values()):
        return None, {}, "unlinked", [], []
    candidates = secondary.search_tv(request["title"], request.get("year"))
    if not isinstance(candidates, list):
        raise MetadataError(source, "unexpected_response_shape")
    matches, checked = [], set()
    for item in candidates[:5]:
        if not isinstance(item, dict):
            continue
        try:
            identifier = _source_id(item.get("source_id", item.get("id")))
        except ValueError:
            continue
        if identifier in checked:
            continue
        checked.add(identifier)
        ids = secondary.get_external_ids(identifier)
        relation, matched, different = _identity_relation(primary_ids, ids)
        if relation == "linked":
            matches.append((identifier, ids, relation, matched, different))
    report["evidence"].append({"code": "bounded_secondary_identity_discovery", "source": source,
                                "candidates_checked": len(checked), "candidate_limit": 5,
                                "search_truncated": len(candidates) > 5})
    if len(matches) == 1 and len(candidates) <= 5:
        return matches[0]
    if len(matches) > 1:
        report["conflicts"].append({"code": "multiple_secondary_external_id_matches", "source": source})
    # A truncated search cannot prove uniqueness, even if a checked row links.
    return None, {}, "unlinked", [], []


def collect_updates(request, primary, secondary=None, secondary_id=None, as_of=None):
    """Read and conservatively cross-check one explicitly bound regular season.

    ``episodes`` can feed the existing workflow without changing its job ID.
    Callers must stop automated writes whenever ``automatic_transfer_allowed``
    is false. Even expected_due rows require a real share/video confirmation.
    """
    if not isinstance(request, dict):
        raise ValueError("a confirmed request is required")
    identifier = _source_id(request.get("series_id"))
    season = _integer(request.get("season"), 1, 99)
    if season is None or not isinstance(request.get("title"), str) or not request["title"].strip():
        raise ValueError("a confirmed title and season are required")
    source = _source(primary)
    if request.get("metadata_source") != source:
        raise ValueError("the primary metadata source must match the bound identity")
    current = _aware_datetime(as_of if as_of is not None else request.get("as_of") or datetime.now(timezone.utc))
    report = {"schema_version": 1, "status": "ok", "basis": "expected_schedule_only",
              "checked_at": current.isoformat(), "series": None, "episodes": [],
              "source_links": [_link(source, identifier, "primary", "bound")], "evidence": [],
              "conflicts": [], "errors": [], "expected_due": [], "upcoming": [], "unknown": [],
              "automatic_transfer_allowed": False, "availability_verified": False, "next_check_at": None}
    try:
        report["series"] = _series(primary, identifier)
        if report["series"].get("year") is not None and str(report["series"]["year"]) != str(request.get("year")):
            report["conflicts"].append({"code": "bound_series_year_mismatch", "source": source})
        rows = _index(_episodes(primary, identifier, season), season, source, report)
    except (MetadataError, ValueError) as exc:
        report["status"] = "error"
        report["errors"].append(_error(exc, source))
        return report
    report["episodes"] = [rows[key] for key in sorted(rows)]
    report["evidence"].append({"code": "primary_schedule_loaded", "source": source,
                                "series_id": identifier, "episode_count": len(rows),
                                "fetched_at": report["series"].get("fetched_at")})
    if not rows:
        report["unknown"].append({"source": source, "reasons": ["season_episode_schedule_missing"]})
    if secondary is not None:
        second_source = _source(secondary)
        link = _link(second_source, None, "secondary", "unlinked")
        report["source_links"].append(link)
        try:
            if second_source == source:
                raise ValueError("secondary source must be different")
            first_ids = primary.get_external_ids(identifier)
            sid, second_ids, relation, matched, different = _secondary_identity(first_ids, secondary, secondary_id, request, report)
            link.update(_link(second_source, sid, "secondary", relation,
                              matched_external_ids=matched, fetched_at=second_ids.get("fetched_at")))
            report["evidence"].append({"code": "external_identity_crosscheck",
                                        "primary": _external_evidence(first_ids, primary, identifier),
                                        "secondary": _external_evidence(second_ids, secondary, sid), "result": relation})
            if relation == "conflict":
                report["conflicts"].append({"code": "external_identity_mismatch", "source": second_source,
                                            "conflicting_external_ids": different})
            elif relation == "linked":
                series = _series(secondary, sid)
                other = _index(_episodes(secondary, sid, season), season, second_source, report)
                first_count, second_count = _season_count(report["series"], season), _season_count(series, season)
                if first_count is not None and second_count is not None and first_count != second_count:
                    report["conflicts"].append({"code": "season_episode_count_mismatch", "primary_count": first_count,
                                                "secondary_count": second_count})
                report["episodes"] = _merge(rows, other, report)
            else:
                report["status"] = "degraded"
        except (MetadataError, ValueError, AttributeError) as exc:
            link["status"] = "error"
            report["errors"].append(_error(exc, second_source))
            report["status"] = "degraded"
    difference = calculate_missing(report["episodes"], build_episode_inventory([]), current)
    report["expected_due"], report["upcoming"] = difference["missing"], difference["future"]
    report["unknown"].extend(difference["unknown"])
    blocked = any(error.get("http_status") in {401, 403, 429} for error in report["errors"])
    report["automatic_transfer_allowed"] = bool(rows) and not report["conflicts"] and not blocked
    if blocked:
        report["status"] = "error"
    elif report["conflicts"]:
        report["status"] = "conflict"
    # A recommendation only; the subscription runner owns persistence/scheduling.
    next_check = current + timedelta(hours=6)
    for row in report["upcoming"]:
        local_date, stamp = _schedule(row)
        expected = stamp or (datetime.combine(local_date, datetime.min.time(), SHANGHAI) if local_date else None)
        if expected is not None and current < expected < next_check:
            next_check = expected
    report["next_check_at"] = next_check.isoformat()
    return report


class UpdatesMetadata:
    """Reuse a checked schedule while preserving the primary provider identity."""

    def __init__(self, primary, episodes, series=None):
        if not isinstance(episodes, list):
            raise ValueError("a checked episode list is required")
        self._primary = primary
        self._episodes = deepcopy(episodes)
        self._series = deepcopy(series) if series is not None else None
        self.source = getattr(primary, "source", None)

    def get_series(self, series_id):
        if self._series is None:
            return self._primary.get_series(series_id)
        if str(self._series.get("source_id", self._series.get("id"))) != str(series_id):
            raise ValueError("checked metadata belongs to a different series")
        return deepcopy(self._series)

    def get_season(self, series_id, season):
        series = self.get_series(series_id)
        if str(series.get("source_id", series.get("id"))) != str(series_id):
            raise ValueError("checked metadata belongs to a different series")
        if any(_integer(row.get("season"), 1, 99) != _integer(season, 1, 99) for row in self._episodes):
            raise ValueError("checked metadata belongs to a different season")
        return deepcopy(self._episodes)

    def __getattr__(self, name):
        return getattr(self._primary, name)
