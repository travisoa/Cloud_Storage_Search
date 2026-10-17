import json
from pathlib import Path
import tempfile
import unittest

from media_helper.journal import JobJournal, run_lock
from media_helper.search import PanSouSearch, SearchError


class JournalTests(unittest.TestCase):
    def test_pending_intent_survives_process_reopen_and_history_is_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.sqlite"
            store = JobJournal(path)
            store.put("save-1", {"state": "save_pending", "source_fids": ["a"]})
            store.close()
            store = JobJournal(path)
            self.assertEqual(store.get("save-1")["state"], "save_pending")
            store.put("save-1", {"state": "complete", "saved_fids": ["b"]})
            self.assertEqual(store.connection.execute("SELECT count(*) FROM events").fetchone()[0], 2)
            store.close()

    def test_credential_fields_never_reach_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobJournal(Path(directory) / "jobs.sqlite")
            for record in ({"cookie": "sensitive"}, {"source": {"stoken": "sensitive"}}, {"share_fid_token": "sensitive"}):
                with self.assertRaises(ValueError):
                    store.put("unsafe", record)
            self.assertIsNone(store.get("unsafe"))
            store.close()

    def test_concurrent_runs_cannot_mutate_same_account(self):
        with tempfile.TemporaryDirectory() as directory:
            with run_lock(Path(directory) / "run.lock"):
                with self.assertRaises(RuntimeError):
                    with run_lock(Path(directory) / "run.lock"):
                        self.fail("second runner acquired lock")


class SearchTests(unittest.TestCase):
    def test_empty_results_are_distinct_from_failure(self):
        search = PanSouSearch(transport=lambda *_: {"code": 0, "data": {"merged_by_type": {}}})
        self.assertEqual(search.search("嫁金钗"), [])
        search.transport = lambda *_: {"code": 1, "message": "upstream failed"}
        with self.assertRaises(SearchError):
            search.search("嫁金钗")

    def test_only_quark_links_are_returned_without_opening_them(self):
        records = [{"url": "https://pan.quark.cn/s/abc", "note": "剧"},
                   {"url": "https://pan.quark.cn.evil.example/s/abc"},
                   {"url": "https://pan.quark.cn/s/abc"}]
        search = PanSouSearch(transport=lambda *_: {"code": 0, "data": {"merged_by_type": {"quark": records}}})
        self.assertEqual(len(search.search("剧")), 1)


if __name__ == "__main__":
    unittest.main()
