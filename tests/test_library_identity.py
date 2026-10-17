from copy import deepcopy
import unittest

from media_helper.catalog import MetadataError
from media_helper.library_identity import MediaResolver


class Metadata:
    source = "tmdb"

    def __init__(self):
        self.tv = [{"id": 10, "source": "tmdb", "title": "示例剧", "original_title": "Example Show", "year": 2026}]
        self.movies = [{"id": 20, "source": "tmdb", "title": "示例电影", "original_title": "Example Movie", "year": 2025}]
        self.season = [{"season": 1, "episode": 1}, {"season": 1, "episode": 2}]
        self.calls = []
        self.error = None

    def search_tv(self, title, year):
        self.calls.append(("search_tv", title, year))
        if self.error:
            raise self.error
        return deepcopy(self.tv)

    def get_series(self, identifier):
        self.calls.append(("get_series", identifier))
        return deepcopy(next(row for row in self.tv if row["id"] == identifier))

    def get_season(self, identifier, season):
        self.calls.append(("get_season", identifier, season))
        return deepcopy(self.season)

    def search_movie(self, title, year):
        self.calls.append(("search_movie", title, year))
        return deepcopy(self.movies)

    def get_movie(self, identifier):
        self.calls.append(("get_movie", identifier))
        return deepcopy(next(row for row in self.movies if row["id"] == identifier))


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.metadata = Metadata()
        self.resolver = MediaResolver(self.metadata)

    def resolve(self, name, **context):
        return self.resolver({"fid": "file-1", "name": name, "size": 100}, context)

    def test_tv_requires_unique_exact_identity_and_real_episode(self):
        result = self.resolve("示例剧 (2026) S01E02 [2160p].mkv")
        self.assertEqual((result["status"], result["kind"], result["series_id"], result["season"], result["episode"]), ("MATCHED", "tv", "10", 1, 2))
        self.assertEqual(self.metadata.calls, [("search_tv", "示例剧", 2026), ("get_series", 10), ("get_season", 10, 1)])

    def test_filename_episode_and_ancestor_title_year(self):
        result = self.resolve("S01E01.mkv", ancestors=["影库", "电视剧", "示例剧 (2026)", "Season 01"])
        self.assertEqual(result["status"], "MATCHED")

    def test_path_can_supply_ancestor_context(self):
        self.assertEqual(self.resolve("S01E01.mkv", path="/电视剧/示例剧 (2026)/Season 01/S01E01.mkv")["status"], "MATCHED")

    def test_original_title_with_dotted_release_name(self):
        result = self.resolve("Example.Show.2026.S01E01.1080p.mkv")
        self.assertEqual(result["title"], "示例剧")

    def test_chinese_explicit_season_episode(self):
        result = self.resolve("示例剧 (2026) 第1季第2集.mkv")
        self.assertEqual((result["status"], result["season"], result["episode"]), ("MATCHED", 1, 2))

    def test_bare_numbers_do_not_become_episodes(self):
        result = self.resolve("01.mkv", ancestors=["示例剧 (2026)", "Season 01"])
        self.assertEqual(result["reason"], "explicit_season_and_episode_required")
        self.assertFalse(self.metadata.calls)

    def test_standalone_episode_never_infers_season(self):
        result = self.resolve("示例剧 (2026) E01.mkv", ancestors=["Season 01"])
        self.assertEqual(result["reason"], "explicit_season_and_episode_required")

    def test_multiple_episode_markers_require_review(self):
        result = self.resolve("示例剧 (2026) S01E01E02.mkv")
        self.assertEqual(result["reason"], "ambiguous_explicit_episode_markers")

    def test_ancestor_season_conflict_requires_review(self):
        result = self.resolve("示例剧 (2026) S02E01.mkv", ancestors=["Season 01"])
        self.assertEqual(result["reason"], "filename_and_directory_season_conflict")

    def test_year_conflict_requires_review(self):
        result = self.resolve("示例剧 (2026) S01E01.mkv", ancestors=["示例剧 (2025)"])
        self.assertEqual(result["reason"], "filename_and_directory_year_conflict")

    def test_year_is_required_even_when_one_title_match(self):
        self.assertEqual(self.resolve("示例剧 S01E01.mkv")["reason"], "title_and_release_year_required")
        self.assertFalse(self.metadata.calls)

    def test_multiple_exact_titles_are_not_selected_by_rank(self):
        self.metadata.tv.append({**self.metadata.tv[0], "id": 11})
        result = self.resolve("示例剧 (2026) S01E01.mkv")
        self.assertEqual(result["reason"], "identity_not_unique")
        self.assertEqual(len(result["candidates"]), 2)
        self.assertEqual(len(self.metadata.calls), 1)

    def test_near_title_and_missing_year_metadata_do_not_match(self):
        self.metadata.tv = [{**self.metadata.tv[0], "title": "示例剧之二"}, {**self.metadata.tv[0], "id": 11, "year": None}]
        self.assertEqual(self.resolve("示例剧 (2026) S01E01.mkv")["reason"], "identity_not_matched")

    def test_movie_also_requires_live_details(self):
        result = self.resolve("Example.Movie.2025.2160p.mkv")
        self.assertEqual((result["status"], result["kind"], result["series_id"], result["season"]), ("MATCHED", "movie", "20", None))
        self.assertEqual(self.metadata.calls[-1], ("get_movie", 20))

    def test_tv_category_does_not_turn_unmarked_video_into_movie(self):
        result = self.resolve("示例剧 (2026).mkv", ancestors=["电视剧"])
        self.assertEqual(result["reason"], "explicit_season_and_episode_required")

    def test_episode_must_exist_and_not_be_duplicate_or_special(self):
        for season in ([{"season": 1, "episode": 3}], [{"season": 1, "episode": 1}] * 2, [{"season": 1, "episode": 1, "is_special": True}]):
            with self.subTest(season=season):
                metadata = Metadata()
                metadata.season = season
                self.assertEqual(MediaResolver(metadata)({"name": "示例剧 (2026) S01E01.mkv"}, {})["reason"], "episode_not_uniquely_confirmed")

    def test_search_and_details_identity_disagreement(self):
        self.metadata.get_series = lambda identifier: {**self.metadata.tv[0], "year": 2025}
        self.assertEqual(self.resolve("示例剧 (2026) S01E01.mkv")["reason"], "metadata_details_identity_changed")

    def test_search_and_details_title_disagreement(self):
        self.metadata.get_series = lambda identifier: {**self.metadata.tv[0], "title": "别的剧", "original_title": "Other"}
        self.assertEqual(self.resolve("示例剧 (2026) S01E01.mkv")["reason"], "metadata_details_title_changed")

    def test_detail_provider_identity_is_checked(self):
        self.metadata.get_series = lambda identifier: {**self.metadata.tv[0], "source": "tvmaze"}
        self.assertEqual(self.resolve("示例剧 (2026) S01E01.mkv")["reason"], "metadata_details_identity_changed")

    def test_invalid_or_wrong_show_episode_payload_requires_review(self):
        for rows in (None, [{"season": 1, "episode": 1, "series_id": 999}], [{"season": 1, "episode": 1, "source": "tvmaze"}]):
            with self.subTest(rows=rows):
                metadata = Metadata()
                metadata.season = rows
                result = MediaResolver(metadata)({"name": "示例剧 (2026) S01E01.mkv"}, {})
                self.assertEqual(result["status"], "NEEDS_INPUT")

    def test_transport_failure_is_not_no_match(self):
        self.metadata.error = MetadataError("tmdb", "transport_or_invalid_json")
        result = self.resolve("示例剧 (2026) S01E01.mkv")
        self.assertEqual(result["reason"], "metadata_unavailable")
        self.assertEqual(result["metadata_error"]["code"], "transport_or_invalid_json")

    def test_auth_and_rate_limit_escape_for_caller_to_stop_scan(self):
        for status in (401, 403, 429):
            with self.subTest(status=status):
                self.metadata.error = MetadataError("tmdb", "http_error", status=status)
                with self.assertRaises(MetadataError):
                    self.resolve("示例剧 (2026) S01E01.mkv")

    def test_caches_are_local_to_one_resolver_instance(self):
        for episode in (1, 2):
            self.resolve(f"示例剧 (2026) S01E{episode:02d}.mkv")
        self.assertEqual(len(self.metadata.calls), 3)
        MediaResolver(self.metadata)({"name": "示例剧 (2026) S01E01.mkv"}, {})
        self.assertEqual(len(self.metadata.calls), 6)

    def test_bound_alias_is_rechecked_against_live_canonical(self):
        binding = {"metadata_source": "tmdb", "series_id": "10", "year": 2026, "title": "常用剧名", "canonical_title": "示例剧", "aliases": ["别名"]}
        resolver = MediaResolver(self.metadata, bindings=[binding])
        result = resolver({"name": "别名 (2026) S01E01.mkv"}, {})
        self.assertEqual((result["status"], result["title"]), ("MATCHED", "常用剧名"))
        self.assertNotIn("search_tv", [call[0] for call in self.metadata.calls])
        changed = Metadata()
        changed.tv[0]["title"] = "身份已改变"
        self.assertEqual(MediaResolver(changed, bindings=[binding])({"name": "别名 (2026) S01E01.mkv"}, {})["reason"], "confirmed_binding_identity_changed")

    def test_missing_cross_provider_binding_fails_closed(self):
        binding = {"metadata_source": "tvmaze", "series_id": "10", "year": 2026, "title": "别名", "canonical_title": "Example"}
        result = MediaResolver(self.metadata, bindings=[binding])({"name": "别名 (2026) S01E01.mkv"}, {})
        self.assertEqual(result["reason"], "confirmed_binding_provider_unavailable")

    def test_candidate_summaries_do_not_copy_arbitrary_api_fields(self):
        self.metadata.tv[0].update(title="其他", cookie="do-not-copy", overview="do-not-copy", token="do-not-copy")
        result = self.resolve("示例剧 (2026) S01E01.mkv")
        self.assertEqual(set(result["candidates"][0]), {"id", "source", "title", "original_title", "year"})

    def test_unsafe_metadata_target_is_not_proposed(self):
        self.metadata.tv[0]["title"] = "示例:剧"
        self.assertEqual(self.resolve("示例剧 (2026) S01E01.mkv")["reason"], "unsafe_target_title_requires_confirmation")


class TMDBMovieAdapterTests(unittest.TestCase):
    class Provider:
        source = "tmdb"

        def __init__(self, total_pages=1):
            self.calls = []
            self.total_pages = total_pages

        def _request(self, path, params=None):
            self.calls.append((path, params))
            row = {"id": 20, "title": "示例电影", "original_title": "Example Movie", "release_date": "2025-04-01"}
            return {"results": [row], "total_pages": self.total_pages} if path == "/search/movie" else row

    def test_adapter_uses_movie_search_then_verified_details(self):
        provider = self.Provider()
        result = MediaResolver(provider)({"name": "示例电影 (2025).mkv"}, {})
        self.assertEqual(result["status"], "MATCHED")
        self.assertEqual([row[0] for row in provider.calls], ["/search/movie", "/movie/20"])
        self.assertEqual(provider.calls[0][1]["primary_release_year"], 2025)

    def test_unbounded_results_do_not_manufacture_a_unique_match(self):
        result = MediaResolver(self.Provider(total_pages=6))({"name": "示例电影 (2025).mkv"}, {})
        self.assertEqual(result["reason"], "metadata_unavailable")
        self.assertEqual(result["metadata_error"]["code"], "movie_search_incomplete")

    def test_an_exact_tv_match_on_second_page_prevents_rank_selection(self):
        class PagedTV(self.Provider):
            def _request(self, path, params=None):
                self.calls.append((path, params))
                return {"total_pages": 2, "results": [{"id": 9 + params["page"], "name": "示例剧", "original_name": "Example Show", "first_air_date": "2026-01-01"}]}
        provider = PagedTV()
        result = MediaResolver(provider)({"name": "示例剧 (2026) S01E01.mkv"}, {})
        self.assertEqual(result["reason"], "identity_not_unique")
        self.assertEqual(len(provider.calls), 2)


if __name__ == "__main__":
    unittest.main()
