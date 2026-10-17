"""Offline subscription safety and existing workflow integration checks."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from media_helper.providers import AuthenticationError, RiskControlError
from media_helper.sources import safe_source
from media_helper.tasks import TaskStore
from media_helper.watching import run_subscription, select_ready
from media_helper import workflow
from tests.test_workflow import Metadata, Storage, file, request


SOURCE = safe_source({"url": "https://pan.quark.cn/s/0a1b2c3d4e5f", "title": "测试剧",
                      "parent_id": "source4k", "path": "测试剧/4K", "quality_tags": ["2160p"]})


def candidate(identifier="candidate2", **changes):
    return {"candidate_id": identifier, "fid": "source2", "name": "02.mkv", "size": 100,
            "parent_id": "source4k", "suggested_episode": 2, "episode_key": None,
            "share_source": {"source_hash": SOURCE["source_hash"]},
            "media_info": {"resolution": "2160p"}, "score": 200, **changes}


def subscription(**changes):
    return {"subscription_id": "a" * 64,
            "request": request(quality_preference=["2160p"]), "auto_save": True,
            "blocked": False, "preferred_sources": [deepcopy(SOURCE)],
            "numeric_policy": {"source_hash": SOURCE["source_hash"], "parent_id": "source4k",
                               "season": 1, "quality": "2160p"}, **changes}


class SelectionTests(unittest.TestCase):
    def select(self, candidates, **changes):
        sub = subscription(**changes)
        report = {"missing": [{"episode_key": "S01E02"}], "source_candidates": candidates}
        return select_ready(sub["request"], report, sub)

    def test_numeric_policy_produces_observed_name_size_mapping(self):
        chosen, mapped, ambiguous = self.select([candidate()])
        self.assertEqual(chosen, {"S01E02": "candidate2"})
        self.assertEqual(mapped, {"candidate2": {"season": 1, "episode": 2, "name": "02.mkv", "size": 100}})
        self.assertEqual(ambiguous, [])

    def test_numeric_policy_never_authorizes_other_source_parent_or_season(self):
        cases = [(candidate(share_source={"source_hash": "b" * 64}), {}),
                 (candidate(parent_id="different-folder"), {}),
                 (candidate(), {"numeric_policy": {**subscription()["numeric_policy"], "season": 2}}),
                 (candidate(), {"numeric_policy": None})]
        for item, changes in cases:
            with self.subTest(item=item, changes=changes):
                self.assertEqual(self.select([item], **changes), ({}, {}, ["S01E02"]))

    def test_numeric_suggestion_requires_matching_pure_numeric_filename(self):
        for name in ("03.mkv", "02-花絮.mkv", "测试剧02.mkv", "S02E02.mkv"):
            with self.subTest(name=name):
                chosen, mapped, _ = self.select([candidate(name=name)])
                self.assertEqual((chosen, mapped), ({}, {}))

    def test_quality_is_exact_and_zero_or_unverified_sizes_are_ineligible(self):
        for changes in ({"media_info": {"resolution": "1080p"}}, {"media_info": {}},
                        {"size": 0}, {"size": None}, {"size": True}):
            with self.subTest(changes=changes):
                self.assertEqual(self.select([candidate(**changes)]), ({}, {}, []))
        policy = {**subscription()["numeric_policy"], "quality": "1080p"}
        self.assertEqual(self.select([candidate()], numeric_policy=policy), ({}, {}, ["S01E02"]))

    def test_explicit_episode_does_not_require_bare_number_authorization(self):
        row = candidate(name="测试剧.S01E02.2160p.mkv", episode_key="S01E02", suggested_episode=None,
                        share_source={"source_hash": "b" * 64}, parent_id="other")
        self.assertEqual(self.select([row], numeric_policy=None), ({"S01E02": "candidate2"}, {}, []))

    def test_tied_versions_require_input_and_already_present_episodes_are_ignored(self):
        self.assertEqual(self.select([candidate("first"), candidate("second")]), ({}, {}, ["S01E02"]))
        self.assertEqual(self.select([candidate(episode_key="S01E01", suggested_episode=None)]), ({}, {}, []))

    def test_unapproved_source_needs_title_in_real_file_or_path(self):
        row = candidate(name="其他剧.S01E02.2160p.mkv", episode_key="S01E02", suggested_episode=None,
                        share_source={"source_hash": "b" * 64}, parent_id="other")
        self.assertEqual(self.select([row]), ({}, {}, ["S01E02"]))
        row.update(name="S01E02.2160p.mkv", path="测试剧/Season 01/S01E02.2160p.mkv")
        self.assertEqual(self.select([row]), ({"S01E02": "candidate2"}, {}, []))

    def test_confirmed_source_wins_over_higher_scoring_fallback_for_same_episode(self):
        preferred = candidate("preferred", episode_key="S01E02", suggested_episode=None, score=10)
        fallback = candidate("fallback", episode_key="S01E02", suggested_episode=None, score=999,
                             share_source={"source_hash": "b" * 64}, parent_id="other")
        self.assertEqual(self.select([fallback, preferred]), ({"S01E02": "preferred"}, {}, []))


class WatchStorage(Storage):
    def __init__(self):
        super().__init__()
        self.shared = [file("source2", "02.mkv", size=400)]
        self.share_calls = []

    def share_files(self, url, password=None, parent_id="0"):
        self.share_calls.append((url, parent_id))
        listing = super().share_files(url, password, parent_id)
        if parent_id == "0":
            listing["files"] = [file("shareSeries", "测试剧", "0", directory=True)]
        elif parent_id == "shareSeries":
            listing["files"] = [file("source4k", "4K", "shareSeries", directory=True)]
        elif parent_id != "source4k":
            raise AssertionError("unexpected source directory")
        return listing


class SubscriptionCycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = {"library_root_id": "root", "pansou_url": "http://127.0.0.1:8888"}
        self.primary = Metadata(episodes=(1, 2))
        self.primary.source = "tmdb"
        self.storage, self.fallback = WatchStorage(), Mock()
        self.fallback.search.return_value = []
        self.updates = self.update_report((1, 2))
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.prepare = self.stack.enter_context(patch(
            "media_helper.headless_cli._prepare_identity",
            side_effect=lambda req, cfg: (deepcopy(req), None, self.primary, None)))
        self.storage_factory = self.stack.enter_context(patch(
            "media_helper.headless_cli._storage", return_value=self.storage))
        self.collect = self.stack.enter_context(patch(
            "media_helper.watching.collect_updates", side_effect=lambda *args: deepcopy(self.updates)))
        self.stack.enter_context(patch("media_helper.watching.PanSouSearch", return_value=self.fallback))

    def update_report(self, numbers):
        return {"status": "ok", "automatic_transfer_allowed": True,
                "series": {"id": 17, "title": "测试剧", "year": 2026, "source": "tmdb"},
                "episodes": [{"season": 1, "episode": number, "episode_key": f"S01E{number:02d}",
                              "air_date": "2026-10-15", "date_precision": "date", "source": "tmdb"}
                             for number in numbers], "conflicts": [], "errors": []}

    def cycle(self, sub=None, *, apply=True):
        return run_subscription(sub or subscription(), self.config, self.root,
                                apply=apply, as_of=datetime(2026, 10, 17, 0, tzinfo=timezone.utc))

    def assert_no_writes(self):
        self.assertEqual((self.storage.saved, self.storage.renamed, self.storage.created), ([], [], []))

    def test_matching_stable_source_saves_only_missing_episode_and_keeps_folder_prefix(self):
        report = self.cycle()
        self.assertEqual(report["status"], "COMPLETE", report)
        self.assertEqual(self.storage.saved, [["source2"]])
        self.assertEqual(self.storage.renamed, [("new-source2", "测试剧 (2026) S01E02 [2160p].mkv")])
        self.assertEqual(report["missing"], [])
        self.assertEqual([row["fid"] for row in report["new_files"]], ["new-source2"])
        self.assertTrue(report["writes_performed"])
        self.assertEqual({parent for _, parent in self.storage.share_calls}, {"0", "shareSeries", "source4k"})
        self.fallback.search.assert_not_called()
        self.assertNotIn("do-not-persist", str(report))

    def test_readonly_auto_disabled_and_blocked_cycles_cannot_write(self):
        for apply, changes in ((False, {}), (True, {"auto_save": False}), (True, {"blocked": True})):
            with self.subTest(apply=apply, changes=changes):
                report = self.cycle(subscription(**changes), apply=apply)
                self.assertEqual(report["status"], "READY", report)
                self.assertFalse(report["writes_attempted"])
                self.assertFalse(report["writes_performed"])
                self.assertFalse(report["automatic_write_enabled"])
                self.assert_no_writes()

    def test_up_to_date_inventory_never_opens_share_or_search(self):
        self.updates = self.update_report((1,))
        report = self.cycle()
        self.assertEqual(report["status"], "UP_TO_DATE", report)
        self.assertEqual(report["source_strategy"], "no_missing_episodes")
        self.assertEqual(self.storage.share_calls, [])
        self.fallback.search.assert_not_called()
        self.assert_no_writes()

    def test_available_subset_is_saved_while_other_due_episode_stays_missing(self):
        self.updates = self.update_report((1, 2, 3))
        report = self.cycle()
        self.assertEqual(report["status"], "COMPLETE", report)
        self.assertEqual(self.storage.saved, [["source2"]])
        self.assertEqual([row["episode_key"] for row in report["missing"]], ["S01E03"])
        self.assertEqual(report["pending_resource_episodes"], ["S01E03"])
        self.assertEqual(report["reasons"], ["available_missing_episodes_saved_others_pending"])
        self.assertEqual(report["source_strategy"], "preferred_then_search")
        self.fallback.search.assert_called()

    def test_other_quality_never_silently_downgrades(self):
        self.storage.shared = [file("source2", "测试剧.S01E02.1080p.mkv", size=100)]
        # The preferred directory also says 4K. The filename has stronger label
        # evidence and must still be rejected under the saved 2160p preference.
        report = self.cycle()
        self.assertEqual(report["status"], "WAITING", report)
        self.assertEqual([row["episode_key"] for row in report["missing"]], ["S01E02"])
        self.assert_no_writes()

    def test_untrusted_bare_number_source_needs_confirmation(self):
        sub = subscription(numeric_policy=None)
        report = self.cycle(sub)
        self.assertEqual(report["status"], "NEEDS_INPUT", report)
        self.assertEqual(report["ambiguous_episodes"], ["S01E02"])
        self.assert_no_writes()
        self.fallback.search.assert_not_called()

    def test_schedule_conflicts_or_auth_failures_stop_before_drive_construction(self):
        cases = [({"conflicts": [{"code": "episode_schedule_mismatch"}], "errors": []}, "NEEDS_INPUT"),
                 ({"conflicts": [], "errors": [{"http_status": 401}]}, "NEEDS_AUTH"),
                 ({"conflicts": [], "errors": [{"http_status": 403}]}, "NEEDS_AUTH"),
                 ({"conflicts": [], "errors": [{"http_status": 429}]}, "RISK_CONTROL"),
                 ({"conflicts": [], "errors": [{"code": "network_unavailable"}]}, "WAITING")]
        for fields, status in cases:
            with self.subTest(status=status, fields=fields):
                self.updates.update(automatic_transfer_allowed=False, **fields)
                report = self.cycle()
                self.assertEqual(report["status"], status, report)
                self.storage_factory.assert_not_called()
                self.assert_no_writes()

    def test_drive_auth_exceptions_are_attention_statuses_and_stop(self):
        for error, expected in ((AuthenticationError("login needed"), "NEEDS_AUTH"),
                                (RiskControlError("captcha needed"), "RISK_CONTROL")):
            with self.subTest(expected=expected):
                self.storage_factory.side_effect = error
                report = self.cycle()
                self.assertEqual(report["status"], expected, report)
                self.assert_no_writes()

    def test_persisted_execution_keeps_exact_numeric_mapping_for_recovery(self):
        report = self.cycle()
        self.assertEqual(report["status"], "COMPLETE", report)
        task = TaskStore(self.root / "runtime/tasks", "root").load(report["task_id"])
        self.assertEqual(set(task["resolved_request"]["source_selection"]), {"S01E02"})
        mapping = next(iter(task["resolved_request"]["source_episode_mapping"].values()))
        self.assertEqual(mapping, {"season": 1, "episode": 2, "name": "02.mkv", "size": 400})
        self.assertEqual(task["library_root_id"], "root")

    def test_unknown_outcome_requires_successful_reconciliation_before_any_resume(self):
        with patch("media_helper.watching.workflow.run", return_value={"status": "UNKNOWN"}) as run:
            with patch("media_helper.watching.workflow.reconcile", return_value={"status": "UNKNOWN"}) as reconcile:
                report = self.cycle()
        self.assertEqual(report["status"], "UNKNOWN")
        self.assertEqual(run.call_count, 1)
        self.assertFalse(run.call_args.kwargs["apply"])
        reconcile.assert_called_once()
        self.assert_no_writes()

    def test_readonly_or_blocked_unknown_outcome_does_not_resume(self):
        for apply, changes in ((False, {}), (True, {"blocked": True})):
            with self.subTest(apply=apply, changes=changes):
                with patch("media_helper.watching.workflow.run", return_value={"status": "UNKNOWN"}) as run:
                    with patch("media_helper.watching.workflow.reconcile") as reconcile:
                        report = self.cycle(subscription(**changes), apply=apply)
                self.assertEqual(report["status"], "UNKNOWN")
                self.assertEqual(run.call_count, 1)
                self.assertFalse(run.call_args.kwargs["apply"])
                reconcile.assert_not_called()
                self.assert_no_writes()

    def test_known_reconciled_submission_resumes_durable_work_without_new_selection(self):
        saved = {"status": "COMPLETE", "writes_performed": True,
                 "operations": [{"state": "complete", "verified_files": [
                     {"saved_fid": "recovered2", "verified": True, "name": "测试剧 (2026) S01E02 [2160p].mkv"}]}]}
        with patch("media_helper.watching.workflow.run", side_effect=[{"status": "UNKNOWN"}, saved]) as run:
            with patch("media_helper.watching.workflow.reconcile", return_value={"status": "READY_TO_RENAME"}) as reconcile:
                report = self.cycle()
        self.assertEqual(report["status"], "COMPLETE", report)
        self.assertEqual(report["source_strategy"], "reconciled_submission")
        self.assertEqual([row["fid"] for row in report["new_files"]], ["recovered2"])
        self.assertEqual([call.kwargs["apply"] for call in run.call_args_list], [False, True])
        reconcile.assert_called_once()
        self.fallback.search.assert_not_called()

    def test_postsave_directory_ambiguity_remains_attention_and_retains_saved_evidence(self):
        original_run = workflow.run

        def run(req, storage, metadata, search, journal, *, apply=False):
            if type(search).__name__ == "_NoSearch":
                return {"status": "NEEDS_INPUT", "reasons": ["multiple_series_directories"],
                        "missing": [], "writes_attempted": False, "writes_performed": False}
            return original_run(req, storage, metadata, search, journal, apply=apply)

        with patch("media_helper.watching.workflow.run", side_effect=run):
            report = self.cycle()
        self.assertEqual(report["status"], "NEEDS_INPUT", report)
        self.assertEqual(report["reasons"], ["multiple_series_directories"])
        self.assertTrue(report["writes_performed"])
        self.assertEqual([row["fid"] for row in report["new_files"]], ["new-source2"])
        self.assertEqual(self.storage.saved, [["source2"]])


if __name__ == "__main__":
    unittest.main()
