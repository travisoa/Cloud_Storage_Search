"""Explicit episode replacement: save a labelled version, rename it, optionally recycle the old file."""
import unittest

from media_helper.workflow import run
from test_workflow import Journal, Metadata, Search, Storage, file, request


class ReplaceStorage(Storage):
    def __init__(self):
        super().__init__()
        self.folders["season"] = [file("old-1", "测试剧.S01E01.mp4", size=900), file("old-2", "测试剧 S01E02.mp4", size=800)]
        self.shared = [file("source-2", "测试剧.S01E02.1080p.mkv", size=2000),
                       file("raw-2", "测试剧.S01E02.mkv", size=2100)]
        self.trashed, self.fail_trash = [], False

    def trash(self, fid):
        if self.fail_trash:
            raise RuntimeError("raw response")
        self.trashed.append(fid)
        self.folders["season"] = [row for row in self.folders["season"] if row["fid"] != fid]
        return {"fid": fid, "status": "submitted"}


class ReplaceTests(unittest.TestCase):
    def setUp(self):
        self.storage, self.journal = ReplaceStorage(), Journal()

    def run_replace(self, apply=False, **changes):
        options = {"replace_episodes": ["S01E02"], "quality_preference": ["1080p"], **changes}
        return run(request(**options), self.storage, Metadata(), Search(), self.journal, apply=apply)

    def test_without_replace_existing_episodes_are_up_to_date(self):
        report = run(request(), self.storage, Metadata(), Search(), self.journal)
        self.assertEqual(report["status"], "UP_TO_DATE", report)

    def test_plan_selects_labelled_version_and_leaves_old_file_alone(self):
        report = self.run_replace()
        self.assertEqual(report["status"], "READY", report)
        self.assertEqual([row["episode_key"] for row in report["missing"]], ["S01E02"])
        self.assertEqual([item["source"]["fid"] for item in report["plan"]], ["source-2"])
        self.assertEqual(report["plan"][0]["target_name"], "测试剧 (2026)S01E02_1080P.mkv")
        self.assertEqual(report["replacements"][0]["old"]["fid"], "old-2")
        self.assertEqual((self.storage.saved, self.storage.trashed), ([], []))

    def test_apply_keeps_old_file_unless_recycling_is_authorized(self):
        report = self.run_replace(apply=True)
        self.assertEqual(report["status"], "COMPLETE", report)
        self.assertEqual(self.storage.saved, [["source-2"]])
        self.assertEqual(self.storage.trashed, [])
        self.assertIn("old-2", [row["fid"] for row in self.storage.folders["season"]])
        self.assertNotIn("old-2", [fid for fid, _ in self.storage.renamed])

    def test_authorized_replace_recycles_only_the_recorded_old_file_after_verification(self):
        report = self.run_replace(apply=True, remove_replaced=True)
        self.assertEqual(report["status"], "COMPLETE", report)
        self.assertEqual(self.storage.trashed, ["old-2"])
        self.assertEqual(report["replacements"][0]["state"], "recycled")
        self.assertEqual(list(self.journal.records), [report["job_key"]])
        names = sorted(row["name"] for row in self.storage.folders["season"])
        self.assertEqual(names, ["测试剧 (2026)S01E02_1080P.mkv", "测试剧.S01E01.mp4"])

    def test_recycle_failure_is_partial_and_never_retried_blindly(self):
        self.storage.fail_trash = True
        report = self.run_replace(apply=True, remove_replaced=True)
        self.assertEqual(report["status"], "PARTIAL", report)
        self.assertEqual(report["replacements"][0]["state"], "unknown")
        self.assertNotIn("raw response", repr(report))

    def test_other_aired_gaps_do_not_block_a_replacement(self):
        report = run(request(replace_episodes=["S01E02"], quality_preference=["1080p"]), self.storage,
                     Metadata(episodes=(1, 2, 3)), Search(), self.journal)
        self.assertEqual(report["status"], "READY", report)
        self.assertEqual([row["episode_key"] for row in report["missing_not_in_scope"]], ["S01E03"])

    def test_interrupted_cleanup_resumes_by_observation_without_resending(self):
        original = self.storage.list_files
        calls = {"n": 0}

        def flaky(parent):
            if self.storage.trashed and calls["n"] == 0:
                calls["n"] += 1
                raise RuntimeError("listing incomplete")
            return original(parent)

        self.storage.shared.append(file("source-1", "测试剧.S01E01.1080p.mkv", size=2000))
        self.storage.list_files = flaky
        first = self.run_replace(apply=True, remove_replaced=True, replace_episodes=["S01E01", "S01E02"])
        self.assertIn(first["status"], {"COMPLETE", "PARTIAL"}, first)
        again = self.run_replace(apply=True, remove_replaced=True, replace_episodes=["S01E01", "S01E02"])
        self.assertEqual(again["status"], "COMPLETE", again)
        self.assertEqual(sorted(self.storage.trashed), ["old-1", "old-2"])
        self.assertEqual({row["state"] for row in again["replacements"]}, {"recycled"})
        self.assertEqual(len(self.storage.saved), 1)

    def test_retry_after_a_stop_before_any_save_plans_the_replacement_again(self):
        storage = self.storage

        class Interrupting(Journal):
            hooked = False

            def put(inner, key, record):
                super().put(key, record)
                if record.get("state") == "ready" and not inner.hooked:
                    inner.hooked = True
                    storage.folders["season"].append(file("intruder", "测试剧.S01E02.720p.mp4", size=5))

        self.journal = Interrupting()
        first = self.run_replace(apply=True, remove_replaced=True)
        self.assertIn("episode_appeared_before_save", first["reasons"], first)
        storage.folders["season"] = [row for row in storage.folders["season"] if row["fid"] != "intruder"]
        again = self.run_replace(apply=True, remove_replaced=True)
        self.assertEqual(again["status"], "COMPLETE", again)
        self.assertNotIn("replacement_already_completed", again["reasons"])
        self.assertEqual((storage.saved, storage.trashed), ([["source-2"]], ["old-2"]))

    def test_partially_saved_replacement_is_not_reported_complete(self):
        self.storage.shared.append(file("source-1", "测试剧.S01E01.1080p.mkv", size=2000))
        both = ["S01E01", "S01E02"]
        first = self.run_replace(apply=True, replace_episodes=both)
        self.assertEqual(first["status"], "COMPLETE", first)
        for operation in self.journal.records[first["job_key"]]["operations"]:
            operation["verified_files"] = [item for item in operation["verified_files"]
                                           if item["episode_key"] != "S01E01"]
        again = self.run_replace(apply=True, replace_episodes=both)
        self.assertEqual(again["status"], "NEEDS_INPUT", again)
        self.assertIn("replacement_partially_completed_rerun_pending_episodes", again["reasons"])
        self.assertEqual((again["completed_episodes"], again["pending_episodes"]), (["S01E02"], ["S01E01"]))
        self.assertEqual(len(self.storage.saved), 1)

    def test_risk_control_while_recycling_stops_all_further_writes(self):
        from media_helper.providers import RiskControlError
        self.storage.shared.append(file("source-1", "测试剧.S01E01.1080p.mkv", size=2000))
        attempts = []

        def blocked(fid):
            attempts.append(fid)
            raise RiskControlError("captcha required", step="trash")

        both = ["S01E01", "S01E02"]
        self.storage.trash = blocked
        report = self.run_replace(apply=True, remove_replaced=True, replace_episodes=both)
        self.assertEqual(report["status"], "RISK_CONTROL", report)
        self.assertEqual(attempts, ["old-1"])
        self.assertEqual({row["episode_key"]: row["state"] for row in report["replacements"]},
                         {"S01E01": "unknown", "S01E02": "kept"})
        del self.storage.trash
        again = self.run_replace(apply=True, remove_replaced=True, replace_episodes=both)
        # The uncertain request is only observed; the untouched episode proceeds.
        self.assertEqual((attempts, self.storage.trashed), (["old-1"], ["old-2"]))
        self.assertEqual({row["episode_key"]: row["state"] for row in again["replacements"]},
                         {"S01E01": "unknown", "S01E02": "recycled"})

    def test_unlabelled_replacement_is_refused(self):
        self.storage.shared = [file("raw-2", "测试剧.S01E02.mkv", size=2100)]
        report = self.run_replace(quality_preference=["1080p"], include_media_tags=True)
        self.assertEqual(report["status"], "NEEDS_INPUT")

    def test_replace_requires_exactly_one_existing_file_and_valid_keys(self):
        self.storage.folders["season"].append(file("old-2b", "测试剧.S01E02.1080p.mkv", size=10))
        self.assertIn("replace_episode_requires_one_existing_file_in_destination", self.run_replace()["reasons"])
        bad = run(request(replace_episodes=["S02E01"]), self.storage, Metadata(), Search(), self.journal)
        self.assertIn("replace_episodes_must_be_unique_keys_of_the_requested_season", bad["reasons"])
        lone = run(request(remove_replaced=True), self.storage, Metadata(), Search(), self.journal)
        self.assertIn("remove_replaced_requires_replace_episodes", lone["reasons"])


if __name__ == "__main__":
    unittest.main()
