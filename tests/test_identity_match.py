"""TMDB exact identity decisions use mocked catalogs, never drive writes."""
from copy import deepcopy
from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from media_helper.catalog import MetadataError, TMDBProvider
from media_helper.identity_match import MatchedMetadata, parse_tmdb_id, resolve_exact
from media_helper.profiles import ProfileStore


def identity(identifier=7, **changes):
    return {"id": identifier, "source_id": identifier, "source": "tmdb", "kind": "tv",
            "title": "示例剧", "original_title": "Example Show", "year": 2026,
            "aliases": ["示例电视剧"], "external_ids": {"imdb_id": "tt1234567", "tvdb_id": 55}, **changes}


class Catalog:
    source = "tmdb"

    def __init__(self, rows=None, *, complete=True):
        self.rows = deepcopy(rows if rows is not None else [identity()])
        self.details = {row["id"]: deepcopy(row) for row in self.rows}
        self.complete, self.calls, self.failure = complete, [], None
        self.found = {"imdb_id": [identity()], "tvdb_id": [identity()]}

    def search_identity(self, kind, query, year):
        self.calls.append(("search", kind, query, year))
        if self.failure:
            raise self.failure
        return {"results": deepcopy(self.rows), "complete": self.complete}

    def find_identity(self, kind, identifier, external_source):
        self.calls.append(("find", kind, identifier, external_source))
        return deepcopy(self.found[external_source])

    def get_identity(self, kind, identifier):
        self.calls.append(("detail", kind, identifier))
        if self.failure:
            raise self.failure
        return deepcopy(self.details[identifier])

    def get_series(self, identifier):
        return self.get_identity("tv", int(identifier))

    def get_external_ids(self, identifier):
        return deepcopy(self.details[int(identifier)]["external_ids"])

    def get_season(self, identifier, season):
        return [{"season": season, "episode": 1}]


class ExactScenarios(unittest.TestCase):
    def test_search_requires_exact_title_year_and_live_details(self):
        catalog = Catalog([identity(2, title="近似示例剧"), identity(7)])
        report = resolve_exact(catalog, title="示例剧", year=2026)
        self.assertEqual(report["status"], "MATCHED")
        self.assertEqual(report["identity"]["id"], 7)
        self.assertEqual(len([row for row in catalog.calls if row[0] == "detail"]), 2)
        self.assertTrue(report["search_complete"])

    def test_same_name_same_year_multiple_id_is_not_unique(self):
        report = resolve_exact(Catalog([identity(), identity(8)]), title="示例剧", year=2026)
        self.assertEqual(report["status"], "NEEDS_INPUT")
        self.assertEqual(report["reason"], "identity_not_unique")
        self.assertIsNone(report["identity"])

    def test_alternative_name_only_from_same_id_details_can_match(self):
        catalog = Catalog([identity(title="别的显示名", aliases=[])])
        catalog.details[7]["aliases"] = ["示例剧"]
        report = resolve_exact(catalog, title="示例剧", year=2026)
        self.assertEqual(report["status"], "MATCHED")
        self.assertEqual(report["identity"]["aliases"], ["示例剧"])

    def test_title_similarity_never_matches(self):
        report = resolve_exact(Catalog([identity(title="示例剧第二季", original_title="Other", aliases=[])]), title="示例剧", year=2026)
        self.assertEqual(report["reason"], "identity_not_matched")

    def test_search_rank_cannot_override_wrong_year(self):
        report = resolve_exact(Catalog([identity(year=2025)]), title="示例剧", year=2026)
        self.assertEqual(report["status"], "NEEDS_INPUT")
        self.assertEqual(report["candidates"][0]["conflicts"][0]["code"], "release_year_mismatch")

    def test_explicit_id_and_public_url_read_unique_details_without_search(self):
        for value in (7, "0007", "https://www.themoviedb.org/tv/7", "https://www.themoviedb.org/tv/7-example-show"):
            catalog = Catalog()
            report = resolve_exact(catalog, tmdb_id=value)
            self.assertEqual(report["status"], "MATCHED", value)
            self.assertEqual(catalog.calls, [("detail", "tv", 7)])

    def test_explicit_url_wrong_kind_or_secret_never_becomes_request(self):
        for value in ("https://www.themoviedb.org/movie/7", "https://www.themoviedb.org/tv/7?api_key=fixture", "https://www.themoviedb.org/tv/7#token", "https://www.themoviedb.org.evil/tv/7", "http://www.themoviedb.org/tv/7", "https://user@www.themoviedb.org/tv/7", True):
            catalog = Catalog()
            report = resolve_exact(catalog, tmdb_id=value)
            self.assertEqual(report["status"], "NEEDS_INPUT")
            self.assertEqual(catalog.calls, [])
            self.assertNotIn("fixture", json.dumps(report))

    def test_explicit_id_with_conflicting_title_cannot_silently_win(self):
        report = resolve_exact(Catalog(), title="另一部剧", tmdb_id=7)
        self.assertEqual(report["reason"], "explicit_identity_conflict")
        self.assertEqual(report["conflicts"][0]["code"], "exact_title_or_confirmed_alias_not_matched")

    def test_caller_confirmed_alias_can_crosscheck_original_name(self):
        report = resolve_exact(Catalog(), title="本地已确认名称", aliases=["Example Show"], year=2026, tmdb_id=7)
        self.assertEqual(report["status"], "MATCHED")

    def test_search_without_release_year_requires_id(self):
        catalog = Catalog()
        report = resolve_exact(catalog, title="示例剧")
        self.assertEqual(report["reason"], "release_year_or_explicit_id_required")
        self.assertEqual(catalog.calls, [])

    def test_incomplete_search_does_not_manufacture_unique_id(self):
        catalog = Catalog(complete=False)
        report = resolve_exact(catalog, title="示例剧", year=2026)
        self.assertEqual(report["reason"], "identity_search_incomplete")
        self.assertFalse(report["search_complete"])
        self.assertFalse(any(row[0] == "detail" for row in catalog.calls))

    def test_detail_limit_does_not_choose_first_twenty(self):
        catalog = Catalog([identity(index) for index in range(1, 22)])
        report = resolve_exact(catalog, title="示例剧", year=2026)
        self.assertEqual(report["reason"], "too_many_candidates_require_explicit_id")
        self.assertFalse(any(row[0] == "detail" for row in catalog.calls))

    def test_external_ids_find_crosschecks_both_fields(self):
        catalog = Catalog()
        report = resolve_exact(catalog, external_ids={"imdb_id": "TT1234567", "tvdb_id": "55"})
        self.assertEqual(report["status"], "MATCHED")
        self.assertEqual(len([row for row in catalog.calls if row[0] == "find"]), 2)
        self.assertFalse(any(row[0] == "search" for row in catalog.calls))

    def test_conflicting_external_find_results_stop_before_details(self):
        catalog = Catalog()
        catalog.found["tvdb_id"] = [identity(8)]
        report = resolve_exact(catalog, external_ids={"imdb_id": "tt1234567", "tvdb_id": 55})
        self.assertEqual(report["reason"], "external_identity_conflict")
        self.assertFalse(any(row[0] == "detail" for row in catalog.calls))

    def test_one_matching_external_id_does_not_override_another_conflict(self):
        report = resolve_exact(Catalog(), tmdb_id=7, external_ids={"imdb_id": "tt1234567", "tvdb_id": 56})
        self.assertEqual(report["reason"], "explicit_identity_conflict")
        self.assertEqual(report["conflicts"][0]["field"], "tvdb_id")

    def test_missing_requested_external_id_is_unverified(self):
        report = resolve_exact(Catalog([identity(external_ids={})]), tmdb_id=7, external_ids={"imdb_id": "tt1234567"})
        self.assertEqual(report["conflicts"][0]["code"], "external_id_unverified")

    def test_remote_id_or_type_mismatch_is_conflict(self):
        for changes in ({"id": 8}, {"kind": "movie"}, {"source": "tvmaze"}):
            catalog = Catalog()
            catalog.details[7].update(changes)
            report = resolve_exact(catalog, tmdb_id=7)
            self.assertEqual(report["reason"], "explicit_identity_conflict")

    def test_movie_namespace_uses_movie_details_and_url(self):
        report = resolve_exact(Catalog([identity(kind="movie")]), kind="movie", tmdb_id="https://www.themoviedb.org/movie/7")
        self.assertEqual(report["status"], "MATCHED")
        self.assertEqual(report["evidence"][-1]["url"], "https://www.themoviedb.org/movie/7")

    def test_metadata_failures_are_structured_without_credentials(self):
        for status, expected in ((401, "NEEDS_AUTH"), (403, "NEEDS_AUTH"), (429, "RISK_CONTROL"), (503, "ERROR")):
            catalog = Catalog()
            catalog.failure = MetadataError("tmdb", "http_error", status=status)
            report = resolve_exact(catalog, tmdb_id=7)
            self.assertEqual(report["status"], expected)
            self.assertEqual(report["metadata_error"]["status"], status)

    def test_wrapper_rechecks_same_id_aliases_without_changing_identity(self):
        catalog = Catalog()
        matched = resolve_exact(catalog, tmdb_id=7)
        wrapped = MatchedMetadata(catalog, matched)
        self.assertEqual(wrapped.source, "tmdb")
        self.assertEqual(wrapped.get_series("7")["aliases"], ["示例电视剧"])
        self.assertEqual(wrapped.get_season(7, 1), [{"season": 1, "episode": 1}])
        catalog.details[7]["year"] = 2025
        with self.assertRaisesRegex(MetadataError, "matched_identity_changed"):
            wrapped.get_series(7)

    def test_wrapper_does_not_reuse_stale_external_ids(self):
        catalog = Catalog()
        wrapped = MatchedMetadata(catalog, resolve_exact(catalog, tmdb_id=7))
        catalog.details[7]["external_ids"]["tvdb_id"] = 99
        with self.assertRaisesRegex(MetadataError, "matched_external_identity_changed"):
            wrapped.get_series(7)

    def test_invalid_arguments_are_not_accepted_as_weak_queries(self):
        for values in ({"year": True}, {"year": 1799}, {"external_ids": {"imdb_id": "not-id"}}, {"external_ids": {"api_key": "fixture"}}, {"kind": "person"}, {"title": 7}, {"aliases": "示例剧"}):
            report = resolve_exact(Catalog(), **values)
            self.assertEqual(report["status"], "NEEDS_INPUT")
            self.assertNotIn("fixture", json.dumps(report))


class Responses:
    def __init__(self, *rows):
        self.rows, self.requests = list(rows), []

    def __call__(self, request, timeout):
        self.requests.append(request)
        return BytesIO(json.dumps(self.rows.pop(0)).encode())


class TMDBIdentityClient(unittest.TestCase):
    def provider(self, *rows):
        self.responses = Responses(*rows)
        return TMDBProvider(read_access_token="fixture-secret", opener=self.responses,
                            now=lambda: datetime(2026, 10, 17, tzinfo=timezone.utc))

    def test_appended_aliases_and_external_ids_are_normalized_and_scoped(self):
        data = {"id": 7, "name": "示例剧", "original_name": "Example", "first_air_date": "2026-01-01",
                "alternative_titles": {"results": [{"title": "示例别名"}]},
                "translations": {"translations": [{"iso_639_1": "en", "data": {"name": "Another Title"}}, {"iso_639_1": "zh", "data": {"name": "示例剧中文"}}, {"iso_639_1": "fr", "data": {"name": "Unverified language omitted"}}]},
                "external_ids": {"id": 7, "imdb_id": "TT1234567", "tvdb_id": "55", "token": "never-return"}}
        row = self.provider(data).get_identity("tv", 7)
        self.assertEqual(row["aliases"], ["示例别名", "Another Title", "示例剧中文"])
        self.assertTrue(row["aliases_checked"])
        self.assertEqual(row["external_ids"], {"imdb_id": "tt1234567", "tvdb_id": 55})
        self.assertTrue(all(item["tmdb_id"] == 7 for item in row["alias_evidence"]))
        self.assertNotIn("never-return", json.dumps(row))
        self.assertNotIn("fixture-secret", json.dumps(row))
        self.assertEqual(parse_qs(urlsplit(self.responses.requests[0].full_url).query)["append_to_response"], ["alternative_titles,translations,external_ids"])

    def test_appended_external_id_wrong_work_is_rejected(self):
        with self.assertRaises(MetadataError):
            self.provider({"id": 7, "external_ids": {"id": 8}}).get_identity("tv", 7)

    def test_movie_details_use_movie_date_and_title_fields(self):
        row = self.provider({"id": 7, "title": "电影", "original_title": "Film", "release_date": "2026-01-01"}).get_movie(7)
        self.assertEqual((row["kind"], row["title"], row["year"]), ("movie", "电影", 2026))

    def test_search_collects_all_pages_before_uniqueness(self):
        provider = self.provider({"page": 1, "total_pages": 2, "results": [{"id": 7, "name": "示例剧"}]},
                                 {"page": 2, "total_pages": 2, "results": [{"id": 8, "name": "示例剧"}]})
        result = provider.search_identity("tv", "示例剧", 2026)
        self.assertTrue(result["complete"])
        self.assertEqual([row["id"] for row in result["results"]], [7, 8])
        self.assertEqual(len(self.responses.requests), 2)

    def test_pagination_limit_or_missing_total_is_incomplete(self):
        for row in ({"page": 1, "total_pages": 6, "results": [{"id": 7}]}, {"results": [{"id": 7}]}):
            result = self.provider(row).search_identity("tv", "示例剧", 2026, max_pages=1)
            self.assertFalse(result["complete"])

    def test_pagination_change_during_search_stays_incomplete(self):
        provider = self.provider({"page": 1, "total_pages": 2, "results": [{"id": 7}]},
                                 {"page": 2, "total_pages": 1, "results": [{"id": 8}]})
        self.assertFalse(provider.search_identity("tv", "示例剧", 2026)["complete"])

    def test_public_external_find_is_namespace_scoped(self):
        provider = self.provider({"tv_results": [{"id": 7}], "movie_results": [{"id": 8}]})
        rows = provider.find_identity("tv", "tt1234567", "imdb_id")
        self.assertEqual([row["id"] for row in rows], [7])
        query = parse_qs(urlsplit(self.responses.requests[0].full_url).query)
        self.assertEqual(query["external_source"], ["imdb_id"])


class ProfileTMDBLink(unittest.TestCase):
    def test_cross_link_retains_tvmaze_primary_and_rechecks_external_ids(self):
        class Primary:
            source = "tvmaze"
            def get_series(self, identifier):
                return {"id": 9, "title": "Example Show", "year": 2026}
            def get_external_ids(self, identifier):
                return {"imdb_id": "tt1234567", "tvdb_id": 55}
        with tempfile.TemporaryDirectory() as directory:
            store = ProfileStore(Path(directory) / "profiles.json")
            profile = store.bind(metadata=Primary(), source="tvmaze", series_id=9, year=2026, season=1,
                                 title="示例剧", canonical_title="Example Show", aliases=["示例剧"],
                                 evidence_urls=["https://example.org/show"])
            report = store.bind_tmdb(profile, primary=Primary(), tmdb=Catalog(), tmdb_id=7)
            self.assertEqual(report["status"], "MATCHED")
            saved = store.all()["tvmaze:9:2026"]
            self.assertEqual((saved["metadata_source"], saved["series_id"], saved["profile_key"]), ("tvmaze", "9", "tvmaze:9:2026"))
            self.assertEqual(saved["tmdb_link"]["series_id"], "7")
            catalog = Catalog([identity(external_ids={"imdb_id": "tt1234567", "tvdb_id": 99})])
            self.assertEqual(store.bind_tmdb(profile, primary=Primary(), tmdb=catalog, tmdb_id=7)["status"], "NEEDS_INPUT")
            self.assertEqual(store.all()["tvmaze:9:2026"]["tmdb_link"], saved["tmdb_link"])


if __name__ == "__main__":
    unittest.main()
