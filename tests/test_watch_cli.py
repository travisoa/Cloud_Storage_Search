"""Subscription CLI authority and scheduling tests; every cloud call is mocked."""
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from media_helper.headless_cli import main
from media_helper.journal import run_lock
from media_helper.sources import safe_source
from media_helper.subscriptions import SubscriptionStore
from media_helper.tasks import TaskStore
from media_helper.workflow import _digest


URL = "https://pan.quark.cn/s/0a1b2c3d4e5f"


class WatchCommandsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = {"library_root_id": "root", "metadata_source": "tmdb",
                       "pansou_url": "http://127.0.0.1:8888"}
        self.request = {"title": "测试剧", "year": 2026, "season": 1, "series_id": "17",
                        "metadata_source": "tmdb", "library_root_id": "root",
                        "quality_preference": ["2160p"]}
        self.source = safe_source(URL)
        self.candidate_id = _digest((self.source["source_hash"], "source2"))
        self.receipt = {"status": "COMPLETE", "plan": [
            {"candidate_id": self.candidate_id, "source": {"fid": "source2", "name": "02.mkv",
                "parent_id": "source4k", "path": "测试剧/4K/02.mkv", "size": 400},
             "media_info": {"resolution": "2160p", "inspection": "release_labels_only"}}],
            "source_episode_mapping_applied": [{"candidate_id": self.candidate_id,
                "fid": "source2", "season": 1, "episode": 2, "name": "02.mkv", "size": 400}]}
        self.tasks = TaskStore(self.root / "runtime/tasks", "root")
        self.task = self.tasks.create(self.request, apply_authorized=True)
        self.task["transfer_receipt"] = deepcopy(self.receipt)
        self.tasks.save(self.task)
        self.store = SubscriptionStore(self.root / "runtime/subscriptions.json", "root")
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("media_helper.headless_cli.ROOT", self.root))
        self.stack.enter_context(patch("media_helper.headless_cli._config", return_value=self.config))
        self.identity = self.stack.enter_context(patch("media_helper.headless_cli._prepare_identity",
            side_effect=lambda request, config: (deepcopy(request), None, object(), None)))
        self.stack.enter_context(patch("media_helper.headless_cli._storage",
            side_effect=AssertionError("CLI tests must never construct a real drive")))
        self.runner = self.stack.enter_context(patch("media_helper.watching.run_subscription",
            side_effect=self.healthy_report))

    def healthy_report(self, subscription, *args, **kwargs):
        return {"status": "UP_TO_DATE", "subscription_id": subscription["subscription_id"],
                "existing": [], "missing": [], "future": [],
                "writes_attempted": False, "writes_performed": False}

    def command(self, arguments):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["watch", *arguments])
        return code, json.loads(output.getvalue())

    def add(self, *options):
        code, report = self.command(["add", "--task-id", self.task["task_id"], *options])
        self.assertEqual(code, 0, report)
        self.assertEqual(report["status"], "SUBSCRIBED", report)
        return report["subscription"]

    def change_receipt(self, mutate):
        task = self.tasks.load(self.task["task_id"])
        mutate(task["transfer_receipt"])
        self.tasks.save(task)

    def block(self, record):
        self.store.record_run(record["subscription_id"],
            {"status": "UNKNOWN", "reasons": ["pending_submission_requires_reconciliation"]})
        self.store.set_enabled(record["subscription_id"], False)

    def test_add_defaults_to_readonly_and_imports_proven_legacy_source(self):
        record = self.add("--source-url", URL, "--numeric-source")
        self.assertFalse(record["auto_save"])
        self.assertEqual(record["interval_hours"], 6)
        self.assertEqual(record["request"]["title"], "测试剧")
        self.assertEqual(record["preferred_sources"], [{"url": URL, "title": "测试剧",
            "source_hash": self.source["source_hash"], "parent_id": "source4k",
            "path": "测试剧/4K", "quality_tags": ["2160p"]}])
        self.assertEqual(record["numeric_policy"], {"source_hash": self.source["source_hash"],
            "parent_id": "source4k", "season": 1, "quality": "2160p"})
        self.runner.assert_not_called()

    def test_explicit_auto_save_and_secondary_identity_are_persisted(self):
        record = self.add("--source-url", URL, "--numeric-source", "--auto-save",
                          "--interval-hours", "12", "--secondary-source", "tvmaze", "--secondary-id", "91309")
        self.assertTrue(record["auto_save"])
        self.assertEqual(record["interval_hours"], 12)
        self.assertEqual(record["secondary"], {"source": "tvmaze", "series_id": "91309"})
        # Re-adding the same bound identity does not revoke standing choices.
        same = self.add()
        self.assertEqual(same["subscription_id"], record["subscription_id"])
        self.assertTrue(same["auto_save"])
        self.assertEqual(same["numeric_policy"], record["numeric_policy"])

    def test_explicit_source_must_hash_to_previously_selected_candidate(self):
        code, report = self.command(["add", "--task-id", self.task["task_id"],
                                     "--source-url", "https://pan.quark.cn/s/unrelated", "--auto-save"])
        self.assertEqual((code, report["status"]), (1, "FAILED"), report)
        self.assertIn("候选标识", report["reason"])
        self.assertEqual(self.store.list(), [])
        self.runner.assert_not_called()

    def test_numeric_policy_requires_exact_prior_file_mapping(self):
        mutations = [lambda receipt: receipt.pop("source_episode_mapping_applied"),
                     lambda receipt: receipt["source_episode_mapping_applied"][0].update(size=999),
                     lambda receipt: receipt["source_episode_mapping_applied"][0].update(fid="other"),
                     lambda receipt: receipt["source_episode_mapping_applied"][0].update(episode=3),
                     lambda receipt: receipt["source_episode_mapping_applied"][0].update(season=2),
                     lambda receipt: receipt["plan"][0]["media_info"].update(resolution="1080p")]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                task = self.tasks.load(self.task["task_id"])
                task["transfer_receipt"] = deepcopy(self.receipt)
                mutate(task["transfer_receipt"])
                self.tasks.save(task)
                code, report = self.command(["add", "--task-id", self.task["task_id"],
                                             "--source-url", URL, "--numeric-source", "--auto-save"])
                self.assertEqual((code, report["status"]), (1, "FAILED"), report)
                self.assertEqual(self.store.list(), [])

    def test_numeric_policy_rejects_non_numeric_source_even_with_matching_proof(self):
        def mutate(receipt):
            receipt["plan"][0]["source"]["name"] = "预告02.mkv"
            receipt["source_episode_mapping_applied"][0]["name"] = "预告02.mkv"
        self.change_receipt(mutate)
        code, report = self.command(["add", "--task-id", self.task["task_id"],
                                     "--source-url", URL, "--numeric-source"])
        self.assertEqual((code, report["status"]), (1, "FAILED"), report)
        self.assertEqual(self.store.list(), [])

    def test_append_source_keeps_the_recorded_sources(self):
        earlier = safe_source("https://pan.quark.cn/s/earlier123", title="测试剧 1080P")
        self.store.add(self.request, preferred_sources=[earlier])
        appended = self.add("--source-url", URL, "--append-source")
        self.assertEqual([row["url"] for row in appended["preferred_sources"]], [earlier["url"], URL])
        replaced = self.add("--source-url", URL)
        self.assertEqual([row["url"] for row in replaced["preferred_sources"]], [URL])
        code, report = self.command(["add", "--task-id", self.task["task_id"], "--append-source"])
        self.assertEqual((code, report["status"]), (1, "FAILED"), report)

    def test_source_policy_flags_are_explicit_and_reversible(self):
        record = self.add()
        self.assertEqual((record["trust_share_title"], record["wait_on_uncertain_source"], record["archive_auto"]),
                         (False, False, None))
        record = self.add("--trust-share-title", "--wait-on-uncertain", "--archive-auto", "largest")
        self.assertEqual((record["trust_share_title"], record["wait_on_uncertain_source"], record["archive_auto"]),
                         (True, True, "largest"))
        self.assertTrue(self.add()["trust_share_title"])
        record = self.add("--no-trust-share-title", "--block-on-uncertain", "--archive-auto", "off")
        self.assertEqual((record["trust_share_title"], record["wait_on_uncertain_source"], record["archive_auto"]),
                         (False, False, None))
        self.runner.assert_not_called()

    def test_secondary_requires_source_and_id_pair(self):
        for arguments in (["--secondary-source", "tvmaze"], ["--secondary-id", "91309"]):
            with self.subTest(arguments=arguments):
                code, report = self.command(["add", "--task-id", self.task["task_id"], *arguments])
                self.assertEqual((code, report["status"]), (1, "FAILED"), report)
                self.assertEqual(self.store.list(), [])

    def test_plan_dominates_apply_and_default_run_is_readonly(self):
        record = self.add("--auto-save")
        for options in ([], ["--apply", "--plan"]):
            with self.subTest(options=options):
                self.runner.reset_mock()
                code, report = self.command(["run", "--subscription-id", record["subscription_id"], *options])
                self.assertEqual((code, report["checked_count"]), (0, 1), report)
                self.assertFalse(self.runner.call_args.kwargs["apply"])
        self.runner.reset_mock()
        code, report = self.command(["run", "--apply"])
        self.assertEqual(code, 0, report)
        self.assertTrue(self.runner.call_args.kwargs["apply"])

    def test_due_checks_skip_unchanged_until_interval_and_force_still_respects_pause(self):
        record = self.add("--auto-save")
        code, first = self.command(["run", "--due", "--apply"])
        self.assertEqual((code, first["checked_count"]), (0, 1), first)
        self.assertFalse(first["meaningful_change"])
        self.runner.reset_mock()
        code, second = self.command(["run", "--due", "--apply"])
        self.assertEqual((code, second["checked_count"]), (0, 0), second)
        self.assertFalse(second["meaningful_change"])
        self.runner.assert_not_called()
        code, forced = self.command(["run", "--due", "--force", "--apply"])
        self.assertEqual((code, forced["checked_count"]), (0, 1), forced)
        self.assertFalse(forced["meaningful_change"])
        code, paused = self.command(["pause", record["subscription_id"]])
        self.assertEqual((code, paused["status"]), (0, "PAUSED"))
        self.runner.reset_mock()
        code, skipped = self.command(["run", "--due", "--force", "--apply"])
        self.assertEqual((code, skipped["checked_count"]), (0, 0), skipped)
        self.runner.assert_not_called()

    def test_busy_account_is_quiet_and_starts_no_cycle(self):
        self.add("--auto-save")
        with run_lock(self.root / "runtime/workflow.lock"):
            code, report = self.command(["run", "--due", "--apply"])
        self.assertEqual((code, report["status"]), (0, "BUSY"), report)
        self.assertFalse(report["meaningful_change"])
        self.assertFalse(report["writes_attempted"])
        self.runner.assert_not_called()

    def test_plain_resume_never_clears_persistent_block(self):
        record = self.add("--auto-save")
        self.block(record)
        code, report = self.command(["resume", record["subscription_id"]])
        self.assertEqual((code, report["status"]), (0, "ENABLED"), report)
        self.assertTrue(report["subscription"]["enabled"])
        self.assertTrue(report["subscription"]["blocked"])
        self.runner.assert_not_called()

    def test_clear_block_runs_fresh_readonly_reconciliation_for_exact_subscription(self):
        record = self.add("--auto-save")
        self.block(record)
        code, report = self.command(["resume", record["subscription_id"], "--clear-block"])
        self.assertEqual((code, report["status"]), (0, "ENABLED"), report)
        self.assertFalse(report["subscription"]["blocked"])
        self.assertFalse(report["subscription"]["needs_attention"])
        self.assertTrue(report["subscription"]["enabled"])
        self.assertFalse(self.runner.call_args.kwargs["apply"])
        self.assertTrue(self.runner.call_args.kwargs["reconcile_pending"])
        self.assertEqual(self.runner.call_args.args[0]["subscription_id"], record["subscription_id"])
        self.assertTrue(self.runner.call_args.args[0]["blocked"])

    def test_clear_block_rejects_foreign_failed_or_mutating_reconciliation(self):
        record = self.add("--auto-save")
        self.block(record)
        bad = [{"status": "UP_TO_DATE", "subscription_id": "b" * 64,
                "writes_attempted": False, "writes_performed": False},
               {"status": "UNKNOWN", "subscription_id": record["subscription_id"],
                "writes_attempted": False, "writes_performed": False},
               {"status": "COMPLETE", "subscription_id": record["subscription_id"],
                "writes_attempted": True, "writes_performed": True},
               {"status": "UP_TO_DATE", "subscription_id": record["subscription_id"]}]
        for result in bad:
            with self.subTest(result=result):
                self.runner.side_effect = None
                self.runner.return_value = deepcopy(result)
                code, report = self.command(["resume", record["subscription_id"], "--clear-block"])
                self.assertEqual((code, report["status"]), (1, "FAILED"), report)
                current = self.store.get(record["subscription_id"])
                self.assertTrue(current["blocked"])
                self.assertFalse(current["enabled"])


if __name__ == "__main__":
    unittest.main()
