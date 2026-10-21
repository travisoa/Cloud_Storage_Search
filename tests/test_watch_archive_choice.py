"""Opt-in archive choices: several archives per episode, archives apart from videos, HDR preference."""
import unittest

from media_helper.naming import media_info
from media_helper.sources import source_digest
from media_helper.watching import select_archives
from media_helper.workflow import _digest
from tests.test_watch_sources import OTHER, ZIPS, CycleCase, archive
from tests.test_watching import candidate, subscription
from tests.test_workflow import file, request


def version(identifier, folder, size, share=OTHER, name="02.zip"):
    """One of several archives a publisher puts in per-version folders of one share."""
    return archive(identifier, path="测试剧/" + folder + "/" + name, size=size, share=share)


class ChoiceCase(unittest.TestCase):
    def select(self, archives, videos=(), **changes):
        sub = subscription(archive_auto="largest", **{"request": request(), **changes})
        report = {"missing": [{"episode_key": "S01E02"}], "archive_candidates": list(archives),
                  "source_candidates": list(videos)}
        return select_archives(sub["request"], report, sub)


class SeveralArchivesTests(ChoiceCase):
    def three(self, dv=9000, sdr_high=8800, sdr_low=4000):
        return [version("dv", "4K DV 60帧率 高码", dv), version("sdrh", "4K SDR 60帧率 高码", sdr_high),
                version("sdrl", "4K SDR 60帧率 低码", sdr_low)]

    def test_default_still_waits_on_several_archives_in_one_share(self):
        self.assertEqual(self.select(self.three()), ({}, ["S01E02"]))

    def test_pick_largest_takes_the_largest_of_one_share(self):
        selected, uncertain = self.select(self.three(), archive_pick_largest=True)
        self.assertEqual(({key: row["candidate_id"] for key, row in selected.items()}, uncertain), ({"S01E02": "dv"}, []))
        selected, _ = self.select(self.three(dv=8000, sdr_high=9000), archive_pick_largest=True)
        self.assertEqual(selected["S01E02"]["candidate_id"], "sdrh")

    def test_pick_largest_still_needs_a_unique_largest_and_an_episode_number(self):
        self.assertEqual(self.select(self.three(dv=9000, sdr_high=9000), archive_pick_largest=True), ({}, ["S01E02"]))
        unnumbered = [version("dv", "4K DV 60帧率 高码", 9000, name="资源.zip"), version("sdrl", "4K SDR 60帧率 低码", 4000)]
        selected, _ = self.select(unnumbered, archive_pick_largest=True)
        self.assertEqual(selected["S01E02"]["candidate_id"], "sdrl")

    def test_labels_break_near_ties_within_three_percent_only(self):
        near = self.three(dv=8800, sdr_high=9000)
        # Without the label option the largest still wins.
        self.assertEqual(self.select(near, archive_pick_largest=True)[0]["S01E02"]["candidate_id"], "sdrh")
        chosen = self.select(near, archive_pick_largest=True, archive_tiebreak_labels=True)[0]
        self.assertEqual(chosen["S01E02"]["candidate_id"], "dv")
        far = self.three(dv=8000, sdr_high=9000)
        chosen = self.select(far, archive_pick_largest=True, archive_tiebreak_labels=True)[0]
        self.assertEqual(chosen["S01E02"]["candidate_id"], "sdrh")

    def test_high_bitrate_beats_low_bitrate_and_hdr_beats_sdr(self):
        pair = [version("low", "4K SDR 低码", 9000), version("high", "4K SDR 高码", 8900)]
        chosen = self.select(pair, archive_pick_largest=True, archive_tiebreak_labels=True)[0]
        self.assertEqual(chosen["S01E02"]["candidate_id"], "high")
        pair = [version("sdr", "4K SDR", 9000), version("hdr", "4K HDR10", 8900)]
        chosen = self.select(pair, archive_pick_largest=True, archive_tiebreak_labels=True)[0]
        self.assertEqual(chosen["S01E02"]["candidate_id"], "hdr")
        pair = [version("hdr", "4K HDR10 高码", 9000), version("dv", "4K杜比视界 高码", 8900)]
        chosen = self.select(pair, archive_pick_largest=True, archive_tiebreak_labels=True)[0]
        self.assertEqual(chosen["S01E02"]["candidate_id"], "dv")

    def test_label_tiebreak_also_ranks_an_archive_against_a_near_size_video(self):
        video = candidate("video", name="测试剧.S01E02.2160p.SDR.mkv", path="测试剧/4K SDR/测试剧.S01E02.2160p.SDR.mkv",
                          episode_key="S01E02", suggested_episode=None, size=9100,
                          media_info=media_info("测试剧.S01E02.2160p.SDR.mkv", "测试剧/4K SDR"))
        dv = [version("dv", "4K DV 高码", 9000)]
        self.assertEqual(self.select(dv, [video]), ({}, []))
        self.assertEqual(self.select(dv, [video], archive_tiebreak_labels=True)[0]["S01E02"]["candidate_id"], "dv")


class HdrPreferenceTests(ChoiceCase):
    def pair(self, dv=9000, hdr=9100):
        return [version("dv", "4K DV", dv), version("hdr", "4K HDR10", hdr)]

    def test_preference_decides_only_near_sizes(self):
        self.assertEqual(self.select(self.pair(), archive_pick_largest=True)[0]["S01E02"]["candidate_id"], "hdr")
        for prefer, expected in (("dv", "dv"), ("hdr", "hdr")):
            with self.subTest(prefer=prefer):
                chosen = self.select(self.pair(), archive_pick_largest=True, prefer_hdr_format=prefer)[0]
                self.assertEqual(chosen["S01E02"]["candidate_id"], expected)
        # Beyond 3% the largest still wins: the preference never overrides size.
        chosen = self.select(self.pair(dv=8000), archive_pick_largest=True, prefer_hdr_format="dv")[0]
        self.assertEqual(chosen["S01E02"]["candidate_id"], "hdr")

    def test_preference_ranks_before_the_default_label_order(self):
        chosen = self.select(self.pair(dv=9100, hdr=9000), archive_pick_largest=True,
                             archive_tiebreak_labels=True, prefer_hdr_format="hdr")[0]
        self.assertEqual(chosen["S01E02"]["candidate_id"], "hdr")
        chosen = self.select(self.pair(dv=9000, hdr=9100), archive_pick_largest=True, archive_tiebreak_labels=True)[0]
        self.assertEqual(chosen["S01E02"]["candidate_id"], "dv")


MP4 = "测试剧.S01E02.1080p.mp4"


class RealShareRegressionTests(CycleCase):
    """A real share: three archives per episode by version folder, plus a 1080p mp4."""

    def setUp(self):
        super().setUp()
        self.share(ZIPS, "测试剧 4K DV 60帧率 更新至02集", {
            "0": [file("sroot", "测试剧", "0", directory=True)],
            "sroot": [file("dvh", "4K DV 60帧率 高码", "sroot", directory=True),
                      file("sdrh", "4K SDR 60帧率 高码", "sroot", directory=True),
                      file("sdrl", "4K SDR 60帧率 低码", "sroot", directory=True),
                      file("p1080", "1080P", "sroot", directory=True)],
            "dvh": [file("zip-dvh", "02.zip", "dvh", size=9000)],
            "sdrh": [file("zip-sdrh", "02.zip", "sdrh", size=8800)],
            "sdrl": [file("zip-sdrl", "02.zip", "sdrl", size=4000)],
            "p1080": [file("mp4", MP4, "p1080", size=3000)]})
        self.drive.outputs["02.zip"] = [("02.mkv", 8900)]

    def sub(self, **changes):
        return subscription(request=request(), numeric_policy=None, preferred_sources=[], archive_auto="largest",
                            **changes)

    def test_strict_default_never_saves_the_episode(self):
        report = self.cycle(self.sub())
        self.assertEqual((report["status"], report["ambiguous_episodes"]), ("NEEDS_INPUT", ["S01E02"]), report)
        waiting = self.cycle(self.sub(wait_on_uncertain_source=True))
        self.assertEqual(waiting["status"], "WAITING", waiting)
        self.assert_no_writes()

    def test_largest_archive_wins_over_the_mp4_and_leftovers_are_recycled(self):
        report = self.cycle(self.sub(archive_pick_largest=True, archive_video_fallback=True))
        self.assertEqual(report["status"], "COMPLETE", report)
        self.assertEqual(self.drive.saved, [["zip-dvh"]])
        target = "测试剧 (2026)S01E02_2160P_DV.mkv"
        self.assertIn(target, [row["name"] for row in self.drive.folders["season"]])
        self.assertEqual(report["archive_episodes"][0]["candidate_id"], _digest((source_digest(ZIPS), "zip-dvh")))
        # Archive, fetch staging and the emptied extraction folder go to the recycle bin.
        self.assertEqual(len(self.drive.trashed), 3)
        self.assertEqual([row["name"] for row in self.drive.folders["root"]], ["测试剧 (2026)"])

    def test_an_uncertain_archive_no_longer_withdraws_the_mp4(self):
        self.drive.shares[ZIPS]["sdrh"][0]["size"] = 9000
        strict = self.cycle(self.sub(archive_pick_largest=True))
        self.assertEqual(strict["status"], "NEEDS_INPUT", strict)
        self.assert_no_writes()
        report = self.cycle(self.sub(archive_pick_largest=True, archive_video_fallback=True))
        self.assertEqual(report["status"], "COMPLETE", report)
        self.assertEqual(self.drive.saved, [["mp4"]])
        self.assertEqual(self.drive.renamed, [("new-mp4", "测试剧 (2026)S01E02_1080P.mp4")])
        self.assertEqual(self.drive.extracted, [])

    def test_a_larger_mp4_keeps_the_ordinary_path(self):
        self.drive.shares[ZIPS]["p1080"][0]["size"] = 12000
        report = self.cycle(self.sub(archive_pick_largest=True, archive_video_fallback=True))
        self.assertEqual(report["status"], "COMPLETE", report)
        self.assertEqual((self.drive.saved, self.drive.extracted), ([["mp4"]], []))

    def test_an_unusable_archive_blocks_by_default(self):
        self.drive.outputs["02.zip"] = [("02.mkv", 8900), ("sample.mkv", 100)]
        strict = self.cycle(self.sub(archive_pick_largest=True))
        self.assertEqual((strict["status"], strict["reasons"]), ("NEEDS_INPUT", ["archive_output_requires_single_video"]))
        self.assertEqual(self.drive.saved, [["zip-dvh"]])

    def test_an_unusable_archive_falls_back_to_the_mp4_in_the_same_cycle(self):
        self.drive.outputs["02.zip"] = [("02.mkv", 8900), ("sample.mkv", 100)]
        report = self.cycle(self.sub(archive_pick_largest=True, archive_video_fallback=True))
        self.assertEqual(report["status"], "COMPLETE", report)
        self.assertEqual(self.drive.saved, [["zip-dvh"], ["mp4"]])
        self.assertEqual(report["video_fallback_episodes"], ["S01E02"])
        self.assertIn("archive_unusable_saved_video_instead", report["reasons"])
        self.assertIn("archive_output_requires_single_video", report["reasons"])
        names = [row["name"] for row in self.drive.folders["season"]]
        self.assertIn("测试剧 (2026)S01E02_1080P.mp4", names)
        self.assertEqual(report["missing"], [])

    def test_unknown_archive_outcome_never_falls_back(self):
        self.drive.move_failure = RuntimeError("timeout after request")
        report = self.cycle(self.sub(archive_pick_largest=True, archive_video_fallback=True))
        self.assertEqual(report["status"], "UNKNOWN", report)
        self.assertEqual(self.drive.saved, [["zip-dvh"]])


if __name__ == "__main__":
    unittest.main()
