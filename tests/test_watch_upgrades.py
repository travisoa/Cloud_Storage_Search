"""Opt-in upgrades of existing episodes: decision rules and guarded replacement cycles."""
import unittest
from unittest.mock import Mock

from media_helper.subscriptions import SubscriptionStore
from media_helper.watching import _apply_upgrades, select_upgrades
from tests.test_watch_sources import FRESH, ZIPS, CycleCase, archive
from tests.test_watching import candidate, subscription
from tests.test_workflow import Metadata, file, request


OLD = "测试剧 (2026)S01E02_1080P.mkv"


def existing(name=OLD, size=1000, parent="season", key="S01E02"):
    return {"episode_key": key, "files": [{"fid": "old", "name": name, "size": size, "parent_id": parent,
                                           "path": "Season 01/" + name}]}


def video(identifier, resolution="2160p", size=5000, key="S01E02"):
    name = "测试剧.%s.%s.mkv" % (key, resolution)
    return candidate(identifier, name=name, path="测试剧/" + resolution + "/" + name, parent_id=resolution,
                     episode_key=key, suggested_episode=None, size=size, share_source={"source_hash": "b" * 64},
                     media_info={"resolution": resolution})


class UpgradeDecisionTests(unittest.TestCase):
    def plan(self, candidates=(), archives=(), rows=None, **changes):
        sub = subscription(**{"request": request(), "numeric_policy": None, "upgrade_existing": True, **changes})
        report = {"target_folder_id": "season", "existing": [existing()] if rows is None else rows,
                  "source_candidates": list(candidates), "archive_candidates": list(archives)}
        return select_upgrades(sub["request"], report, sub)

    def test_off_by_default(self):
        self.assertEqual(self.plan([video("4k")], upgrade_existing=False), {})

    def test_higher_resolution_needs_its_own_size_ratio(self):
        plan = self.plan([video("4k", size=1000)], upgrade_min_ratio=3.0)
        self.assertEqual((plan["S01E02"]["kind"], plan["S01E02"]["basis"]), ("video", "higher_resolution"))
        self.assertEqual(plan["S01E02"]["old"], {"fid": "old", "name": OLD, "size": 1000, "parent_id": "season"})
        self.assertEqual(self.plan([video("4k", size=1999)], upgrade_min_ratio_higher_tier=2.0), {})
        self.assertEqual(self.plan([video("4k", size=700)], upgrade_min_ratio_higher_tier=0.7)["S01E02"]["basis"],
                         "higher_resolution")

    def test_higher_resolution_but_smaller_never_replaces(self):
        self.assertEqual(self.plan([video("4k", size=999)]), {})
        # A 1.90 GB 1080p episode against a 1.35 GB low-bitrate 4K archive.
        rows = [existing("测试剧 (2026)S01E14_1080P.mkv", size=1900, key="S01E14")]
        low = archive("low", path="测试剧/4K SDR 低码/14.zip", size=1350)
        self.assertEqual(self.plan(archives=[low], rows=rows, archive_auto="largest"), {})
        self.assertEqual(self.plan([video("4k", size=1350, key="S01E14")], rows=rows), {})

    def test_a_smaller_higher_tier_leaves_room_for_a_clearly_larger_same_tier(self):
        plan = self.plan([video("4k", size=900), video("big", "1080p", size=1500)])
        self.assertEqual((plan["S01E02"]["candidate"]["candidate_id"], plan["S01E02"]["basis"]), ("big", "larger_size"))

    def test_same_resolution_needs_the_size_ratio(self):
        self.assertEqual(self.plan([video("big", "1080p", size=1499)]), {})
        self.assertEqual(self.plan([video("big", "1080p", size=1500)])["S01E02"]["basis"], "larger_size")
        self.assertEqual(self.plan([video("big", "1080p", size=1500)], upgrade_min_ratio=2.0), {})

    def test_lower_or_unlabelled_candidates_never_replace(self):
        self.assertEqual(self.plan([video("720", "720p", size=9000)]), {})
        unlabelled = candidate("x", name="测试剧.S01E02.mkv", path="测试剧/测试剧.S01E02.mkv", episode_key="S01E02",
                               suggested_episode=None, size=9000, media_info={})
        self.assertEqual(self.plan([unlabelled]), {})

    def test_an_unlabelled_old_file_is_replaced_only_by_the_ratio(self):
        rows = [existing("测试剧 S01E02.mkv")]
        self.assertEqual(self.plan([video("4k", size=1400)], rows=rows), {})
        self.assertEqual(self.plan([video("4k", size=1500)], rows=rows)["S01E02"]["basis"], "larger_size")

    def test_only_one_old_file_inside_the_season_folder_qualifies(self):
        two = {"episode_key": "S01E02", "files": [*existing()["files"], {**existing()["files"][0], "fid": "copy"}]}
        for rows in ([two], [existing(parent="elsewhere")], [existing(size=0)]):
            with self.subTest(rows=rows):
                self.assertEqual(self.plan([video("4k")], rows=rows), {})

    def test_best_tier_then_largest_and_any_authorized_share(self):
        plan = self.plan([video("small4k", size=3000), video("large4k", size=6000), video("1080", "1080p", size=9000)])
        self.assertEqual(plan["S01E02"]["candidate"]["candidate_id"], "large4k")
        # An equally ranked pair leaves the episode alone.
        self.assertEqual(self.plan([video("a", size=6000), video("b", size=6000)]), {})
        # Preferred sources give no priority over a better version elsewhere.
        preferred = candidate("pref", name="测试剧.S01E02.1080p.mkv", episode_key="S01E02", suggested_episode=None,
                              size=1100, media_info={"resolution": "1080p"})
        self.assertEqual(self.plan([preferred, video("4k")])["S01E02"]["candidate"]["candidate_id"], "4k")

    def test_unproven_files_are_not_upgrade_candidates(self):
        unrelated = candidate("4k", name="S01E02.2160p.mkv", path="合集/S01E02.2160p.mkv", parent_id="mixed",
                              episode_key="S01E02", suggested_episode=None, size=5000,
                              share_source={"source_hash": "b" * 64}, media_info={"resolution": "2160p"})
        self.assertEqual(self.plan([unrelated]), {})
        self.assertEqual(self.plan([unrelated], trust_share_title=True), {})

    def test_version_policy_order_defines_the_tier(self):
        policy = {"resolution_order": ["1080p", "720p", "2160p"], "allow_fallback": True}
        self.assertEqual(self.plan([video("4k", size=9000)], request=request(version_policy=policy)), {})
        plan = self.plan([video("big", "1080p", size=2000)], request=request(version_policy=policy))
        self.assertEqual(plan["S01E02"]["basis"], "larger_size")

    def test_archive_upgrade_requires_archive_auto(self):
        dv = archive("dv", path="测试剧/4K DV 高码/02.zip", size=8000)
        self.assertEqual(self.plan(archives=[dv]), {})
        plan = self.plan(archives=[dv], archive_auto="largest")
        self.assertEqual((plan["S01E02"]["kind"], plan["S01E02"]["candidate"]["candidate_id"]), ("archive", "dv"))

    def test_episodes_saved_this_cycle_are_excluded(self):
        sub = subscription(request=request(), numeric_policy=None, upgrade_existing=True)
        report = {"target_folder_id": "season", "existing": [existing()], "source_candidates": [video("4k")]}
        self.assertEqual(select_upgrades(sub["request"], report, sub, exclude={"S01E02"}), {})


class SurveyTests(unittest.TestCase):
    def test_survey_lists_share_candidates_only_in_a_read_only_up_to_date_run(self):
        from media_helper import workflow
        from tests.test_watch_sources import Drive
        from tests.test_workflow import Journal, Search
        drive = Drive()
        drive.folders["season"] = [file("old-1", "测试剧 S01E01.MKV")]
        drive.shares["https://pan.quark.cn/s/fake-share"] = {"0": [file("v", "测试剧.S01E01.2160p.mkv", "0", size=9)]}
        for apply, survey, listed in ((False, False, 0), (True, True, 0), (False, True, 1)):
            with self.subTest(apply=apply, survey=survey):
                search = Search()
                report = workflow.run(request(), drive, Metadata(episodes=(1,)), search, Journal(), apply=apply,
                                      survey_existing=survey)
                self.assertEqual(report["status"], "UP_TO_DATE", report)
                self.assertEqual((search.calls, len(report["source_candidates"])), (listed, listed))
                self.assertFalse(report["writes_attempted"])


class UpgradeReadScopeTests(unittest.TestCase):
    def test_planning_reads_only_the_season_folder(self):
        storage = Mock()
        storage.list_files.return_value = [file("old", OLD, "season", size=1000)]
        sub = subscription(request=request(), numeric_policy=None, upgrade_existing=True)
        report = {"target_folder_id": "season", "naming_prefix": "测试剧 (2026)", "existing": [existing()],
                  "source_candidates": [video("4k")]}
        outcome = _apply_upgrades(sub, sub["request"], report, storage=storage, root=None, config={},
                                  journal=None, search=None, metadata_for=None)
        self.assertEqual([call.args for call in storage.list_files.call_args_list], [("season",)])
        self.assertEqual(outcome["upgrade_plan"][0]["target_name"], "测试剧 (2026)S01E02_2160P.mkv")
        self.assertEqual((outcome["writes_attempted"], outcome["upgrades"]), (False, []))


class UpgradeCycleTests(CycleCase):
    def setUp(self):
        super().setUp()
        self.episodes = (1, 2)
        self.drive.folders["season"] = [file("old-1", "测试剧 S01E01.MKV"), file("old-2", OLD, size=3000)]

    def sub(self, **changes):
        return subscription(**{"request": request(), "numeric_policy": None, "preferred_sources": [],
                               "upgrade_existing": True, **changes})

    def fourk_share(self, size=8000):
        self.share(FRESH, "测试剧 4K", {
            "0": [file("sroot", "测试剧", "0", directory=True)],
            "sroot": [file("dir4k", "4K", "sroot", directory=True)],
            "dir4k": [file("v4k", "测试剧.S01E02.2160p.mkv", "dir4k", size=size)]})

    def names(self):
        return sorted(row["name"] for row in self.drive.folders["season"])

    def test_up_to_date_subscription_without_upgrades_searches_nothing(self):
        self.fourk_share()
        report = self.cycle(self.sub(upgrade_existing=False))
        self.assertEqual(report["status"], "UP_TO_DATE", report)
        self.fallback.search.assert_not_called()
        self.assert_no_writes()

    def test_read_only_cycle_only_plans_the_upgrade(self):
        self.fourk_share()
        report = self.cycle(self.sub(), apply=False)
        self.assertEqual(report["status"], "UP_TO_DATE", report)
        self.assertEqual([(row["episode_key"], row["basis"], row["target_name"]) for row in report["upgrade_plan"]],
                         [("S01E02", "higher_resolution", "测试剧 (2026)S01E02_2160P.mkv")])
        self.assert_no_writes()

    def test_a_smaller_higher_resolution_video_is_not_planned(self):
        self.fourk_share(size=2000)
        report = self.cycle(self.sub())
        self.assertEqual(report["status"], "UP_TO_DATE", report)
        self.assertNotIn("upgrade_plan", report)
        self.assert_no_writes()

    def test_higher_resolution_video_replaces_the_old_file_after_verification(self):
        self.fourk_share()
        report = self.cycle(self.sub())
        self.assertEqual(report["status"], "COMPLETE", report)
        self.assertEqual(self.drive.saved, [["v4k"]])
        self.assertEqual(self.names(), ["测试剧 (2026)S01E02_2160P.mkv", "测试剧 S01E01.MKV"])
        self.assertEqual(self.drive.trashed, ["old-2"])
        self.assertEqual([(row["episode_key"], row["old_state"]) for row in report["upgrades"]], [("S01E02", "recycled")])
        self.assertIn("existing_episodes_upgraded", report["reasons"])
        self.assertEqual([row["target_name"] for row in report["new_files"]], ["测试剧 (2026)S01E02_2160P.mkv"])
        self.assertEqual(report["preferred_sources"][0]["url"], FRESH)
        # The next cycle finds nothing clearly better and writes nothing.
        again = self.cycle(self.sub())
        self.assertEqual(again["status"], "UP_TO_DATE", again)
        self.assertEqual((len(self.drive.saved), len(self.drive.trashed)), (1, 1))

    def test_record_run_treats_an_upgrade_as_a_healthy_change(self):
        self.fourk_share()
        store = SubscriptionStore(self.root / "runtime/subscriptions.json", "root")
        sub = self.sub()
        record = store.add(sub["request"], auto_save=True, upgrade_existing=True)
        outcome = store.record_run(record["subscription_id"], self.cycle(sub))
        self.assertFalse(outcome["record"]["blocked"])
        self.assertTrue(outcome["meaningful_change"])

    def test_a_taken_target_name_skips_without_blocking(self):
        self.share(FRESH, "测试剧 1080P 高码", {
            "0": [file("sroot", "测试剧", "0", directory=True)],
            "sroot": [file("v", "测试剧.S01E02.1080p.mkv", "sroot", size=9000)]})
        report = self.cycle(self.sub())
        self.assertEqual(report["status"], "UP_TO_DATE", report)
        self.assertEqual([(row["episode_key"], row["reason"]) for row in report["upgrade_skipped"]],
                         [("S01E02", "upgrade_target_name_exists")])
        self.assertIn("existing_episode_upgrade_skipped", report["reasons"])
        self.assert_no_writes()

    def test_missing_episode_and_upgrade_in_one_cycle(self):
        self.episodes = (1, 2, 3)
        self.primary = Metadata(episodes=(1, 2, 3))
        self.primary.source = "tmdb"
        self.fourk_share()
        self.drive.shares[FRESH]["dir4k"].append(file("v3", "测试剧.S01E03.2160p.mkv", "dir4k", size=8000))
        report = self.cycle(self.sub())
        self.assertEqual(report["status"], "COMPLETE", report)
        self.assertEqual(self.drive.saved, [["v3"], ["v4k"]])
        self.assertEqual(self.drive.trashed, ["old-2"])
        self.assertEqual(report["missing"], [])
        self.assertEqual([row["episode_key"] for row in report["upgrades"]], ["S01E02"])

    def test_unknown_recycle_of_the_old_file_blocks_and_is_never_resent(self):
        self.fourk_share()
        self.drive.trash_failures.add("old-2")
        report = self.cycle(self.sub())
        self.assertNotIn(report["status"], {"COMPLETE", "UP_TO_DATE", "WAITING", "READY"}, report)
        self.assertIn("old_version_recycle_outcome_unknown", report["reasons"])
        self.drive.trash_failures.clear()
        # Two files now hold the episode: no further upgrade, no second recycle request.
        self.cycle(self.sub())
        self.assertEqual(self.drive.trashed, ["old-2"])


class ArchiveUpgradeCycleTests(CycleCase):
    def setUp(self):
        super().setUp()
        self.episodes = (1, 2)
        self.drive.folders["season"] = [file("old-1", "测试剧 S01E01.MKV"), file("old-2", OLD, size=3000)]
        self.share(ZIPS, "测试剧 4K DV 60帧率 高码", {
            "0": [file("sroot", "测试剧", "0", directory=True)],
            "sroot": [file("dv", "4K DV 60帧率 高码", "sroot", directory=True)],
            "dv": [file("zip2", "02.zip", "dv", size=9000)]})
        self.drive.outputs["02.zip"] = [("02.mkv", 8900), ("02.chs.ass", 30)]

    def sub(self, **changes):
        return subscription(**{"request": request(), "numeric_policy": None, "preferred_sources": [],
                               "upgrade_existing": True, "archive_auto": "largest", **changes})

    def test_archive_upgrade_places_the_new_video_then_recycles_old_and_leftovers(self):
        seen, trash = {}, self.drive.trash

        def recorded(fid):
            seen.setdefault(fid, sorted(row["name"] for row in self.drive.folders["season"]))
            return trash(fid)
        self.drive.trash = recorded
        report = self.cycle(self.sub())
        self.assertEqual(report["status"], "COMPLETE", report)
        target = "测试剧 (2026)S01E02_2160P_DV"
        self.assertEqual(sorted(row["name"] for row in self.drive.folders["season"]),
                         sorted([target + ".mkv", target + ".chs.ass", "测试剧 S01E01.MKV"]))
        # The old video is recycled only once the new one is in the season folder;
        # archive, fetch staging and the emptied extraction folder are recycled too.
        self.assertIn(target + ".mkv", seen["old-2"])
        self.assertEqual(len(self.drive.trashed), 4)
        self.assertEqual([row["name"] for row in self.drive.folders["root"]], ["测试剧 (2026)"])
        self.assertEqual(report["upgrades"][0]["old_state"], "recycled")
        again = self.cycle(self.sub())
        self.assertEqual(again["status"], "UP_TO_DATE", again)
        self.assertEqual(len(self.drive.extracted), 1)

    def test_old_file_changed_during_the_upgrade_is_kept(self):
        original = self.drive.move

        def move(fid, parent):
            result = original(fid, parent)
            self.drive.find("old-2")[1]["name"] = "改名.mkv"
            return result
        self.drive.move = move
        report = self.cycle(self.sub())
        self.assertEqual(report["status"], "PARTIAL", report)
        self.assertIn("old_version_changed", report["reasons"])
        self.assertNotIn("old-2", self.drive.trashed)

    def test_unknown_archive_recycle_is_only_observed_later(self):
        self.drive.trash_failures.add("old-2")
        report = self.cycle(self.sub())
        self.assertEqual(report["status"], "UNKNOWN", report)
        self.assertEqual(report["reasons"], ["archive_placement_not_completed"])
        self.drive.trash_failures.clear()
        self.cycle(self.sub())
        self.assertEqual(self.drive.trashed.count("old-2"), 1)



if __name__ == "__main__":
    unittest.main()
