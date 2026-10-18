"""Catalog scenarios use fictitious cloud files and mocked official responses."""

from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError, URLError

from media_helper.catalog import (
    EpisodeKey, MetadataError, TMDBProvider, TVmazeProvider,
    build_episode_inventory, calculate_missing, match_series_dirs, parse_file,
)


class Responses:
    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        value = self.payloads.pop(0)
        if isinstance(value, Exception):
            raise value
        return io.BytesIO(json.dumps(value).encode())


NOW = lambda: datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


class InventoryScenarios(unittest.TestCase):
    def test_episode_key_serializes_without_guessing(self):
        self.assertEqual(str(EpisodeKey(1, 10)), "S01E10")
        self.assertEqual(EpisodeKey(2, 3).to_dict(), {"season": 2, "episode": 3})
        for season, episode in ((0, 1), (2026, 1), (1, 0), (True, 1)):
            with self.subTest(season=season, episode=episode):
                with self.assertRaises(ValueError):
                    EpisodeKey(season, episode)

    def test_file_number_is_not_derived_from_sort_order(self):
        inventory = build_episode_inventory(["剧.S01E10.mkv", "001.mkv", "剧.S01E01.mkv"], season=1)
        self.assertEqual(set(inventory["existing"]), {"S01E10", "S01E01"})
        self.assertEqual(inventory["review"][0]["name"], "001.mkv")

    def test_chinese_episode_requires_explicit_season(self):
        self.assertTrue(parse_file("剧.第10集.mkv")["ambiguous"])
        self.assertEqual(parse_file("剧.第10集.mkv", season=2)["episode_key"], "S02E10")

    def test_explicit_filename_season_wins_over_default(self):
        self.assertEqual(parse_file("剧.S02E01.mkv", season=1)["episode_key"], "S02E01")

    def test_compact_episode_names_remain_visible_to_inventory(self):
        for name in ("ExampleShowS01E02.mkv", "AS01E02.mkv", "123S01E02.mkv"):
            with self.subTest(name=name):
                parsed = parse_file(name)
                self.assertEqual(parsed["episode_key"], "S01E02", parsed)
                self.assertFalse(parsed["ambiguous"])
        names = ["Example ShowS01E02_2160P_DV_DDP5.1.mkv",
                 "示例剧 (2026)S02E03_1080P_SDR.mp4"]
        inventory = build_episode_inventory(names)
        self.assertEqual(set(inventory["existing"]), {"S01E02", "S02E03"})
        self.assertEqual(inventory["review"], [])

    def test_multi_episode_and_special_files_are_review_only(self):
        inventory = build_episode_inventory(["剧.S01E01-E02.mkv", "剧.SP01.mkv", "剧.S00E01.mkv", "剧.特别篇.mp4"], season=1)
        self.assertEqual(inventory["existing"], {})
        self.assertEqual(len(inventory["review"]), 4)

    def test_subtitle_or_poster_does_not_establish_episode_presence(self):
        inventory = build_episode_inventory(["剧.S01E01.zh.srt", "剧.S01E01.jpg"])
        self.assertEqual(inventory["existing"], {})
        self.assertEqual(len(inventory["skipped"]), 2)

    def test_fid_path_and_multiple_video_versions_are_preserved(self):
        inventory = build_episode_inventory([
            {"name": "剧.S01E01.mkv", "fid": "fixture-a", "path": "/目录/剧.S01E01.mkv"},
            {"name": "剧.S01E01.mp4", "fid": "fixture-b", "path": "/目录/剧.S01E01.mp4"},
        ])
        self.assertEqual([row["fid"] for row in inventory["existing"]["S01E01"]], ["fixture-a", "fixture-b"])
        self.assertFalse(inventory["playability_verified"])

    def test_season_folder_can_resolve_episode_only_filename(self):
        inventory = build_episode_inventory([{"name": "第10集.mp4", "path": "/剧 (2024)/Season 02/第10集.mp4"}], season=1)
        self.assertIn("S02E10", inventory["existing"])

    def test_folder_filename_or_explicit_metadata_disagreement_requires_review(self):
        for reference in (
            {"name": "剧.S01E01.mkv", "path": "/剧/Season 02/剧.S01E01.mkv"},
            {"name": "剧.S01E01.mkv", "season": 2},
            {"name": "剧.S01E01.mkv", "season": "invalid"},
            {"name": "E01.mkv", "path": "/Season 01/Season 02/E01.mkv"},
        ):
            with self.subTest(reference=reference):
                inventory = build_episode_inventory([reference], season=1)
                self.assertEqual(inventory["existing"], {})
                self.assertEqual(len(inventory["review"]), 1)

    def test_zero_length_or_unsafe_files_are_not_existing_episodes(self):
        inventory = build_episode_inventory([
            {"name": "剧.S01E01.mkv", "size": 0},
            {"name": "剧.S01E02.mkv", "path": "/剧/../剧.S01E02.mkv"},
            {"name": r"..\剧.S01E03.mkv"},
        ])
        self.assertEqual(inventory["existing"], {})
        self.assertEqual(len(inventory["review"]), 3)


class DirectoryScenarios(unittest.TestCase):
    def test_exact_title_year_match_preserves_fid(self):
        result = match_series_dirs("示例剧", [{"name": "示例剧 (2024)", "fid": "folder-only"}], 2024)
        self.assertEqual(result["matches"][0]["fid"], "folder-only")

    def test_domain_advertisement_wrapper_can_be_removed(self):
        result = match_series_dirs("示例剧", ["【www.example.com】 示例剧 (2024)"], 2024)
        self.assertEqual(len(result["matches"]), 1)

    def test_quality_or_part_words_are_not_discarded_as_noise(self):
        result = match_series_dirs("示例剧", ["示例剧 第二部 (2024)", "示例剧 4K (2024)", "示例剧 [导演剪辑版] (2024)"], 2024)
        self.assertEqual(result["matches"], [])
        self.assertEqual(len(result["review"]), 3)

    def test_missing_or_wrong_year_requires_review(self):
        result = match_series_dirs("示例剧", ["示例剧", "示例剧 (2023)", "无关作品 (2024)"], 2024)
        self.assertEqual(result["matches"], [])
        self.assertEqual(len(result["review"]), 2)

    def test_equivalent_multiple_directories_are_not_auto_selected(self):
        result = match_series_dirs("示例剧", [{"name": "示例剧 (2024)", "fid": "one"}, {"name": "示例剧 (2024)", "fid": "two"}], 2024)
        self.assertEqual(result["matches"], [])
        self.assertEqual(len(result["review"]), 2)

    def test_legitimate_bracketed_title_is_not_stripped(self):
        result = match_series_dirs("[REC]", ["[REC] (2024)", "REC (2024)"], 2024)
        self.assertEqual([row["name"] for row in result["matches"]], ["[REC] (2024)"])


class ScheduleScenarios(unittest.TestCase):
    def test_expected_missing_existing_future_unknown_are_separate(self):
        episodes = [
            {"season": 1, "episode": 1, "air_date": "2026-10-15", "date_precision": "date"},
            {"season": 1, "episode": 2, "air_date": "2026-10-16", "date_precision": "date"},
            {"season": 1, "episode": 3, "air_date": "2026-10-17", "date_precision": "date"},
            {"season": 1, "episode": 4, "air_date": None, "date_precision": "unknown"},
        ]
        result = calculate_missing(episodes, build_episode_inventory(["剧.S01E01.mkv"]), "2026-10-16T20:00:00+08:00")
        self.assertEqual([row["episode"] for row in result["missing"]], [2])
        self.assertEqual([row["episode"] for row in result["existing"]], [1])
        self.assertEqual([row["episode"] for row in result["future"]], [3])
        self.assertEqual([row["episode"] for row in result["unknown"]], [4])
        self.assertTrue(result["missing"][0]["is_expected"])
        self.assertEqual(result["basis"], "expected_schedule_only")

    def test_utc_timestamp_crosses_to_next_shanghai_day(self):
        episode = {"season": 1, "episode": 1, "air_date": "2026-10-15", "air_at": "2026-10-15T18:00:00Z", "date_precision": "datetime"}
        inventory = build_episode_inventory([])
        before = calculate_missing([episode], inventory, "2026-10-16T01:59:00+08:00")
        after = calculate_missing([episode], inventory, "2026-10-16T02:01:00+08:00")
        self.assertEqual(len(before["future"]), 1)
        self.assertEqual(len(after["missing"]), 1)

    def test_invalid_date_or_naive_timestamp_cannot_be_eligible(self):
        episodes = [
            {"season": 1, "episode": 1, "air_date": "2026-02-30", "date_precision": "date"},
            {"season": 1, "episode": 2, "air_at": "2026-10-16T10:00:00", "date_precision": "datetime"},
            {"season": 1, "episode": 3, "air_at": "2026-10-16", "date_precision": "datetime"},
        ]
        result = calculate_missing(episodes, build_episode_inventory([]), "2026-10-16")
        self.assertEqual(result["missing"], [])
        self.assertEqual(len(result["unknown"]), 3)

    def test_duplicate_metadata_or_special_episode_key_requires_review(self):
        episodes = [
            {"season": 1, "episode": 1, "air_date": "2026-10-15"},
            {"season": 1, "episode": 1, "air_date": "2026-10-14"},
            {"season": 1, "episode": None, "air_date": "2026-10-15"},
            {"season": 1, "episode": 2, "air_date": "2026-10-15", "is_special": True},
        ]
        result = calculate_missing(episodes, build_episode_inventory([]), "2026-10-16")
        self.assertEqual(len(result["unknown"]), 4)
        self.assertEqual(result["missing"], [])


class MetadataScenarios(unittest.TestCase):
    def test_tmdb_external_ids_are_public_normalized_and_identity_checked(self):
        responses = Responses({"id": 7, "imdb_id": "TT1234567", "tvdb_id": 54321,
                               "facebook_id": "ignored", "token": "fixture-do-not-copy"})
        result = TMDBProvider(read_access_token="fixture-secret", opener=responses, now=NOW).get_external_ids(7)
        self.assertEqual(result, {"source": "tmdb", "series_id": 7, "imdb_id": "tt1234567",
                                  "tvdb_id": 54321, "fetched_at": "2026-10-16T12:00:00Z"})
        self.assertIn("/tv/7/external_ids", responses.requests[0].full_url)
        self.assertNotIn("fixture-secret", json.dumps(result))
        with self.assertRaises(MetadataError):
            TMDBProvider(api_key="fixture", opener=Responses({"id": 8})).get_external_ids(7)

    def test_tvmaze_external_ids_do_not_load_aliases_or_seasons(self):
        responses = Responses({"id": 9, "externals": {"imdb": "tt1234567", "thetvdb": 54321,
                                                       "tvrage": 12, "secret": "ignored"}})
        result = TVmazeProvider(opener=responses, now=NOW).get_external_ids(9)
        self.assertEqual(result["imdb_id"], "tt1234567")
        self.assertEqual(result["tvdb_id"], 54321)
        self.assertNotIn("tvrage", result)
        self.assertEqual(len(responses.requests), 1)
        self.assertTrue(responses.requests[0].full_url.endswith("/shows/9"))

    def test_external_ids_do_not_accept_urls_or_unbounded_identifiers(self):
        result = TVmazeProvider(opener=Responses({"id": 9, "externals": {
            "imdb": "https://example.invalid/?token=secret", "thetvdb": True}})).get_external_ids(9)
        self.assertIsNone(result["imdb_id"])
        self.assertIsNone(result["tvdb_id"])

    def test_tmdb_search_returns_candidates_never_first_selection(self):
        responses = Responses({"results": [{"id": 1, "name": "同名作品", "first_air_date": "2024-01-01"}, {"id": 2, "name": "同名作品", "first_air_date": "2024-02-01"}]})
        provider = TMDBProvider(read_access_token="fixture-secret", opener=responses, now=NOW)
        candidates = provider.search_tv("同名作品", 2024)
        self.assertEqual([row["id"] for row in candidates], [1, 2])
        self.assertTrue(all(row["identity_confirmed"] is False for row in candidates))
        self.assertEqual(candidates[0]["fetched_at"], "2026-10-16T12:00:00Z")
        self.assertNotIn("fixture-secret", json.dumps(candidates))
        self.assertNotIn("fixture-secret", responses.requests[0].full_url)

    def test_tmdb_series_includes_explicit_season_choices(self):
        responses = Responses({"id": 7, "name": "剧", "first_air_date": "2024-01-01", "number_of_seasons": 2, "seasons": [{"season_number": 0, "air_date": None}, {"season_number": 1, "air_date": "2024-01-01", "episode_count": 12}, {"season_number": 2, "air_date": "2025-01-01", "episode_count": 10}]})
        series = TMDBProvider(api_key="fixture-key", opener=responses, now=NOW).get_series(7)
        self.assertEqual(series["season_count"], 2)
        self.assertEqual([season["season"] for season in series["seasons"]], [0, 1, 2])

    def test_tmdb_season_dates_are_expected_not_proven_broadcasts(self):
        responses = Responses({"season_number": 1, "episodes": [{"id": 11, "season_number": 1, "episode_number": 10, "air_date": "2026-10-16", "name": "第十集"}]})
        episodes = TMDBProvider(read_access_token="fixture-token", opener=responses, now=NOW).get_season(7, 1)
        self.assertEqual(episodes[0]["date_precision"], "date")
        self.assertTrue(episodes[0]["is_expected"])
        self.assertEqual(episodes[0]["source"], "tmdb")
        self.assertIsNone(episodes[0]["air_at"])

    def test_tmdb_credentials_are_explicit_private_and_not_returned(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tmdb.json"
            path.write_text('{"read_access_token":"fixture-local-token"}')
            path.chmod(0o600)
            provider = TMDBProvider.from_credentials_file(path, opener=Responses({"results": []}))
            self.assertEqual(provider.search_tv("作品"), [])
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "0600"):
                TMDBProvider.from_credentials_file(path)

    def test_tmdb_rejects_missing_id_or_implicit_season(self):
        provider = TMDBProvider(api_key="fixture")
        for value in (None, "", "first", 0):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    provider.get_series(value)
        with self.assertRaises(ValueError):
            provider.get_season(1, None)

    def test_provider_failure_does_not_reveal_key_or_url(self):
        secret = "fixture-very-sensitive-key"
        url = "https://api.themoviedb.org/3/tv/7?api_key=" + secret
        for failure in (HTTPError(url, 429, secret, {"Retry-After": "15"}, None), URLError(url)):
            with self.subTest(failure_type=type(failure).__name__):
                provider = TMDBProvider(api_key=secret, opener=Responses(failure))
                with self.assertRaises(MetadataError) as caught:
                    provider.get_series(7)
                self.assertNotIn(secret, str(caught.exception))
                self.assertNotIn("https:", str(caught.exception))

    def test_tvmaze_search_needs_no_key_and_does_not_pick_identity(self):
        responses = Responses([{"show": {"id": 9, "name": "剧", "premiered": "2024-01-01"}}, {"show": {"id": 10, "name": "剧", "premiered": "2023-01-01"}}], [])
        candidates = TVmazeProvider(opener=responses, now=NOW).search_tv("剧", 2024)
        self.assertEqual([row["id"] for row in candidates], [9])
        self.assertFalse(candidates[0]["identity_confirmed"])
        self.assertNotIn("Authorization", responses.requests[0].headers)

    def test_tvmaze_series_loads_season_list_without_guessing_one(self):
        responses = Responses({"id": 9, "name": "剧", "premiered": "2024-01-01"}, [{"id": 1, "number": 1, "premiereDate": "2024-01-01", "episodeOrder": 12}, {"id": 2, "number": 2, "premiereDate": "2025-01-01", "episodeOrder": 10}], [])
        series = TVmazeProvider(opener=responses, now=NOW).get_series(9)
        self.assertEqual(series["season_count"], 2)
        self.assertEqual([row["season"] for row in series["seasons"]], [1, 2])
        self.assertTrue(responses.requests[1].full_url.endswith("/shows/9/seasons"))

    def test_tvmaze_chinese_alias_is_evidence_but_not_automatic_identity(self):
        responses = Responses(
            [{"show": {"id": 88082, "name": "Where the Mask Ends", "premiered": "2026-10-16"}}],
            [{"name": "嫁金钗"}, {"name": "Jia Jin Chai"}],
        )
        candidates = TVmazeProvider(opener=responses, now=NOW).search_tv("嫁金钗", 2026)
        self.assertEqual(candidates[0]["title"], "Where the Mask Ends")
        self.assertIn("嫁金钗", candidates[0]["aliases"])
        self.assertTrue(candidates[0]["aliases_checked"])
        self.assertFalse(candidates[0]["identity_confirmed"])

    def test_tvmaze_alias_request_cap_does_not_truncate_candidates(self):
        payload = [{"show": {"id": number, "name": f"Candidate {number}", "premiered": "2026-01-01"}} for number in range(1, 8)]
        responses = Responses(payload, *[[{"name": "同名作品"}]] * 5)
        candidates = TVmazeProvider(opener=responses, now=NOW).search_tv("同名作品", 2026)
        self.assertEqual(len(candidates), 7)
        self.assertEqual(len(responses.requests), 6)
        self.assertEqual([item["aliases_checked"] for item in candidates], [True] * 5 + [False] * 2)
        self.assertTrue(all(not item["identity_confirmed"] for item in candidates))

    def test_tvmaze_alias_failure_keeps_all_candidates_unconfirmed(self):
        responses = Responses(
            [{"show": {"id": 1, "name": "Candidate", "premiered": "2026-01-01"}}],
            URLError("fixture alias endpoint unavailable"),
        )
        candidates = TVmazeProvider(opener=responses, now=NOW).search_tv("作品", 2026)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["aliases"], [])
        self.assertFalse(candidates[0]["aliases_checked"])
        self.assertFalse(candidates[0]["identity_confirmed"])

    def test_tvmaze_series_exposes_chinese_alias_for_workflow_identity_check(self):
        responses = Responses(
            {"id": 88082, "name": "Where the Mask Ends", "premiered": "2026-10-16"},
            [{"id": 1, "number": 1, "premiereDate": "2026-10-16", "episodeOrder": 24}],
            [{"name": "嫁金钗"}, {"name": "嫁金钗"}, {"name": "Jia Jin Chai"}],
        )
        series = TVmazeProvider(opener=responses, now=NOW).get_series(88082)
        self.assertEqual(series["aliases"], ["嫁金钗", "Jia Jin Chai"])
        self.assertEqual(series["seasons"][0]["season"], 1)
        self.assertFalse(series["identity_confirmed"])

    def test_tvmaze_without_airtime_does_not_fabricate_precise_datetime(self):
        responses = Responses([
            {"id": 21, "season": 1, "number": 1, "airdate": "2026-10-16", "airtime": "", "airstamp": "2026-10-16T00:00:00+00:00", "type": "regular"},
            {"id": 22, "season": 1, "number": 2, "airdate": "2026-10-16", "airtime": "21:00", "airstamp": "2026-10-17T01:00:00+00:00", "type": "regular"},
        ])
        episodes = TVmazeProvider(opener=responses, now=NOW).get_season(9, 1)
        self.assertEqual(episodes[0]["date_precision"], "date")
        self.assertIsNone(episodes[0]["air_at"])
        self.assertEqual(episodes[1]["date_precision"], "datetime")
        self.assertTrue(all(episode["is_expected"] for episode in episodes))

    def test_unexpected_provider_identity_is_not_accepted(self):
        provider = TVmazeProvider(opener=Responses({"id": 8, "name": "wrong"}))
        with self.assertRaises(MetadataError):
            provider.get_series(9)


if __name__ == "__main__":
    unittest.main()
