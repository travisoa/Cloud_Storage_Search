"""Archive pipeline: selected share archive → staging → extracted output → library plan."""
from copy import deepcopy
import unittest

from media_helper.archive_flow import extracted_folder, fetch, output_library, staging_name, task_candidate
from media_helper.library_import import apply_import, plan_import, scan
from media_helper.workflow import _digest

URL = "https://pan.quark.cn/s/archiveshare"
SOURCE = {"url": URL, "title": "沙丘2 合集", "source_hash": _digest(URL)}
KEY = "archive-fetch:" + "0" * 64


def row(fid, name, parent, size=10, directory=False):
    return {"fid": fid, "name": name, "parent_id": parent, "size": 0 if directory else size, "is_dir": directory}


def candidate(fid="arc", name="沙丘2.2024.2160p.rar", size=5000):
    return {"candidate_id": _digest((SOURCE["source_hash"], fid)), "fid": fid, "name": name, "size": size,
            "path": name, "share_source": {"source_hash": SOURCE["source_hash"]}}


class Journal:
    def __init__(self):
        self.records = {}

    def get(self, key):
        return deepcopy(self.records.get(key))

    def put(self, key, value):
        self.records[key] = deepcopy(value)


class Storage:
    capabilities = {"save_submission_callback": True}
    account_scope = "quark:test"

    def __init__(self):
        self.folders = {"root": [row("movies", "电影", "root", directory=True), row("tv", "电视剧", "root", directory=True)],
                        "movies": [], "tv": []}
        self.shared = [row("arc", "沙丘2.2024.2160p.rar", "0", size=5000),
                       row("multi", "沙丘2.part1.rar", "0", size=5000)]
        self.saves, self.mkdirs, self.moves, self.renames = [], [], [], []
        self.fail = None

    def list_files(self, parent):
        return deepcopy(self.folders[parent])

    def share_files(self, url, password=None, parent_id="0"):
        return {"share_ref": {"stoken": "secret"}, "files": deepcopy(self.shared)}

    def mkdir(self, parent, name):
        new = row("stage", name, parent, directory=True)
        self.folders[parent].append(new)
        self.folders["stage"] = []
        self.mkdirs.append(name)
        return deepcopy(new)

    def save_files(self, share, files, target, on_submitted=None):
        self.saves.append([item["fid"] for item in files])
        if self.fail == "before":
            raise RuntimeError("cookie in transport error")
        on_submitted({"task_id": "t1", "source_fids": [item["fid"] for item in files], "target_parent_id": target})
        self.folders[target].append(row("saved-arc", files[0]["name"], target, size=files[0]["size"]))
        if self.fail == "after":
            raise RuntimeError("polling failed")
        return {"task_id": "t1", "status": "done", "saved_fids": ["saved-arc"]}

    def task_status(self, task_id):
        return {"task_id": task_id, "status": "done", "saved_fids": ["saved-arc"]}

    def move(self, fid, parent):
        for rows in self.folders.values():
            for item in list(rows):
                if item["fid"] == fid:
                    rows.remove(item)
                    self.folders[parent].append(dict(item, parent_id=parent))
                    self.moves.append((fid, parent))
                    return {"fid": fid}
        raise KeyError(fid)

    def rename(self, fid, name):
        for rows in self.folders.values():
            for item in rows:
                if item["fid"] == fid:
                    item["name"] = name
                    self.renames.append((fid, name))
                    return {"fid": fid}
        raise KeyError(fid)


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.storage, self.journal = Storage(), Journal()

    def fetch(self, item=None, **options):
        return fetch(self.storage, candidate=item or candidate(), source=SOURCE, library_root_id="root",
                     journal=self.journal, job_key=KEY, **options)

    def test_plan_names_staging_folder_without_writes(self):
        report = self.fetch()
        self.assertEqual(report["status"], "PLAN", report)
        self.assertEqual(report["staging"]["name"], staging_name(candidate()["candidate_id"]))
        self.assertEqual((self.storage.mkdirs, self.storage.saves, self.journal.records), ([], [], {}))

    def test_apply_saves_once_into_new_staging_and_verifies_new_fid(self):
        report = self.fetch(apply=True)
        self.assertEqual(report["status"], "FETCHED", report)
        self.assertEqual(report["save_task_id"], "t1")
        self.assertEqual(report["archive_fid"], "saved-arc")
        self.assertEqual(self.storage.saves, [["arc"]])
        again = self.fetch(apply=True)
        self.assertEqual(again["status"], "FETCHED")
        self.assertEqual(self.storage.saves, [["arc"]])
        self.assertNotIn("secret", repr(self.journal.records))

    def test_multipart_or_changed_candidate_is_refused(self):
        multi = candidate("multi", "沙丘2.part1.rar")
        self.assertEqual(self.fetch(multi, apply=True)["reasons"], ["only_single_supported_archive_can_be_fetched"])
        changed = self.fetch(candidate(size=1), apply=True)
        self.assertEqual(changed["reasons"], ["archive_candidate_changed_or_not_observed"])
        self.assertEqual(self.storage.saves, [])

    def test_failure_after_submission_recovers_by_task_id_without_resubmitting(self):
        self.storage.fail = "after"
        self.assertEqual(self.fetch(apply=True)["status"], "UNKNOWN")
        self.storage.fail = None
        checked = self.fetch(reconcile=True)
        self.assertEqual(checked["status"], "FETCHED", checked)
        self.assertEqual(self.storage.saves, [["arc"]])

    def test_failure_without_task_id_is_never_resubmitted(self):
        self.storage.fail = "before"
        report = self.fetch(apply=True)
        self.assertEqual(report["status"], "UNKNOWN")
        self.assertNotIn("cookie", repr(report))
        self.storage.fail = None
        again = self.fetch(apply=True)
        self.assertEqual(again["reasons"], ["archive_save_unknown_without_task_identifier"])
        self.assertEqual(self.storage.saves, [["arc"]])

    def test_task_candidate_requires_password_free_source_ref(self):
        item = candidate()
        task = {"report": {"archive_candidates": [item], "source_refs": [SOURCE]}}
        self.assertEqual(task_candidate(task, item["candidate_id"])[1]["url"], URL)
        with self.assertRaises(ValueError):
            task_candidate({"report": {"archive_candidates": [item], "source_refs": []}}, item["candidate_id"])


class OutputPlanTests(unittest.TestCase):
    def test_verified_extraction_output_becomes_ordinary_import_plan(self):
        storage, journal = Storage(), Journal()
        storage.folders["root"].append(row("out", "解压待整理_x", "root", directory=True))
        storage.folders["out"] = [row("v", "Dune.Part.Two.2024.2160p.mkv", "out", size=9000),
                                  row("s", "Dune.Part.Two.2024.2160p.zh.srt", "out", size=50)]
        journal.put("archive:k", {"kind": "archive_extract", "status": "EXTRACTED",
                                  "target": {"folder_id": "out"}, "bound": {"library_root_id": "root"}})
        folder, root = extracted_folder(journal, "archive:k")
        snapshot = scan(storage, source_id=folder, excluded_ids=[root])
        config = {"library_root_id": "root", "categories": {"tv": {"folder_id": "tv", "name": "电视剧"},
                                                             "movie": {"folder_id": "movies", "name": "电影"}}}
        library = {**output_library(config), "account_scope_hash": snapshot["account_scope_hash"]}
        resolver = lambda file, context: {"kind": "movie", "title": "沙丘2", "year": 2024,
                                          "metadata_source": "tmdb", "movie_id": "693134"}
        plan = plan_import(snapshot, library, resolver)
        self.assertEqual(plan["status"], "READY", plan)
        self.assertEqual([op["target_name"] for op in plan["operations"]],
                         ["沙丘2 (2024)_2160P.mkv", "沙丘2 (2024)_2160P.zh.srt"])
        result = apply_import(plan, storage, journal=journal, job_key="archive-import-test", apply=True)
        self.assertEqual(result["status"], "COMPLETE", result)
        movie_dir = next(item for item in storage.folders["movies"] if item["name"] == "沙丘2 (2024)")
        self.assertEqual(sorted(item["name"] for item in storage.folders[movie_dir["fid"]]),
                         ["沙丘2 (2024)_2160P.mkv", "沙丘2 (2024)_2160P.zh.srt"])

    def test_unfinished_extraction_cannot_be_planned(self):
        journal = Journal()
        journal.put("archive:k", {"kind": "archive_extract", "status": "SUBMITTED"})
        with self.assertRaises(ValueError):
            extracted_folder(journal, "archive:k")


if __name__ == "__main__":
    unittest.main()
