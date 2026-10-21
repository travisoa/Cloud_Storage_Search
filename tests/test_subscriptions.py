from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from media_helper.sources import safe_source
from media_helper.subscriptions import SubscriptionStore


REQUEST = {"title": "白色黑手党时代", "year": 2026, "season": 1, "series_id": "17", "metadata_source": "tvmaze"}
NOW = datetime(2026, 10, 17, 9, tzinfo=timezone(timedelta(hours=8)))
SOURCE = safe_source("https://pan.quark.cn/s/abcdef123", title="白色黑手党时代 4K",
                     parent_id="sourcefolder", path="白色黑手党时代/4K", quality_tags=["2160p"])


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.path = self.directory / "private" / "subscriptions.json"
        self.store = SubscriptionStore(self.path, "library-root")

    def add(self, **options):
        return self.store.add(REQUEST, **options)

    def report(self, status="UP_TO_DATE", **changes):
        return {"status": status, "existing": [{"episode_key": "S01E01"}], "missing": [], "future": [],
                "writes_attempted": False, "writes_performed": False, "reasons": [], **changes}

    def run_record(self, record, report, **changes):
        return self.store.record_run(record["subscription_id"], report, checked_at=NOW, **changes)

    def test_defaults_are_private_root_bound_and_immediately_due(self):
        record = self.add()
        self.assertEqual(record["request"]["library_root_id"], "library-root")
        self.assertTrue(record["enabled"])
        self.assertFalse(record["auto_save"])
        self.assertEqual(record["interval_hours"], 6)
        self.assertEqual(self.store.due(NOW), [record])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)

    def test_duplicate_identity_survives_alias_and_numeric_id_normalization(self):
        first = self.add(auto_save=True, interval_hours=12, preferred_sources=[SOURCE])
        second = self.store.add({**REQUEST, "title": "白色黑手党", "series_id": 17})
        self.assertEqual(first["subscription_id"], second["subscription_id"])
        self.assertEqual(len(self.store.list()), 1)
        self.assertTrue(second["auto_save"])
        self.assertEqual(second["interval_hours"], 12)
        self.assertEqual(second["preferred_sources"], [SOURCE])
        third = self.store.add(REQUEST, auto_save=False, interval_hours=6, preferred_sources=[])
        self.assertFalse(third["auto_save"])
        self.assertEqual(third["preferred_sources"], [])

    def test_identity_source_id_year_season_and_root_never_cross_scope(self):
        first = self.add()
        for change in ({"metadata_source": "tmdb"}, {"series_id": "18"}, {"year": 2025}, {"season": 2}):
            another = self.store.add({**REQUEST, **change})
            self.assertNotEqual(first["subscription_id"], another["subscription_id"])
        with self.assertRaises(ValueError):
            SubscriptionStore(self.path, "another-root").list()
        with self.assertRaises(ValueError):
            self.store.add({**REQUEST, "library_root_id": "another-root"})

    def test_identity_must_already_be_confirmed_and_valid(self):
        for field in REQUEST:
            request = deepcopy(REQUEST)
            request.pop(field)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.store.add(request)
        for field, value in (("season", True), ("season", 0), ("year", "2026"), ("series_id", 0),
                             ("metadata_source", "auto"), ("title", "../别的目录"), ("title", "测试\n剧")):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.store.add({**REQUEST, field: value})

    def test_saving_request_drops_single_run_decisions_and_text(self):
        record = self.store.add({**REQUEST, "request_text": "请转存 https://pan.quark.cn/s/abc?pwd=1234",
                                 "source_selection": {"S01E01": {"url": "https://pan.quark.cn/s/abc?pwd=1234"}},
                                 "source_episode_mapping": {"a" * 64: {"episode": 1}}})
        self.assertNotIn("source_selection", record["request"])
        self.assertNotIn("source_episode_mapping", record["request"])
        self.assertNotIn("request_text", record["request"])
        self.assertNotIn("1234", self.path.read_text())

    def test_options_reject_credentials_and_unsafe_sources(self):
        for options in ({"preferred_sources": [{"url": SOURCE["url"], "stoken": "secret"}]},
                        {"preferred_sources": [SOURCE["url"] + "?pwd=secret"]},
                        {"preferred_sources": [{**SOURCE, "title": "密码 secret"}]},
                        {"secondary": {"source": "tmdb", "series_id": 1, "api_key": "secret"}}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.add(**options)
        with self.assertRaises(ValueError):
            self.store.add({**REQUEST, "Cookie": "secret"})
        self.assertFalse(self.path.exists())

    def test_interval_authority_and_source_type_are_strict(self):
        for interval in (True, 0, 169, "6", float("nan"), float("inf")):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                self.add(interval_hours=interval)
        for value in (1, "yes", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.add(auto_save=value)
        with self.assertRaises(ValueError):
            self.add(preferred_sources=SOURCE)

    def test_sources_preserve_distinct_confirmed_directories_and_dedupe_exact_repeats(self):
        second = safe_source(SOURCE, parent_id="anotherfolder", path="白色黑手党时代/1080p", quality_tags=["1080p"])
        record = self.add(preferred_sources=[SOURCE, SOURCE, second])
        self.assertEqual(record["preferred_sources"], [SOURCE, second])
        self.store.update_sources(record["subscription_id"], [second])
        self.assertEqual(self.store.get(record["subscription_id"])["preferred_sources"], [second])

    def test_numeric_policy_is_limited_to_confirmed_source_directory_and_season(self):
        policy = {"source_hash": SOURCE["source_hash"], "parent_id": SOURCE["parent_id"], "season": 1, "quality": "4K"}
        record = self.add(preferred_sources=[SOURCE], numeric_policy=policy)
        self.assertEqual(record["numeric_policy"]["quality"], "2160p")
        for change in ({"source_hash": "0" * 64}, {"parent_id": "other"}, {"season": 2}, {"quality": "best"}, {"guess_all": True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.add(preferred_sources=[SOURCE], numeric_policy={**policy, **change})
        with self.assertRaises(ValueError):
            self.store.update_sources(record["subscription_id"], [])

    def test_secondary_ids_keep_explicit_source_namespace(self):
        record = self.add(secondary={"source": "tmdb", "series_id": 17})
        self.assertEqual(record["secondary"], {"source": "tmdb", "series_id": "17"})
        for secondary in ({"source": "tvmaze", "series_id": 99}, {"series_id": 17},
                          {"source": "tmdb", "series_id": 0}, {"source": "auto", "series_id": 17}):
            with self.subTest(secondary=secondary), self.assertRaises(ValueError):
                self.add(secondary=secondary)

    def test_due_uses_aware_times_and_disabled_records_are_excluded(self):
        record = self.add()
        result = self.run_record(record, self.report())
        self.assertEqual(result["record"]["last_checked_at"], "2026-10-17T01:00:00+00:00")
        self.assertEqual(self.store.due(NOW + timedelta(hours=5, minutes=59)), [])
        self.assertEqual(len(self.store.due(NOW + timedelta(hours=6))), 1)
        self.store.set_enabled(record["subscription_id"], False)
        self.assertEqual(self.store.due(NOW + timedelta(days=2)), [])
        for value in ("2026-10-17T09:00:00", datetime(2026, 10, 17), "invalid", 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.due(value)
        with self.assertRaises(ValueError):
            self.run_record(record, self.report(), next_check_at=NOW)

    def test_first_healthy_run_and_unchanged_checks_stay_quiet(self):
        record = self.add()
        first = self.run_record(record, self.report())
        self.assertFalse(first["meaningful_change"])
        second = self.run_record(record, self.report("WAITING", future=[{"episode_key": "S01E02"}],
                                                     task_id="a" * 32, checked_at="ignored", reasons=["resource_not_ready"]))
        self.assertFalse(second["meaningful_change"])
        third = self.run_record(record, self.report("COMPLETE", task_id="b" * 32))
        self.assertFalse(third["meaningful_change"])

    def test_missing_episode_changes_notify_even_when_count_stays_same(self):
        record = self.add()
        self.assertTrue(self.run_record(record, self.report("WAITING", missing=[{"episode_key": "S01E02"}]))["meaningful_change"])
        self.assertFalse(self.run_record(record, self.report("WAITING", missing=[{"episode_key": "S01E02"}]))["meaningful_change"])
        self.assertTrue(self.run_record(record, self.report("WAITING", missing=[{"episode_key": "S01E03"}]))["meaningful_change"])
        self.assertTrue(self.run_record(record, self.report())["meaningful_change"])

    def test_only_new_verified_file_fids_notify_once(self):
        record = self.add()
        report = self.report("COMPLETE", operations=[{"verified_files": [
            {"saved_fid": "newfid", "target_name": "测试剧 S01E02.mkv", "episode_key": "S01E02", "verified": True},
            {"saved_fid": "unverified", "target_name": "测试剧 S01E03.mkv", "verified": False}]}])
        first = self.run_record(record, report)
        self.assertTrue(first["meaningful_change"])
        self.assertEqual(first["new_files"], [{"fid": "newfid", "name": "测试剧 S01E02.mkv", "episode_key": "S01E02"}])
        self.assertFalse(self.run_record(record, report)["meaningful_change"])
        self.assertFalse(self.run_record(record, self.report())["meaningful_change"])
        self.assertFalse(self.run_record(record, report)["meaningful_change"])

    def test_attention_changes_notify_but_do_not_repeat_or_clear_implicitly(self):
        record = self.add(auto_save=True)
        first = self.run_record(record, self.report("UNKNOWN", reasons=["save_outcome_unknown"]))
        self.assertTrue(first["meaningful_change"])
        self.assertTrue(first["record"]["blocked"])
        self.assertFalse(self.run_record(record, self.report("UNKNOWN", reasons=["save_outcome_unknown"]))["meaningful_change"])
        self.assertTrue(self.run_record(record, self.report("UNKNOWN", reasons=["saved_fid_not_found"]))["meaningful_change"])
        self.store.set_enabled(record["subscription_id"], True)
        self.assertTrue(self.store.get(record["subscription_id"])["blocked"])
        self.assertTrue(self.add()["blocked"])
        self.assertTrue(self.run_record(record, self.report())["record"]["blocked"])

    def test_future_unknown_status_is_fail_closed(self):
        record = self.add()
        result = self.run_record(record, self.report("NEW_PROVIDER_STATUS"))
        self.assertTrue(result["record"]["blocked"])
        self.assertTrue(result["meaningful_change"])

    def test_clear_block_requires_explicit_successful_strictly_readonly_reconciliation(self):
        record = self.add()
        self.run_record(record, self.report("NEEDS_AUTH"))
        for report in ({"status": "COMPLETE"}, self.report("UNKNOWN"), self.report(writes_performed=True),
                       self.report(writes_attempted=0), self.report("METADATA_CONFLICT")):
            with self.subTest(report=report), self.assertRaises(ValueError):
                self.store.clear_block(record["subscription_id"], report)
        cleared = self.store.clear_block(record["subscription_id"], self.report("READY_TO_RENAME"))
        self.assertFalse(cleared["blocked"])
        self.assertFalse(cleared["needs_attention"])
        self.assertEqual(cleared["reconciliation"]["status"], "READY_TO_RENAME")

    def test_run_summary_whitelists_data_instead_of_persisting_raw_report(self):
        record = self.add()
        report = self.report("NEEDS_INPUT", reasons=["source_conflict", "do not save this freeform text"],
                             error={"error_type": "ProviderError", "step": "share_inventory", "message": "cookie=secret"},
                             source_refs=[{"url": SOURCE["url"] + "?pwd=secret"}],
                             source_candidates=[{"stoken": "secret"}], cookie="secret",
                             task_id="a" * 32, resource_checks=[{"source": SOURCE["source_hash"], "status": "enumerated", "token": "secret"}])
        result = self.run_record(record, report)
        text = self.path.read_text()
        self.assertNotIn("secret", text)
        self.assertNotIn("source_candidates", text)
        self.assertEqual(result["summary"]["last_task_id"], "a" * 32)
        self.assertEqual(result["summary"]["reason_ids"], ["source_conflict"])

    def test_metadata_changes_ignore_fetch_time_but_keep_conflict_and_degraded_semantics(self):
        record = self.add()
        updates = {"status": "degraded", "errors": [{"source": "tmdb", "code": "http_error", "http_status": 429,
                                                        "retry_after_seconds": 30}], "conflicts": [], "fetched_at": "old"}
        first = self.run_record(record, self.report(metadata_updates=updates))
        self.assertTrue(first["meaningful_change"])
        updates["fetched_at"] = "new"
        updates["errors"][0]["retry_after_seconds"] = 60
        self.assertFalse(self.run_record(record, self.report(metadata_updates=updates))["meaningful_change"])
        updates = {"status": "conflict", "errors": [], "conflicts": [{"code": "episode_schedule_mismatch", "episode_key": "S01E03",
                                                                       "evidence": [{"raw": "private"}]}]}
        changed = self.run_record(record, self.report("METADATA_CONFLICT", metadata_updates=updates))
        self.assertTrue(changed["meaningful_change"])
        self.assertTrue(changed["record"]["blocked"])
        self.assertNotIn("private", self.path.read_text())

    def test_initial_successful_metadata_check_and_future_counts_are_quiet(self):
        record = self.add()
        self.assertFalse(self.run_record(record, self.report())["meaningful_change"])
        updates = {"status": "ok", "conflicts": [], "errors": [], "fetched_at": "new"}
        result = self.run_record(record, self.report(metadata_updates=updates, future=[{"episode_key": "S01E04"}]))
        self.assertFalse(result["meaningful_change"])

    def test_stale_run_cannot_overwrite_newer_check(self):
        record = self.add()
        self.run_record(record, self.report())
        with self.assertRaises(ValueError):
            self.store.record_run(record["subscription_id"], self.report("UNKNOWN"), checked_at=NOW - timedelta(hours=1))
        self.assertFalse(self.store.get(record["subscription_id"])["blocked"])

    def test_record_and_lock_symlinks_are_rejected(self):
        self.path.parent.mkdir()
        other = self.directory / "other.json"
        other.write_text("{}")
        self.path.symlink_to(other)
        with self.assertRaises(ValueError):
            self.add()
        self.path.unlink()
        self.path.with_name(self.path.name + ".lock").symlink_to(other)
        with self.assertRaises(ValueError):
            self.add()

    def test_parent_symlink_and_nonprivate_records_are_rejected(self):
        real = self.directory / "real"
        real.mkdir()
        self.path.parent.symlink_to(real, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.add()
        self.path.parent.unlink()
        self.add()
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.store.list()

    def test_corrupt_identity_or_unsafe_source_cannot_be_loaded(self):
        record = self.add(preferred_sources=[SOURCE])
        original = json.loads(self.path.read_text())
        for change in ("root", "source", "identity"):
            data = deepcopy(original)
            row = data["subscriptions"][record["subscription_id"]]
            if change == "root":
                row["request"]["library_root_id"] = "different"
            elif change == "source":
                row["preferred_sources"][0]["url"] += "?pwd=secret"
            else:
                row["request"]["series_id"] = "999"
            self.path.write_text(json.dumps(data))
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.store.list()

    def test_source_policies_default_off_persist_and_keep_prior_choices(self):
        record = self.add()
        self.assertEqual({key: record[key] for key in ("trust_share_title", "wait_on_uncertain_source", "archive_auto")},
                         {"trust_share_title": False, "wait_on_uncertain_source": False, "archive_auto": None})
        record = self.add(trust_share_title=True, wait_on_uncertain_source=True, archive_auto="largest")
        same = self.add(auto_save=True)
        self.assertEqual({key: same[key] for key in ("trust_share_title", "wait_on_uncertain_source", "archive_auto")},
                         {"trust_share_title": True, "wait_on_uncertain_source": True, "archive_auto": "largest"})
        self.assertEqual(self.store.get(record["subscription_id"])["archive_auto"], "largest")
        self.assertIsNone(self.add(archive_auto=None)["archive_auto"])
        for options in ({"trust_share_title": "yes"}, {"wait_on_uncertain_source": 1},
                        {"archive_auto": "all"}, {"archive_auto": True}, {"append_sources": "yes"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.add(**options)

    def test_records_written_before_source_policies_still_load(self):
        record = self.add()
        data = json.loads(self.path.read_text())
        for field in ("trust_share_title", "wait_on_uncertain_source", "archive_auto"):
            data["subscriptions"][record["subscription_id"]].pop(field)
        self.path.write_text(json.dumps(data))
        self.assertNotIn("archive_auto", self.store.get(record["subscription_id"]))
        data["subscriptions"][record["subscription_id"]]["archive_auto"] = "everything"
        self.path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            self.store.list()

    def test_append_sources_keeps_recorded_sources_ahead_of_new_ones(self):
        second = safe_source("https://pan.quark.cn/s/second99", title="白色黑手党时代 DV",
                             parent_id="dvfolder", path="白色黑手党时代/DV", quality_tags=["2160p"])
        self.add(preferred_sources=[SOURCE])
        appended = self.add(preferred_sources=[second, SOURCE], append_sources=True)
        self.assertEqual(appended["preferred_sources"], [SOURCE, second])
        replaced = self.add(preferred_sources=[second])
        self.assertEqual(replaced["preferred_sources"], [second])

    def test_unknown_or_filesystem_subscription_ids_are_rejected(self):
        for identifier in ("../elsewhere", "a" * 64, "/tmp/task", None):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                self.store.get(identifier)
        with self.assertRaises(ValueError):
            self.store.set_enabled("a" * 64, "true")


if __name__ == "__main__":
    unittest.main()
