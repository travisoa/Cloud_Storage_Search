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
        self.assertEqual(report["plan"][0]["target_name"], "沙丘2 (2024) [2160p HEVC].mkv")
        self.assertEqual(report["plan"][1]["target_name"], "沙丘2 (2024) [2160p HEVC].zh.srt")
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
        self.assertEqual(sorted(self.storage.renamed), [("new-sub", "沙丘2 (2024) [2160p HEVC].zh.srt"),
                                                        ("new-v4k", "沙丘2 (2024) [2160p HEVC].mkv")])
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

    def test_movie_without_confirmed_identity_needs_input(self):
        report = run(request(movie_id=None), self.storage, self.search, self.journal, apply=True)
        self.assertEqual(report["status"], "NEEDS_INPUT")
        self.assertIn("confirmed_movie_id_required", report["reasons"])

    def test_movie_filename_keeps_prefix_and_tags(self):
        self.assertEqual(movie_filename("沙丘2 (2024)", ".mkv", {"resolution": "2160p", "hdr": "HDR10"}),
                         "沙丘2 (2024) [2160p HDR10].mkv")
        self.assertEqual(movie_filename("沙丘2 (2024)", ".mp4", {}), "沙丘2 (2024).mp4")


if __name__ == "__main__":
    unittest.main()
