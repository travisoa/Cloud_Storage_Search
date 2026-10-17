"""Locally confirmed series identities; each alias remains tied to one live ID."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import tempfile
import unicodedata
from urllib.parse import parse_qsl, urlsplit

from .journal import safe_record


def normalize(value):
    return re.sub(r"[\s._\-·:：()（）]", "", unicodedata.normalize("NFC", str(value))).casefold()


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    if path.is_symlink():
        raise ValueError("本地记录不能是符号链接")
    serialized = json.dumps(safe_record(value), ensure_ascii=False, indent=2, allow_nan=False)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(serialized + "\n")
            stream.flush()
            import os
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _title(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 160
            or any(c in value for c in '/\\<>:"|?*')
            or any(unicodedata.category(c) in {"Cc", "Cs"} for c in value)):
        raise ValueError("作品名称必须是安全的单个名称")
    return unicodedata.normalize("NFC", value.strip())


def _identity_text(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 240
            or any(unicodedata.category(c) in {"Cc", "Cs"} for c in value)):
        raise ValueError("元数据名称或别名必须是有效文本")
    return unicodedata.normalize("NFC", value.strip())


def evidence_url(value):
    parts = urlsplit(value)
    secret = re.compile(r"cookie|auth|token|password|passcode|secret|credential|api[-_]?key|signature|(?:^|_)(?:pwd|key|sig)(?:$|_)", re.I)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.fragment or any(secret.search(key) for key, _ in parse_qsl(parts.query, keep_blank_values=True))):
        raise ValueError("身份依据必须为不含凭据或片段的 HTTPS 链接")
    return value


def quality_preferences(values):
    result = []
    for value in values or []:
        quality = {"4k": "2160p", "uhd": "2160p"}.get(str(value).casefold(), str(value).casefold())
        if quality not in {"2160p", "1080p", "720p", "480p"}:
            raise ValueError("画质偏好支持 2160p、1080p、720p、480p")
        if quality not in result:
            result.append(quality)
    return result


def identity_key(source, series_id, year):
    if source not in {"tmdb", "tvmaze"}:
        raise ValueError("作品身份需要明确元数据来源")
    identifier = str(series_id).strip()
    if not identifier.isdecimal() or int(identifier) < 1:
        raise ValueError("作品 ID 必须为正整数")
    if type(year) is not int or not 1800 <= year <= 2999:
        raise ValueError("作品年份无效")
    return f"{source}:{int(identifier)}:{year}"


def live_identity(series, source, series_id, year, canonical_title):
    """Aliases never validate the canonical name of an externally supplied ID."""
    key = identity_key(source, series_id, year)
    actual_id = series.get("id", series.get("source_id"))
    canonical = series.get("name") or series.get("title")
    if (str(actual_id).lstrip("0") != str(series_id).lstrip("0")
            or str(series.get("year")) != str(year)
            or not isinstance(canonical, str)
            or unicodedata.normalize("NFC", canonical.strip()) != unicodedata.normalize("NFC", canonical_title.strip())):
        raise ValueError("实时作品 ID、年份或原始名称与确认信息不一致")
    return key


class ProfileStore:
    def __init__(self, path):
        self.path = Path(path)

    def all(self):
        if not self.path.exists():
            return {}
        if self.path.is_symlink():
            raise ValueError("作品身份记录不能是符号链接")
        data = json.loads(self.path.read_text())
        if data.get("schema_version") != 1 or not isinstance(data.get("series"), dict):
            raise ValueError("作品身份记录格式无效")
        return data["series"]

    def bind(self, *, metadata, source, series_id, year, season, title, canonical_title,
             aliases=(), evidence_urls=(), quality_preference=()):
        title, canonical_title = _title(title), _identity_text(canonical_title)
        aliases = list(dict.fromkeys([title, *[_identity_text(value) for value in aliases]]))
        urls = list(dict.fromkeys(evidence_url(value) for value in evidence_urls))
        if not urls:
            raise ValueError("确认中文别名需要至少一个 HTTPS 身份依据链接")
        if type(season) is not int or not 1 <= season <= 99:
            raise ValueError("季号必须为 1–99")
        series = metadata.get_series(series_id)
        key = live_identity(series, source, series_id, year, canonical_title)
        items = self.all()
        previous = items.get(key, {})
        if previous and previous["canonical_title"] != canonical_title:
            raise ValueError("同一作品的原始名称发生变化，需要核对现有身份记录")
        tmdb_link = deepcopy(previous.get('tmdb_link'))
        if tmdb_link is not None:
            from .catalog import _external_ids
            from .identity_match import parse_tmdb_id
            if (not isinstance(tmdb_link, dict) or tmdb_link.get('source') != 'tmdb'
                    or tmdb_link.get('year') != year):
                raise ValueError("现有 TMDB 关联的身份无效，需要重新确认")
            linked_id = parse_tmdb_id(tmdb_link.get('series_id'), 'tv')
            if source == 'tmdb':
                if linked_id != int(str(series_id)):
                    raise ValueError("现有 TMDB 关联与主作品 ID 不一致")
            else:
                if not callable(getattr(metadata, 'get_external_ids', None)):
                    raise ValueError("无法核验主作品公共 ID，未保留或改写现有 TMDB 关联")
                current_ids = _external_ids(metadata.get_external_ids(series_id))
                linked_ids = _external_ids(tmdb_link.get('external_ids'))
                common = [name for name in current_ids if current_ids[name] is not None and linked_ids[name] is not None]
                if not common or any(current_ids[name] != linked_ids[name] for name in common):
                    raise ValueError("主作品公共 ID 与现有 TMDB 关联不一致，需要重新确认")
        preferences = quality_preferences(quality_preference) or previous.get("quality_preference", [])
        record = {"profile_key": key, "metadata_source": source, "series_id": str(int(str(series_id))),
                  "year": year, "season": season, "title": title, "canonical_title": canonical_title,
                  "aliases": list(dict.fromkeys([*previous.get("aliases", []), *aliases])),
                  "evidence_urls": list(dict.fromkeys([*previous.get("evidence_urls", []), *urls])),
                  "verified_at": datetime.now(timezone.utc).isoformat()}
        if preferences:
            record["quality_preference"] = preferences
        if tmdb_link is not None:
            # This keeps the verified association, not refreshed TMDB evidence.
            # watch still cross-checks both live catalogs before using it.
            record['tmdb_link'] = tmdb_link
        items[key] = record
        private_json(self.path, {"schema_version": 1, "series": items})
        return record

    def match(self, request):
        matches = []
        for item in self.all().values():
            names = [item["title"], item["canonical_title"], *item.get("aliases", [])]
            if not any(normalize(request["title"]) == normalize(name) for name in names):
                continue
            if request.get("year") and str(request["year"]) != str(item["year"]):
                continue
            if request.get("series_id") and str(request["series_id"]).lstrip("0") != str(item["series_id"]).lstrip("0"):
                continue
            if request.get("metadata_source") not in {None, "auto", item["metadata_source"]}:
                continue
            matches.append(item)
        if len(matches) > 1:
            raise ValueError("本地存在多个同名作品，请明确年份或作品 ID")
        return deepcopy(matches[0]) if matches else None

    def remember_quality(self, profile, preferences):
        preferences = quality_preferences(preferences)
        items = self.all()
        key = profile["profile_key"]
        if items.get(key, {}).get("canonical_title") != profile["canonical_title"]:
            raise ValueError("作品身份记录变化，未保存本剧画质偏好")
        items[key]["quality_preference"] = preferences
        private_json(self.path, {"schema_version": 1, "series": items})

    def bind_tmdb(self, profile, *, primary, tmdb, tmdb_id):
        """Add a cross-checked TMDB link without changing a primary job identity.

        A local alias is not enough to cross-link providers: the live primary
        and TMDB records must also share every supplied public external ID.
        """
        from .identity_match import resolve_exact
        items = self.all()
        key = profile.get("profile_key")
        current = items.get(key)
        if not current or current.get("canonical_title") != profile.get("canonical_title"):
            raise ValueError("作品身份记录变化，未保存 TMDB 关联")
        if getattr(primary, "source", None) != current["metadata_source"]:
            raise ValueError("TMDB 关联的主元数据来源与作品身份不一致")
        details = primary.get_series(current["series_id"])
        live_identity(details, current["metadata_source"], current["series_id"], current["year"], current["canonical_title"])
        from .catalog import _external_ids
        public_ids = _external_ids(primary.get_external_ids(current["series_id"]))
        public_ids = {name: value for name, value in public_ids.items() if value is not None}
        if current["metadata_source"] == "tmdb":
            # A TMDB primary already carries the exact ID; it is never rebound.
            from .identity_match import parse_tmdb_id
            if parse_tmdb_id(tmdb_id, "tv") != int(current["series_id"]):
                raise ValueError("现有 TMDB 主作品 ID 不允许改绑")
        elif not public_ids:
            raise ValueError("主元数据缺少 IMDb 或 TVDB 公共 ID，无法交叉确认 TMDB 关联")
        matched = resolve_exact(tmdb, title=current["canonical_title"], year=current["year"],
                                kind="tv", tmdb_id=tmdb_id, external_ids=public_ids,
                                aliases=current.get("aliases", []))
        if matched["status"] != "MATCHED":
            return matched
        identity = matched["identity"]
        link = {"source": "tmdb", "series_id": str(identity["id"]), "year": identity["year"],
                "title": identity.get("title", ""), "external_ids": identity["external_ids"],
                "evidence_url": f"https://www.themoviedb.org/tv/{identity['id']}",
                "verified_at": datetime.now(timezone.utc).isoformat(), "basis": "live_primary_and_public_external_ids"}
        current["tmdb_link"] = link
        items[key] = current
        private_json(self.path, {"schema_version": 1, "series": items})
        return {**matched, "profile_key": key, "tmdb_link": deepcopy(link)}


class ProfileMetadata:
    """Add confirmed aliases only after rechecking the exact remote identity."""
    def __init__(self, provider, profile):
        self.provider, self.profile = provider, profile

    def __getattr__(self, key):
        return getattr(self.provider, key)

    def get_series(self, identifier):
        series = self.provider.get_series(identifier)
        profile = self.profile
        if str(identifier).lstrip("0") == str(profile["series_id"]).lstrip("0"):
            live_identity(series, profile["metadata_source"], profile["series_id"], profile["year"], profile["canonical_title"])
            series = deepcopy(series)
            series["aliases"] = list(dict.fromkeys([*(series.get("aliases") or []), *profile["aliases"]]))
        return series
