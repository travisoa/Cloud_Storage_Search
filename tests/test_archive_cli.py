"""Archive CLI tests using mock providers and temporary private runtime files."""
import argparse
from contextlib import ExitStack, redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from media_helper.archive_cli import add_parser, run_command
from media_helper.journal import JobJournal
from media_helper.library_registry import LibraryRegistry, drive_reference
from media_helper.providers import UnknownOutcome
from tests.test_archives import ArchiveStorage, item


class ArchiveCommandTests(unittest.TestCase):
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
        self.stack.enter_context(patch("media_helper.headless_cli._storage", return_value=self.storage))
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


if __name__ == "__main__":
    unittest.main()
