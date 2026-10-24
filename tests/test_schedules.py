"""Schedule cache, Trakt gap filling and check timing with mocked catalogs only."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from media_helper.catalog import MetadataError
from media_helper.schedules import ScheduleCache, obtain_schedule, schedule_timing


SUBSCRIPTION = "a" * 64
REQUEST = {"metadata_source": "tmdb", "series_id": "300146", "season": 1, "title": "嫁金钗", "year": 2026}
NOW = datetime(2026, 10, 17, 0, 0, tzinfo=timezone.utc)


def tmdb_row(episode, air_date):
    return {"season": 1, "episode": episode, "episode_key": f"S01E{episode:02d}", "source": "tmdb",
            "air_date": air_date, "air_at": None, "date_precision": "date" if air_date else "unknown",
            "is_expected": True}


def secondary_link(status):
    return {"source": "tvmaze", "series_id": 88082, "role": "secondary", "status": status}


def update_report(rows=None, status="Returning Series"):
    rows = rows if rows is not None else [tmdb_row(1, "2026-10-16"), tmdb_row(2, "2026-10-17"), tmdb_row(3, None)]
    return {"schema_version": 1, "status": "ok", "automatic_transfer_allowed": True,
            "series": {"id": 300146, "source_id": 300146, "source": "tmdb", "title": "嫁金钗", "year": 2026,
                       "status": status, "overview": "long text", "seasons": [{"season": 1, "episode_count": 3}]},
            "episodes": rows, "source_links": [{"source": "tmdb", "series_id": 300146, "role": "primary"}],
            "evidence": [], "conflicts": [], "errors": [], "unknown": [], "expected_due": [], "upcoming": []}


def trakt_row(episode, air_at):
    return {"season": 1, "episode": episode, "air_at": air_at, "air_date": air_at[:10] if air_at else None,
            "date_precision": "datetime" if air_at else "unknown", "source": "trakt"}


class Fetch:
    def __init__(self, report=None):
        self.report, self.calls = report or update_report(), 0

    def __call__(self):
        self.calls += 1
        return deepcopy(self.report)


class Primary:
    source = "tmdb"

    def __init__(self, ids=None):
        self.ids = ids if ids is not None else {"imdb_id": "tt42576855", "tvdb_id": 471705}

    def get_external_ids(self, identifier):
        return {"source": "tmdb", "series_id": identifier, **self.ids}


class Trakt:
    source = "trakt"

    def __init__(self, *, found=None, series=None, rows=None, error=None):
        self.found = found if found is not None else [{"id": 5, "source_id": 5, "source": "trakt", "tmdb_id": 300146}]
        self.series = series or {"id": 5, "source": "trakt", "tmdb_id": 300146, "imdb_id": "tt42576855",
                                 "tvdb_id": 471705, "status": "returning series",
                                 "airs": {"day": "Monday", "time": "12:00", "timezone": "Asia/Shanghai"}}
        self.rows = rows if rows is not None else [trakt_row(1, "2026-10-16T04:00:00Z"),
                                                   trakt_row(2, "2026-10-17T04:00:00Z"),
                                                   trakt_row(3, "2026-10-18T04:00:00Z")]
        self.error, self.calls = error, []

    def _maybe_fail(self):
        if self.error is not None:
            raise self.error

    def find_by_tmdb(self, tmdb_id):
        self.calls.append(("find", tmdb_id))
        self._maybe_fail()
        return deepcopy(self.found)

    def get_series(self, series_id):
        self.calls.append(("series", series_id))
        self._maybe_fail()
        return deepcopy(self.series)

    def get_season(self, series_id, season, timezone_name):
        self.calls.append(("season", series_id, season, timezone_name))
        self._maybe_fail()
        return deepcopy(self.rows)


class ScheduleCacheScenarios(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = ScheduleCache(self.root)
        self.fetch, self.primary, self.trakt = Fetch(), Primary(), Trakt()

    def obtain(self, now=NOW, **changes):
        options = {"fetch": self.fetch, "primary": self.primary, "trakt": self.trakt, "cache": self.cache,
                   "now": now, "timezone_name": "Asia/Shanghai", **changes}
        return obtain_schedule(SUBSCRIPTION, REQUEST, **options)

    def episodes(self, report):
        return {row["episode"]: row for row in report["episodes"]}

    def test_first_check_fetches_links_trakt_and_saves_cache(self):
        report = self.obtain()
        self.assertEqual(self.fetch.calls, 1)
        self.assertTrue(report["automatic_transfer_allowed"])
        self.assertEqual(report["schedule_cache"]["used_cache"], False)
        self.assertEqual(report["schedule_cache"]["supplement"],
                         {"source": "trakt", "status": "linked", "trakt_id": 5, "reason": None, "http_status": None})
        rows = self.episodes(report)
        self.assertEqual((rows[3]["air_date"], rows[3]["schedule_basis"]), ("2026-10-18", "supplement"))
        self.assertEqual(rows[2]["air_at"], "2026-10-17T04:00:00Z")
        self.assertEqual([row["episode"] for row in report["expected_due"]], [1])
        self.assertEqual([row["episode"] for row in report["upcoming"]], [2, 3])
        self.assertEqual(report["schedule_hint"], {"airs_time": "12:00", "timezone": "Asia/Shanghai"})
        text = (self.root / "runtime/schedules" / f"{SUBSCRIPTION}.json").read_text()
        self.assertNotIn("client", text)
        self.assertNotIn("long text", text)
        self.assertEqual(json.loads(text)["supplement"]["trakt_id"], 5)

    def test_second_check_within_day_uses_cache_without_fetch(self):
        self.obtain()
        calls = list(self.trakt.calls)
        report = self.obtain(NOW + timedelta(hours=6))
        self.assertEqual(self.fetch.calls, 1)
        self.assertEqual(self.trakt.calls, calls)
        self.assertTrue(report["schedule_cache"]["used_cache"])
        self.assertEqual([row["episode"] for row in report["expected_due"]], [1, 2])
        self.assertEqual(self.episodes(report)[3]["air_date"], "2026-10-18")

    def test_refresh_after_24_hours_and_on_force(self):
        self.obtain()
        self.obtain(NOW + timedelta(hours=23))
        self.assertEqual(self.fetch.calls, 1)
        self.obtain(NOW + timedelta(hours=24))
        self.assertEqual(self.fetch.calls, 2)
        self.obtain(NOW + timedelta(hours=25), refresh=True)
        self.assertEqual(self.fetch.calls, 3)

    def test_closed_schedule_is_not_refreshed(self):
        self.fetch = Fetch(update_report([tmdb_row(1, "2026-10-15"), tmdb_row(2, "2026-10-16")], status="Ended"))
        first = self.obtain()
        self.obtain(NOW + timedelta(days=3))
        self.assertEqual(self.fetch.calls, 1)
        self.assertEqual(first["schedule_cache"]["supplement"]["status"], "not_needed")
        self.assertEqual(self.trakt.calls, [])

    def test_refresh_transport_failure_uses_cache_and_retries_next_check(self):
        self.obtain()
        self.fetch.report = {"status": "error", "automatic_transfer_allowed": False, "conflicts": [],
                             "errors": [{"source": "tmdb", "code": "transport_or_invalid_json"}]}
        report = self.obtain(NOW + timedelta(hours=25))
        self.assertTrue(report["automatic_transfer_allowed"])
        self.assertTrue(report["schedule_cache"]["used_cache"])
        self.assertIn("schedule_refresh_failed_using_cache", [row["code"] for row in report["evidence"]])
        self.assertEqual(report["schedule_cache"]["refreshed_at"], NOW.isoformat())
        self.obtain(NOW + timedelta(hours=26))
        self.assertEqual(self.fetch.calls, 3)

    def test_auth_or_conflict_fetch_result_is_returned_unchanged(self):
        self.obtain()
        for failure in ({"errors": [{"source": "tmdb", "http_status": 401}], "conflicts": []},
                        {"errors": [], "conflicts": [{"code": "episode_schedule_mismatch"}]}):
            with self.subTest(failure=failure):
                self.fetch.report = {"status": "error", "automatic_transfer_allowed": False, **failure}
                report = self.obtain(NOW + timedelta(hours=25))
                self.assertFalse(report["automatic_transfer_allowed"])
                self.assertNotIn("schedule_cache", report)

    def test_secondary_source_change_discards_cache(self):
        self.obtain()
        self.obtain(NOW + timedelta(hours=1), secondary={"source": "tvmaze", "series_id": "88082"})
        self.assertEqual(self.fetch.calls, 2)
        self.obtain(NOW + timedelta(hours=2), secondary={"source": "tvmaze", "series_id": 88082})
        self.assertEqual(self.fetch.calls, 2)
        self.obtain(NOW + timedelta(hours=3), secondary={"source": "tvmaze", "series_id": "1"})
        self.assertEqual(self.fetch.calls, 3)

    def test_blocking_refresh_requires_a_new_read_before_cache_is_trusted(self):
        self.obtain()
        healthy = self.fetch.report
        self.fetch.report = {"status": "conflict", "automatic_transfer_allowed": False, "errors": [],
                             "conflicts": [{"code": "external_identity_mismatch"}]}
        self.assertFalse(self.obtain(NOW + timedelta(hours=1), refresh=True)["automatic_transfer_allowed"])
        still = self.obtain(NOW + timedelta(hours=2))
        self.assertEqual(self.fetch.calls, 3)
        self.assertFalse(still["automatic_transfer_allowed"])
        self.fetch.report = healthy
        recovered = self.obtain(NOW + timedelta(hours=3))
        self.assertTrue(recovered["automatic_transfer_allowed"])
        self.assertTrue(self.obtain(NOW + timedelta(hours=4))["schedule_cache"]["used_cache"])
        self.assertEqual(self.fetch.calls, 4)

    def secondary_cycle(self, hour, link_status=None, conflict=False, conflicts=None):
        report = update_report()
        report["source_links"].append(secondary_link(link_status or ("conflict" if conflict else "linked")))
        if conflict or conflicts:
            report.update(status="conflict", automatic_transfer_allowed=False,
                          conflicts=conflicts or [{"code": "external_identity_mismatch", "source": "tvmaze"}])
        elif link_status == "error":
            report.update(status="degraded", errors=[{"source": "tvmaze", "code": "transport_or_invalid_json"}])
        self.fetch.report = report
        return self.obtain(NOW + timedelta(hours=hour), refresh=True,
                           secondary={"source": "tvmaze", "series_id": "88082"})

    def test_secondary_outage_cannot_resolve_a_known_conflict(self):
        self.secondary_cycle(0)
        self.assertFalse(self.secondary_cycle(1, conflict=True)["automatic_transfer_allowed"])
        outage = self.secondary_cycle(2, "error")
        self.assertFalse(outage["automatic_transfer_allowed"])
        self.assertIn("unresolved_secondary_conflict", [row["code"] for row in outage["conflicts"]])
        resolved = self.secondary_cycle(3, "linked")
        self.assertTrue(resolved["automatic_transfer_allowed"])
        self.assertTrue(self.secondary_cycle(4, "error")["automatic_transfer_allowed"])

    def test_primary_only_conflict_recovers_without_a_secondary_link(self):
        duplicate = [{"code": "duplicate_episode_key", "source": "tmdb", "episode_key": "S01E02"}]
        self.assertFalse(self.secondary_cycle(0, "unlinked", conflicts=duplicate)["automatic_transfer_allowed"])
        self.assertTrue(self.secondary_cycle(1, "unlinked")["automatic_transfer_allowed"])

    def test_completed_secondary_read_without_link_resolves_its_conflict(self):
        self.assertFalse(self.secondary_cycle(0, conflict=True)["automatic_transfer_allowed"])
        self.assertTrue(self.secondary_cycle(1, "unlinked")["automatic_transfer_allowed"])

    def test_first_read_conflict_is_remembered_without_a_schedule(self):
        self.assertFalse(self.secondary_cycle(0, conflict=True)["automatic_transfer_allowed"])
        self.assertFalse(self.secondary_cycle(1, "error")["automatic_transfer_allowed"])
        self.assertTrue(self.secondary_cycle(2, "linked")["automatic_transfer_allowed"])

    def test_identity_change_discards_cache(self):
        self.obtain()
        obtain_schedule(SUBSCRIPTION, {**REQUEST, "season": 2}, fetch=self.fetch, primary=self.primary,
                        trakt=None, cache=self.cache, now=NOW + timedelta(hours=1))
        self.assertEqual(self.fetch.calls, 2)

    def test_trakt_link_requires_unique_matching_tmdb_id(self):
        cases = ([{"id": 5, "tmdb_id": 300146}, {"id": 6, "tmdb_id": 300146}], [{"id": 5, "tmdb_id": 1}], [])
        for index, found in enumerate(cases):
            with self.subTest(found=found):
                self.cache = ScheduleCache(self.root / f"case-{index}")
                self.trakt = Trakt(found=found)
                report = self.obtain()
                self.assertEqual(report["schedule_cache"]["supplement"]["status"], "unlinked")
                self.assertIsNone(self.episodes(report)[3]["air_date"])
                self.assertTrue(report["automatic_transfer_allowed"])

    def test_conflicting_imdb_disables_trakt(self):
        self.trakt = Trakt(series={**Trakt().series, "imdb_id": "tt0000001"})
        report = self.obtain()
        self.assertEqual(report["schedule_cache"]["supplement"]["status"], "conflict")
        self.assertIsNone(self.episodes(report)[3]["air_date"])
        self.trakt = Trakt(series={**Trakt().series, "imdb_id": None, "tvdb_id": None})
        self.cache = ScheduleCache(self.root / "no-ids")
        self.assertEqual(self.obtain()["schedule_cache"]["supplement"]["status"], "linked")

    def test_trakt_not_requested_when_all_episodes_past(self):
        self.fetch = Fetch(update_report([tmdb_row(1, "2026-10-15"), tmdb_row(2, "2026-10-16")]))
        report = self.obtain()
        self.assertEqual(self.trakt.calls, [])
        self.assertEqual(report["schedule_cache"]["supplement"]["status"], "not_needed")

    def test_trakt_failure_keeps_transfer_allowed(self):
        self.trakt = Trakt(error=MetadataError("trakt", "http_error", status=401))
        report = self.obtain()
        supplement = report["schedule_cache"]["supplement"]
        self.assertEqual((supplement["status"], supplement["http_status"], supplement["action"]),
                         ("error", 401, "rerun_configure_trakt"))
        self.assertTrue(report["automatic_transfer_allowed"])
        self.assertEqual(report["errors"], [])

    def test_unparseable_primary_id_only_loses_the_fill(self):
        report = obtain_schedule(SUBSCRIPTION, {**REQUEST, "series_id": "tmdb-x"}, fetch=self.fetch,
                                 primary=self.primary, trakt=self.trakt, cache=self.cache, now=NOW)
        self.assertEqual(report["schedule_cache"]["supplement"]["status"], "error")
        self.assertTrue(report["automatic_transfer_allowed"])

    def test_trakt_rate_limit_defers_until_retry_after(self):
        self.trakt = Trakt(error=MetadataError("trakt", "http_error", status=429, retry_after="120"))
        report = self.obtain()
        self.assertEqual(report["schedule_cache"]["supplement"]["status"], "deferred")
        calls = len(self.trakt.calls)
        self.trakt.error = None
        self.obtain(NOW + timedelta(seconds=60), refresh=True)
        self.assertEqual(len(self.trakt.calls), calls)
        report = self.obtain(NOW + timedelta(seconds=180), refresh=True)
        self.assertEqual(report["schedule_cache"]["supplement"]["status"], "linked")

    def test_disabled_trakt_strips_cached_supplement(self):
        self.obtain()
        report = self.obtain(NOW + timedelta(hours=1), trakt=None)
        self.assertEqual(report["schedule_cache"]["supplement"]["status"], "disabled")
        self.assertIsNone(self.episodes(report)[3]["air_date"])
        saved = json.loads((self.root / "runtime/schedules" / f"{SUBSCRIPTION}.json").read_text())
        self.assertEqual(saved["supplement"], {"status": "disabled"})


def timed(episode, air_at):
    return {"season": 1, "episode": episode, "episode_key": f"S01E{episode:02d}", "source": "tmdb",
            "air_date": air_at[:10], "air_at": air_at, "date_precision": "datetime",
            "schedule_basis": "primary+supplement_time"}


def daily(first_day, count, hour_utc=4):
    start = datetime(2026, 10, first_day, hour_utc, tzinfo=timezone.utc)
    return [timed(index + 1, (start + timedelta(days=index)).isoformat().replace("+00:00", "Z"))
            for index in range(count)]


def schedule(episodes, *, status="Returning Series", hint=None):
    return {"series": {"status": status}, "episodes": episodes,
            "schedule_hint": hint or {"airs_time": None, "timezone": "Asia/Shanghai"}}


UP_TO_DATE = {"status": "UP_TO_DATE", "missing": [], "new_files": []}


class TimingScenarios(unittest.TestCase):
    def timing(self, update, run=UP_TO_DATE, *, interval_hours=6, previous_plan=None, now=NOW):
        return schedule_timing(update, run, interval_hours=interval_hours, now=now,
                               timezone_name="Asia/Shanghai", previous_plan=previous_plan)

    def test_obtained_skips_to_next_episode_plus_30_minutes(self):
        result = self.timing(schedule([timed(1, "2026-10-16T04:00:00Z"), timed(2, "2026-10-17T04:00:00Z")]))
        self.assertEqual(result["next_check_at"], datetime(2026, 10, 17, 4, 30, tzinfo=timezone.utc))

    def test_missing_episode_retries_at_plan_slot_nearest_the_interval(self):
        # 08:00 local is off-slot; 14:00 local is nearest the 12:30 slot.
        run = {"status": "WAITING", "missing": [{"episode_key": "S01E01"}], "new_files": []}
        result = self.timing(schedule([timed(1, "2026-10-16T04:00:00Z"), timed(2, "2026-10-17T04:00:00Z")]), run)
        self.assertEqual(result["next_check_at"], datetime(2026, 10, 17, 4, 30, tzinfo=timezone.utc))
        attention = {"status": "NEEDS_INPUT", "missing": [], "new_files": []}
        on_slot = datetime(2026, 10, 17, 4, 30, tzinfo=timezone.utc)
        self.assertEqual(self.timing(schedule([timed(2, "2026-10-17T04:00:00Z")]), attention, now=on_slot)["next_check_at"],
                         datetime(2026, 10, 17, 10, 30, tzinfo=timezone.utc))
        self.assertEqual(self.timing(schedule([]), run)["next_check_at"], NOW + timedelta(hours=6))

    def test_baseline_caps_at_24_hours_on_a_plan_slot(self):
        result = self.timing(schedule([timed(1, "2026-10-20T04:00:00Z")]))
        self.assertEqual(result["next_check_at"], datetime(2026, 10, 17, 22, 30, tzinfo=timezone.utc))
        self.assertEqual(self.timing(schedule([]))["next_check_at"], NOW + timedelta(hours=6))

    def test_start_delay_still_hits_the_next_slot(self):
        late = datetime(2026, 10, 17, 10, 30, 10, tzinfo=timezone.utc)
        run = {"status": "WAITING", "missing": [{"episode_key": "S01E01"}], "new_files": []}
        result = self.timing(schedule(daily(8, 18)), run, now=late)
        self.assertEqual(result["next_check_at"], datetime(2026, 10, 17, 16, 30, tzinfo=timezone.utc))
        obtained = self.timing(schedule(daily(8, 18)), now=late)
        self.assertEqual(obtained["next_check_at"], datetime(2026, 10, 18, 4, 30, tzinfo=timezone.utc))

    def test_short_interval_sets_matching_slot_spacing(self):
        slot = datetime(2026, 10, 17, 4, 30, 5, tzinfo=timezone.utc)
        run = {"status": "WAITING", "missing": [{"episode_key": "S01E01"}], "new_files": []}
        cases = ((1, list(range(24)), timedelta(hours=1)), (2, list(range(0, 24, 2)), timedelta(hours=2)),
                 (4, [0, 4, 8, 12, 16, 20], timedelta(hours=4)), (5, [0, 4, 8, 12, 16, 20], timedelta(hours=4)),
                 (12, [0, 6, 12, 18], timedelta(hours=12)))
        for interval, hours, wait in cases:
            with self.subTest(interval=interval):
                result = self.timing(schedule(daily(8, 18)), run, interval_hours=interval, now=slot)
                rule = dict(part.split("=") for part in result["wake_plan"]["rrule"].split(";"))
                self.assertEqual(rule["BYHOUR"], ",".join(str(hour) for hour in hours))
                self.assertEqual(rule["BYMINUTE"], "30")
                self.assertEqual(result["next_check_at"], slot.replace(second=0) + wait)

    def test_interval_rule_never_exceeds_the_24_hour_baseline(self):
        result = self.timing(schedule([]), interval_hours=48)
        self.assertEqual(result["wake_plan"]["rrule"], "FREQ=HOURLY;INTERVAL=24")
        self.assertEqual(result["next_check_at"], NOW + timedelta(hours=24))

    def test_weekly_retry_and_baseline_are_covered_by_the_plan(self):
        weekly = [timed(index + 1, f"2026-10-{day}T12:00:00Z") for index, day in enumerate((5, 12, 19, 26))]
        monday = datetime(2026, 10, 19, 12, 30, tzinfo=timezone.utc)
        missing = {"status": "WAITING", "missing": [{"episode_key": "S01E03"}], "new_files": []}
        for run, expected in ((missing, datetime(2026, 10, 19, 18, 30, tzinfo=timezone.utc)),
                              (UP_TO_DATE, datetime(2026, 10, 20, 12, 30, tzinfo=timezone.utc))):
            with self.subTest(status=run["status"]):
                result = self.timing(schedule(weekly), run, now=monday)
                self.assertEqual(result["next_check_at"], expected)
                local = result["next_check_at"].astimezone(timezone(timedelta(hours=8)))
                rule = dict(part.split("=") for part in result["wake_plan"]["rrule"].split(";"))
                self.assertEqual(rule["FREQ"], "DAILY")
                self.assertIn(str(local.hour), rule["BYHOUR"].split(","))
                self.assertEqual(rule["BYMINUTE"], str(local.minute))

    def test_saved_episodes_count_as_obtained(self):
        run = {"status": "COMPLETE", "missing": [{"season": 1, "episode": 1}],
               "new_files": [{"episode_key": "S01E01", "fid": "saved"}]}
        result = self.timing(schedule([timed(1, "2026-10-16T04:00:00Z"), timed(2, "2026-10-17T04:00:00Z")]), run)
        self.assertEqual(result["next_check_at"], datetime(2026, 10, 17, 4, 30, tzinfo=timezone.utc))

    def test_next_check_is_always_after_now(self):
        run = {"status": "WAITING", "missing": [{"episode_key": "S01E01"}], "new_files": []}
        result = self.timing(schedule([]), run, interval_hours=0)
        self.assertEqual(result["next_check_at"], NOW + timedelta(hours=6))

    def test_daily_fixed_time_plan(self):
        plan = self.timing(schedule(daily(8, 18)))["wake_plan"]
        self.assertEqual((plan["mode"], plan["rrule"], plan["summary"]),
                         ("fixed_times", "FREQ=DAILY;BYHOUR=0,6,12,18;BYMINUTE=30",
                          "每天 12:30 检查，18:30、00:30、06:30 补查"))
        self.assertIn("每天 12:00", plan["basis"])
        self.assertIn("Trakt", plan["basis"])
        late = self.timing(schedule(daily(8, 18, hour_utc=12)))["wake_plan"]
        self.assertEqual((late["rrule"], late["summary"]),
                         ("FREQ=DAILY;BYHOUR=2,8,14,20;BYMINUTE=30", "每天 20:30 检查，02:30、08:30、14:30 补查"))

    def test_weekly_plan_and_inconsistent_times_fall_back_to_interval(self):
        weekly = [timed(index + 1, f"2026-10-{day}T12:00:00Z") for index, day in enumerate((12, 13, 19, 20, 26, 27))]
        plan = self.timing(schedule(weekly))["wake_plan"]
        self.assertEqual((plan["mode"], plan["rrule"]), ("fixed_times", "FREQ=DAILY;BYHOUR=2,8,14,20;BYMINUTE=30"))
        self.assertIn("每周一、二 20:00", plan["basis"])
        irregular = self.timing(schedule([timed(1, "2026-10-16T04:00:00Z"), timed(2, "2026-10-18T12:00:00Z")]))
        self.assertEqual(irregular["next_check_at"], NOW + timedelta(hours=6))
        mixed = [timed(1, "2026-10-16T04:00:00Z"), timed(2, "2026-10-18T12:00:00Z")]
        plan = self.timing(schedule(mixed))["wake_plan"]
        self.assertEqual((plan["mode"], plan["rrule"]), ("interval", "FREQ=HOURLY;INTERVAL=6"))
        self.assertEqual(self.timing(schedule([]), interval_hours=1.5)["wake_plan"]["rrule"], "FREQ=MINUTELY;INTERVAL=90")

    def test_date_only_uses_airs_time_hint(self):
        rows = [tmdb_row(index + 1, f"2026-10-{day:02d}") for index, day in enumerate(range(10, 25))]
        update = schedule(rows, hint={"airs_time": "12:00", "timezone": "Asia/Shanghai"})
        result = self.timing(update)
        self.assertEqual(result["next_check_at"], datetime(2026, 10, 17, 4, 30, tzinfo=timezone.utc))
        self.assertEqual(result["wake_plan"]["rrule"], "FREQ=DAILY;BYHOUR=0,6,12,18;BYMINUTE=30")
        self.assertEqual(self.timing(schedule(rows))["wake_plan"]["mode"], "interval")

    def test_stop_and_changed_flags(self):
        finished = schedule([timed(1, "2026-10-15T04:00:00Z"), timed(2, "2026-10-16T04:00:00Z")], status="Ended")
        plan = self.timing(finished)["wake_plan"]
        self.assertTrue(plan["stop"])
        self.assertTrue(plan["changed"])
        again = self.timing(finished, previous_plan=plan)["wake_plan"]
        self.assertFalse(again["changed"])
        waiting = {"status": "WAITING", "missing": [{"episode_key": "S01E02"}], "new_files": []}
        resumed = self.timing(finished, waiting, previous_plan=plan)["wake_plan"]
        self.assertFalse(resumed["stop"])
        self.assertTrue(resumed["changed"])


if __name__ == "__main__":
    unittest.main()
