"""Movie search/save/rename scenarios with fake providers."""
from copy import deepcopy
import unittest

from media_helper.movie_workflow import reconcile, run
from media_helper.naming import movie_filename
from media_helper.workflow import _digest

GB = 1024 ** 3
URL = "https://pan.quark.cn/s/movieshare"


def file(fid, name, parent, size=4 * GB, directory=False):
    return {"fid": fid, "name": name, "parent_id": parent, "size": 0 if directory else size, "is_dir": directory}


def request(**changes):
    return {"kind": "movie", "title": "沙丘2", "year": 2024, "movie_id": "693134", "metadata_source": "tmdb",
            "library_root_id": "root", "aliases": ["Dune: Part Two"], **changes}


def candidate(fid):
    return _digest((_digest(URL), fid))


class Journal:
    def __init__(self):
        self.records = {}

    def get(self, key):
        return deepcopy(self.records.get(key))

    def put(self, key, record):
        self.records[key] = deepcopy(record)


class Search:
    def __init__(self):
        self.calls = 0

    def search(self, query):
        self.calls += 1
        return [{"title": "沙丘2 2024 4K", "url": URL, "provider": "quark"}]


class Storage:
    capabilities = {"save_submission_callback": True}
    account_scope = "quark:test"

    def __init__(self):
        self.folders = {
            "root": [file("movies", "电影🎬", "root", directory=True), file("tv", "电视剧", "root", directory=True)],
            "movies": [file("other", "星际穿越 (2014)", "movies", directory=True)],
            "other": [file("old", "星际穿越 (2014) [1080p].mkv", "other")],
            "tv": [],
        }
        self.shared = {"0": [file("s-dir", "沙丘2.2024", "0", directory=True)],
                       "s-dir": [file("v4k", "Dune.Part.Two.2024.2160p.HEVC.mkv", "s-dir"),
                                 file("sub", "Dune.Part.Two.2024.2160p.HEVC.zh.srt", "s-dir", size=4000),
                                 file("trailer", "沙丘2 预告片.mp4", "s-dir", size=200 * 1024 * 1024),
                                 file("v1080", "Dune.Part.Two.2024.1080p.mkv", "s-dir"),
                                 file("zip", "沙丘2.2024.原盘.rar", "s-dir")]}
        self.saved, self.renamed, self.created = [], [], []
        self.fail_save = False

    def list_files(self, parent):
        return deepcopy(self.folders[parent])

    def share_files(self, url, password=None, parent_id="0"):
        return {"share_ref": {"share_id": "s", "stoken": "never-persist"},
                "files": deepcopy(self.shared[parent_id])}

    def mkdir(self, parent, name):
        row = file("dir-%d" % len(self.created), name, parent, directory=True)
        self.created.append(row)
        self.folders[parent].append(row)
        self.folders[row["fid"]] = []
        return deepcopy(row)

    def save_files(self, share, files, target, on_submitted=None):
        on_submitted({"task_id": "task-1", "source_fids": [row["fid"] for row in files], "target_parent_id": target})
        if self.fail_save:
            raise RuntimeError("raw response with cookie")
        ids = []
        for row in files:
            self.folders[target].append(file("new-" + row["fid"], row["name"], target, size=row["size"]))
            ids.append("new-" + row["fid"])
        self.saved.append(ids)
        return {"task_id": "task-1", "status": "done", "saved_fids": ids}

    def task_status(self, task_id):
        target = next(key for key, rows in self.folders.items() if any(r["fid"].startswith("new-") for r in rows)) \
            if any(r["fid"].startswith("new-") for rows in self.folders.values() for r in rows) else None
        if target is None:
            for row in self.created:
                ids = []
                for source in [self.shared["s-dir"][0], self.shared["s-dir"][1]]:
                    self.folders[row["fid"]].append(file("new-" + source["fid"], source["name"], row["fid"], size=source["size"]))
                    ids.append("new-" + source["fid"])
                return {"status": "done", "task_id": task_id, "saved_fids": ids}
        return {"status": "done", "task_id": task_id,
                "saved_fids": [r["fid"] for r in self.folders[target] if r["fid"].startswith("new-")]}

    def rename(self, fid, name):
        for rows in self.folders.values():
            for row in rows:
                if row["fid"] == fid:
                    row["name"] = name
                    self.renamed.append((fid, name))
                    return {"fid": fid}
        raise KeyError(fid)


class MovieWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.storage, self.search, self.journal = Storage(), Search(), Journal()

    def test_plan_selects_best_feature_skips_trailer_and_archive_without_writes(self):
        report = run(request(), self.storage, self.search, self.journal)
        self.assertEqual(report["status"], "READY", report)
        self.assertFalse(report["writes_attempted"])
        self.assertEqual(report["directories"], [{"parent_id": "movies", "name": "沙丘2 (2024)"}])
        self.assertEqual([row["source"]["fid"] for row in report["plan"]], ["v4k", "sub"])
        self.assertEqual(report["plan"][0]["target_name"], "沙丘2 (2024)_2160P.mkv")
        self.assertEqual(report["plan"][1]["target_name"], "沙丘2 (2024)_2160P.zh.srt")
        reasons = {row["fid"]: row["reason"] for row in report["source_review"]}
        self.assertEqual(reasons["trailer"], "extra_or_trailer_not_a_feature")
        self.assertEqual([row["fid"] for row in report["archive_candidates"]], ["zip"])
        self.assertEqual(self.journal.records, {})
        self.assertNotIn("never-persist", repr(report))

    def test_apply_creates_folder_saves_once_and_renames_only_new_fids(self):
        report = run(request(), self.storage, self.search, self.journal, apply=True)
        self.assertEqual(report["status"], "COMPLETE", report)
        self.assertEqual([row["name"] for row in self.storage.created], ["沙丘2 (2024)"])
        self.assertEqual(self.storage.saved, [["new-v4k", "new-sub"]])
        self.assertEqual(sorted(self.storage.renamed), [("new-sub", "沙丘2 (2024)_2160P.zh.srt"),
                                                        ("new-v4k", "沙丘2 (2024)_2160P.mkv")])
        self.assertNotIn("old", [fid for fid, _ in self.storage.renamed])
        again = run(request(), self.storage, self.search, self.journal, apply=True)
        self.assertEqual(again["status"], "COMPLETE", again)
        self.assertEqual(len(self.storage.saved), 1)
        self.assertNotIn("never-persist", repr(self.journal.records))

    def test_existing_movie_folder_with_video_is_up_to_date_without_search(self):
        self.storage.folders["movies"].append(file("dune", "沙丘2 (2024)", "movies", directory=True))
        self.storage.folders["dune"] = [file("have", "Dune2.mkv", "dune")]
        report = run(request(), self.storage, self.search, self.journal, apply=True)
        self.assertEqual(report["status"], "UP_TO_DATE", report)
        self.assertEqual(self.search.calls, 0)
        self.assertFalse(report["writes_attempted"])

    def test_movie_in_region_folder_is_found_without_opening_other_movies(self):
        self.storage.folders["movies"].append(file("west", "欧美", "movies", directory=True))
        self.storage.folders["west"] = [file("dune", "沙丘2 (2024)", "west", directory=True)]
        self.storage.folders["dune"] = [file("have", "Dune2.mkv", "dune")]
        del self.storage.folders["other"]  # 星际穿越 (2014) is never opened
        report = run(request(), self.storage, self.search, self.journal, apply=True)
        self.assertEqual(report["status"], "UP_TO_DATE", report)
        self.assertEqual(self.search.calls, 0)

    def test_movie_in_unrecognized_collection_folder_is_still_found(self):
        self.storage.folders["movies"].append(file("set", "科幻合集", "movies", directory=True))
        self.storage.folders["set"] = [file("dune", "沙丘2 (2024)", "set", directory=True)]
        self.storage.folders["dune"] = [file("have", "Dune2.mkv", "dune")]
        del self.storage.folders["other"]  # dated works stay closed
        report = run(request(), self.storage, self.search, self.journal, apply=True)
        self.assertEqual(report["status"], "UP_TO_DATE", report)
        self.assertEqual((self.storage.created, self.storage.saved), ([], []))

    def test_existing_empty_year_folder_is_reused_and_keeps_its_name_as_prefix(self):
        self.storage.folders["movies"].append(file("dune", "沙丘2 (2024)", "movies", directory=True))
        self.storage.folders["dune"] = []
        report = run(request(), self.storage, self.search, self.journal)
        self.assertEqual(report["directories"], [])
        self.assertEqual(report["target_folder_id"], "dune")

    def test_competing_bare_and_year_directories_need_confirmation(self):
        self.storage.folders["movies"] += [file("a", "沙丘2", "movies", directory=True),
                                           file("b", "沙丘2 (2024)", "movies", directory=True)]
        self.storage.folders["a"] = self.storage.folders["b"] = []
        report = run(request(), self.storage, self.search, self.journal)
        self.assertEqual(report["status"], "NEEDS_INPUT")
        self.assertIn("movie_directory_has_competing_identity_or_year", report["reasons"])

    def test_qualified_or_separated_year_directories_block_parallel_movie_folder(self):
        for name in ("沙丘2.2024", "沙丘2_2024", "沙丘2-2024", "沙丘2 (2024) 4K"):
            with self.subTest(name=name):
                storage = Storage()
                storage.folders["movies"].append(file("dune", name, "movies", directory=True))
                storage.folders["dune"] = []
                search = Search()
                report = run(request(), storage, search, Journal(), apply=True)
                self.assertEqual(report["status"], "NEEDS_INPUT", report)
                self.assertIn("movie_directory_identity_requires_confirmation", report["reasons"])
                self.assertEqual((storage.created, storage.saved, storage.renamed), ([], [], []))
                self.assertEqual(search.calls, 0)

    def test_folders_inside_works_and_other_dated_works_do_not_block(self):
        self.storage.folders["movies"] += [file("dune", "沙丘2 (2024)", "movies", directory=True),
                                           file("dune1", "沙丘 (2021)", "movies", directory=True)]
        self.storage.folders["dune"] = [file("release", "沙丘2.2024.2160p.UHD.BluRay", "dune", directory=True)]
        self.storage.folders["release"] = [file("have", "Dune2.mkv", "release")]
        self.storage.folders["dune1"] = [file("teaser", "沙丘2 先导预告", "dune1", directory=True)]
        self.storage.folders["teaser"] = []
        report = run(request(), self.storage, self.search, self.journal)
        self.assertEqual(report["status"], "UP_TO_DATE", report)

    def test_latin_title_blocks_only_as_a_whole_word_of_the_same_year(self):
        for names, blocked in ((["Kite", "Split (2016)", "It Follows (2014)", "Little Women"], False),
                               (["It.2017.2160p"], True), (["It Chapter One"], True)):
            with self.subTest(names=names):
                storage = Storage()
                for index, name in enumerate(names):
                    storage.folders["movies"].append(file("d%d" % index, name, "movies", directory=True))
                    storage.folders["d%d" % index] = []
                report = run(request(title="It", year=2017, aliases=[]), storage, Search(), Journal())
                self.assertEqual("movie_directory_identity_requires_confirmation" in report["reasons"], blocked, report)

    def test_unverifiable_subtitle_size_stops_before_any_write(self):
        for size in (0, None):
            with self.subTest(size=size):
                storage = Storage()
                storage.shared["s-dir"][1]["size"] = size
                report = run(request(), storage, Search(), Journal(), apply=True)
                self.assertEqual(report["status"], "NEEDS_INPUT", report)
                self.assertIn("associated_subtitle_size_unverified", report["reasons"])
                self.assertEqual((storage.created, storage.saved, storage.renamed), ([], [], []))

    def test_equal_versions_need_selection_and_explicit_choice_is_honoured(self):
        self.storage.shared["s-dir"].append(file("v4k-b", "沙丘2.2024.4K.mkv", "s-dir"))
        report = run(request(quality_preference=["2160p"]), self.storage, self.search, self.journal)
        self.assertEqual(report["status"], "NEEDS_INPUT", report)
        self.assertEqual({row["fid"] for row in report["unresolved_movie"]}, {"v4k", "v4k-b"})
        chosen = run(request(quality_preference=["2160p"], source_selection={"movie": candidate("v4k-b")}),
                     self.storage, self.search, self.journal)
        self.assertEqual(chosen["status"], "READY", chosen)
        self.assertEqual(chosen["plan"][0]["source"]["fid"], "v4k-b")

    def test_year_mismatch_and_episode_files_are_not_movie_candidates(self):
        self.storage.shared["s-dir"] = [file("old-cut", "沙丘2.2021.2160p.mkv", "s-dir"),
                                        file("ep", "沙丘2.S01E01.2160p.mkv", "s-dir")]
        report = run(request(), self.storage, self.search, self.journal)
        self.assertEqual(report["status"], "NEEDS_INPUT")
        reasons = {row["fid"]: row["reason"] for row in report["source_review"]}
        self.assertEqual(reasons, {"old-cut": "release_year_mismatch", "ep": "episode_marker_not_a_movie"})

    def test_unknown_save_is_never_resubmitted_and_reconcile_is_read_only(self):
        self.storage.fail_save = True
        report = run(request(), self.storage, self.search, self.journal, apply=True)
        self.assertEqual(report["status"], "UNKNOWN", report)
        self.assertNotIn("cookie", repr(report))
        self.storage.fail_save = False
        plan = run(request(), self.storage, self.search, self.journal)
        self.assertEqual(plan["status"], "UNKNOWN")
        checked = reconcile(request(), self.storage, self.journal)
        self.assertEqual(checked["status"], "READY_TO_RENAME", checked)
        self.assertEqual(self.storage.renamed, [])
        resumed = run(request(), self.storage, self.search, self.journal, apply=True)
        self.assertEqual(resumed["status"], "COMPLETE", resumed)
        self.assertEqual(self.storage.saved, [])
        self.assertEqual(len(self.storage.renamed), 2)

    def test_completed_movie_is_reverified_and_changed_files_are_not_complete(self):
        mutations = ("size", "parent", "missing", "saved_count", "duplicate_saved_fid")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                storage, journal = Storage(), Journal()
                saved = run(request(), storage, Search(), journal, apply=True)
                self.assertEqual(saved["status"], "COMPLETE", saved)
                record = journal.records[saved["job_key"]]
                operation = record["operations"][0]
                files = storage.folders[operation["target_parent_id"]]
                if mutation == "size":
                    files[0]["size"] += 1
                elif mutation == "parent":
                    files[0]["parent_id"] = "other"
                elif mutation == "missing":
                    files.pop()
                elif mutation == "saved_count":
                    operation["saved_fids"].pop()
                else:
                    operation["saved_fids"][1] = operation["saved_fids"][0]
                before = deepcopy((storage.created, storage.saved, storage.renamed))
                checked = reconcile(request(), storage, journal)
                self.assertEqual(checked["status"], "UNKNOWN", checked)
                self.assertFalse(checked["writes_attempted"])
                self.assertEqual((storage.created, storage.saved, storage.renamed), before)

    def test_reconcile_verifies_completed_files_and_preserves_their_source_mapping(self):
        self.storage.shared["s-dir"][1]["size"] = self.storage.shared["s-dir"][0]["size"]
        saved = run(request(), self.storage, self.search, self.journal, apply=True)
        checked = reconcile(request(), self.storage, self.journal)
        self.assertEqual(checked["status"], "COMPLETE", checked)
        operation = self.journal.records[saved["job_key"]]["operations"][0]
        files = self.storage.folders[operation["target_parent_id"]]
        files[0]["name"], files[1]["name"] = files[1]["name"], files[0]["name"]
        checked = reconcile(request(), self.storage, self.journal)
        self.assertEqual(checked["status"], "UNKNOWN", checked)
        self.assertIn("saved_file_source_mapping_changed", checked["reasons"])

    def test_reconcile_rejects_operation_outside_the_current_movie_directory(self):
        saved = run(request(), self.storage, self.search, self.journal, apply=True)
        self.journal.records[saved["job_key"]]["operations"][0]["target_parent_id"] = "other"
        checked = reconcile(request(), self.storage, self.journal)
        self.assertEqual(checked["status"], "NEEDS_INPUT", checked)
        self.assertIn("operation_target_not_in_observed_destination_scope", checked["reasons"])

    def test_movie_without_confirmed_identity_needs_input(self):
        report = run(request(movie_id=None), self.storage, self.search, self.journal, apply=True)
        self.assertEqual(report["status"], "NEEDS_INPUT")
        self.assertIn("confirmed_movie_id_required", report["reasons"])

    def test_movie_filename_keeps_prefix_and_tags(self):
        self.assertEqual(movie_filename("沙丘2 (2024)", ".mkv", {"resolution": "2160p", "hdr": "HDR10"}),
                         "沙丘2 (2024)_2160P_HDR10.mkv")
        self.assertEqual(movie_filename("沙丘2 (2024)", ".mp4", {}), "沙丘2 (2024).mp4")


if __name__ == "__main__":
    unittest.main()
