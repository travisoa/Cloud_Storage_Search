from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from media_helper.headless_cli import main
from media_helper.journal import run_lock
from media_helper.tasks import TaskStore
from tests.test_workflow import Metadata, Search, Storage, file


class ProfileMetadata(Metadata):
    def get_series(self, identifier):
        return {"id": int(identifier), "name": "Doctor X: Mafia in White", "year": 2026,
                "source": "tvmaze", "seasons": [{"season": 1}], "aliases": []}


class VersionsStorage(Storage):
    def __init__(self):
        super().__init__()
        self.source_size = 400

    def share_files(self, url, password=None, parent_id="0"):
        if parent_id == "0":
            files = [file("share-4k", "测试剧 4K", directory=True), file("share-hd", "测试剧 1080p", directory=True)]
        else:
            files = [file("source-" + parent_id, "02.mkv", size=self.source_size if parent_id == "share-4k" else 100)]
        return {"share_ref": {"provider": "quark", "share_id": "share-1", "parent_id": parent_id, "stoken": "do-not-persist-this"},
                "files": [dict(deepcopy(row), share_fid_token="do-not-persist-this-either") for row in files]}


class FormalTaskCommandsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = {"library_root_id": "root", "metadata_source": "tvmaze", "pansou_url": "http://127.0.0.1:8888"}
        self.storage, self.metadata = VersionsStorage(), ProfileMetadata()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("media_helper.headless_cli.ROOT", self.root))
        self.stack.enter_context(patch("media_helper.headless_cli._config", return_value=self.config))
        self.stack.enter_context(patch("media_helper.headless_cli._metadata", return_value=(self.metadata, "tvmaze")))
        self.stack.enter_context(patch("media_helper.headless_cli._quark_ready", return_value=True))
        self.stack.enter_context(patch("media_helper.headless_cli._storage", return_value=self.storage))
        self.search_patch = self.stack.enter_context(patch("media_helper.search.PanSouSearch", side_effect=lambda *_: Search()))

    def command(self, arguments):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(arguments)
        return code, json.loads(out.getvalue())

    def bind(self):
        code, result = self.command(["bind-series", "--title", "测试剧", "--canonical-title", "Doctor X: Mafia in White",
                                     "--alias", "测试别名", "--series-id", "17", "--year", "2026", "--season", "1",
                                     "--metadata-source", "tvmaze", "--evidence-url", "https://www.tvmaze.com/shows/17/example"])
        self.assertEqual(code, 0, result)
        return result

    def initial_task(self, *, readonly=True):
        self.bind()
        arguments = ["request", "帮我查下《测试别名》存到我的网盘"]
        if readonly:
            arguments.append("--plan")
        code, result = self.command(arguments)
        self.assertEqual((code, result["status"]), (3, "NEEDS_INPUT"), result)
        self.assertEqual(len(result["source_candidates"]), 2)
        self.assertEqual((self.storage.saved, self.storage.renamed, self.storage.created), ([], [], []))
        return result["task_id"]

    def test_formal_bind_request_confirm_apply_runs_without_private_script(self):
        task_id = self.initial_task()
        code, result = self.command(["confirm", task_id, "--quality", "2160p", "--numeric-episodes", "--apply", "--remember-quality"])
        self.assertEqual((code, result["status"]), (0, "COMPLETE"), result)
        self.assertEqual(result["task_id"], task_id)
        self.assertEqual(self.storage.saved, [["source-share-4k"]])
        self.assertEqual(self.storage.renamed, [("new-source-share-4k", "测试剧 (2026)S01E02_2160P.mkv")])
        self.assertEqual(result["quality_preference_scope"], "tvmaze:17:2026")
        task = TaskStore(self.root / "runtime/tasks", "root").load(task_id)
        self.assertEqual(task["decisions"][0]["candidates"][0]["name"], "02.mkv")
        self.assertTrue(task["decisions"][0]["apply_authorized"])
        self.assertEqual(task["resolved_request"]["identity_profile"]["canonical_title"], "Doctor X: Mafia in White")
        self.assertNotIn("do-not-persist", json.dumps(task))

    def test_named_file_credentials_reach_request_confirm_resume_and_saved_naming(self):
        from media_helper.storage_connection import storage, ready
        self.config["credential_source"] = {"type": "file", "name": "drives/family.json"}
        with patch.dict("os.environ", {"QUARK_COOKIE": "wrong-default-cookie"}), \
                patch("media_helper.headless_cli._storage", side_effect=storage), \
                patch("media_helper.headless_cli._quark_ready", side_effect=ready), \
                patch("media_helper.credentials.exists", side_effect=lambda name: name == "drives/family.json"), \
                patch("media_helper.credentials.read_secret", return_value={"cookie": "selected-cookie"}) as read, \
                patch("media_helper.providers.QuarkProvider", return_value=self.storage) as provider:
            task_id = self.initial_task()
            code, planned = self.command(["confirm", task_id, "--quality", "2160p", "--numeric-episodes"])
            self.assertEqual((code, planned["status"]), (0, "READY"), planned)
            code, saved = self.command(["resume", task_id, "--apply"])
            self.assertEqual((code, saved["status"]), (0, "COMPLETE"), saved)
            code, named = self.command(["normalize-saved", task_id, "--apply"])
            self.assertEqual((code, named["status"]), (0, "UP_TO_DATE"), named)
        self.assertEqual({call.args for call in read.call_args_list}, {("drives/family.json",)})
        self.assertTrue(all(call.kwargs == {"cookie": "selected-cookie"} for call in provider.call_args_list))
        self.assertEqual(TaskStore(self.root / "runtime/tasks", "root").load(task_id)["account_scope"],
                         "quark:file:family")

    def test_confirm_defaults_to_plan_and_can_later_apply_same_numeric_decision(self):
        task_id = self.initial_task(readonly=False)
        code, report = self.command(["confirm", task_id, "--quality", "4K", "--numeric-episodes"])
        self.assertEqual((code, report["status"]), (0, "READY"), report)
        self.assertEqual((self.storage.saved, self.storage.renamed), ([], []))
        code, report = self.command(["confirm", task_id, "--quality", "2160p", "--apply"])
        self.assertEqual((code, report["status"]), (0, "COMPLETE"), report)
        self.assertEqual(self.storage.saved, [["source-share-4k"]])

    def test_changed_source_after_confirmation_stops_before_writes(self):
        task_id = self.initial_task()
        self.storage.source_size = 999
        code, report = self.command(["confirm", task_id, "--quality", "2160p", "--numeric-episodes", "--apply"])
        self.assertEqual((code, report["status"]), (3, "NEEDS_INPUT"), report)
        self.assertIn("source_episode_mapping_requires_reconfirmation", report["reasons"])
        self.assertEqual((self.storage.saved, self.storage.renamed), ([], []))

    def test_status_is_local_and_reconcile_cannot_search_or_write(self):
        task_id = self.initial_task()
        self.search_patch.reset_mock()
        with patch("media_helper.headless_cli._storage", side_effect=AssertionError("status cannot access cloud")):
            code, result = self.command(["status", task_id])
        self.assertEqual((code, result["status"]), (0, "TASK_STATUS"))
        with patch("media_helper.workflow.reconcile", return_value={"status": "RECONCILED", "writes_performed": False}) as reconcile:
            code, result = self.command(["reconcile", task_id])
        self.assertEqual(code, 0, result)
        reconcile.assert_called_once()
        self.search_patch.assert_not_called()
        self.assertEqual((self.storage.saved, self.storage.renamed), ([], []))

    def test_resume_has_explicit_apply_and_retains_prior_authorization_fact(self):
        task_id = self.initial_task(readonly=False)
        store = TaskStore(self.root / "runtime/tasks", "root")
        task = store.load(task_id)
        self.assertTrue(task["apply_authorized"])
        with patch("media_helper.workflow.run", return_value={"status": "UNKNOWN", "writes_performed": False}) as run:
            code, result = self.command(["resume", task_id])
            self.assertFalse(run.call_args.kwargs["apply"])
        self.assertEqual(code, 3, result)
        self.assertTrue(store.load(task_id)["apply_authorized"])
        self.assertFalse(store.load(task_id)["last_execution_apply"])
        with patch("media_helper.workflow.run", return_value={"status": "COMPLETE", "writes_performed": False}) as run:
            code, result = self.command(["resume", task_id, "--apply"])
            self.assertTrue(run.call_args.kwargs["apply"])
        self.assertEqual(code, 0, result)
        self.assertTrue(store.load(task_id)["last_execution_apply"])

    def test_changed_connection_blocks_task_commands_without_destroying_evidence(self):
        task_id = self.initial_task()
        path = self.root / "runtime/tasks" / (task_id + ".json")
        original = path.read_bytes()
        self.config["credential_source"] = {"type": "alist", "storage_id": 2}
        for command in (["resume", task_id, "--apply"], ["confirm", task_id, "--quality", "2160p", "--apply"],
                        ["normalize-saved", task_id], ["replan", task_id]):
            with self.subTest(command=command), \
                    patch("media_helper.headless_cli._storage", side_effect=AssertionError("wrong scope")):
                code, report = self.command(command)
            self.assertEqual((code, report["status"]), (3, "NEEDS_INPUT"), report)
            self.assertEqual(report["reason"], "task_account_scope_changed")
            self.assertEqual(path.read_bytes(), original)
        self.config.pop("credential_source")
        code, report = self.command(["confirm", task_id, "--quality", "2160p", "--numeric-episodes", "--apply"])
        self.assertEqual((code, report["status"]), (0, "COMPLETE"), report)

    def test_unbound_legacy_task_requires_read_only_replan_before_new_authorization(self):
        task_id = self.initial_task()
        store = TaskStore(self.root / "runtime/tasks", "root")
        task = store.load(task_id)
        task.pop("account_scope")
        store.save(task)
        old_task = store.load(task_id)
        code, report = self.command(["resume", task_id, "--apply"])
        self.assertEqual((code, report["reason"]), (3, "legacy_task_account_scope_unbound"))
        self.assertEqual(store.load(task_id), old_task)
        code, planned = self.command(["replan", task_id])
        self.assertEqual((code, planned["status"]), (3, "NEEDS_INPUT"), planned)
        self.assertNotEqual(planned["task_id"], task_id)
        fresh = store.load(planned["task_id"])
        self.assertEqual(fresh["supersedes_task_id"], task_id)
        self.assertIn("account_scope", fresh)
        self.assertFalse(fresh["apply_authorized"])
        self.assertEqual((self.storage.saved, self.storage.renamed, self.storage.created), ([], [], []))

    def test_scope_block_preserves_a_completed_transfer_receipt(self):
        task_id = self.initial_task()
        code, complete = self.command(["confirm", task_id, "--quality", "2160p", "--numeric-episodes", "--apply"])
        self.assertEqual((code, complete["status"]), (0, "COMPLETE"), complete)
        path = self.root / "runtime/tasks" / (task_id + ".json")
        original = path.read_bytes()
        self.config["credential_source"] = {"type": "alist", "storage_id": 2}
        code, blocked = self.command(["normalize-saved", task_id])
        self.assertEqual((code, blocked["reason"]), (3, "task_account_scope_changed"))
        self.assertEqual(path.read_bytes(), original)
        self.config.pop("credential_source")
        code, named = self.command(["normalize-saved", task_id])
        self.assertEqual((code, named["status"]), (0, "UP_TO_DATE"), named)

    def test_replan_refreshes_dates_and_resets_candidate_scope_without_cloud_writes(self):
        from datetime import datetime
        task_id = self.initial_task()
        code, _ = self.command(["confirm", task_id, "--quality", "2160p", "--numeric-episodes"])
        self.assertEqual(code, 0)
        store = TaskStore(self.root / "runtime/tasks", "root")
        old = store.load(task_id)
        old["resolved_request"]["as_of"] = "2026-10-16T20:00:00+08:00"
        store.save(old)
        self.metadata.episodes.append({"season": 1, "episode": 3, "air_date": "2026-10-18",
                                       "source": "tvmaze", "date_precision": "date"})
        with patch("media_helper.headless_cli.datetime") as clock:
            clock.now.return_value = datetime.fromisoformat("2026-10-18T20:00:00+08:00")
            code, planned = self.command(["replan", task_id])
        self.assertEqual((code, planned["status"]), (3, "NEEDS_INPUT"), planned)
        fresh = store.load(planned["task_id"])
        self.assertEqual(fresh["resolved_request"]["as_of"], "2026-10-18T20:00:00+08:00")
        self.assertNotIn("source_selection", fresh["resolved_request"])
        self.assertNotIn("source_episode_mapping", fresh["resolved_request"])
        self.assertEqual(fresh["decisions"], [])
        self.assertIn("S01E03", [item.get("episode_key") for item in planned["missing"]])
        self.assertEqual(store.load(task_id), old)
        self.assertEqual((self.storage.saved, self.storage.renamed, self.storage.created), ([], [], []))

    def test_identity_mismatch_and_metadata_failure_still_have_task_ids(self):
        self.bind()
        with patch.object(self.metadata, "get_series", return_value={"id": 17, "name": "Unexpected Show", "year": 2026}):
            code, result = self.command(["request", "只查《测试剧》", "--plan"])
        self.assertEqual(code, 1, result)
        self.assertRegex(result["task_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(TaskStore(self.root / "runtime/tasks", "root").load(result["task_id"])["status"], "FAILED")

    def test_binding_and_task_operations_share_the_account_lock(self):
        with run_lock(self.root / "runtime/workflow.lock"):
            code, result = self.command(["request", "只查《测试剧》", "--plan"])
        self.assertEqual(code, 1)
        self.assertIn("另一个影库任务", result["reason"])
        self.assertFalse((self.root / "runtime/tasks").exists())

    def test_legacy_bindings_still_work_when_remote_title_is_confirmed(self):
        runtime = self.root / "runtime"
        runtime.mkdir()
        (runtime / "series-bindings.json").write_text(json.dumps({"测试剧": {"series_id": "17", "year": 2026, "season": 1, "metadata_source": "tvmaze"}}))
        with patch.object(self.metadata, "get_series", return_value={"id": 17, "title": "测试剧", "year": 2026, "season_count": 1}):
            code, result = self.command(["request", "只查《测试剧》", "--plan"])
        self.assertEqual((code, result["status"]), (3, "NEEDS_INPUT"), result)
        self.assertIn("task_id", result)
        self.assertEqual((self.storage.saved, self.storage.renamed), ([], []))

    def test_saved_naming_cli_keeps_receipt_across_plan_and_apply_without_retransfer(self):
        task_id = self.initial_task()
        code, _ = self.command(["confirm", task_id, "--quality", "2160p", "--numeric-episodes", "--apply"])
        self.assertEqual(code, 0)
        self.storage.renamed.clear()
        before_saves = deepcopy(self.storage.saved)
        code, plan = self.command(["normalize-saved", task_id])
        self.assertEqual((code, plan["status"]), (0, "UP_TO_DATE"), plan)
        self.assertFalse(plan["writes_attempted"])
        code, result = self.command(["normalize-saved", task_id, "--apply"])
        self.assertEqual((code, result["status"]), (0, "UP_TO_DATE"), result)
        self.assertEqual(self.storage.saved, before_saves)
        self.assertEqual(self.storage.renamed, [])
        task = TaskStore(self.root / "runtime/tasks", "root").load(task_id)
        self.assertEqual(task["transfer_receipt"]["operations"][0]["state"], "complete")
        code, reconciled = self.command(["reconcile", task_id])
        self.assertEqual((code, reconciled["status"]), (0, "COMPLETE"), reconciled)
        self.assertEqual(self.storage.renamed, [])

    def test_saved_naming_cli_rejects_forged_receipt_without_mutation(self):
        task_id = self.initial_task()
        self.command(["confirm", task_id, "--quality", "2160p", "--numeric-episodes", "--apply"])
        self.storage.renamed.clear()
        store = TaskStore(self.root / "runtime/tasks", "root")
        task = store.load(task_id)
        task["transfer_receipt"] = deepcopy(task["report"])
        task["transfer_receipt"]["operations"][0]["saved_fids"] = ["old-1"]
        store.save(task)
        before = deepcopy(self.storage.folders)
        code, result = self.command(["normalize-saved", task_id, "--apply"])
        self.assertEqual((code, result["status"]), (3, "NEEDS_INPUT"), result)
        self.assertEqual(self.storage.folders, before)
        self.assertEqual(self.storage.renamed, [])
