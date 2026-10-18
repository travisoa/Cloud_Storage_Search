"""Formal headless entry points for connected-drive library creation/import."""
import argparse
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from media_helper.headless_cli import main
from media_helper.library_cli import _load_record, _save_record, add_parser, run_command
from media_helper.library_registry import LibraryRegistry, drive_reference
from tests.test_library_init import Storage, folder


class LibraryCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = {"provider": "quark", "library_root_id": "existing-library",
                       "credential_source": {"type": "alist", "storage_id": 1},
                       "categories": {"tv": {"folder_id": "old-tv", "name": "电视剧📺"}},
                       "metadata_source": "auto", "timezone": "Asia/Shanghai",
                       "overwrite": False, "delete": False, "open_browser": False}
        (self.root / "config").mkdir()
        self.config_path = self.root / "config/headless.json"
        self.config_path.write_text(json.dumps(self.config, ensure_ascii=False), encoding="utf-8")
        self.config_bytes = self.config_path.read_bytes()
        self.registry = LibraryRegistry(self.root / "runtime/libraries.json")
        self.registry.connect(drive_reference("new-drive", 2))
        self.storage = Storage()
        self.storage.rows["existing-library"] = [dict(folder("old-file", "已有资源.mkv", "existing-library", False), size=123)]
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        def storage(drive):
            self.storage.account_scope = drive["account_scope"]
            return self.storage
        self.storage_mock = self.stack.enter_context(patch("media_helper.library_cli._storage", side_effect=storage))
        self.stack.enter_context(patch("media_helper.headless_cli.ROOT", self.root))
        self.stack.enter_context(patch("media_helper.headless_cli._config", return_value=self.config))
        parser = argparse.ArgumentParser()
        add_parser(parser.add_subparsers(dest="command", required=True))
        self.parser = parser

    def command(self, arguments):
        return run_command(self.parser.parse_args(["library", *arguments]), self.config, self.root)

    def initialize(self, **options):
        arguments = ["init", "--drive-id", options.get("drive_id", "new-drive"),
                     "--library-id", options.get("library_id", "family"),
                     "--parent-id", options.get("parent_id", "0"), "--apply"]
        if options.get("test"):
            arguments.append("--test")
        return self.command(arguments)

    def snapshot(self, library_id="family", **changes):
        scope = self.registry.get_drive("new-drive", self.config)["account_scope"]
        report = {"status": "COMPLETE", "complete": True, "source_id": "0", "excluded_ids": [],
                  "entries": [], "account_scope_hash": hashlib.sha256(scope.encode()).hexdigest(), **changes}
        return _save_record(self.root, "scans", library_id, report)

    def saved_plan(self, library_id="family", **library_changes):
        record = self.registry.get_library(library_id)
        scope = self.registry.get_drive(record["drive_id"], self.config)["account_scope"]
        selected = {**record, "account_scope_hash": hashlib.sha256(scope.encode()).hexdigest(), **library_changes}
        return _save_record(self.root, "plans", library_id,
                            {"status": "READY", "library": selected, "scan": {"source_id": "0"}, "operations": []})

    def test_current_requires_test_flag_before_storage_or_initialization(self):
        with patch("media_helper.library_init.initialize") as initialize:
            with self.assertRaises(ValueError):
                self.command(["init", "--library-id", "trial", "--parent-id", "existing-library", "--apply"])
            initialize.assert_not_called()
        self.storage_mock.assert_not_called()
        self.assertEqual(self.storage.created, [])

    def test_current_test_parent_must_be_inside_existing_authorized_root(self):
        with patch("media_helper.library_init.initialize") as initialize:
            with self.assertRaises(ValueError):
                self.command(["init", "--library-id", "trial", "--parent-id", "0", "--test", "--apply"])
            initialize.assert_not_called()
        self.assertEqual(self.storage.created, [])
        self.assertEqual(self.registry.read()["libraries"], {})

    def test_current_test_can_create_under_existing_root_without_touching_old_files(self):
        before = deepcopy(self.storage.rows["existing-library"])
        result = self.initialize(drive_id="current", parent_id="existing-library", library_id="trial", test=True)
        self.assertEqual(result["status"], "COMPLETE", result)
        self.assertEqual(len(self.storage.created), 6)
        self.assertTrue(self.registry.get_library("trial")["test_only"])
        self.assertEqual(self.storage.rows["existing-library"][0], before[0])
        self.assertEqual(self.config_path.read_bytes(), self.config_bytes)
        with self.assertRaises(ValueError):
            self.registry.config_for("trial", self.config)

    def test_init_plan_dominates_apply_and_does_not_register_incomplete_structure(self):
        result = self.command(["init", "--drive-id", "new-drive", "--library-id", "family",
                               "--parent-id", "0", "--apply", "--plan"])
        self.assertEqual(result["status"], "PLAN", result)
        self.assertFalse(result["writes_attempted"])
        self.assertEqual(self.storage.created, [])
        self.assertEqual(self.registry.read()["libraries"], {})
        self.assertEqual(self.config_path.read_bytes(), self.config_bytes)

    def test_init_complete_registers_once_without_scanning_or_changing_default_config(self):
        with patch("media_helper.library_import.scan", side_effect=AssertionError("init must not scan")) as scan:
            result = self.initialize()
            scan.assert_not_called()
        self.assertEqual(result["status"], "COMPLETE", result)
        self.assertEqual(len(self.storage.created), 6)
        library = self.registry.get_library("family")
        self.assertEqual(library["library_root_id"], result["root_id"])
        self.assertEqual(set(library["categories"]), {"movie", "tv", "animation", "documentary", "variety"})
        self.assertFalse(library["test_only"])
        self.assertFalse(result["browser_opened"])
        self.assertEqual(self.config_path.read_bytes(), self.config_bytes)
        second = self.initialize()
        self.assertEqual(second["status"], "COMPLETE", second)
        self.assertEqual(len(self.storage.created), 6)

    def test_unknown_or_failed_initialization_never_registers_library(self):
        for status in ("UNKNOWN", "NEEDS_AUTH", "RISK_CONTROL", "NEEDS_INPUT"):
            with patch("media_helper.library_init.initialize", return_value={
                    "status": status, "root_id": None, "categories": {}, "operations": [],
                    "writes_attempted": False, "writes_performed": False}):
                result = self.initialize()
                self.assertEqual(result["status"], status)
                self.assertEqual(self.registry.read()["libraries"], {})

    def test_init_reconcile_is_readonly_even_with_apply(self):
        result = self.command(["init", "--drive-id", "new-drive", "--library-id", "family",
                               "--parent-id", "0", "--apply", "--reconcile"])
        self.assertEqual(result["status"], "READY_TO_CONTINUE", result)
        self.assertFalse(result["writes_attempted"])
        self.assertEqual(self.storage.created, [])

    def test_scan_excludes_initialized_target_tree_and_binds_account_and_library(self):
        initial = self.initialize()
        root_id = initial["root_id"]
        self.storage.rows["0"].append(dict(folder("loose", "散落电影 (2025).mkv", "0", False), size=321))
        self.storage.rows[root_id].append(dict(folder("never-scan", "已入库.mkv", root_id, False), size=111))
        self.storage.listed.clear()
        result = self.command(["scan", "family", "--source-id", "0"])
        self.assertEqual(result["status"], "COMPLETE", result)
        self.assertEqual([row["fid"] for row in result["entries"]], ["loose"])
        self.assertEqual(result["excluded_ids"], [root_id])
        self.assertNotIn(root_id, self.storage.listed)
        self.assertFalse(result["writes_attempted"])
        snapshot = _load_record(self.root, "scans", result["scan_id"], "family")
        self.assertEqual(snapshot["account_scope_hash"], hashlib.sha256(self.storage.account_scope.encode()).hexdigest())
        path = self.root / "runtime/library-scans" / (result["scan_id"] + ".json")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError):
            _load_record(self.root, "scans", result["scan_id"], "other-library")

    def test_scan_source_cannot_be_target_library_itself(self):
        result = self.initialize()
        with self.assertRaises(ValueError):
            self.command(["scan", "family", "--source-id", result["root_id"]])

    def test_scan_saves_real_source_directory_chain_for_tv_root(self):
        self.initialize()
        self.storage.rows['0'].append(folder('source-tv', '电视剧', '0'))
        self.storage.rows['source-tv'] = [folder('source-show', '示例 (2026)', 'source-tv')]
        self.storage.rows['source-show'] = []
        self.storage.listed.clear()
        result = self.command(['scan', 'family', '--source-id', 'source-show'])
        snapshot = _load_record(self.root, 'scans', result['scan_id'], 'family')
        self.assertEqual(snapshot['source_context']['ancestors'], ['电视剧', '示例 (2026)'])
        self.assertNotIn(self.registry.get_library('family')['library_root_id'], self.storage.listed)

    def test_scan_binds_configured_category_kind_even_with_a_custom_name(self):
        self.initialize(drive_id='current', parent_id='existing-library', library_id='trial', test=True)
        self.storage.rows['existing-library'].append(folder('old-tv', '自定义分类', 'existing-library'))
        self.storage.rows['old-tv'] = []
        result = self.command(['scan', 'trial', '--source-id', 'old-tv'])
        self.assertEqual(result['source_context'], {'ancestors': ['自定义分类'], 'kind': 'tv'})

    def test_legacy_scan_gets_live_root_context_before_building_an_import_plan(self):
        from media_helper.library_identity import MediaResolver
        from media_helper.library_import import _digest, _scan_payload
        from tests.test_library_identity import Metadata
        self.initialize()
        self.storage.rows['0'].append(folder('source-tv', '电视剧', '0'))
        self.storage.rows['source-tv'] = [dict(folder('feature', '示例电影.2025.mkv', 'source-tv', False), size=123)]
        result = self.command(['scan', 'family', '--source-id', 'source-tv'])
        legacy = _load_record(self.root, 'scans', result['scan_id'], 'family')
        legacy.pop('source_context')
        legacy['snapshot_hash'] = _digest(_scan_payload(legacy))
        scan_id = _save_record(self.root, 'scans', 'family', legacy)
        with patch('media_helper.library_cli._media_resolver', return_value=MediaResolver(Metadata())):
            planned = self.command(['plan', 'family', scan_id])
        self.assertEqual(planned['operations'], [])
        self.assertEqual(planned['review'][0]['reason'], 'explicit_season_and_episode_required')
        self.assertEqual(planned['scan']['source_context'], {'ancestors': ['电视剧']})
        self.assertEqual(planned['scan']['snapshot_hash'], _digest(_scan_payload(planned['scan'])))

    def test_scan_for_current_test_cannot_escape_original_library(self):
        self.initialize(drive_id="current", parent_id="existing-library", library_id="trial", test=True)
        with self.assertRaises(ValueError):
            self.command(["scan", "trial", "--source-id", "0"])

    def test_plan_rejects_saved_scan_from_different_account(self):
        self.initialize()
        scan_id = self.snapshot(account_scope_hash="b" * 64)
        with patch("media_helper.library_import.plan_import") as plan:
            with self.assertRaises(ValueError):
                self.command(["plan", "family", scan_id])
            plan.assert_not_called()

    def test_plan_record_cannot_be_reused_for_other_library(self):
        self.initialize()
        scan_id = self.snapshot(library_id="different-library")
        with patch("media_helper.library_import.plan_import") as plan:
            with self.assertRaises(ValueError):
                self.command(["plan", "family", scan_id])
            plan.assert_not_called()

    def test_plan_passes_exact_account_bound_library_and_writes_only_private_plan(self):
        self.initialize()
        scan_id = self.snapshot()
        with patch("media_helper.library_cli._media_resolver", return_value="resolver"), \
             patch("media_helper.library_import.plan_import", return_value={"status": "READY", "operations": []}) as plan:
            result = self.command(["plan", "family", scan_id])
        self.assertEqual(result["status"], "READY", result)
        snapshot, selected, resolver = plan.call_args.args
        self.assertEqual(snapshot["account_scope_hash"], selected["account_scope_hash"])
        self.assertEqual(selected["library_root_id"], self.registry.get_library("family")["library_root_id"])
        self.assertEqual(resolver, "resolver")
        path = self.root / "runtime/library-plans" / (result["plan_id"] + ".json")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_organize_plan_dominates_apply(self):
        self.initialize()
        plan_id = self.saved_plan()
        with patch("media_helper.library_import.apply_import", return_value={"status": "READY", "writes_performed": False}) as apply:
            result = self.command(["organize", "family", plan_id, "--apply", "--plan"])
        self.assertEqual(result["status"], "READY")
        self.assertFalse(apply.call_args.kwargs["apply"])

    def test_organize_rejects_test_library_apply_but_allows_preview(self):
        self.initialize(test=True)
        plan_id = self.saved_plan()
        with patch("media_helper.library_import.apply_import", return_value={"status": "READY"}) as apply:
            with self.assertRaises(ValueError):
                self.command(["organize", "family", plan_id, "--apply"])
            apply.assert_not_called()
            result = self.command(["organize", "family", plan_id, "--apply", "--plan"])
            self.assertEqual(result["status"], "READY")
            self.assertFalse(apply.call_args.kwargs["apply"])

    def test_organize_rejects_different_account_or_library_root_before_import(self):
        self.initialize()
        for changes in ({"account_scope_hash": "b" * 64}, {"library_root_id": "another-root"}):
            plan_id = self.saved_plan(**changes)
            with patch("media_helper.library_import.apply_import") as apply:
                with self.assertRaises(ValueError):
                    self.command(["organize", "family", plan_id, "--apply"])
                apply.assert_not_called()

    def test_live_library_root_change_blocks_scan_before_import(self):
        result = self.initialize()
        self.storage.rows["0"][0]["name"] = "被外部改名"
        with patch("media_helper.library_import.scan") as scan:
            with self.assertRaises(ValueError):
                self.command(["scan", "family", "--source-id", "0"])
            scan.assert_not_called()
        self.assertEqual(self.registry.get_library("family")["library_root_id"], result["root_id"])

    def test_record_ids_and_private_permissions_are_required(self):
        self.initialize()
        for record_id in ("../outside", "A" * 32, "short"):
            with self.assertRaises(ValueError):
                _load_record(self.root, "scans", record_id, "family")
        scan_id = self.snapshot()
        path = self.root / "runtime/library-scans" / (scan_id + ".json")
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            _load_record(self.root, "scans", scan_id, "family")

    def test_formal_media_cli_runs_library_plan_without_cloud_write_or_browser(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["library", "init", "--drive-id", "new-drive", "--library-id", "family",
                         "--parent-id", "0", "--apply", "--plan"])
        result = json.loads(output.getvalue())
        self.assertEqual((code, result["status"]), (0, "PLAN"), result)
        self.assertFalse(result["browser_opened"])
        self.assertEqual(self.storage.created, [])


if __name__ == "__main__":
    unittest.main()
