"""Strict, read-only metadata identities for an optional library import scan.

The resolver never chooses search rank, invents episode numbers or mutates a
drive.  Its caches last for this resolver instance, normally one scan only.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
import posixpath
import re
import unicodedata

from .catalog import MetadataError, parse_file
from .planner import _SE as _PAIR, _episode_metadata


_CN_PAIR = re.compile(r"第\s*[零〇一二两三四五六七八九十百\d]+\s*季.*?第\s*[零〇一二两三四五六七八九十百\d]+\s*集")
_ANY_EPISODE = re.compile(r"(?<![A-Za-z0-9])(?:S\d{1,2}|EP?\d{1,3})(?!\d)|第.+?[季集]", re.I)
_YEAR = re.compile(r"(?<!\d)((?:18|19|2\d)\d{2})(?!\d)")
_SEASON_FOLDER = re.compile(r"(?:Season[ ._-]*(\d{1,2})|第\s*(\d{1,2})\s*季)", re.I)
_SEASON_ONLY = re.compile(r"(?<![A-Za-z0-9])S(\d{1,2})(?![0-9])(?![ ._-]*E\d)", re.I)
_QUALITY = re.compile(r"(?<![A-Za-z0-9])(?:\d{3,4}p|[248]k|uhd|hevc|avc|[hx][ ._-]?26[45]|av1|hdr(?:10\+?)?|dovi|dolby[ ._-]*vision|blu[ ._-]*ray|web[ ._-]*(?:dl|rip)|hdtv|remux)(?![A-Za-z0-9])"
                      r"|_(?:DV|SDR|HLG|Atmos|(?:DDP|AC3|TrueHD|DTS(?:-HD(?:\.(?:MA|HRA))?|-X)?|AAC|FLAC|Opus|MP3|L?PCM)(?:[1-9]\.[01](?:\.[2-9])?|[1-9][0-9]?ch)?(?:\.Atmos)?)(?=$|[ ._\-])", re.I)
_GENERIC = {"电影", "电视剧", "剧集", "影库", "movies", "movie", "tv", "tvshows", "television", "videos", "video", "4k", "1080p", "2160p"}


def _normal(value):
    return re.sub(r"[\s._\-·:：()（）\[\]【】]", "", unicodedata.normalize("NFKC", str(value))).casefold()


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 240 and not any(unicodedata.category(c) in {"Cc", "Cs"} for c in value)


def _safe_title(value):
    return _text(value) and not any(c in value for c in '/\\<>:"|?*') and value.strip() not in {".", ".."}


def _integer(value, low=1, high=2**63 - 1):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).isdecimal():
        return None
    number = int(value)
    return number if low <= number <= high else None


def _needs(reason, candidates=(), **extra):
    return {"status": "NEEDS_INPUT", "reason": reason, "candidates": [_summary(item) for item in list(candidates)[:20]], **extra}


def _summary(item):
    return {key: item[key] for key in ("id", "source_id", "source", "title", "original_title", "year")
            if key in item and ((key in {"id", "source_id", "year"} and _integer(item[key], 1)) or (key not in {"id", "source_id", "year"} and _text(item[key])))}


def _names(item):
    return [value for value in [item.get("title"), item.get("name"), item.get("original_title"), item.get("original_name"), *(item.get("aliases") or [])] if _text(value)]


def _title_year(value):
    """Extract a year at the end of a title, before release label suffixes."""
    text = unicodedata.normalize("NFKC", value).strip()
    label = _QUALITY.search(text)
    if label:
        text = text[:label.start()].strip(" ._-[({")
    years = list(_YEAR.finditer(text))
    # A leading numeric title (1917, 2001) is retained; only a final year is a
    # release-year assertion.  Multiple years in the title are not discarded.
    year = years[-1] if years and not text[years[-1].end():].strip(" ._-)]}") else None
    if year and not text[:year.start()].strip(" ._-([{"):
        year = None
    core = text[:year.start()].strip(" ._-([{") if year else text.strip(" ._-[](){}")
    return re.sub(r"[._]+", " ", core).strip(), int(year[1]) if year else None


def _explicit(context):
    """Caller-confirmed season/episode (and optional title/year) for a single file."""
    episode = context.get("explicit_episode") if isinstance(context, dict) else None
    if episode is None:
        return None, None
    if (not isinstance(episode, dict) or _integer(episode.get("season"), 1, 99) is None
            or _integer(episode.get("episode"), 1, 999) is None or isinstance(episode["season"], str)
            or isinstance(episode["episode"], str)):
        raise ValueError("explicit_episode_invalid")
    title = context.get("explicit_title")
    if title is not None and (not isinstance(title, dict) or not _safe_title(title.get("title"))
                              or _integer(title.get("year"), 1800, 2999) is None or isinstance(title["year"], str)):
        raise ValueError("explicit_title_invalid")
    return (episode["season"], episode["episode"]), title


def _hints(file, context):
    name = file.get("name") if isinstance(file, dict) else None
    if not _text(name) or "/" in name or "\\" in name:
        return None, "invalid_source_name"
    try:
        explicit, explicit_title = _explicit(context)
    except ValueError as error:
        return None, str(error)
    stem = posixpath.splitext(name)[0]
    pair = _PAIR.search(stem) or _CN_PAIR.search(stem)
    parsed = parse_file(name)
    if pair and (parsed.get("ambiguous") or parsed.get("season") is None or parsed.get("episode") is None):
        return None, "ambiguous_explicit_episode_markers"
    marker = None
    if explicit and pair:
        if (parsed["season"], parsed["episode"]) != explicit:
            return None, "explicit_episode_disagrees_with_filename"
    elif explicit:
        # The supplied pair fills in a missing season; it never overrides a
        # different episode number, special or multi-episode marker in the name.
        _, episode, reasons = _episode_metadata(stem, {}, {"season": explicit[0]})
        number = re.fullmatch(r"\d{1,3}", stem.strip())
        seasons = {int(value) for value in _SEASON_ONLY.findall(stem)}
        if (set(reasons) - {"episode_unknown_no_sort_order_inference"} or (not reasons and episode != explicit[1])
                or (number and int(number[0]) != explicit[1]) or seasons - {explicit[0]}):
            return None, "explicit_episode_disagrees_with_filename"
        marker = _ANY_EPISODE.search(stem) or number
    elif not pair and (_ANY_EPISODE.search(stem) or re.fullmatch(r"\d{1,3}", stem.strip())):
        return None, "explicit_season_and_episode_required"
    season = parsed["season"] if pair else explicit[0] if explicit else None
    ancestors = context.get("ancestors") if isinstance(context, dict) else None
    if not isinstance(ancestors, list):
        path = context.get("path", "") if isinstance(context, dict) else ""
        ancestors = posixpath.dirname(path).split("/") if isinstance(path, str) else []
    folders = [entry.get("name") if isinstance(entry, dict) else entry for entry in ancestors]
    folders = [entry for entry in folders if _text(entry)]
    folder_seasons = {int(next(v for v in match.groups() if v is not None)) for folder in folders
                      if (match := _SEASON_FOLDER.fullmatch(folder.strip()))}
    if season and (len(folder_seasons) > 1 or (folder_seasons and season not in folder_seasons)):
        return None, "filename_and_directory_season_conflict"
    title, year = _title_year(stem[:pair.start()] if pair else stem[:marker.start()] if marker else stem)
    folder_identities = [_title_year(folder) for folder in reversed(folders) if not _SEASON_FOLDER.fullmatch(folder.strip())]
    folder_identities = [(t, y) for t, y in folder_identities if y and _normal(t) not in _GENERIC]
    if not title or _normal(title) in _GENERIC:
        if folder_identities:
            title, year = folder_identities[0]
        elif not explicit_title:
            return None, "title_and_release_year_required"
    else:
        same = [(t, y) for t, y in folder_identities if _normal(t) == _normal(title)]
        if same and (len({y for _, y in same}) > 1 or (year and any(y != year for _, y in same))):
            return None, "filename_and_directory_year_conflict"
        if not year and same:
            year = same[0][1]
    if explicit_title:
        # A complete name/folder identity must agree; an incomplete one yields.
        if title and year and (_normal(title) != _normal(explicit_title["title"]) or year != explicit_title["year"]):
            return None, "filename_and_explicit_title_conflict"
        title, year = explicit_title["title"], explicit_title["year"]
    if not year or not title:
        return None, "title_and_release_year_required"
    from .workflow import _TV_CATEGORY_NAMES, _TV_SUBCATEGORY_NAMES, _normal as category_name
    tv_folders = _TV_CATEGORY_NAMES | _TV_SUBCATEGORY_NAMES | {"television"}
    if not season and (context.get("kind") == "tv" or any(category_name(folder) in tv_folders for folder in folders)):
        return None, "explicit_season_and_episode_required"
    if season and context.get("kind") == "movie":
        return None, "filename_and_directory_kind_conflict"
    episode = (parsed.get("episode") if pair else explicit[1]) if season else None
    return {"kind": "tv" if season else "movie", "title": title, "year": year,
            "season": season, "episode": episode}, None


class MediaResolver:
    """Confirm one exact title/year and details before proposing an import.

    ``bindings`` accepts existing confirmed profile records, with optional
    ``kind`` (default tv). Cross-provider profiles require their provider in
    ``providers``. Aliases are revalidated against live ID/year/canonical title.
    Context uses ``path`` and root-to-parent ``ancestors`` names (or name dicts).
    """

    def __init__(self, metadata, *, bindings=(), providers=None):
        self.metadata = metadata
        self.providers = {getattr(metadata, "source", ""): metadata, **(providers or {})}
        self.bindings = list(bindings.values()) if isinstance(bindings, dict) else list(bindings or ())
        self._cache = {}

    def _cached(self, key, callback):
        if key not in self._cache:
            self._cache[key] = deepcopy(callback())
        return deepcopy(self._cache[key])

    def _movie(self, provider, identifier):
        if callable(getattr(provider, "get_movie", None)):
            return provider.get_movie(identifier)
        if getattr(provider, "source", "") != "tmdb" or not callable(getattr(provider, "_request", None)):
            raise MetadataError(getattr(provider, "source", "metadata"), "movie_metadata_unavailable")
        data = provider._request(f"/movie/{identifier}")
        if not isinstance(data, dict) or _integer(data.get("id")) != _integer(identifier):
            raise MetadataError("tmdb", "unexpected_movie_identity")
        return self._movie_record(data)

    @staticmethod
    def _movie_record(data):
        released = data.get("release_date")
        try:
            year = date.fromisoformat(released).year
        except (TypeError, ValueError):
            year = None
        return {"id": data.get("id"), "source_id": data.get("id"), "source": "tmdb",
                "title": data.get("title") or "", "original_title": data.get("original_title") or "", "year": year}

    def _search(self, provider, hints):
        if getattr(provider, "source", "") == "tmdb" and callable(getattr(provider, "_request", None)):
            return self._tmdb_search(provider, hints)
        if hints["kind"] == "tv":
            return provider.search_tv(hints["title"], hints["year"])
        if callable(getattr(provider, "search_movie", None)):
            return provider.search_movie(hints["title"], hints["year"])
        raise MetadataError(getattr(provider, "source", "metadata"), "movie_metadata_unavailable")

    def _tmdb_search(self, provider, hints):
        kind = hints["kind"]
        records, pages = [], 1
        for page in range(1, 6):
            year_key = "first_air_date_year" if kind == "tv" else "primary_release_year"
            data = provider._request(f"/search/{kind}", {"query": hints["title"], year_key: hints["year"], "include_adult": False, "page": page})
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise MetadataError("tmdb", f"unexpected_{kind}_search_response")
            pages = _integer(data.get("total_pages"), 0, 100000)
            if pages is None or pages > 5:
                raise MetadataError("tmdb", f"{kind}_search_incomplete")
            for item in data["results"]:
                if not isinstance(item, dict):
                    continue
                if kind == "movie":
                    records.append(self._movie_record(item))
                else:
                    first = item.get("first_air_date")
                    try:
                        year = date.fromisoformat(first).year
                    except (TypeError, ValueError):
                        year = None
                    records.append({"id": item.get("id"), "source": "tmdb", "title": item.get("name") or "", "original_title": item.get("original_name") or "", "year": year})
            if page >= pages:
                break
        return records

    def __call__(self, file, context=None):
        context = context if isinstance(context, dict) else {}
        hints, issue = _hints(file, context)
        if issue:
            return _needs(issue)
        try:
            return self._resolve(hints)
        except MetadataError as error:
            if error.status in {401, 403, 429} or error.code in {"authentication_required", "risk_control", "captcha_required"}:
                raise
            return _needs("metadata_unavailable", metadata_error={"source": error.source, "code": error.code, "status": error.status})

    def _resolve(self, hints):
        profiles = [item for item in self.bindings if isinstance(item, dict) and item.get("kind", "tv") == hints["kind"]
                    and _integer(item.get("year"), 1800, 2999) == hints["year"]
                    and any(_normal(name) == _normal(hints["title"]) for name in _names({**item, "original_title": item.get("canonical_title")}))]
        if len(profiles) > 1:
            return _needs("multiple_confirmed_bindings", profiles)
        profile = profiles[0] if profiles else None
        provider = self.providers.get(profile.get("metadata_source")) if profile else self.metadata
        if provider is None:
            return _needs("confirmed_binding_provider_unavailable")
        source = getattr(provider, "source", "")
        if source not in {"tmdb", "tvmaze"}:
            return _needs("unsupported_metadata_source")
        if profile:
            identifier = _integer(profile.get("series_id"))
            if not identifier:
                return _needs("invalid_confirmed_binding")
        elif source == 'tmdb' and callable(getattr(provider, 'get_identity', None)) and callable(getattr(provider, 'search_identity', None)):
            from .identity_match import resolve_exact
            matched = self._cached(('exact', source, hints['kind'], _normal(hints['title']), hints['year']),
                                   lambda: resolve_exact(provider, title=hints['title'], year=hints['year'], kind=hints['kind']))
            if matched['status'] in {'NEEDS_AUTH', 'RISK_CONTROL', 'ERROR'}:
                error = matched.get('metadata_error', {})
                raise MetadataError('tmdb', error.get('code', 'identity_match_unavailable'), status=error.get('status'))
            if matched['status'] != 'MATCHED':
                return _needs(matched['reason'], matched.get('candidates', []),
                              identity_evidence=matched.get('evidence', []), conflicts=matched.get('conflicts', []),
                              search_complete=matched.get('search_complete'))
            identifier = _integer(matched['identity']['id'])
        else:
            rows = self._cached(("search", source, hints["kind"], _normal(hints["title"]), hints["year"]), lambda: self._search(provider, hints))
            if not isinstance(rows, list):
                return _needs("invalid_metadata_search_response")
            matches = {str(_integer(row.get("id", row.get("source_id")))): row for row in rows if isinstance(row, dict)
                       and _integer(row.get("id", row.get("source_id")))
                       and _integer(row.get("year"), 1800, 2999) == hints["year"]
                       and any(_normal(name) == _normal(hints["title"]) for name in _names(row))}
            if len(matches) != 1:
                return _needs("identity_not_unique" if matches else "identity_not_matched", rows)
            identifier = int(next(iter(matches)))
        def live_details():
            if source == 'tmdb' and callable(getattr(provider, 'get_identity', None)):
                return provider.get_identity(hints['kind'], identifier)
            return provider.get_series(identifier) if hints["kind"] == "tv" else self._movie(provider, identifier)
        details = self._cached(("details", source, hints["kind"], identifier), live_details)
        if (not isinstance(details, dict) or _integer(details.get("id", details.get("source_id"))) != identifier
                or _integer(details.get("year"), 1800, 2999) != hints["year"] or details.get("source", source) != source):
            return _needs("metadata_details_identity_changed", [details] if isinstance(details, dict) else [])
        if profile:
            canonical = details.get("title", details.get("name"))
            if canonical != profile.get("canonical_title"):
                return _needs("confirmed_binding_identity_changed", [details])
            title = profile.get("title")
        else:
            if not any(_normal(name) == _normal(hints["title"]) for name in _names(details)):
                return _needs("metadata_details_title_changed", [details])
            title = details.get("title") or hints["title"]
        if not _safe_title(title):
            return _needs("unsafe_target_title_requires_confirmation", [details])
        if hints["kind"] == "tv":
            rows = self._cached(("season", source, identifier, hints["season"]), lambda: provider.get_season(identifier, hints["season"]))
            if not isinstance(rows, list):
                return _needs("invalid_season_metadata_response")
            matches = [row for row in rows if isinstance(row, dict) and _integer(row.get("season", row.get("season_number")), 1, 99) == hints["season"]
                       and _integer(row.get("episode", row.get("episode_number")), 1, 999) == hints["episode"]]
            if (len(matches) != 1 or matches[0].get("is_special")
                    or matches[0].get("source", source) != source
                    or _integer(matches[0].get("series_id", identifier)) != identifier):
                return _needs("episode_not_uniquely_confirmed")
        return {"status": "MATCHED", **hints, "title": title.strip(), "metadata_source": source, "series_id": str(identifier), "identity_basis": "exact_title_year_and_live_details"}
