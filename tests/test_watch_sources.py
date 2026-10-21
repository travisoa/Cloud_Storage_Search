"""Opt-in subscription source policies: share-title trust, waiting, source registration, archives."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import posixpath
import tempfile
import unittest
from unittest.mock import Mock, patch

from media_helper.archives import classify_archive
from media_helper.sources import safe_source, source_digest
from media_helper.subscriptions import HEALTHY_STATUSES, SubscriptionStore
from media_helper.watching import UNCERTAIN_SOURCE_REASON, archive_episode, run_subscription, select_archives, select_ready
from media_helper.workflow import _digest
from tests.test_watching import SOURCE, candidate, subscription
from tests.test_workflow import Metadata, file


OTHER = "b" * 64
THIRD = "c" * 64


def hidden(identifier="hidden", path="🏠/S01E02.mkv", share=OTHER, **changes):
    """A fallback-share video whose path never names the work."""
    return candidate(identifier, name=posixpath.basename(path), path=path, parent_id="other",
                     episode_key="S01E02", suggested_episode=None, share_source={"source_hash": share}, **changes)


def archive(identifier="zip", path="测试剧/4K DV 60帧率 高码/02.zip", size=900, share=OTHER, **changes):
    name = posixpath.basename(path)
    return {"candidate_id": identifier, "fid": identifier, "name": name, "size": size, "parent_id": "dv",
            "path": path, "share_source": {"source_hash": share}, "classification": classify_archive(name), **changes}


class TrustShareTitleTests(unittest.TestCase):
    def select(self, candidates, *, title="测试剧 4K 更新至02集", trust=True):
        sub = subscription(trust_share_title=trust, numeric_policy=None)
        report = {"missing": [{"episode_key": "S01E02"}], "source_candidates": candidates,
                  "source_refs": [{"url": "https://pan.quark.cn/s/other", "title": title, "source_hash": OTHER},
                                  {"url": "https://pan.quark.cn/s/third", "title": title, "source_hash": THIRD}]}
        return select_ready(sub["request"], report, sub)

    def test_default_still_requires_the_title_in_the_real_path(self):
        self.assertEqual(self.select([hidden()], trust=False), ({}, {}, ["S01E02"]))

    def test_share_title_accepts_an_explicit_episode_without_the_title_in_its_path(self):
        self.assertEqual(self.select([hidden()]), ({"S01E02": "hidden"}, {}, []))
        self.assertEqual(self.select([hidden(path="价J拆_xxx/测试 S01E02.mkv")]), ({"S01E02": "hidden"}, {}, []))

    def test_unrelated_share_title_still_needs_confirmation(self):
        self.assertEqual(self.select([hidden()], title="热门美剧合集"), ({}, {}, ["S01E02"]))

    def test_one_season_directory_fixes_a_bare_numbered_file(self):
        for folder in ("Season 1", "S01", "第一季", "第1季"):
            with self.subTest(folder=folder):
                row = candidate("bare", path="🏠/" + folder + "/02.mkv", parent_id="other",
                                share_source={"source_hash": OTHER})
                self.assertEqual(self.select([row]), ({"S01E02": "bare"},
                    {"bare": {"season": 1, "episode": 2, "name": "02.mkv", "size": 100}}, []))

    def test_bare_number_without_a_season_directory_or_with_another_season_stays_open(self):
        for path in ("价J拆_xxx/02.mp4", "🏠/Season 2/02.mkv", "🏠/Season 1/Season 2/02.mkv"):
            with self.subTest(path=path):
                row = candidate("bare", name=posixpath.basename(path), path=path, parent_id="other",
                                share_source={"source_hash": OTHER})
                self.assertEqual(self.select([row]), ({}, {}, ["S01E02"]))
        self.assertEqual(self.select([hidden(path="🏠/Season 2/S01E02.mkv")]), ({}, {}, ["S01E02"]))

    def test_a_share_holding_two_files_for_the_episode_is_not_trusted(self):
        self.assertEqual(self.select([hidden("a", path="甲/S01E02.mkv"), hidden("b", path="乙/S01E02.mkv")]),
                         ({}, {}, ["S01E02"]))
        # Only files passing the quality filter compete within a share.
        self.assertEqual(self.select([hidden("a", path="甲/S01E02.mkv"),
                                      hidden("b", path="乙/S01E02.mkv", media_info={"resolution": "1080p"})]),
                         ({"S01E02": "a"}, {}, []))

    def test_separately_trusted_shares_still_compete_by_score(self):
        self.assertEqual(self.select([hidden("a"), hidden("b", share=THIRD)]), ({}, {}, ["S01E02"]))
        self.assertEqual(self.select([hidden("a", score=10), hidden("b", share=THIRD, score=20)]),
                         ({"S01E02": "b"}, {}, []))


class ArchiveSelectionTests(unittest.TestCase):
    def select(self, archives, videos=(), **changes):
        sub = subscription(archive_auto="largest", **changes)
        report = {"missing": [{"episode_key": "S01E02"}], "archive_candidates": list(archives),
                  "source_candidates": list(videos)}
        return select_archives(sub["request"], report, sub)

    def test_episode_comes_from_the_archive_name_or_its_directory_only(self):
        expected = {"测试剧/4K DV 60帧率 高码/02.zip": "S01E02", "S01E02.7z": "S01E02",
                    "第2集/资源.rar": "S01E02", "4K/E02.zip": "S01E02", "S02E02.zip": None,
                    "Season 2/02.zip": None, "第2集/花絮02.zip": None, "01-02.zip": None,
                    "4K DV/资源.zip": None, "S01E02-E03.zip": None}
        for path, key in expected.items():
            with self.subTest(path=path):
                self.assertEqual(archive_episode(archive(path=path), 1), key)

    def test_policy_is_off_unless_the_subscription_opted_in(self):
        sub = subscription()
        report = {"missing": [{"episode_key": "S01E02"}], "archive_candidates": [archive()], "source_candidates": []}
        self.assertEqual(select_archives(sub["request"], report, sub), ({}, []))

    def test_largest_archive_outranks_a_smaller_video_but_not_a_larger_one(self):
        video = candidate("video", size=400)
        self.assertEqual(self.select([archive()], [video]), ({"S01E02": archive()}, []))
        self.assertEqual(self.select([archive(size=400)], [video]), ({}, []))
        # An unrelated quality video does not count against the archive.
        self.assertEqual(self.select([archive()], [candidate("v", size=5000, media_info={"resolution": "1080p"})]),
                         ({"S01E02": archive()}, []))

    def test_ties_and_duplicates_within_one_share_leave_the_episode_uncertain(self):
        self.assertEqual(self.select([archive("a"), archive("b", share=THIRD)]), ({}, ["S01E02"]))
        self.assertEqual(self.select([archive("a"), archive("b", path="测试剧/4K 高码/02.zip", size=800)]),
                         ({}, ["S01E02"]))
        # A larger archive in another share still wins over a smaller duplicated one.
        self.assertEqual(self.select([archive("a", size=500), archive("b", path="测试剧/4K 高码/02.zip", size=500),
                                      archive("c", share=THIRD, size=900)]),
                         ({"S01E02": archive("c", share=THIRD, size=900)}, []))

    def test_multipart_unsupported_wrong_quality_or_unsized_archives_are_ignored(self):
        for row in (archive(path="测试剧/4K/02.part1.rar"), archive(path="测试剧/4K/02.iso"),
                    archive(path="测试剧/1080P/02.zip"), archive(size=0), archive(size=None)):
            with self.subTest(row=row["path"]):
                self.assertEqual(self.select([row]), ({}, []))


class Drive:
    """A library with one season plus named shares; every write is recorded."""
    account_scope = "quark:watch-test"
    capabilities = {"archive_extract": True, "trash": True}
    _sleep = staticmethod(lambda delay: None)

    def __init__(self):
        self.folders = {"root": [file("series", "测试剧 (2026)", "root", directory=True)],
                        "series": [file("season", "Season 01", "series", directory=True)],
                        "season": [file("old-1", "测试剧 S01E01.MKV")]}
        self.shares, self.dead, self.outputs = {}, set(), {}
        self.saved, self.renamed, self.moved, self.trashed, self.extracted = [], [], [], [], []
        self.move_failure = None
        self.trash_failures, self.listed = set(), []
        self.tasks = {}

    def find(self, fid):
        return next((parent, row) for parent, rows in self.folders.items() for row in rows if row["fid"] == fid)

    def list_files(self, parent):
        self.listed.append(parent)
        return deepcopy(self.folders[parent])

    def share_files(self, url, password=None, parent_id="0"):
        if url in self.dead:
            raise RuntimeError("41004 share expired")
        return {"share_ref": {"url": url, "stoken": "do-not-persist"},
                "files": deepcopy(self.shares[url].get(parent_id, []))}

    def save_files(self, share, files, target):
        self.saved.append([row["fid"] for row in files])
        for row in files:
            self.folders[target].append(file("new-" + row["fid"], row["name"], target, size=row["size"]))
        return {"task_id": "save-1", "status": "done", "saved_fids": ["new-" + row["fid"] for row in files]}

    def task_status(self, task_id):
        return {"status": "done", "task_id": task_id}

    def rename(self, fid, name):
        self.renamed.append((fid, name))
        self.find(fid)[1]["name"] = name
        return {"fid": fid, "name": name}

    def mkdir(self, parent, name):
        row = file("dir-" + str(len(self.folders)), name, parent, directory=True)
        self.folders[parent].append(row)
        self.folders[row["fid"]] = []
        return deepcopy(row)

    def move(self, fid, parent):
        self.moved.append((fid, parent))
        if self.move_failure:
            raise self.move_failure
        old, row = self.find(fid)
        self.folders[old].remove(row)
        self.folders[parent].append(dict(row, parent_id=parent))
        return {"fid": fid}

    def trash(self, fid):
        self.trashed.append(fid)
        if fid in self.trash_failures:
            raise RuntimeError("timeout after request")
        old, row = self.find(fid)
        self.folders[old].remove(row)
        return {"fid": fid, "status": "submitted"}

    def extract_archive(self, fid, target, *, on_submitted):
        task = "unzip-" + fid
        on_submitted({"source_fid": fid, "target_parent_id": target, "task_id": task})
        self.extracted.append(fid)
        rows = [file("out-%d" % index, name, target, size=size)
                for index, (name, size) in enumerate(self.outputs[self.find(fid)[1]["name"]])]
        self.folders[target].extend(rows)
        self.tasks[task] = {"task_id": task, "status": "done", "extracted_fids": [row["fid"] for row in rows]}
        return deepcopy(self.tasks[task])

    def archive_task_status(self, task_id):
        return deepcopy(self.tasks[task_id])


FRESH = "https://pan.quark.cn/s/fresh0share"
ZIPS = "https://pan.quark.cn/s/dvzips0share"


class CycleCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = {"library_root_id": "root", "pansou_url": "http://127.0.0.1:8888"}
        self.drive, self.fallback = Drive(), Mock()
        self.fallback.search.return_value = []
        self.primary = Metadata(episodes=(1, 2))
        self.primary.source = "tmdb"
        self.episodes = (1, 2)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("media_helper.headless_cli._prepare_identity",
                                       side_effect=lambda req, cfg: (deepcopy(req), None, self.primary, None)))
        self.stack.enter_context(patch("media_helper.headless_cli._storage", return_value=self.drive))
        self.stack.enter_context(patch("media_helper.watching.collect_updates", side_effect=lambda *args: {
            "status": "ok", "automatic_transfer_allowed": True, "conflicts": [], "errors": [],
            "series": {"id": 17, "title": "测试剧", "year": 2026, "source": "tmdb"},
            "episodes": [{"season": 1, "episode": number, "episode_key": f"S01E{number:02d}",
                          "air_date": "2026-10-15", "date_precision": "date", "source": "tmdb"}
                         for number in self.episodes]}))
        self.stack.enter_context(patch("media_helper.watching.PanSouSearch", return_value=self.fallback))

    def share(self, url, title, tree):
        self.drive.shares[url] = tree
        self.fallback.search.return_value = [*self.fallback.search.return_value,
                                             {"url": url, "title": title, "provider": "quark"}]

    def cycle(self, sub, *, apply=True):
        return run_subscription(sub, self.config, self.root, apply=apply,
                                as_of=datetime(2026, 10, 17, tzinfo=timezone.utc))

    def assert_no_writes(self):
        self.assertEqual((self.drive.saved, self.drive.renamed, self.drive.moved, self.drive.trashed), ([], [], [], []))


class SourcePolicyCycleTests(CycleCase):
    def hidden_share(self):
        self.share(FRESH, "测试剧 4K 更新至02集", {
            "0": [file("home", "🏠", "0", directory=True)],
            "home": [file("hidden2", "S01E02.2160p.mkv", "home", size=700)]})

    def test_trusted_share_title_saves_and_appends_the_new_source(self):
        self.drive.dead.add(SOURCE["url"])
        self.hidden_share()
        strict = self.cycle(subscription(numeric_policy=None))
        self.assertEqual(strict["status"], "NEEDS_INPUT", strict)
        self.assertEqual(strict["reasons"], ["subscription_source_or_episode_confirmation_required"])
        self.assert_no_writes()
        report = self.cycle(subscription(numeric_policy=None, trust_share_title=True))
        self.assertEqual(report["status"], "COMPLETE", report)
        self.assertEqual(self.drive.saved, [["hidden2"]])
        self.assertEqual(self.drive.renamed, [("new-hidden2", "测试剧 (2026)S01E02_2160P.mkv")])
        # The failed confirmed source is kept and the saving one appended after it.
        self.assertEqual([row["url"] for row in report["preferred_sources"]], [SOURCE["url"], FRESH])
        self.assertEqual({key: report["preferred_sources"][1][key] for key in ("parent_id", "path", "quality_tags")},
                         {"parent_id": "home", "path": "🏠", "quality_tags": ["2160p"]})

    def test_uncertain_source_waits_without_blocking_when_opted_in(self):
        self.drive.dead.add(SOURCE["url"])
        self.hidden_share()
        sub = subscription(numeric_policy=None, wait_on_uncertain_source=True)
        report = self.cycle(sub)
        self.assertEqual(report["status"], "WAITING", report)
        self.assertEqual((report["reasons"], report["ambiguous_episodes"]), ([UNCERTAIN_SOURCE_REASON], ["S01E02"]))
        self.assert_no_writes()
        store = SubscriptionStore(self.root / "runtime/subscriptions.json", "root")
        record = store.add(sub["request"], preferred_sources=[SOURCE], auto_save=True, wait_on_uncertain_source=True)
        outcome = store.record_run(record["subscription_id"], report)
        self.assertFalse(outcome["record"]["blocked"])
        self.assertIn(outcome["summary"]["status"], HEALTHY_STATUSES)
        # A real write failure still blocks the opted-in subscription.
        failed = store.record_run(record["subscription_id"], {"status": "UNKNOWN", "reasons": ["save_task_not_complete"]},
                                  checked_at=datetime.now(timezone.utc))
        self.assertTrue(failed["record"]["blocked"])

    def test_waiting_mode_still_saves_certain_episodes_and_reports_the_rest(self):
        self.episodes = (1, 2, 3)
        self.primary = Metadata(episodes=(1, 2, 3))
        self.primary.source = "tmdb"
        self.drive.shares[SOURCE["url"]] = {
            "0": [file("shareSeries", "测试剧", "0", directory=True)],
            "shareSeries": [file("source4k", "4K", "shareSeries", directory=True)],
            "source4k": [file("source2", "02.mkv", "source4k", size=400),
                         file("tie-a", "S01E03.2160p.mkv", "source4k", size=400),
                         file("tie-b", "S01E03.4K.mkv", "source4k", size=400)]}
        strict = self.cycle(subscription())
        self.assertEqual((strict["status"], strict["ambiguous_episodes"]), ("NEEDS_INPUT", ["S01E03"]), strict)
        self.assert_no_writes()
        report = self.cycle(subscription(wait_on_uncertain_source=True))
        self.assertEqual(report["status"], "COMPLETE", report)
        self.assertEqual(self.drive.saved, [["source2"]])
        self.assertEqual(report["ambiguous_episodes"], ["S01E03"])
        self.assertEqual(report["pending_resource_episodes"], ["S01E03"])
        self.assertIn(UNCERTAIN_SOURCE_REASON, report["reasons"])


class ArchiveCycleTests(CycleCase):
    def setUp(self):
        super().setUp()
        self.share(ZIPS, "测试剧 4K DV 60帧率 高码", {
            "0": [file("sroot", "测试剧", "0", directory=True)],
            "sroot": [file("dv", "4K DV 60帧率 高码", "sroot", directory=True)],
            "dv": [file("zip2", "02.zip", "dv", size=9000)]})
        self.drive.outputs["02.zip"] = [("02.mkv", 8900), ("02.chs.ass", 30)]
        self.candidate_id = _digest((source_digest(ZIPS), "zip2"))

    def sub(self, **changes):
        return subscription(numeric_policy=None, preferred_sources=[], archive_auto="largest", **changes)

    def test_default_never_saves_an_archive(self):
        report = self.cycle(subscription(numeric_policy=None, preferred_sources=[]))
        self.assertEqual(report["status"], "WAITING", report)
        self.assertEqual(report["archive_candidates"][0]["handling"], "explicit_selection_required")
        self.assert_no_writes()

    def test_readonly_cycle_only_plans_the_archive(self):
        report = self.cycle(self.sub(), apply=False)
        self.assertEqual(report["status"], "READY", report)
        self.assertEqual(report["archive_plan"], [{"episode_key": "S01E02", "candidate_id": self.candidate_id,
                                                   "name": "02.zip", "size": 9000, "path": "测试剧/4K DV 60帧率 高码/02.zip"}])
        self.assert_no_writes()
        self.assertEqual(self.drive.extracted, [])

    def test_archive_is_fetched_extracted_renamed_and_moved_into_the_season(self):
        # Subscriptions recycle leftovers by default; the manual commands' setting does not apply.
        self.config["archive_cleanup"] = "keep"
        report = self.cycle(self.sub())
        self.assertEqual(report["status"], "COMPLETE", report)
        target = "测试剧 (2026)S01E02_2160P_DV"
        season = sorted(row["name"] for row in self.drive.folders["season"])
        self.assertEqual(season, sorted(["测试剧 S01E01.MKV", target + ".mkv", target + ".chs.ass"]))
        self.assertEqual(report["missing"], [])
        self.assertEqual(sorted(row["target_name"] for row in report["new_files"]),
                         sorted([target + ".mkv", target + ".chs.ass"]))
        # Archive, fetch staging and the emptied extraction folder are all recycled.
        self.assertEqual(len(self.drive.trashed), 3)
        self.assertEqual(sorted(row["name"] for row in self.drive.folders["root"]), ["测试剧 (2026)"])
        self.assertEqual(report["preferred_sources"][0]["url"], ZIPS)
        self.assertEqual(report["preferred_sources"][0]["quality_tags"], ["2160p", "Dolby Vision"])
        again = self.cycle(self.sub())
        self.assertEqual(again["status"], "UP_TO_DATE", again)
        self.assertEqual((len(self.drive.saved), len(self.drive.extracted)), (1, 1))

    def test_unplaced_leftovers_keep_the_extraction_folder(self):
        self.drive.outputs["02.zip"].append(("说明.txt", 1))
        report = self.cycle(self.sub())
        self.assertEqual(report["status"], "COMPLETE", report)
        self.assertIn("archive_cleanup_incomplete_see_archive_cleanup", report["reasons"])
        cleanup = report["archive_episodes"][0]["cleanup"]["cleanup"]
        self.assertEqual((cleanup["target"]["state"], cleanup["target"]["reason"]), ("kept", "extraction_folder_not_empty"))
        self.assertEqual(len(self.drive.trashed), 2)

    def test_keep_setting_leaves_the_temporary_folders(self):
        self.config["archive_cleanup"] = "recycle"
        report = self.cycle(self.sub(archive_cleanup="keep"))
        self.assertEqual(report["status"], "COMPLETE", report)
        self.assertEqual(self.drive.trashed, [])
        self.assertEqual(len(self.drive.folders["root"]), 3)

    def test_ask_setting_reports_the_cleanup_choice(self):
        report = self.cycle(self.sub(archive_cleanup="ask"))
        self.assertEqual(report["status"], "COMPLETE", report)
        self.assertIn("archive_cleanup_awaiting_choice", report["reasons"])
        cleanup = report["archive_episodes"][0]["cleanup"]
        self.assertRegex(cleanup["choices"]["recycle"], r"^python3 scripts/media.py archive cleanup --fid \S+ --apply$")
        self.assertEqual(self.drive.trashed, [])

    def test_several_extracted_videos_stop_before_any_placement(self):
        self.drive.outputs["02.zip"] = [("02.mkv", 8900), ("sample.mkv", 100)]
        report = self.cycle(self.sub())
        self.assertEqual(report["status"], "NEEDS_INPUT", report)
        self.assertEqual(report["reasons"], ["archive_output_requires_single_video"])
        self.assertEqual((self.drive.renamed, self.drive.moved), ([], []))

    def test_unknown_move_is_observed_and_never_resent(self):
        self.drive.move_failure = RuntimeError("timeout after request")
        report = self.cycle(self.sub())
        self.assertEqual(report["status"], "UNKNOWN", report)
        self.assertEqual(report["reasons"], ["archive_placement_not_completed"])
        self.drive.move_failure = None
        again = self.cycle(self.sub())
        self.assertEqual(again["status"], "UNKNOWN", again)
        self.assertEqual(again["reasons"], ["archive_output_move_outcome_unknown"])
        self.assertEqual(len(self.drive.moved), 1)
        self.assertEqual((len(self.drive.saved), len(self.drive.extracted)), (1, 1))

    def test_archive_without_an_existing_season_folder_waits(self):
        self.drive.folders["series"] = []
        del self.drive.folders["season"]
        self.drive.folders["series"].append(file("old-1", "测试剧 S01E01.MKV", "series"))
        report = self.cycle(self.sub())
        # A flat series folder is the destination; only a missing one waits.
        self.assertEqual(report["status"], "COMPLETE", report)
        self.drive.folders["root"] = [file("tv", "电视剧", "root", directory=True)]
        self.drive.folders["tv"] = []
        waiting = self.cycle(self.sub())
        self.assertEqual(waiting["status"], "WAITING", waiting)
        self.assertEqual(waiting["reasons"], ["archive_auto_requires_existing_season_directory"])


if __name__ == "__main__":
    unittest.main()
