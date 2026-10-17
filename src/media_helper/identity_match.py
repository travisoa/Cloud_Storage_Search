"""Read-only TMDB identities with explicit conflicts and bounded search.

An exact ID names a work, never a movie/TV namespace interchangeably. Search
rank and similarity do not establish identity; appended names remain scoped to
their verified detail ID. ``aliases`` are already-confirmed caller aliases.
"""
from __future__ import annotations

from copy import deepcopy
import re
import unicodedata
from urllib.parse import urlsplit

from .catalog import MetadataError, _external_ids


def _number(value, minimum=1, maximum=2**63 - 1):
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).isdecimal():
        return None
    value = int(value)
    return value if minimum <= value <= maximum else None


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 240 and not any(unicodedata.category(char) in {"Cc", "Cs"} for char in value)


def _normal(value):
    return re.sub(r"[\s._\-·:：()（）]", "", unicodedata.normalize("NFKC", value)).casefold()


def parse_tmdb_id(value, kind="tv"):
    """Accept a positive ID or the public HTTPS work page, without secrets."""
    if kind not in {"tv", "movie"}:
        raise ValueError("TMDB 类型必须为 tv 或 movie")
    identifier = _number(value)
    if identifier:
        return identifier
    if not isinstance(value, str):
        raise ValueError("TMDB ID 或链接无效")
    try:
        parts = urlsplit(value.strip())
        valid_host = parts.hostname in {"www.themoviedb.org", "themoviedb.org"}
        if (parts.scheme != "https" or not valid_host or parts.username or parts.password
                or parts.query or parts.fragment or parts.port is not None):
            raise ValueError
        match = re.fullmatch(r"/(tv|movie)/([1-9]\d*)(?:-[^/?#]+)?/?", parts.path)
        if not match or match[1] != kind or not _number(match[2]):
            raise ValueError
        return int(match[2])
    except (TypeError, ValueError):
        raise ValueError("TMDB ID 或链接类型无效") from None


def _summary(item):
    result = {key: deepcopy(item[key]) for key in ("id", "source_id", "source", "kind", "title", "original_title", "year", "fetched_at")
              if key in item and (key in {"id", "source_id", "year"} and _number(item[key]) or key not in {"id", "source_id", "year"} and _text(item[key]))}
    result["aliases"] = list(dict.fromkeys(value.strip() for value in item.get("aliases", []) if _text(value)))[:100]
    result["external_ids"] = _external_ids(item.get("external_ids", item))
    return result


def _names(item):
    return [value for value in [item.get("title"), item.get("original_title"), *(item.get("aliases") or [])] if _text(value)]


class MatchedMetadata:
    """Forward metadata calls while rechecking the exact matched ID's names.

    The enriched response is fetched again before workflow use. Previously
    observed aliases are never injected into a different or changed identity.
    Other primary providers, including stable TVmaze job IDs, are untouched.
    """
    def __init__(self, provider, match):
        identity = match.get("identity") if isinstance(match, dict) else None
        if (getattr(provider, "source", None) != "tmdb" or not isinstance(identity, dict)
                or match.get("status") != "MATCHED" or not _number(identity.get("id"))):
            raise ValueError("verified TMDB match required")
        self.provider, self.identity = provider, deepcopy(identity)

    def __getattr__(self, key):
        return getattr(self.provider, key)

    def _details(self, kind, identifier):
        if kind != self.identity.get("kind") or _number(identifier) != self.identity["id"]:
            getter = self.provider.get_series if kind == "tv" else self.provider.get_movie
            return getter(identifier)
        current = self.provider.get_identity(kind, _number(identifier))
        if (_number(current.get("id")) != self.identity["id"] or current.get("kind", kind) != kind
                or current.get("source", "tmdb") != "tmdb" or current.get("year") != self.identity.get("year")
                or current.get("title") != self.identity.get("title")):
            raise MetadataError("tmdb", "matched_identity_changed")
        expected_ids = self.identity.get("external_ids", {})
        actual_ids = _external_ids(current.get("external_ids", current))
        if any(value is not None and actual_ids.get(key) != value for key, value in expected_ids.items()):
            raise MetadataError("tmdb", "matched_external_identity_changed")
        return current

    def get_series(self, identifier):
        return self._details("tv", identifier)

    def get_movie(self, identifier):
        return self._details("movie", identifier)


def resolve_exact(provider, *, title="", year=None, kind="tv", tmdb_id=None, external_ids=None, aliases=()):
    """Return MATCHED only for one complete, conflict-free live identity.

    Result keys are stable: status, reason, identity, candidates, evidence,
    conflicts, search_complete. Network/auth failures have safe structured
    codes; arbitrary exception text or API payloads are never returned.
    """
    report = {"status": "NEEDS_INPUT", "reason": None, "identity": None, "candidates": [],
              "evidence": [], "conflicts": [], "search_complete": None}

    def finish(reason, status="NEEDS_INPUT"):
        report.update(status=status, reason=reason)
        return report

    if getattr(provider, "source", None) != "tmdb":
        return finish("tmdb_provider_required")
    if kind not in {"tv", "movie"}:
        return finish("invalid_media_kind")
    if not isinstance(title, str) or title and not _text(title) or not isinstance(aliases, (list, tuple)) or len(aliases) > 20 or any(not _text(value) for value in aliases):
        return finish("invalid_title_or_alias")
    expected_year = _number(year, 1800, 2999) if year is not None else None
    if year is not None and expected_year is None:
        return finish("invalid_release_year")
    if external_ids is not None and not isinstance(external_ids, dict):
        return finish("invalid_external_ids")
    expected_ids = _external_ids(external_ids or {})
    if external_ids and (set(external_ids) - {"imdb_id", "tvdb_id"}
                         or any(value is not None and expected_ids.get(key) is None for key, value in external_ids.items())):
        return finish("invalid_external_ids")
    names = list(dict.fromkeys(value.strip() for value in [title, *aliases] if _text(value)))
    explicit = tmdb_id is not None
    if not names and not explicit and not any(expected_ids.values()):
        return finish("title_or_explicit_id_required")
    try:
        if explicit:
            try:
                identifier = parse_tmdb_id(tmdb_id, kind)
            except ValueError:
                return finish("invalid_tmdb_id_or_type")
            identifiers = [identifier]
            report["search_complete"] = True
            report["evidence"].append({"basis": "explicit_tmdb_id", "kind": kind, "tmdb_id": identifier})
        elif any(expected_ids.values()) and callable(getattr(provider, "find_identity", None)):
            found_sets = []
            for key, value in expected_ids.items():
                if value is None:
                    continue
                rows = provider.find_identity(kind, value, key)
                if not isinstance(rows, list):
                    return finish("invalid_external_search_response", "ERROR")
                found = {_number(row.get("id", row.get("source_id"))) for row in rows if isinstance(row, dict)} - {None}
                found_sets.append(found)
                report["evidence"].append({"basis": "external_id_lookup", "field": key, "value": value, "candidate_ids": sorted(found)})
            identifiers = sorted(set.intersection(*found_sets)) if found_sets else []
            report["search_complete"] = True
            if found_sets and not identifiers:
                report["conflicts"].append({"code": "external_ids_do_not_resolve_to_one_work"})
                return finish("external_identity_conflict")
        else:
            if expected_year is None:
                return finish("release_year_or_explicit_id_required")
            if not callable(getattr(provider, "search_identity", None)):
                return finish("complete_identity_search_unavailable")
            identifiers = set()
            # Caller aliases are verified names, but arbitrary search rank is not.
            for name in names:
                result = provider.search_identity(kind, name, expected_year)
                if not isinstance(result, dict) or not isinstance(result.get("results"), list):
                    return finish("invalid_identity_search_response", "ERROR")
                report["search_complete"] = result.get("complete") is True
                report["candidates"].extend(_summary(row) for row in result["results"] if isinstance(row, dict))
                if not report["search_complete"]:
                    return finish("identity_search_incomplete")
                identifiers.update(_number(row.get("id", row.get("source_id"))) for row in result["results"] if isinstance(row, dict))
            identifiers = sorted(identifiers - {None})
        if not identifiers:
            return finish("identity_not_matched")
        # Detail every candidate, including potential alternative-name matches.
        # A bounded subset may not manufacture a unique identity.
        if len(identifiers) > 20:
            return finish("too_many_candidates_require_explicit_id")
        matched, details = [], []
        for identifier in identifiers:
            if callable(getattr(provider, "get_identity", None)):
                item = provider.get_identity(kind, identifier)
            else:
                getter = getattr(provider, "get_series" if kind == "tv" else "get_movie", None)
                if not callable(getter):
                    return finish("identity_details_unavailable", "ERROR")
                item = getter(identifier)
            if not isinstance(item, dict):
                return finish("invalid_identity_details", "ERROR")
            candidate = _summary(item)
            candidate["match_evidence"], candidate["conflicts"] = [], []
            actual_id = _number(item.get("id", item.get("source_id")))
            candidate["id"], candidate["source_id"], candidate["source"], candidate["kind"] = actual_id, actual_id, "tmdb", kind
            if actual_id != identifier or item.get("source", "tmdb") != "tmdb" or item.get("kind", kind) != kind:
                candidate["conflicts"].append({"code": "details_identity_or_type_changed"})
            if not _names(item):
                candidate["conflicts"].append({"code": "remote_title_unverified"})
            remote_year = _number(item.get("year"), 1800, 2999)
            if expected_year is not None and remote_year != expected_year:
                candidate["conflicts"].append({"code": "release_year_unverified" if remote_year is None else "release_year_mismatch", "expected": expected_year, "actual": remote_year})
            common_names = [name for name in names if any(_normal(name) == _normal(remote) for remote in _names(item))]
            if names and not common_names:
                candidate["conflicts"].append({"code": "exact_title_or_confirmed_alias_not_matched"})
            if common_names:
                candidate["match_evidence"].append({"basis": "exact_remote_title_or_id_scoped_alias", "names": common_names})
            actual_ids = _external_ids(item.get("external_ids", item))
            for key, value in expected_ids.items():
                if value is None:
                    continue
                if actual_ids[key] != value:
                    candidate["conflicts"].append({"code": "external_id_unverified" if actual_ids[key] is None else "external_id_mismatch", "field": key, "expected": value, "actual": actual_ids[key]})
                else:
                    candidate["match_evidence"].append({"basis": "same_public_external_id", "field": key, "value": value})
            details.append(candidate)
            if not candidate["conflicts"]:
                matched.append(candidate)
        report["candidates"] = details
        if explicit and details[0]["conflicts"]:
            report["conflicts"] = details[0]["conflicts"]
            return finish("explicit_identity_conflict")
        if len(matched) != 1:
            return finish("identity_not_unique" if matched else "identity_not_matched")
        identity = deepcopy(matched[0])
        identity.pop("conflicts", None)
        report["identity"] = identity
        report["evidence"].extend(identity.pop("match_evidence", []))
        report["evidence"].append({"basis": "live_tmdb_details", "url": f"https://www.themoviedb.org/{kind}/{identity['id']}", "tmdb_id": identity["id"], "kind": kind})
        return finish("unique_verified_tmdb_identity", "MATCHED")
    except MetadataError as error:
        report["metadata_error"] = {"source": error.source, "code": error.code, "status": error.status}
        status = "NEEDS_AUTH" if error.status in {401, 403} else "RISK_CONTROL" if error.status == 429 else "ERROR"
        return finish("metadata_request_failed", status)
    except (TypeError, ValueError):
        return finish("invalid_metadata_identity", "ERROR")
