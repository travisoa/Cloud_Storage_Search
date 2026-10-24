"""Expected updates are cross-checked with mocked catalogs, never live APIs."""

from copy import deepcopy
import json
import unittest

from media_helper.catalog import MetadataError, calculate_missing, build_episode_inventory
from media_helper.updates import UpdatesMetadata, collect_updates, fill_schedule


AS_OF = "2026-10-17T10:00:00+08:00"
REQUEST = {"metadata_source": "tvmaze", "series_id": 9, "season": 1, "title": "示例剧", "year": 2026}


class Catalog:
    def __init__(self, source="tvmaze", identifier=9, episodes=None, ids=None, count=3):
        self.source, self.identifier = source, identifier
        self.ids = ids if ids is not None else {"imdb_id": "tt1234567", "tvdb_id": 12345}
        self.series = {"source": source, "id": identifier, "source_id": identifier,
                       "title": "示例剧", "year": 2026, "fetched_at": "2026-10-17T01:00:00Z",
                       "seasons": [{"season": 1, "episode_count": count}]}
        self.episodes = episodes if episodes is not None else [
            {"season": 1, "episode": 1, "air_date": "2026-10-16", "date_precision": "date"},
            {"season": 1, "episode": 2, "air_date": "2026-10-18", "date_precision": "date"},
            {"season": 1, "episode": 3, "air_date": None, "date_precision": "unknown"},
        ]
        self.episodes = [{"source": source, "series_id": identifier, "source_id": index + 1,
                          "fetched_at": "2026-10-17T01:00:00Z", **row} for index, row in enumerate(self.episodes)]
        self.calls = []

    def get_series(self, identifier):
        self.calls.append(("series", identifier))
        return deepcopy(self.series)

    def get_season(self, identifier, season):
        self.calls.append(("season", identifier, season))
        return deepcopy(self.episodes)

    def get_external_ids(self, identifier):
        self.calls.append(("external_ids", identifier))
        return {"source": self.source, "series_id": identifier, "fetched_at": "2026-10-17T01:00:00Z", **self.ids}

    def search_tv(self, title, year):
        self.calls.append(("search", title, year))
        return [self.series]


class UpdatesScenarios(unittest.TestCase):
    def setUp(self):
        self.primary = Catalog()
        self.secondary = Catalog("tmdb", 17)

    def collect(self, **kwargs):
        return collect_updates(REQUEST, self.primary, as_of=AS_OF, **kwargs)

    def test_primary_only_separates_expected_future_and_unknown_without_claiming_release(self):
        result = self.collect()
        self.assertEqual(result["status"], "ok")
        self.assertEqual([row["episode"] for row in result["expected_due"]], [1])
        self.assertEqual([row["episode"] for row in result["upcoming"]], [2])
        self.assertEqual([row["episode"] for row in result["unknown"]], [3])
        self.assertFalse(result["availability_verified"])
        self.assertEqual(result["basis"], "expected_schedule_only")
        self.assertTrue(result["expected_due"][0]["is_expected"])
        self.assertEqual(result["series"]["source"], "tvmaze")
        self.assertEqual(result["series"]["id"], 9)

    def test_explicit_secondary_requires_shared_external_id_and_preserves_primary_identity(self):
        result = self.collect(secondary=self.secondary, secondary_id=17)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["source_links"][1]["status"], "linked")
        self.assertEqual(result["series"]["id"], 9)
        self.assertEqual(result["episodes"][0]["source"], "tvmaze")
        self.assertEqual(result["episodes"][0]["schedule_sources"], ["tvmaze", "tmdb"])
        self.assertEqual(len(result["episodes"][0]["schedule_evidence"]), 2)

    def test_date_only_is_refined_by_timestamp_using_local_airdate_not_utc_date(self):
        # Korean midnight is the previous UTC day. Both catalogs mean Oct 17.
        self.primary.episodes[0].update(air_date="2026-10-17")
        self.secondary.episodes[0].update(air_date="2026-10-17", air_at="2026-10-16T15:30:00Z",
                                         date_precision="datetime")
        result = self.collect(secondary=self.secondary, secondary_id=17)
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(result["episodes"][0]["air_at"], "2026-10-16T15:30:00Z")
        self.assertEqual(result["episodes"][0]["date_precision"], "datetime")

    def test_precise_primary_timestamp_wins_over_matching_secondary_date(self):
        self.primary.episodes[0].update(air_at="2026-10-16T12:00:00Z", date_precision="datetime")
        result = self.collect(secondary=self.secondary, secondary_id=17)
        self.assertEqual(result["episodes"][0]["air_at"], "2026-10-16T12:00:00Z")

    def test_future_timestamp_does_not_become_due_from_date_only_secondary(self):
        self.primary.episodes[0].update(air_date="2026-10-17")
        self.secondary.episodes[0].update(air_date="2026-10-17", air_at="2026-10-17T12:00:00Z",
                                         date_precision="datetime")
        result = self.collect(secondary=self.secondary, secondary_id=17)
        self.assertEqual(result["expected_due"], [])
        self.assertEqual([row["episode"] for row in result["upcoming"]], [1, 2])

    def test_known_different_dates_block_automatic_transfer_and_exclude_conflicting_episode(self):
        self.secondary.episodes[0]["air_date"] = "2026-10-15"
        result = self.collect(secondary=self.secondary, secondary_id=17)
        self.assertEqual(result["status"], "conflict")
        self.assertFalse(result["automatic_transfer_allowed"])
        self.assertNotIn(1, [row["episode"] for row in result["episodes"]])
        self.assertEqual(result["conflicts"][0]["code"], "episode_schedule_mismatch")

    def test_distinct_precise_timestamps_conflict_even_on_same_local_date(self):
        for provider, stamp in ((self.primary, "2026-10-16T10:00:00Z"), (self.secondary, "2026-10-16T11:00:00Z")):
            provider.episodes[0].update(air_at=stamp, date_precision="datetime")
        result = self.collect(secondary=self.secondary, secondary_id=17)
        self.assertEqual(result["status"], "conflict")

    def test_numbering_set_or_count_disagreement_blocks_whole_automatic_run(self):
        self.secondary.episodes.pop()
        self.secondary.series["seasons"][0]["episode_count"] = 2
        result = self.collect(secondary=self.secondary, secondary_id=17)
        self.assertFalse(result["automatic_transfer_allowed"])
        self.assertEqual({row["code"] for row in result["conflicts"]},
                         {"season_episode_count_mismatch", "episode_numbering_mismatch"})

    def test_duplicate_keys_or_wrong_season_do_not_become_transfer_candidates(self):
        self.primary.episodes.append(deepcopy(self.primary.episodes[0]))
        self.primary.episodes[1]["season"] = 2
        result = self.collect()
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["expected_due"], [])
        self.assertFalse(result["automatic_transfer_allowed"])

    def test_missing_external_ids_keep_primary_and_do_not_read_secondary_episodes(self):
        self.primary.ids = {}
        result = self.collect(secondary=self.secondary, secondary_id=17)
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["source_links"][1]["status"], "unlinked")
        self.assertEqual(len(result["episodes"]), 3)
        self.assertFalse(any(call[0] == "season" for call in self.secondary.calls))

    def test_one_matching_external_id_does_not_override_another_conflicting_id(self):
        self.secondary.ids["tvdb_id"] = 98765
        result = self.collect(secondary=self.secondary, secondary_id=17)
        self.assertEqual(result["status"], "conflict")
        self.assertFalse(result["automatic_transfer_allowed"])
        self.assertEqual(result["conflicts"][0]["conflicting_external_ids"], ["tvdb_id"])
        self.assertFalse(any(call[0] == "season" for call in self.secondary.calls))

    def test_secondary_error_is_sanitized_and_does_not_claim_crosscheck_success(self):
        def failure(_):
            raise MetadataError("tmdb", "http_error", status=401, retry_after="Bearer fixture-secret")
        self.secondary.get_external_ids = failure
        result = self.collect(secondary=self.secondary, secondary_id=17)
        self.assertEqual(result["status"], "error")
        self.assertFalse(result["automatic_transfer_allowed"])
        self.assertEqual(result["source_links"][1]["status"], "error")
        self.assertEqual(result["errors"], [{"source": "tmdb", "code": "http_error", "http_status": 401}])
        self.assertNotIn("fixture-secret", json.dumps(result))
        self.assertEqual(len(result["episodes"]), 3)

    def test_secondary_transport_failure_preserves_primary_with_degraded_status(self):
        def failure(_):
            raise MetadataError("tmdb", "transport_or_invalid_json")
        self.secondary.get_external_ids = failure
        result = self.collect(secondary=self.secondary, secondary_id=17)
        self.assertEqual(result["status"], "degraded")
        self.assertTrue(result["automatic_transfer_allowed"])
        self.assertEqual([row["episode"] for row in result["expected_due"]], [1])

    def test_secondary_rate_limit_stops_automatic_transfer_with_retry_hint(self):
        def failure(_):
            raise MetadataError("tmdb", "http_error", status=429, retry_after="120")
        self.secondary.get_external_ids = failure
        result = self.collect(secondary=self.secondary, secondary_id=17)
        self.assertEqual(result["status"], "error")
        self.assertFalse(result["automatic_transfer_allowed"])
        self.assertEqual(result["errors"][0]["retry_after_seconds"], 120)

    def test_primary_failure_never_falls_back_to_changing_show_identity(self):
        self.primary.get_series = lambda _: (_ for _ in ()).throw(MetadataError("tvmaze", "transport_or_invalid_json"))
        result = self.collect(secondary=self.secondary, secondary_id=17)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["episodes"], [])
        self.assertFalse(result["automatic_transfer_allowed"])
        self.assertEqual(self.secondary.calls, [])

    def test_secondary_search_requires_unique_external_id_link_not_search_order(self):
        self.secondary.search_tv = lambda *_: [{"id": 100}, {"id": 17}]
        self.secondary.get_external_ids = lambda identifier: ({"imdb_id": "tt9999999"} if identifier == 100 else self.secondary.ids)
        result = self.collect(secondary=self.secondary)
        self.assertEqual(result["source_links"][1]["series_id"], 17)
        self.assertEqual(result["source_links"][1]["status"], "linked")

    def test_truncated_search_does_not_manufacture_unique_identity(self):
        self.secondary.search_tv = lambda *_: [{"id": number} for number in range(1, 7)]
        result = self.collect(secondary=self.secondary)
        self.assertEqual(result["source_links"][1]["status"], "unlinked")
        self.assertEqual(sum(call[0] == "external_ids" for call in self.secondary.calls), 5)
        self.assertFalse(any(call[0] == "season" for call in self.secondary.calls))

    def test_secondary_can_fill_unknown_schedule_after_identity_is_proven(self):
        self.secondary.episodes[2].update(air_date="2026-10-19", date_precision="date")
        result = self.collect(secondary=self.secondary, secondary_id=17)
        self.assertEqual(result["unknown"], [])
        self.assertEqual([row["episode"] for row in result["upcoming"]], [2, 3])

    def test_empty_or_all_unknown_schedule_never_generates_a_due_episode(self):
        self.primary.episodes = [self.primary.episodes[2]]
        result = self.collect()
        self.assertEqual(result["expected_due"], [])
        difference = calculate_missing(result["episodes"], build_episode_inventory([]), AS_OF)
        self.assertEqual(difference["missing"], [])
        self.primary.episodes = []
        self.assertFalse(self.collect()["automatic_transfer_allowed"])

    def test_invalid_or_special_rows_are_unknown_and_not_due(self):
        self.primary.episodes = [{"season": 1, "episode": 0, "air_date": "2026-10-15"},
                                 {"season": 1, "episode": 1, "air_date": "2026-10-15", "is_special": True}]
        result = self.collect()
        self.assertEqual(result["expected_due"], [])
        self.assertEqual(result["episodes"], [])
        self.assertGreaterEqual(len(result["unknown"]), 2)

    def test_bound_source_and_year_mismatches_cannot_change_identity(self):
        with self.assertRaises(ValueError):
            collect_updates({**REQUEST, "metadata_source": "tmdb"}, self.primary)
        self.primary.series["year"] = 2025
        result = self.collect()
        self.assertEqual(result["status"], "conflict")
        self.assertFalse(result["automatic_transfer_allowed"])

    def test_wrapped_metadata_preserves_id_and_returns_detached_schedule(self):
        result = self.collect(secondary=self.secondary, secondary_id=17)
        wrapped = UpdatesMetadata(self.primary, result["episodes"], result["series"])
        self.assertEqual(wrapped.get_series(9)["id"], 9)
        episodes = wrapped.get_season(9, 1)
        episodes[0]["episode"] = 99
        self.assertEqual(wrapped.get_season(9, 1)[0]["episode"], 1)
        with self.assertRaises(ValueError):
            wrapped.get_series(17)
        with self.assertRaises(ValueError):
            wrapped.get_season(9, 2)


def primary_row(episode, air_date, **changes):
    return {"season": 1, "episode": episode, "episode_key": f"S01E{episode:02d}", "source": "tmdb",
            "air_date": air_date, "air_at": None, "date_precision": "date" if air_date else "unknown", **changes}


def trakt_row(episode, air_at, air_date):
    return {"season": 1, "episode": episode, "source": "trakt", "air_at": air_at, "air_date": air_date,
            "date_precision": "datetime" if air_at else "unknown"}


class FillScheduleScenarios(unittest.TestCase):
    def fill(self, primary, supplement):
        report = {"evidence": [], "conflicts": []}
        return fill_schedule(primary, supplement, report), report

    def test_same_date_takes_supplement_time(self):
        source = [primary_row(1, "2026-10-25")]
        rows, report = self.fill(source, [trakt_row(1, "2026-10-25T04:00:00Z", "2026-10-25")])
        self.assertEqual((rows[0]["air_date"], rows[0]["air_at"], rows[0]["date_precision"]),
                         ("2026-10-25", "2026-10-25T04:00:00Z", "datetime"))
        self.assertEqual(rows[0]["schedule_basis"], "primary+supplement_time")
        self.assertEqual(rows[0]["source"], "tmdb")
        self.assertIsNone(source[0]["air_at"])
        self.assertEqual(report["evidence"], [])

    def test_missing_primary_date_is_filled(self):
        rows, _ = self.fill([primary_row(23, None)], [trakt_row(23, "2026-10-30T04:00:00Z", "2026-10-30")])
        self.assertEqual((rows[0]["air_date"], rows[0]["air_at"], rows[0]["date_precision"], rows[0]["schedule_basis"]),
                         ("2026-10-30", "2026-10-30T04:00:00Z", "datetime", "supplement"))

    def test_earlier_supplement_wins_and_is_recorded(self):
        rows, report = self.fill([primary_row(2, "2026-10-26")], [trakt_row(2, "2026-10-25T04:00:00Z", "2026-10-25")])
        self.assertEqual((rows[0]["air_date"], rows[0]["air_at"], rows[0]["schedule_basis"]),
                         ("2026-10-25", "2026-10-25T04:00:00Z", "supplement"))
        self.assertEqual(report["evidence"], [{"code": "supplement_schedule_disagreement", "episode_key": "S01E02",
                                               "primary_date": "2026-10-26", "supplement_date": "2026-10-25",
                                               "chosen": "supplement"}])
        self.assertEqual(report["conflicts"], [])

    def test_earlier_primary_wins_and_keeps_date_precision(self):
        rows, report = self.fill([primary_row(2, "2026-10-25")], [trakt_row(2, "2026-10-26T04:00:00Z", "2026-10-26")])
        self.assertEqual((rows[0]["air_date"], rows[0]["air_at"], rows[0]["date_precision"], rows[0]["schedule_basis"]),
                         ("2026-10-25", None, "date", "primary"))
        self.assertEqual(report["evidence"][0]["chosen"], "primary")

    def test_primary_timestamp_or_missing_supplement_is_untouched(self):
        timed = primary_row(1, "2026-10-25", air_at="2026-10-25T12:00:00Z", date_precision="datetime")
        rows, report = self.fill([timed, primary_row(2, "2026-10-26"), primary_row(3, "2026-10-27")],
                                 [trakt_row(1, "2026-10-24T04:00:00Z", "2026-10-24"), trakt_row(2, None, None),
                                  trakt_row(4, "2026-10-28T04:00:00Z", "2026-10-28")])
        self.assertEqual([(row["air_date"], row["air_at"], row["schedule_basis"]) for row in rows],
                         [("2026-10-25", "2026-10-25T12:00:00Z", "primary"), ("2026-10-26", None, "primary"),
                          ("2026-10-27", None, "primary")])
        self.assertEqual(report["evidence"], [{"code": "supplement_episode_set_difference",
                                               "primary_only": ["S01E03"], "supplement_only": ["S01E04"]}])


if __name__ == "__main__":
    unittest.main()
