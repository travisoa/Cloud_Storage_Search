"""Conservative episode inventory and explicit-ID metadata clients.

No search result is selected automatically. Metadata dates are schedules, not
proof of an actual broadcast or of a file being available on a cloud drive.
Network calls occur only when a provider method is explicitly invoked.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
import json
from pathlib import Path
import posixpath
import re
import stat
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .planner import VIDEO_EXTENSIONS, SUBTITLE_EXTENSIONS, _component_error, _episode_metadata, _integer


SHANGHAI = ZoneInfo("Asia/Shanghai")
_DOMAIN = r"(?:https?://)?(?:www\.)?[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.(?:com|net|org|tv|cn|cc|xyz|top|me|io)(?:/)?"
_SITE_BRACKET = re.compile(r"(?:\[|【|\()\s*(?:" + _DOMAIN + r")\s*(?:\]|】|\))", re.I)
_FOLDER_SEASON = re.compile(r"(?i)(?:^|/)(?:Season[ ._-]*(\d{1,2})|第\s*(\d{1,2})\s*季)(?=/|$)")


@dataclass(frozen=True, order=True)
class EpisodeKey:
    season: int
    episode: int

    def __post_init__(self):
        if _integer(self.season, 1, 99) is None or _integer(self.episode, 1, 999) is None:
            raise ValueError("episode key requires season 1..99 and episode 1..999")
        object.__setattr__(self, "season", int(self.season))
        object.__setattr__(self, "episode", int(self.episode))

    def __str__(self):
        return f"S{self.season:02d}E{self.episode:02d}"

    def to_dict(self):
        return {"season": self.season, "episode": self.episode}


def parse_file(name: str, season: int | None = None) -> dict:
    """Recognize explicit episode markers; the supplied season is only a fallback.

    A bare number remains unknown. A filename's explicit S02 is not silently
    overwritten by a caller's default S01. Basenames are required here.
    """
    result = {"name": name, "kind": "other", "extension": "", "stem": None,
              "season": None, "episode": None, "episode_key": None,
              "ambiguous": False, "reasons": []}
    error = _component_error(name)
    if error:
        result.update(ambiguous=True, reasons=["invalid_source_name:" + error])
        return result
    stem, extension = posixpath.splitext(name)
    result.update(stem=stem, extension=extension)
    if extension.casefold() in VIDEO_EXTENSIONS:
        result["kind"] = "video"
    elif extension.casefold() in SUBTITLE_EXTENSIONS:
        result["kind"] = "subtitle"
    else:
        return result
    parsed_season, episode, reasons = _episode_metadata(stem, {}, {})
    if parsed_season is None and season is not None:
        parsed_season, episode, reasons = _episode_metadata(stem, {}, {"season": season})
    result.update(season=parsed_season, episode=episode, ambiguous=bool(reasons), reasons=reasons)
    if not reasons:
        result["episode_key"] = str(EpisodeKey(parsed_season, episode))
    return result


def _reference(value) -> dict:
    if isinstance(value, str):
        if "/" in value:
            return {"name": posixpath.basename(value.rstrip("/")), "path": value}
        return {"name": value}
    if isinstance(value, dict):
        result = dict(value)
        if not result.get("name") and isinstance(result.get("path"), str):
            result["name"] = posixpath.basename(result["path"].rstrip("/"))
        return result
    return {"name": None, "invalid_input": True}


def _path_reasons(path) -> list[str]:
    if path is None:
        return []
    if not isinstance(path, str) or "\\" in path or any(part in {".", ".."} for part in path.split("/")):
        return ["unsafe_listing_path"]
    return []


def build_episode_inventory(files: list, season: int | None = None) -> dict:
    """Index video presence from a caller's already-scoped cloud file listing.

    ``files`` contains strings or file references with name/fid/id/path/size.
    Recursion, show identity, listing completeness and file playability are the
    caller's responsibility. Subtitles never establish that an episode exists.
    """
    if not isinstance(files, list):
        raise ValueError("files must be a list")
    if season is not None and _integer(season, 1, 99) is None:
        raise ValueError("default season must be an integer 1..99")
    result = {"schema_version": 1, "existing": {}, "review": [], "skipped": [],
              "presence_basis": "explicit_video_episode_marker",
              "listing_completeness": "caller_responsibility", "playability_verified": False}
    for value in files:
        reference = _reference(value)
        if reference.get("is_dir") or reference.get("type") in {"dir", "folder", "directory"}:
            result["skipped"].append({**reference, "reasons": ["directory_not_video"]})
            continue
        reasons = _path_reasons(reference.get("path"))
        folder_seasons = set()
        if not reasons and isinstance(reference.get("path"), str):
            folder_seasons = {int(next(part for part in match.groups() if part is not None))
                              for match in _FOLDER_SEASON.finditer(posixpath.dirname(reference["path"]) + "/")}
        explicit_season = reference.get("season")
        if explicit_season is not None and _integer(explicit_season, 1, 99) is None:
            reasons.append("invalid_explicit_listing_season")
        if len(folder_seasons) > 1:
            reasons.append("conflicting_season_directories")
        folder_season = next(iter(folder_seasons)) if len(folder_seasons) == 1 else None
        effective_season = explicit_season if explicit_season is not None else folder_season if folder_season is not None else season
        parsed = parse_file(reference.get("name"), effective_season)
        if parsed["kind"] == "other" and not parsed["ambiguous"] and not reasons:
            result["skipped"].append({**reference, "reasons": ["unsupported_or_nonmedia_extension"]})
            continue
        if parsed["kind"] == "subtitle" and not reasons:
            result["skipped"].append({**reference, "reasons": ["subtitle_does_not_establish_video_presence"]})
            continue
        reasons.extend(parsed["reasons"])
        for known_season in (explicit_season, folder_season):
            if _integer(known_season, 1, 99) is not None and parsed["season"] is not None and int(known_season) != parsed["season"]:
                reasons.append("filename_season_disagrees_with_listing_or_directory")
        if reference.get("size") == 0:
            reasons.append("zero_byte_video_requires_review")
        item = {**reference, "season": parsed["season"], "episode": parsed["episode"], "episode_key": parsed["episode_key"]}
        if reasons or not parsed["episode_key"]:
            result["review"].append({**item, "reasons": list(dict.fromkeys(reasons or ["episode_identity_unknown"]))})
        else:
            result["existing"].setdefault(parsed["episode_key"], []).append(item)
    return result


def _strip_site_noise(name: str) -> str:
    text = unicodedata.normalize("NFKC", name).strip()
    text = _SITE_BRACKET.sub(" ", text)
    text = re.sub(r"^" + _DOMAIN + r"(?:\s+|[ _-]+)", "", text, flags=re.I)
    text = re.sub(r"(?:\s+|[ _-]+)" + _DOMAIN + r"$", "", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def match_series_dirs(title: str, dirs: list, year: int | str | None = None) -> dict:
    """Match only a confirmed title and, when supplied, an explicit folder year.

    Strip domain-only advertisement wrappers, not quality/version/season words.
    Missing years or multiple equivalent folders are review candidates.
    """
    if _component_error(title) or not isinstance(dirs, list):
        raise ValueError("a confirmed title and directory list are required")
    expected_year = _integer(year, 1800, 2999) if year is not None else None
    if year is not None and expected_year is None:
        raise ValueError("year must be a four digit year")
    title_key = _strip_site_noise(title).casefold()
    matches, review = [], []
    for value in dirs:
        reference = _reference(value)
        name = reference.get("name")
        if _component_error(name):
            continue
        normalized = _strip_site_noise(name)
        year_match = re.search(r"(?:\s*\((\d{4})\)|\s*\[(\d{4})\]|\s+(\d{4}))$", normalized)
        folder_year = int(next(value for value in year_match.groups() if value is not None)) if year_match else None
        core = normalized[:year_match.start()].strip() if year_match else normalized
        reasons = _path_reasons(reference.get("path"))
        if core.casefold() != title_key:
            if title_key and title_key in core.casefold():
                review.append({**reference, "reasons": ["title_has_unconfirmed_qualifiers"]})
            continue
        if expected_year is not None and folder_year != expected_year:
            reasons.append("directory_year_missing" if folder_year is None else "directory_year_mismatch")
        if reasons:
            review.append({**reference, "reasons": reasons})
        else:
            matches.append({**reference, "matched_title": title, "matched_year": folder_year})
    if len(matches) > 1:
        review.extend({**item, "reasons": ["multiple_matching_series_directories"]} for item in matches)
        matches = []
    return {"matches": matches, "review": review}


def _aware_datetime(value, *, allow_date=True) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        if not allow_date:
            raise ValueError("a date without a time is not a timestamp")
        parsed = datetime.combine(value, time.min, SHANGHAI)
    elif isinstance(value, str):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            if not allow_date:
                raise ValueError("a date without a time is not a timestamp")
            parsed = datetime.combine(date.fromisoformat(value), time.min, SHANGHAI)
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("as_of must be an ISO date or timezone-aware datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("datetime must include an explicit timezone")
    return parsed


def calculate_missing(episodes: list, inventory: dict, as_of) -> dict:
    """Separate expected missing candidates from future and unknown schedules.

    Date-only records become eligible on that Shanghai calendar date; this is
    explicitly an expected-date signal, never confirmation of real broadcast.
    """
    if not isinstance(episodes, list) or not isinstance(inventory, dict) or not isinstance(inventory.get("existing"), dict):
        raise ValueError("episodes and an episode inventory are required")
    current = _aware_datetime(as_of)
    result = {"missing": [], "existing": [], "future": [], "unknown": [],
              "basis": "expected_schedule_only", "timezone": "Asia/Shanghai", "as_of": current.isoformat(),
              "inventory_review": list(inventory.get("review", []))}
    keyed = []
    counts = {}
    for episode in episodes:
        if not isinstance(episode, dict):
            result["unknown"].append({"reasons": ["invalid_episode_metadata"], "is_expected": True})
            continue
        row = {**episode, "is_expected": True}
        if row.get("is_special"):
            result["unknown"].append({**row, "reasons": ["special_episode_requires_review"]})
            continue
        try:
            key = str(EpisodeKey(episode.get("season", episode.get("season_number")), episode.get("episode", episode.get("episode_number"))))
        except ValueError:
            result["unknown"].append({**row, "reasons": ["invalid_or_special_episode_key"]})
            continue
        row["episode_key"] = key
        counts[key] = counts.get(key, 0) + 1
        keyed.append((key, row))
    for key, row in keyed:
        if counts[key] > 1:
            result["unknown"].append({**row, "reasons": ["duplicate_metadata_episode_key"]})
            continue
        if inventory["existing"].get(key):
            result["existing"].append({**row, "files": inventory["existing"][key]})
            continue
        precision = row.get("date_precision") or ("datetime" if row.get("air_at") else "date" if row.get("air_date") else "unknown")
        try:
            if precision == "datetime":
                scheduled = _aware_datetime(row.get("air_at"), allow_date=False)
                eligible = scheduled <= current
                reason = "expected_air_timestamp_reached" if eligible else "expected_air_timestamp_in_future"
            elif precision == "date":
                scheduled_date = date.fromisoformat(row.get("air_date", ""))
                eligible = scheduled_date <= current.astimezone(SHANGHAI).date()
                reason = "expected_air_date_reached_time_unconfirmed" if eligible else "expected_air_date_in_future"
            else:
                raise ValueError("no reliable schedule precision")
        except (ValueError, TypeError):
            result["unknown"].append({**row, "reasons": ["unknown_or_invalid_expected_air_date"]})
            continue
        result["missing" if eligible else "future"].append({**row, "eligibility_reason": reason, "eligible": eligible})
    return result


class MetadataError(RuntimeError):
    """Sanitized provider error; never includes request URLs or credentials."""

    def __init__(self, source: str, code: str, *, status: int | None = None, retry_after: str | None = None):
        self.source, self.code, self.status, self.retry_after = source, code, status, retry_after
        super().__init__(f"{source} metadata request failed: {code}" + (f" (HTTP {status})" if status is not None else ""))


class _Provider:
    source = ""
    base_url = ""

    def __init__(self, *, opener=None, now=None):
        self._opener = opener or urlopen
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _fetched_at(self):
        return self._now().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _get(self, path: str, params=None, headers=None):
        query = urlencode({key: value for key, value in (params or {}).items() if value is not None})
        request = Request(self.base_url + path + ("?" + query if query else ""), headers={"Accept": "application/json", "User-Agent": "media-helper/1 (personal catalog)", **(headers or {})})
        try:
            with self._opener(request, timeout=15) as response:
                payload = response.read(8 * 1024 * 1024 + 1)
                if len(payload) > 8 * 1024 * 1024:
                    raise MetadataError(self.source, "response_too_large")
                return json.loads(payload)
        except HTTPError as error:
            retry_after = error.headers.get("Retry-After") if error.headers else None
            if retry_after and not re.fullmatch(r"\d{1,8}", retry_after):
                retry_after = None
            status = error.code
            error.close()
            raise MetadataError(self.source, "http_error", status=status, retry_after=retry_after) from None
        except (URLError, OSError, ValueError, TypeError):
            raise MetadataError(self.source, "transport_or_invalid_json") from None


def _source_id(value):
    number = _integer(value, 1, 2**63 - 1)
    if number is None:
        raise ValueError("an explicit positive source ID is required; search results are never auto-selected")
    return number


def _date_precision(value):
    try:
        date.fromisoformat(value)
        return "date"
    except (ValueError, TypeError):
        return "unknown"


def _external_ids(data):
    """Return only recognized public identifiers, never arbitrary API fields."""
    if not isinstance(data, dict):
        return {"imdb_id": None, "tvdb_id": None}
    imdb = data.get("imdb_id", data.get("imdb"))
    imdb = imdb.strip().casefold() if isinstance(imdb, str) else None
    if not imdb or not re.fullmatch(r"tt\d{5,12}", imdb):
        imdb = None
    return {"imdb_id": imdb,
            "tvdb_id": _integer(data.get("tvdb_id", data.get("thetvdb")), 1, 2**63 - 1)}


def _private_credentials(path, label):
    candidate = Path(path)
    try:
        if candidate.is_symlink() or stat.S_IMODE(candidate.stat().st_mode) != 0o600:
            raise ValueError(f"{label} credentials file must be a regular private 0600 file")
        if not candidate.is_file():
            raise ValueError(f"{label} credentials file must be a regular private 0600 file")
        data = json.loads(candidate.read_text(encoding="utf-8"))
    except OSError:
        raise ValueError(f"{label} credentials file is not readable") from None
    except (json.JSONDecodeError, UnicodeError):
        raise ValueError(f"{label} credentials file must contain valid JSON") from None
    if not isinstance(data, dict):
        raise ValueError(f"{label} credentials file must contain a JSON object")
    return data


class TMDBProvider(_Provider):
    source = "tmdb"
    base_url = "https://api.themoviedb.org/3"

    def __init__(self, *, read_access_token=None, api_key=None, opener=None, now=None):
        super().__init__(opener=opener, now=now)
        self._token = read_access_token.strip() if isinstance(read_access_token, str) else ""
        self._key = api_key.strip() if isinstance(api_key, str) else ""
        if not self._token and not self._key:
            raise ValueError("TMDB requires a Read Access Token or API key")

    @classmethod
    def from_credentials_file(cls, path, **kwargs):
        """Read only the explicitly supplied private JSON file; no discovery."""
        data = _private_credentials(path, "TMDB")
        return cls(read_access_token=data.get("read_access_token"), api_key=data.get("api_key"), **kwargs)

    def _request(self, path, params=None):
        headers = {"Authorization": "Bearer " + self._token} if self._token else {}
        query = {"language": "zh-CN", **(params or {})}
        if not self._token:
            query["api_key"] = self._key
        return self._get(path, query, headers)

    def search_tv(self, query: str, year=None) -> list[dict]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("a nonempty title query is required")
        data = self._request("/search/tv", {"query": query.strip(), "first_air_date_year": year})
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise MetadataError(self.source, "unexpected_response_shape")
        fetched = self._fetched_at()
        return [self._series(item, fetched) for item in data["results"] if isinstance(item, dict) and item.get("id")]

    def _series(self, data: dict, fetched: str) -> dict:
        first = data.get("first_air_date") or ""
        return {"id": data.get("id"), "source_id": data.get("id"), "source": self.source, "kind": "tv",
                "title": data.get("name") or "", "original_title": data.get("original_name") or "",
                "year": int(first[:4]) if _date_precision(first) == "date" else None,
                "first_air_date": first, "date_precision": _date_precision(first),
                "status": data.get("status"), "overview": data.get("overview") or "",
                "seasons": [{"season": item.get("season_number"), "air_date": item.get("air_date"),
                             "episode_count": item.get("episode_count"), "title": item.get("name") or ""}
                            for item in (data.get("seasons") or []) if isinstance(item, dict)],
                "season_count": data.get("number_of_seasons"), "origin_country": data.get("origin_country") or [],
                "fetched_at": fetched, "identity_confirmed": False,
                **self._identity_fields(data, "tv")}

    @staticmethod
    def _identity_fields(data, kind):
        """Public names belong only to this detail response's exact TMDB ID."""
        aliases, evidence, seen = [], [], set()
        alternatives = data.get("alternative_titles")
        alternatives = alternatives if isinstance(alternatives, dict) else {}
        for row in alternatives.get("results" if kind == "tv" else "titles", []) or []:
            name = row.get("title") if isinstance(row, dict) else None
            if isinstance(name, str) and name.strip() and name.strip().casefold() not in seen:
                aliases.append(name.strip())
                seen.add(name.strip().casefold())
                evidence.append({"name": name.strip(), "basis": "tmdb_alternative_title", "tmdb_id": data.get("id")})
        translations = data.get("translations")
        translations = translations.get("translations", []) if isinstance(translations, dict) else []
        for row in translations or []:
            if not isinstance(row, dict) or row.get("iso_639_1") not in {"zh", "en"}:
                continue
            value = row.get("data")
            name = value.get("name" if kind == "tv" else "title") if isinstance(value, dict) else None
            if isinstance(name, str) and name.strip() and name.strip().casefold() not in seen:
                aliases.append(name.strip())
                seen.add(name.strip().casefold())
                evidence.append({"name": name.strip(), "basis": "tmdb_translation", "language": row["iso_639_1"], "tmdb_id": data.get("id")})
        return {"aliases": aliases, "alias_evidence": evidence,
                "external_ids": _external_ids(data.get("external_ids", data)),
                "aliases_checked": "alternative_titles" in data and "translations" in data}

    def _movie(self, data, fetched):
        first = data.get("release_date") or ""
        return {"id": data.get("id"), "source_id": data.get("id"), "source": self.source, "kind": "movie",
                "title": data.get("title") or "", "original_title": data.get("original_title") or "",
                "year": int(first[:4]) if _date_precision(first) == "date" else None,
                "release_date": first, "date_precision": _date_precision(first),
                "status": data.get("status"), "overview": data.get("overview") or "",
                "fetched_at": fetched, "identity_confirmed": False, **self._identity_fields(data, "movie")}

    def get_movie(self, movie_id):
        movie_id = _source_id(movie_id)
        data = self._request(f"/movie/{movie_id}")
        if not isinstance(data, dict) or data.get("id") != movie_id:
            raise MetadataError(self.source, "unexpected_movie_identity")
        return self._movie(data, self._fetched_at())

    def get_identity(self, kind, identifier):
        """Retrieve details, names and public IDs in one ID-scoped response."""
        if kind not in {"tv", "movie"}:
            raise ValueError("TMDB identity kind must be tv or movie")
        identifier = _source_id(identifier)
        data = self._request(f"/{kind}/{identifier}", {"append_to_response": "alternative_titles,translations,external_ids"})
        if not isinstance(data, dict) or data.get("id") != identifier:
            raise MetadataError(self.source, "unexpected_identity")
        title = data.get("name" if kind == "tv" else "title")
        if not isinstance(title, str) or not title.strip():
            raise MetadataError(self.source, "unexpected_identity_title")
        if "external_ids" in data and (not isinstance(data["external_ids"], dict)
                or data["external_ids"].get("id", identifier) != identifier):
            raise MetadataError(self.source, "unexpected_external_identity")
        return self._series(data, self._fetched_at()) if kind == "tv" else self._movie(data, self._fetched_at())

    def search_identity(self, kind, query, year=None, *, max_pages=5):
        """Bounded complete search; truncation can never imply uniqueness."""
        if kind not in {"tv", "movie"} or not isinstance(query, str) or not query.strip():
            raise ValueError("TMDB search needs a type and a nonempty title")
        if type(max_pages) is not int or not 1 <= max_pages <= 5:
            raise ValueError("TMDB search permits 1..5 pages")
        rows, expected_pages, fetched_pages = [], None, 0
        for page in range(1, max_pages + 1):
            data = self._request(f"/search/{kind}", {"query": query.strip(), "first_air_date_year" if kind == "tv" else "primary_release_year": year,
                                                     "include_adult": "false", "page": page})
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                raise MetadataError(self.source, "unexpected_identity_search_response")
            pages = _integer(data.get("total_pages"), 0, 100000)
            if pages is None or data.get("page", page) != page or expected_pages is not None and pages != expected_pages:
                return {"results": rows, "complete": False, "reason": "search_pagination_unverified", "pages_fetched": fetched_pages}
            if pages == 0 and data["results"]:
                return {"results": rows, "complete": False, "reason": "search_pagination_unverified", "pages_fetched": fetched_pages}
            expected_pages = pages
            fetched_pages += 1
            for item in data["results"]:
                if isinstance(item, dict) and _integer(item.get("id"), 1, 2**63 - 1):
                    rows.append(self._series(item, self._fetched_at()) if kind == "tv" else self._movie(item, self._fetched_at()))
            if page >= pages:
                return {"results": rows, "complete": True, "pages_fetched": fetched_pages}
        return {"results": rows, "complete": False, "reason": "search_page_limit", "pages_fetched": fetched_pages}

    def search_movie(self, query, year=None):
        result = self.search_identity("movie", query, year)
        if not result["complete"]:
            raise MetadataError(self.source, "movie_search_incomplete")
        return result["results"]

    def find_identity(self, kind, identifier, external_source):
        if kind not in {"tv", "movie"} or external_source not in {"imdb_id", "tvdb_id"}:
            raise ValueError("unsupported TMDB external identity type")
        value = _external_ids({external_source: identifier})[external_source]
        if value is None:
            raise ValueError("invalid public external ID")
        data = self._request(f"/find/{value}", {"external_source": external_source})
        key = "tv_results" if kind == "tv" else "movie_results"
        if not isinstance(data, dict) or not isinstance(data.get(key), list):
            raise MetadataError(self.source, "unexpected_external_search_response")
        return [self._series(row, self._fetched_at()) if kind == "tv" else self._movie(row, self._fetched_at())
                for row in data[key] if isinstance(row, dict) and _integer(row.get("id"), 1, 2**63 - 1)]

    def get_series(self, series_id) -> dict:
        series_id = _source_id(series_id)
        data = self._request(f"/tv/{series_id}")
        if not isinstance(data, dict) or data.get("id") != series_id:
            raise MetadataError(self.source, "unexpected_series_identity")
        return self._series(data, self._fetched_at())

    def get_external_ids(self, series_id) -> dict:
        series_id = _source_id(series_id)
        data = self._request(f"/tv/{series_id}/external_ids")
        if not isinstance(data, dict) or data.get("id") != series_id:
            raise MetadataError(self.source, "unexpected_series_identity")
        return {"source": self.source, "series_id": series_id,
                **_external_ids(data), "fetched_at": self._fetched_at()}

    def get_season(self, series_id, season) -> list[dict]:
        series_id = _source_id(series_id)
        season = _integer(season, 1, 99)
        if season is None:
            raise ValueError("season must be explicitly selected within 1..99")
        data = self._request(f"/tv/{series_id}/season/{season}")
        if not isinstance(data, dict) or not isinstance(data.get("episodes"), list) or data.get("season_number") != season:
            raise MetadataError(self.source, "unexpected_season_response")
        fetched = self._fetched_at()
        return [{"season": item.get("season_number", season), "episode": item.get("episode_number"),
                 "title": item.get("name") or "", "air_date": item.get("air_date") or None, "air_at": None,
                 "source": self.source, "source_id": item.get("id"), "series_id": series_id,
                 "fetched_at": fetched, "date_precision": _date_precision(item.get("air_date")),
                 "airing_status": "expected", "is_expected": True} for item in data["episodes"] if isinstance(item, dict)]


class TVmazeProvider(_Provider):
    source = "tvmaze"
    base_url = "https://api.tvmaze.com"

    def search_tv(self, query: str, year=None) -> list[dict]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("a nonempty title query is required")
        data = self._get("/search/shows", {"q": query.strip()})
        if not isinstance(data, list):
            raise MetadataError(self.source, "unexpected_response_shape")
        fetched = self._fetched_at()
        candidates = [self._series(item["show"], fetched) for item in data if isinstance(item, dict) and isinstance(item.get("show"), dict)]
        candidates = [item for item in candidates if year is None or item["year"] in {None, _integer(year, 1800, 2999)}]
        # Enrich only a bounded number of results, but retain EVERY remaining
        # candidate. Truncation must never manufacture a unique title identity.
        for candidate in candidates[:5]:
            self._add_aliases(candidate)
        return candidates

    def _add_aliases(self, series: dict) -> None:
        try:
            series_id = _source_id(series.get("id"))
            aliases = self._get(f"/shows/{series_id}/akas")
            if not isinstance(aliases, list):
                raise MetadataError(self.source, "unexpected_aliases_response")
            names, seen = [], set()
            for item in aliases:
                name = item.get("name") if isinstance(item, dict) else None
                if isinstance(name, str) and name.strip() and name.strip().casefold() not in seen:
                    names.append(name.strip())
                    seen.add(name.strip().casefold())
            series["aliases"] = names
            series["aliases_checked"] = True
        except (MetadataError, ValueError):
            # An unavailable alias endpoint does not delete the candidate or
            # turn its search rank into identity evidence.
            series["aliases"] = []
            series["aliases_checked"] = False

    def _series(self, data: dict, fetched: str) -> dict:
        first = data.get("premiered") or ""
        return {"id": data.get("id"), "source_id": data.get("id"), "source": self.source,
                "title": data.get("name") or "", "original_title": data.get("name") or "",
                "year": int(first[:4]) if _date_precision(first) == "date" else None,
                "first_air_date": first, "date_precision": _date_precision(first),
                "status": data.get("status"), "overview": data.get("summary") or "",
                "seasons": [], "season_count": None, "fetched_at": fetched, "identity_confirmed": False,
                "aliases": [], "aliases_checked": False}

    def get_series(self, series_id) -> dict:
        series_id = _source_id(series_id)
        data = self._get(f"/shows/{series_id}")
        if not isinstance(data, dict) or data.get("id") != series_id:
            raise MetadataError(self.source, "unexpected_series_identity")
        seasons = self._get(f"/shows/{series_id}/seasons")
        if not isinstance(seasons, list):
            raise MetadataError(self.source, "unexpected_seasons_response")
        result = self._series(data, self._fetched_at())
        result["seasons"] = [{"season": item.get("number"), "air_date": item.get("premiereDate"),
                              "episode_count": item.get("episodeOrder"), "source_id": item.get("id")}
                             for item in seasons if isinstance(item, dict)]
        result["season_count"] = len(result["seasons"])
        self._add_aliases(result)
        return result

    def get_external_ids(self, series_id) -> dict:
        series_id = _source_id(series_id)
        data = self._get(f"/shows/{series_id}")
        if not isinstance(data, dict) or data.get("id") != series_id:
            raise MetadataError(self.source, "unexpected_series_identity")
        return {"source": self.source, "series_id": series_id,
                **_external_ids(data.get("externals")), "fetched_at": self._fetched_at()}

    def get_season(self, series_id, season) -> list[dict]:
        series_id = _source_id(series_id)
        season = _integer(season, 1, 99)
        if season is None:
            raise ValueError("season must be explicitly selected within 1..99; year-based numbering requires review")
        data = self._get(f"/shows/{series_id}/episodes", {"specials": 1})
        if not isinstance(data, list):
            raise MetadataError(self.source, "unexpected_response_shape")
        fetched, episodes = self._fetched_at(), []
        for item in data:
            if not isinstance(item, dict) or item.get("season") != season:
                continue
            air_date, air_at = item.get("airdate") or None, item.get("airstamp") or None
            precision = _date_precision(air_date)
            if item.get("airtime") and air_at:
                try:
                    _aware_datetime(air_at, allow_date=False)
                    precision = "datetime"
                except (TypeError, ValueError):
                    air_at = None
            else:
                air_at = None
            episodes.append({"season": season, "episode": item.get("number"), "title": item.get("name") or "",
                             "air_date": air_date, "air_at": air_at, "source": self.source,
                             "source_id": item.get("id"), "series_id": series_id, "fetched_at": fetched,
                             "date_precision": precision, "airing_status": "expected", "is_expected": True,
                             "is_special": item.get("type") not in {None, "regular"}})
        return episodes


def _zone(name):
    try:
        return ZoneInfo(name) if isinstance(name, str) and name else SHANGHAI
    except (ValueError, KeyError):
        return SHANGHAI


class TraktProvider(_Provider):
    """Public Trakt reads with a Client ID only; no user OAuth.

    Trakt supplements a bound TMDB schedule. It never selects an identity and
    its broadcast instants are expected schedules, not release confirmations.
    """
    source = "trakt"
    base_url = "https://api.trakt.tv"

    def __init__(self, *, client_id, opener=None, now=None):
        super().__init__(opener=opener, now=now)
        self._client_id = client_id.strip() if isinstance(client_id, str) else ""
        if not self._client_id:
            raise ValueError("Trakt requires a Client ID")

    @classmethod
    def from_credentials_file(cls, path, **kwargs):
        """Read only the explicitly supplied private JSON file; no discovery."""
        return cls(client_id=_private_credentials(path, "Trakt").get("client_id"), **kwargs)

    def _request(self, path, params=None):
        return self._get(path, params, {"Content-Type": "application/json", "trakt-api-key": self._client_id,
                                        "trakt-api-version": "2"})

    def find_by_tmdb(self, tmdb_id) -> list[dict]:
        tmdb_id = _source_id(tmdb_id)
        data = self._request(f"/search/tmdb/{tmdb_id}", {"type": "show"})
        if not isinstance(data, list):
            raise MetadataError(self.source, "unexpected_external_search_response")
        fetched, rows = self._fetched_at(), []
        for item in data:
            show = item.get("show") if isinstance(item, dict) and item.get("type") == "show" else None
            ids = show.get("ids") if isinstance(show, dict) else None
            if not isinstance(ids, dict) or _integer(ids.get("trakt"), 1, 2**63 - 1) is None:
                continue
            rows.append({"id": ids["trakt"], "source_id": ids["trakt"], "source": self.source,
                         "title": show.get("title") or "", "year": _integer(show.get("year"), 1800, 2999),
                         "tmdb_id": _integer(ids.get("tmdb"), 1, 2**63 - 1), "fetched_at": fetched})
        return rows

    def get_series(self, series_id) -> dict:
        series_id = _source_id(series_id)
        data = self._request(f"/shows/{series_id}", {"extended": "full"})
        ids = data.get("ids") if isinstance(data, dict) else None
        if not isinstance(ids, dict) or ids.get("trakt") != series_id:
            raise MetadataError(self.source, "unexpected_series_identity")
        airs = data.get("airs") if isinstance(data.get("airs"), dict) else {}
        return {"id": series_id, "source_id": series_id, "source": self.source,
                "title": data.get("title") or "", "year": _integer(data.get("year"), 1800, 2999),
                "status": data.get("status"),
                "airs": {key: airs.get(key) if isinstance(airs.get(key), str) else None
                         for key in ("day", "time", "timezone")},
                "tmdb_id": _integer(ids.get("tmdb"), 1, 2**63 - 1),
                **_external_ids({"imdb_id": ids.get("imdb"), "tvdb_id": ids.get("tvdb")}),
                "fetched_at": self._fetched_at()}

    def get_season(self, series_id, season, timezone_name="Asia/Shanghai") -> list[dict]:
        series_id = _source_id(series_id)
        season = _integer(season, 1, 99)
        if season is None:
            raise ValueError("season must be explicitly selected within 1..99")
        data = self._request(f"/shows/{series_id}/seasons/{season}", {"extended": "full"})
        if not isinstance(data, list):
            raise MetadataError(self.source, "unexpected_season_response")
        zone, fetched, episodes = _zone(timezone_name), self._fetched_at(), []
        for item in data:
            if not isinstance(item, dict) or item.get("season") != season:
                continue
            air_at = air_date = None
            try:
                stamp = _aware_datetime(item.get("first_aired"), allow_date=False)
                air_at = stamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                air_date = stamp.astimezone(zone).date().isoformat()
            except (TypeError, ValueError):
                pass
            ids = item.get("ids") if isinstance(item.get("ids"), dict) else {}
            episodes.append({"season": season, "episode": item.get("number"), "title": item.get("title") or "",
                             "air_date": air_date, "air_at": air_at, "source": self.source,
                             "source_id": ids.get("trakt"), "series_id": series_id, "fetched_at": fetched,
                             "date_precision": "datetime" if air_at else "unknown",
                             "airing_status": "expected", "is_expected": True})
        return episodes
