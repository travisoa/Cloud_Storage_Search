"""In-place naming cleanup for one existing work folder (fake storage)."""
from copy import deepcopy
import unittest

from media_helper.tidy import apply_tidy, plan_tidy

CATEGORIES = {"tv": {"folder_id": "tv", "name": "电视剧"}, "movie": {"folder_id": "movies", "name": "电影"}}


def row(fid, name, parent, size=100, directory=False):
    return {"fid": fid, "name": name, "parent_id": parent, "size": 0 if directory else size, "is_dir": directory}


class Journal:
    def __init__(self):
        self.records = {}

    def get(self, key):
        return deepcopy(self.records.get(key))

    def put(self, key, value):
        self.records[key] = deepcopy(value)


class Storage:
    def __init__(self):
        self.folders = {
            "root": [row("tv", "电视剧", "root", directory=True), row("movies", "电影", "root", directory=True)],
            "tv": [row("show", "嫁金钗 (2026)", "tv", directory=True), row("kr", "韩剧", "tv", directory=True)],
            "kr": [],
            "show": [row("s1", "Season 01", "show", directory=True), row("extra", "花絮", "show", directory=True)],
            "s1": [row("e1", "嫁金钗.S01E01.1080p.mp4", "s1"), row("e1sub", "嫁金钗.S01E01.1080p.zh.srt", "s1", 5),
                   row("e2", "嫁金钗 (2026) S01E02.mp4", "s1"), row("e3", "03.mp4", "s1"),
                   row("e4a", "嫁金钗.S01E04.mp4", "s1"), row("e4b", "Jia.S01E04.mp4", "s1")],
            "extra": [row("x", "幕后花絮.mp4", "extra")],
            "movies": [row("dune", "沙丘2 (2024)", "movies", directory=True)],
            "dune": [row("m", "Dune.Part.Two.2024.2160p.HDR10.mkv", "dune", 9000),
                     row("ms", "Dune.Part.Two.2024.2160p.HDR10.chs.ass", "dune", 9)],
        }
        self.renames = []
        self.fail = False

    def list_files(self, parent):
        return deepcopy(self.folders[parent])

    def rename(self, fid, name):
        if self.fail:
            raise RuntimeError("response body")
        for rows in self.folders.values():
            for item in rows:
                if item["fid"] == fid:
                    item["name"] = name
                    self.renames.append((fid, name))
                    return {}
        raise KeyError(fid)


class TidyTests(unittest.TestCase):
    def setUp(self):
        self.storage, self.journal = Storage(), Journal()

    def plan(self, folder="show", **options):
        return plan_tidy(self.storage, folder_id=folder, library_root_id="root", categories=CATEGORIES, **options)

    def test_tv_plan_uses_folder_prefix_and_keeps_ambiguous_files(self):
        plan = self.plan()
        self.assertEqual(plan["status"], "READY", plan)
        self.assertEqual(plan["kind"], "tv")
        targets = {op["fid"]: op["target_name"] for op in plan["operations"]}
        self.assertEqual(targets, {"e1": "嫁金钗 (2026)S01E01_1080P.mp4",
                                   "e1sub": "嫁金钗 (2026)S01E01_1080P.zh.srt",
                                   "e2": "嫁金钗 (2026)S01E02.mp4"})
        reasons = {item["fid"]: item["reason"] for item in plan["review"]}
        self.assertEqual(reasons, {"e3": "episode_number_unrecognized", "e4a": "duplicate_versions_same_target",
                                   "e4b": "duplicate_versions_same_target"})
        self.assertEqual(plan["compliant"], 0)
        self.assertEqual([item["fid"] for item in plan["skipped"]], ["x"])
        self.assertEqual(self.storage.renames, [])

    def test_apply_renames_only_planned_fids_and_is_idempotent(self):
        plan = self.plan()
        preview = apply_tidy(plan, self.storage, journal=self.journal, job_key="t")
        self.assertEqual({op["state"] for op in preview["operations"]}, {"planned"})
        self.assertEqual(self.storage.renames, [])
        result = apply_tidy(plan, self.storage, journal=self.journal, job_key="t", apply=True)
        self.assertEqual(result["status"], "COMPLETE", result)
        self.assertEqual(sorted(fid for fid, _ in self.storage.renames), ["e1", "e1sub", "e2"])
        again = apply_tidy(plan, self.storage, journal=self.journal, job_key="t", apply=True)
        self.assertEqual(again["status"], "COMPLETE")
        self.assertEqual(len(self.storage.renames), 3)

    def test_movie_folder_gets_movie_name_and_subtitle_follows(self):
        plan = self.plan("dune")
        self.assertEqual(plan["kind"], "movie")
        self.assertEqual([op["target_name"] for op in plan["operations"]],
                         ["沙丘2 (2024)_2160P_HDR10.mkv", "沙丘2 (2024)_2160P_HDR10.chs.ass"])

    def compliant_episode(self, subtitle='嫁金钗.S01E01.zh.srt'):
        self.storage.folders['show'] = [row('s1', 'Season 01', 'show', directory=True)]
        self.storage.folders['s1'] = [row('e1', '嫁金钗 (2026)S01E01_1080P.mkv', 's1'),
                                      row('sub', subtitle, 's1', 5)]

    def test_compliant_video_can_repair_only_its_legacy_subtitle(self):
        self.compliant_episode()
        plan = self.plan()
        self.assertEqual([(op['fid'], op['target_name']) for op in plan['operations']],
                         [('sub', '嫁金钗 (2026)S01E01_1080P.zh.srt')])
        self.assertEqual(plan['compliant'], 1)
        result = apply_tidy(plan, self.storage, journal=self.journal, job_key='sub', apply=True)
        self.assertEqual(result['status'], 'COMPLETE')
        self.assertEqual(self.storage.renames, [('sub', '嫁金钗 (2026)S01E01_1080P.zh.srt')])
        self.assertEqual(self.plan()['status'], 'UP_TO_DATE')

    def test_compliant_movie_can_repair_title_matched_legacy_subtitle(self):
        self.storage.folders['dune'] = [row('m', '沙丘2 (2024)_2160P_HDR10.mkv', 'dune'),
                                         row('ms', '沙丘2 (2024).chs.ass', 'dune', 9)]
        plan = self.plan('dune')
        self.assertEqual([(op['fid'], op['target_name']) for op in plan['operations']],
                         [('ms', '沙丘2 (2024)_2160P_HDR10.chs.ass')])

    def test_legacy_subtitle_never_guesses_title_year_season_or_bare_number(self):
        for name in ('别的作品.S01E01.zh.srt', '嫁金钗 (2025)S01E01.zh.srt',
                     '嫁金钗.S02E01.zh.srt', '01.zh.srt'):
            with self.subTest(name=name):
                self.compliant_episode(name)
                self.assertEqual(self.plan()['operations'], [])

    def test_legacy_subtitle_requires_one_same_episode_version(self):
        self.compliant_episode()
        self.storage.folders['s1'].append(row('other', '嫁金钗 (2026)S01E01_2160P.mkv', 's1'))
        plan = self.plan()
        self.assertEqual(plan['operations'], [])
        self.assertIn({'fid': 'sub', 'name': '嫁金钗.S01E01.zh.srt', 'parent_id': 's1', 'size': 5,
                       'reason': 'subtitle_has_no_unique_video'}, plan['review'])

    def test_legacy_subtitle_does_not_cross_directories_or_overwrite(self):
        self.compliant_episode()
        self.storage.folders['show'].append(row('other', 'Season 02', 'show', directory=True))
        self.storage.folders['other'] = [self.storage.folders['s1'].pop()]
        self.storage.folders['other'][0]['parent_id'] = 'other'
        self.assertEqual(self.plan()['operations'], [])
        self.compliant_episode()
        self.storage.folders['s1'].append(row('taken', '嫁金钗 (2026)S01E01_1080P.zh.srt', 's1', 5))
        plan = self.plan()
        self.assertEqual(plan['operations'], [])
        self.assertIn('target_name_already_used', {item['reason'] for item in plan['review']})

    def test_new_episode_version_invalidates_subtitle_association_before_write(self):
        self.compliant_episode()
        plan = self.plan()
        self.storage.folders['s1'].append(row('new-version', '嫁金钗 (2026)S01E01_2160P.mkv', 's1'))
        result = apply_tidy(plan, self.storage, journal=self.journal, job_key='sub', apply=True)
        self.assertEqual((result['status'], result['reason']), ('NEEDS_INPUT', 'subtitle_video_identity_changed'))
        self.assertEqual(self.storage.renames, [])

    def test_changed_video_invalidates_subtitle_association_before_write(self):
        self.compliant_episode()
        plan = self.plan()
        self.storage.folders['s1'][0]['size'] += 1
        result = apply_tidy(plan, self.storage, journal=self.journal, job_key='sub', apply=True)
        self.assertEqual((result['status'], result['reason']), ('NEEDS_INPUT', 'subtitle_video_identity_changed'))
        self.assertEqual(self.storage.renames, [])

    def test_category_like_folders_are_refused(self):
        for folder in ("tv", "kr"):
            with self.assertRaises(ValueError):
                self.plan(folder)
        self.storage.folders["tv"].append(row("bundle", "合集", "tv", directory=True))
        self.storage.folders["bundle"] = [row("a", "甲 (2020)", "bundle", directory=True),
                                          row("b", "乙 (2021)", "bundle", directory=True)]
        self.storage.folders["a"] = self.storage.folders["b"] = []
        with self.assertRaises(ValueError):
            self.plan("bundle")

    def test_changed_source_or_new_conflict_stops_before_rename(self):
        plan = self.plan()
        self.storage.folders["s1"].append(row("dup", "嫁金钗 (2026)S01E01_1080P.mp4", "s1"))
        result = apply_tidy(plan, self.storage, journal=self.journal, job_key="t", apply=True)
        self.assertEqual((result["status"], result["reason"]), ("NEEDS_INPUT", "target_name_conflict"))
        self.assertEqual(self.storage.renames, [])

    def test_unknown_rename_is_reconciled_read_only_before_continuing(self):
        plan = self.plan()
        self.storage.fail = True
        self.assertEqual(apply_tidy(plan, self.storage, journal=self.journal, job_key="t", apply=True)["status"],
                         "UNKNOWN_OUTCOME")
        self.storage.fail = False
        checked = apply_tidy(plan, self.storage, journal=self.journal, job_key="t", apply=True)
        self.assertEqual(checked["status"], "READY_TO_CONTINUE")
        self.assertEqual(self.storage.renames, [])
        done = apply_tidy(plan, self.storage, journal=self.journal, job_key="t", apply=True)
        self.assertEqual(done["status"], "COMPLETE", done)

    def test_plan_opens_group_folders_first_and_apply_rechecks_only_the_chain(self):
        # "other" has no listing: opening an unrelated work would raise.
        self.storage.folders["tv"].insert(0, row("other", "他剧 (2025)", "tv", directory=True))
        self.storage.folders["kr"] = [row("kshow", "韩剧作品 (2024)", "kr", directory=True)]
        self.storage.folders["kshow"] = [row("k1", "韩剧作品.S01E01.mp4", "kshow")]
        reads = []
        original = self.storage.list_files
        self.storage.list_files = lambda parent: (reads.append(parent), original(parent))[1]
        plan = self.plan("kshow")
        self.assertEqual([step["fid"] for step in plan["chain"]], ["tv", "kr", "kshow"])
        self.assertNotIn("other", reads)
        reads.clear()
        result = apply_tidy(plan, self.storage, journal=self.journal, job_key="t", apply=True)
        self.assertEqual(result["status"], "COMPLETE", result)
        self.assertEqual(reads.count("tv"), 1)
        self.assertNotIn("show", reads)

    def test_moved_folder_invalidates_plan(self):
        plan = self.plan()
        self.storage.folders["tv"][0]["name"] = "嫁金钗"
        with self.assertRaises(ValueError):
            apply_tidy(plan, self.storage, journal=self.journal, job_key="t", apply=True)


if __name__ == "__main__":
    unittest.main()
