"""Archive CLI tests using mock providers and temporary private runtime files."""
import argparse
from contextlib import ExitStack, redirect_stderr
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from media_helper.archive_cli import _fetched_archive_recycled, _job_key, add_parser, run_command
from media_helper.journal import JobJournal
from media_helper.library_registry import LibraryRegistry, drive_reference
from media_helper.profiles import private_json
from media_helper.providers import UnknownOutcome
from tests.test_archives import ArchiveStorage, item


class ArchiveCommandCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.storage = ArchiveStorage()
        self.storage.rows["0"] = [item("root", "影库", "0", directory=True)]
        self.config = {"provider": "quark", "library_root_id": "root",
                       "credential_source": {"type": "alist", "storage_id": 1},
                       "categories": {"tv": {"folder_id": "tv", "name": "电视剧"}}}
        self.config_before = deepcopy(self.config)
        self.registry = LibraryRegistry(self.root / "runtime/libraries.json")
        self.registry.connect(drive_reference("new-drive", 2))
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        def default_storage(config):
            self.storage.account_scope = "quark:alist-storage:" + str(config["credential_source"]["storage_id"])
            return self.storage
        self.stack.enter_context(patch("media_helper.headless_cli._storage", side_effect=default_storage))
        def named(drive):
            self.storage.account_scope = drive["account_scope"]
            return self.storage
        self.stack.enter_context(patch("media_helper.library_cli._storage", side_effect=named))
        self.parser = argparse.ArgumentParser()
        add_parser(self.parser.add_subparsers(dest="command", required=True))

    def command(self, *arguments):
        args = self.parser.parse_args(["archive", *arguments])
        return run_command(args, self.config, self.root)

    def register(self, *, test=False):
        self.registry.register({"library_id": "family", "drive_id": "new-drive", "parent_id": "0",
                                "library_root_id": "root", "name": "影库", "categories": {
                                    "movie": {"folder_id": "movies", "name": "电影"},
                                    "tv": {"folder_id": "tv", "name": "电视剧"}},
                                "init_job_key": "library-init:" + "a" * 64, "test_only": test})


class ArchiveCommandTests(ArchiveCommandCase):
    def test_inspect_reuses_current_credentials_without_cloud_writes(self):
        result = self.command("inspect", "--folder-id", "source")
        self.assertEqual(result["status"], "INSPECTED", result)
        self.assertFalse(result["browser_opened"])
        self.assertEqual(self.storage.created, [])
        self.assertEqual(self.storage.submissions, [])
        self.assertEqual(self.storage.account_scope, "quark:alist-storage:1")
        self.assertEqual(self.config, self.config_before)
        path = self.root / "runtime/reports/archive-latest.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_extract_plan_is_default_and_plan_overrides_apply(self):
        for flags in ([], ["--apply", "--plan"], ["--apply", "--reconcile"]):
            result = self.command("extract", "--fid", "archive1", "--parent-id", "source", *flags)
            self.assertEqual(result["status"], "PLAN", result)
            self.assertFalse(result["writes_attempted"])
            self.assertEqual((self.storage.created, self.storage.submissions), ([], []))

    def test_fetch_reports_source_and_save_task_ids_without_changing_legacy_task_id(self):
        from tests.test_archive_flow import SOURCE, candidate
        source_task_id = "a" * 32
        with patch("media_helper.tasks.TaskStore.load", return_value={}):
            with patch("media_helper.archive_flow.task_candidate", return_value=(candidate(), SOURCE)):
                with patch("media_helper.archive_flow.fetch", return_value={
                        "status": "UNKNOWN", "task_id": "cloud-save-task", "save_task_id": "cloud-save-task"}):
                    result = self.command("fetch", source_task_id, "--candidate-id", candidate()["candidate_id"], "--reconcile")
        self.assertEqual(result["task_id"], source_task_id)
        self.assertEqual(result["source_task_id"], source_task_id)
        self.assertEqual(result["save_task_id"], "cloud-save-task")

    def test_fetch_cannot_reuse_another_connection_or_unbound_task_for_writes(self):
        for scope in (None, "quark:alist-storage:2"):
            with self.subTest(scope=scope):
                task = {} if scope is None else {"account_scope": scope}
                with patch("media_helper.tasks.TaskStore.load", return_value=task), \
                        patch("media_helper.archive_flow.task_candidate") as candidate, \
                        patch("media_helper.archive_flow.fetch") as fetch:
                    result = self.command("fetch", "a" * 32, "--candidate-id", "b" * 64, "--apply")
                self.assertEqual(result["status"], "NEEDS_INPUT", result)
                self.assertFalse(result["writes_attempted"])
                candidate.assert_not_called()
                fetch.assert_not_called()

    def test_apply_stages_only_and_task_id_is_durable_before_reconciliation(self):
        with patch("media_helper.library_import.apply_import", side_effect=AssertionError("extract must not import")):
            result = self.command("extract", "--fid", "archive1", "--parent-id", "source", "--apply")
        self.assertEqual(result["status"], "EXTRACTED", result)
        self.assertFalse(result["imported"])
        self.assertEqual(len(self.storage.submissions), 1)
        self.assertEqual(self.config, self.config_before)
        journal = JobJournal(self.root / "runtime/archive-jobs.sqlite3")
        try:
            record = journal.get(result["job_key"])
        finally:
            journal.close()
        self.assertEqual(record["task_id"], "task1")
        self.assertEqual(record["bound"]["source"]["parent_id"], "source")
        second = self.command("extract", "--fid", "archive1", "--parent-id", "source", "--reconcile", "--apply")
        self.assertEqual(second["status"], "EXTRACTED", second)
        self.assertFalse(second["writes_attempted"])
        self.assertEqual(self.storage.queries, ["task1"])
        self.assertEqual(len(self.storage.submissions), 1)

    def test_named_library_selects_its_credential_reference_and_exact_root(self):
        self.register()
        result = self.command("inspect", "--folder-id", "source", "--library-id", "family")
        self.assertEqual(result["library_id"], "family")
        self.assertEqual(self.storage.account_scope, "quark:alist-storage:2")
        self.assertEqual(result["library_root_id"], "root")
        self.assertEqual(self.config, self.config_before)

    def test_named_library_changed_root_blocks_inspection(self):
        self.register()
        self.storage.rows["0"][0]["name"] = "外部改名"
        with self.assertRaises(ValueError):
            self.command("inspect", "--folder-id", "source", "--library-id", "family")
        self.assertEqual(self.storage.submissions, [])

    def test_test_only_library_allows_inspection_and_plan_but_not_actual_extract(self):
        self.register(test=True)
        self.assertEqual(self.command("inspect", "--folder-id", "source", "--library-id", "family")["status"], "INSPECTED")
        result = self.command("extract", "--fid", "archive1", "--parent-id", "source",
                              "--library-id", "family", "--apply", "--plan")
        self.assertEqual(result["status"], "PLAN", result)
        with self.assertRaises(ValueError):
            self.command("extract", "--fid", "archive1", "--parent-id", "source", "--library-id", "family", "--apply")
        self.assertEqual(self.storage.submissions, [])

    def test_sources_outside_library_are_rejected(self):
        self.storage.rows["outside"] = [item("archive1", "outside.zip", "outside")]
        with self.assertRaises(ValueError):
            self.command("inspect", "--folder-id", "outside")
        result = self.command("extract", "--fid", "archive1", "--parent-id", "outside", "--apply")
        self.assertEqual(result["status"], "NEEDS_INPUT", result)
        self.assertEqual(self.storage.created, [])

    def test_changed_source_parent_cannot_mint_another_submission_key(self):
        first = self.command("extract", "--fid", "archive1", "--parent-id", "source", "--apply")
        self.storage.rows["root"].append(item("second", "另一个目录", "root", directory=True))
        moved = self.storage.rows["source"].pop()
        moved["parent_id"] = "second"
        self.storage.rows["second"] = [moved]
        second = self.command("extract", "--fid", "archive1", "--parent-id", "second", "--apply")
        self.assertEqual(second["job_key"], first["job_key"])
        self.assertEqual(second["status"], "NEEDS_INPUT", second)
        self.assertEqual(len(self.storage.submissions), 1)

    def test_nested_library_switch_cannot_mint_second_key_for_unknown_source_submission(self):
        self.register()
        self.registry.register({"library_id": "nested", "drive_id": "new-drive", "parent_id": "root",
                                "library_root_id": "source", "name": "待处理", "categories": {
                                    "movie": {"folder_id": "nested-movies", "name": "电影"},
                                    "tv": {"folder_id": "nested-tv", "name": "电视剧"}},
                                "init_job_key": "library-init:" + "b" * 64, "test_only": False})
        self.storage.skip_callback = True
        self.storage.extract_failure = UnknownOutcome("transport outcome unknown")
        first = self.command("extract", "--fid", "archive1", "--parent-id", "source", "--library-id", "family", "--apply")
        self.assertEqual(first["status"], "UNKNOWN", first)
        self.storage.extract_failure = None
        second = self.command("extract", "--fid", "archive1", "--parent-id", "source", "--library-id", "nested", "--apply")
        self.assertEqual(first["job_key"], second["job_key"])
        self.assertEqual(second["status"], "NEEDS_INPUT", second)
        self.assertEqual(len(self.storage.submissions), 1)

    def test_password_flags_are_not_supported_or_saved(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.parser.parse_args(["archive", "extract", "--fid", "archive1", "--parent-id", "source", "--password", "private"])
        self.assertEqual(self.storage.submissions, [])
        self.assertFalse((self.root / "runtime/archive-jobs.sqlite3").exists())

    def test_multivolume_cli_returns_pending_without_creating_directories(self):
        self.storage.rows["source"][0]["name"] = "show.7z.001"
        result = self.command("extract", "--fid", "archive1", "--parent-id", "source", "--apply")
        self.assertEqual(result["status"], "NEEDS_INPUT", result)
        self.assertIn("multipart_api_not_verified", result["reasons"])
        self.assertEqual(self.storage.created, [])
        saved = json.loads((self.root / "runtime/reports/archive-latest.json").read_text())
        self.assertFalse(saved["imported"])


class ArchiveCleanupOptionTests(ArchiveCommandCase):
    """Leftover handling: project setting, per-run flags, the ask flow and organize."""

    def extract(self, *flags):
        return self.command("extract", "--fid", "archive1", "--parent-id", "source", "--apply", *flags)

    def journal(self):
        journal = JobJournal(self.root / "runtime/archive-jobs.sqlite3")
        self.addCleanup(journal.close)
        return journal

    def record_fetch(self):
        account = hashlib.sha256(b"quark:alist-storage:1").hexdigest()
        self.journal().put("archive-fetch:" + "c" * 64, {
            "kind": "archive_fetch", "status": "FETCHED", "archive_fid": "archive1",
            "bound": {"account_scope_hash": account, "library_root_id": "root"},
            "target": {"parent_id": "root", "name": "待处理", "folder_id": "source"}})

    def organize(self, *flags):
        account = hashlib.sha256(b"quark:alist-storage:1").hexdigest()
        plan_id = "d" * 32
        private_json(self.root / "runtime/archive-plans" / (plan_id + ".json"), {
            "record_id": plan_id, "library_root_id": "root", "archive_fid": "archive1",
            "report": {"library": {"library_root_id": "root", "account_scope_hash": account}}})
        self.storage.rows["isolated1"] = []
        with patch("media_helper.library_import.apply_import", return_value={
                "status": "COMPLETE", "writes_attempted": True, "writes_performed": True}):
            return self.command("organize", plan_id, *flags)

    def test_unset_setting_keeps_archive_and_explains_the_options(self):
        result = self.extract()
        self.assertEqual(result["cleanup"]["source"]["state"], "kept", result)
        self.assertEqual(result["cleanup"]["basis"], "default")
        self.assertIn("archive_cleanup", result["cleanup"]["hint"])
        self.assertEqual(self.storage.trashed, [])

    def test_recycle_setting_and_flags(self):
        self.config["archive_cleanup"] = "recycle"
        kept = self.extract("--keep-archive")
        self.assertEqual((kept["cleanup"]["source"]["state"], kept["cleanup"]["basis"]), ("kept", "flag"))
        self.assertEqual(self.storage.trashed, [])
        recycled = self.extract()
        self.assertEqual((recycled["cleanup"]["source"]["state"], recycled["cleanup"]["basis"]),
                         ("recycled", "config"))
        self.assertEqual(self.storage.trashed, ["archive1"])
        self.setUp()
        self.config["archive_cleanup"] = "keep"
        self.assertEqual(self.extract("--remove-archive")["cleanup"]["source"]["state"], "recycled")
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.extract("--remove-archive", "--keep-archive")

    def test_recycle_finds_the_fetch_staging_folder_from_the_journal(self):
        self.record_fetch()
        result = self.extract("--remove-archive")
        self.assertEqual(result["cleanup"]["staging"]["state"], "recycled", result)
        self.assertEqual(self.storage.trashed, ["archive1", "source"])

    def test_ask_setting_returns_a_cleanup_command_that_works_later(self):
        self.config["archive_cleanup"] = "ask"
        self.record_fetch()
        result = self.extract()
        self.assertEqual(result["cleanup"]["source"]["state"], "awaiting_choice", result)
        self.assertEqual(result["cleanup"]["choices"]["recycle"],
                         "python3 scripts/media.py archive cleanup --fid archive1 --apply")
        self.assertEqual(self.storage.trashed, [])
        planned = self.command("cleanup", "--fid", "archive1", "--apply", "--plan")
        self.assertEqual(planned["status"], "PLAN", planned)
        done = self.command("cleanup", "--fid", "archive1", "--apply")
        self.assertEqual(done["status"], "PARTIAL", done)
        self.assertEqual({key: value["state"] for key, value in done["cleanup"].items()},
                         {"source": "recycled", "staging": "recycled", "target": "kept"})
        again = self.extract()
        self.assertEqual(again["status"], "EXTRACTED", again)
        self.assertEqual(again["cleanup"]["source"]["state"], "recycled")
        self.assertEqual(self.storage.trashed, ["archive1", "source"])

    def test_organize_recycles_the_emptied_extraction_folder_when_set_to_recycle(self):
        self.config["archive_cleanup"] = "recycle"
        self.record_fetch()
        self.assertEqual(self.extract()["cleanup"]["staging"]["state"], "recycled")
        result = self.organize("--apply")
        self.assertEqual(result["status"], "COMPLETE", result)
        self.assertEqual(result["cleanup"]["status"], "CLEAN", result)
        self.assertEqual(self.storage.trashed, ["archive1", "source", "isolated1"])

    def test_organize_only_offers_cleanup_when_set_to_ask_and_skips_it_for_keep(self):
        self.extract()
        self.config["archive_cleanup"] = "ask"
        asked = self.organize("--apply")
        self.assertEqual(asked["cleanup"]["choices"]["recycle"],
                         "python3 scripts/media.py archive cleanup --fid archive1 --apply")
        self.assertEqual({key: value["state"] for key, value in asked["cleanup"]["cleanup"].items()},
                         {"source": "planned", "target": "planned"})
        self.config["archive_cleanup"] = "keep"
        self.assertNotIn("cleanup", self.organize("--apply"))
        self.assertEqual(self.storage.trashed, [])

    def test_invalid_setting_is_rejected_before_cloud_writes(self):
        self.config["archive_cleanup"] = True
        with self.assertRaises(ValueError):
            self.extract()
        self.assertEqual((self.storage.created, self.storage.submissions), ([], []))

    def test_invalid_setting_stops_fetch_extract_before_the_archive_is_saved(self):
        self.config["archive_cleanup"] = "delete"
        with patch("media_helper.tasks.TaskStore.load", return_value={"account_scope": "quark:alist-storage:1"}), \
                patch("media_helper.archive_flow.task_candidate") as candidate, \
                patch("media_helper.archive_flow.fetch") as fetch:
            with self.assertRaises(ValueError):
                self.command("fetch", "a" * 32, "--candidate-id", "b" * 64, "--apply", "--extract")
        candidate.assert_not_called()
        fetch.assert_not_called()

    def test_test_only_library_cannot_recycle(self):
        self.register(test=True)
        with self.assertRaises(ValueError):
            self.command("cleanup", "--fid", "archive1", "--library-id", "family", "--apply")
        self.assertEqual(self.storage.trashed, [])

    def test_fetch_rerun_accepts_staging_emptied_by_its_own_recycle(self):
        journal = self.journal()
        fetched = {"kind": "archive_fetch", "status": "FETCHED", "archive_fid": "archive1",
                   "target": {"folder_id": "source"}}
        self.assertFalse(_fetched_archive_recycled(self.storage, journal, fetched))
        journal.put(_job_key("archive", self.storage, "archive1"),
                    {"kind": "archive_extract", "status": "EXTRACTED", "staging_cleanup": {"state": "recycled"},
                     "bound": {"source": {"parent_id": "source"}}})
        self.assertTrue(_fetched_archive_recycled(self.storage, journal, fetched))
        self.assertFalse(_fetched_archive_recycled(self.storage, journal, {**fetched, "target": {"folder_id": "x"}}))


if __name__ == "__main__":
    unittest.main()
