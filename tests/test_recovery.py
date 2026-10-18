"""Read-only recovery tests use only fake cloud providers and an in-memory journal."""
from copy import deepcopy
import unittest

from media_helper import workflow
from media_helper.providers import AuthenticationError, RiskControlError
from media_helper.recovery import reconcile


def file(fid, name, parent, size=123, directory=False):
    return {"fid": fid, "name": name, "parent_id": parent,
            "size": size, "is_dir": directory}


def request(**changes):
    return {"title": "测试剧", "year": 2026, "season": 1, "series_id": "17",
            "metadata_source": "tmdb", "library_root_id": "root",
            "library_root_is_series_category": True, **changes}


class Metadata:
    def __init__(self):
        self.calls = []

    def get_series(self, identifier):
        self.calls.append(identifier)
        return {"id": identifier, "title": "测试剧", "year": 2026, "source": "tmdb"}


class Journal:
    def __init__(self):
        self.records, self.events = {}, []

    def get(self, key):
        return deepcopy(self.records.get(key))

    def put(self, key, record):
        self.records[key] = deepcopy(record)
        self.events.append(deepcopy(record))


class Storage:
    provider = "quark"
    account_scope = "test-account"

    def __init__(self):
        self.folders = {
            "root": [file("series", "测试剧 (2026)", "root", directory=True)],
            "series": [file("season", "Season 01", "series", directory=True)],
            "season": [file("old1", "测试剧 (2026) S01E01.mkv", "season"),
                       file("new2", "source.S01E02.2160p.mkv", "season")],
        }
        self.reads, self.tasks, self.mutations = [], [], []
        self.task = {"status": "done", "task_id": "task1", "saved_fids": ["new2"]}
        self.list_error = self.task_error = None

    def list_files(self, parent):
        self.reads.append(parent)
        if self.list_error is not None:
            raise self.list_error
        return deepcopy(self.folders[parent])

    def task_status(self, task_id):
        self.tasks.append(task_id)
        if self.task_error is not None:
            raise self.task_error
        return deepcopy(self.task)

    def save_files(self, *args, **kwargs):
        self.mutations.append("save")
        raise AssertionError("read-only recovery must not save")

    def rename(self, *args, **kwargs):
        self.mutations.append("rename")
        raise AssertionError("read-only recovery must not rename")

    def mkdir(self, *args, **kwargs):
        self.mutations.append("mkdir")
        raise AssertionError("read-only recovery must not mkdir")


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.storage, self.metadata, self.journal = Storage(), Metadata(), Journal()
        self.request = request()
        self.identity = workflow._job_identity(self.request, self.storage)
        self.key = "season:" + workflow._digest(self.identity)

    def record(self, state="unknown", **changes):
        operation = {"state": state, "target_parent_id": "season", "before_fids": ["old1"],
                     "task_id": "task1", "files": [
                         {"source_fid": "source2", "name": "source.S01E02.2160p.mkv", "size": 123,
                          "target_name": "测试剧 (2026) S01E02.2160p.mkv", "episode_key": "S01E02"}]}
        record = {"schema_version": 2, "state": state, "identity": self.identity,
                  "configuration": workflow._execution_config(self.request),
                  "target_folder_id": "season", "operations": [operation], **changes}
        self.journal.records[self.key] = deepcopy(record)
        return record

    def run_recovery(self, **changes):
        report = reconcile(request(**changes), self.storage, self.metadata, self.journal)
        self.assertEqual(self.storage.mutations, [])
        self.assertFalse(report["writes_attempted"])
        self.assertFalse(report["writes_performed"])
        self.assertFalse(report["browser_opened"])
        return report

    def test_unknown_save_task_recovers_new_fid_without_rename(self):
        self.record()
        report = self.run_recovery()
        self.assertEqual(report["status"], "READY_TO_RENAME", report)
        record = self.journal.get(self.key)
        self.assertEqual(record["state"], "saved")
        self.assertEqual(record["operations"][0]["state"], "saved")
        self.assertEqual(record["operations"][0]["saved_fids"], ["new2"])
        self.assertEqual(record["operations"][0]["verified_files"][0]["saved_fid"], "new2")
        self.assertEqual(self.storage.tasks, ["task1"])
        self.assertEqual(self.storage.folders["season"][1]["name"], "source.S01E02.2160p.mkv")

    def test_new_fid_already_has_target_name_completes_local_record(self):
        record = self.record()
        self.storage.folders["season"][1]["name"] = record["operations"][0]["files"][0]["target_name"]
        report = self.run_recovery()
        self.assertEqual(report["status"], "COMPLETE", report)
        self.assertEqual(self.journal.get(self.key)["state"], "complete")
        self.assertTrue(self.journal.get(self.key)["operations"][0]["verified_files"][0]["verified"])

    def test_completed_record_is_reverified_and_cannot_hide_missing_or_source_named_file(self):
        for scenario in ("source_name", "missing", "wrong_size"):
            with self.subTest(scenario=scenario):
                self.setUp()
                record = self.record("complete")
                record["operations"][0]["saved_fids"] = ["new2"]
                self.journal.records[self.key] = record
                if scenario == "missing":
                    self.storage.folders["season"].pop()
                elif scenario == "wrong_size":
                    self.storage.folders["season"][1]["size"] = 124
                report = self.run_recovery()
                self.assertEqual(report["status"], "READY_TO_RENAME" if scenario == "source_name" else "UNKNOWN", report)
                self.assertEqual(self.storage.tasks, [])

    def test_unknown_save_without_task_id_is_not_resubmitted_or_claimed_by_name(self):
        record = self.record()
        record["operations"][0].pop("task_id")
        self.journal.records[self.key] = record
        report = self.run_recovery()
        self.assertEqual(report["status"], "UNKNOWN", report)
        self.assertEqual(self.storage.tasks, [])
        self.assertEqual(self.journal.events, [])

    def test_pending_remote_task_is_read_only_and_not_marked_saved(self):
        self.record()
        self.storage.task = {"status": "pending", "task_id": "task1", "saved_fids": []}
        report = self.run_recovery()
        self.assertEqual(report["status"], "PENDING", report)
        self.assertEqual(self.journal.get(self.key)["state"], "save_pending")
        self.assertEqual(self.storage.tasks, ["task1"])

    def test_old_fid_or_unproven_target_scope_cannot_be_adopted(self):
        for conflict in ("old_fid", "out_of_scope"):
            with self.subTest(conflict=conflict):
                self.setUp()
                record = self.record()
                if conflict == "old_fid":
                    self.storage.task["saved_fids"] = ["old1"]
                else:
                    record["operations"][0]["target_parent_id"] = "outside-root"
                self.journal.records[self.key] = record
                report = self.run_recovery()
                self.assertEqual(report["status"], "UNKNOWN" if conflict == "old_fid" else "NEEDS_INPUT", report)
                self.assertNotIn("outside-root", self.storage.reads)
                self.assertEqual(self.storage.tasks, ["task1"] if conflict == "old_fid" else [])

    def test_target_name_collision_prevents_ready_to_rename(self):
        record = self.record()
        target_name = record["operations"][0]["files"][0]["target_name"]
        self.storage.folders["season"].append(file("concurrent", target_name, "season"))
        report = self.run_recovery()
        self.assertEqual(report["status"], "NEEDS_INPUT", report)
        self.assertIn("target_filename_conflict_requires_confirmation", report["reasons"])

    def test_mkdir_with_returned_fid_is_verified_by_identity_not_name(self):
        self.storage.folders["series"] = []
        self.record("mkdir_pending", operations=[], directory={
            "parent_id": "root", "name": "测试剧 (2026)",
            "returned": {"fid": "series", "parent_id": "root", "name": "测试剧 (2026)", "is_dir": True}})
        report = self.run_recovery()
        self.assertEqual(report["status"], "READY_TO_CONTINUE", report)
        record = self.journal.get(self.key)
        self.assertEqual(record["state"], "ready")
        self.assertEqual(record["target_folder_id"], "series")
        self.assertEqual(record["directory_verifications"][0]["fid"], "series")

    def test_mkdir_without_returned_fid_does_not_adopt_existing_same_name(self):
        self.record("mkdir_pending", operations=[], directory={"parent_id": "root", "name": "测试剧 (2026)"})
        report = self.run_recovery()
        self.assertEqual(report["status"], "UNKNOWN", report)
        self.assertEqual(self.journal.get(self.key)["state"], "mkdir_pending")
        self.assertEqual(self.journal.events, [])

    def test_mkdir_returned_fid_on_unrelated_show_path_is_rejected(self):
        self.storage.folders["root"].append(file("other-show", "其他剧 (2026)", "root", directory=True))
        self.record("mkdir_pending", operations=[], directory={
            "parent_id": "root", "name": "其他剧 (2026)",
            "returned": {"fid": "other-show", "parent_id": "root", "name": "其他剧 (2026)", "is_dir": True}})
        report = self.run_recovery()
        self.assertEqual(report["status"], "NEEDS_INPUT", report)
        self.assertEqual(self.journal.events, [])

    def test_authentication_or_risk_control_stops_on_first_failure(self):
        for error, status in ((AuthenticationError, "NEEDS_AUTH"), (RiskControlError, "RISK_CONTROL")):
            for point in ("list", "task"):
                with self.subTest(error=error, point=point):
                    self.setUp()
                    self.record()
                    setattr(self.storage, point + "_error", error("secret diagnostic must be hidden"))
                    report = self.run_recovery()
                    self.assertEqual(report["status"], status, report)
                    if point == "list":
                        self.assertEqual(self.storage.reads, ["root"])
                        self.assertEqual(self.storage.tasks, [])
                    else:
                        self.assertEqual(self.storage.tasks, ["task1"])
                    self.assertNotIn("secret", str(report))
                    self.assertEqual(self.journal.events, [])

    def test_identity_configuration_and_metadata_conflicts_stop_before_task_reads(self):
        for conflict in ("identity", "configuration", "metadata"):
            with self.subTest(conflict=conflict):
                self.setUp()
                record = self.record()
                if conflict == "identity":
                    record["identity"]["account_scope"] = "different-account"
                elif conflict == "configuration":
                    record["configuration"]["title"] = "另一剧"
                else:
                    self.metadata.get_series = lambda _: {"id": "18", "title": "测试剧", "year": 2026}
                self.journal.records[self.key] = record
                report = self.run_recovery()
                self.assertEqual(report["status"], "NEEDS_INPUT", report)
                self.assertEqual((self.storage.reads, self.storage.tasks, self.journal.events), ([], [], []))

    def test_no_record_or_journal_needs_no_cloud_or_metadata_reads(self):
        report = self.run_recovery()
        self.assertEqual(report["status"], "NO_RECORD")
        report = reconcile(self.request, self.storage, self.metadata, None)
        self.assertEqual(report["status"], "NO_RECORD")
        self.assertEqual((self.storage.reads, self.storage.tasks, self.metadata.calls), ([], [], []))

    def test_request_validation_and_missing_size_stop_without_task_reads(self):
        self.record()
        report = self.run_recovery(season=False)
        self.assertEqual(report["status"], "NEEDS_INPUT", report)
        self.assertEqual((self.storage.reads, self.storage.tasks), ([], []))
        record = self.journal.records[self.key]
        record["operations"][0]["files"][0]["size"] = None
        report = self.run_recovery()
        self.assertEqual(report["status"], "NEEDS_INPUT", report)
        self.assertEqual(self.storage.tasks, [])

    def test_changed_account_scope_cannot_bypass_legacy_execution_record(self):
        record = self.record()
        old_identity = dict(self.identity, account_scope="quark-root:root")
        old_key = "season:" + workflow._digest(old_identity)
        self.journal.records = {old_key: {**record, "identity": old_identity}}
        self.storage.account_scope = "quark:alist-storage:family"
        self.storage.legacy_account_scopes = ("quark-root:root",)
        report = self.run_recovery()
        self.assertEqual(report["status"], "NEEDS_INPUT", report)
        self.assertEqual(report["reasons"], ["legacy_account_scope_requires_confirmation"])
        self.assertEqual(report["legacy_job_key"], old_key)
        self.assertEqual((self.storage.reads, self.storage.tasks, self.metadata.calls), ([], [], []))

    def test_current_account_record_is_used_even_if_a_legacy_record_exists(self):
        self.storage.account_scope = "quark:alist-storage:family"
        self.storage.legacy_account_scopes = ("quark-root:root",)
        self.identity = workflow._job_identity(self.request, self.storage)
        self.key = "season:" + workflow._digest(self.identity)
        self.record()
        old_identity = dict(self.identity, account_scope="quark-root:root")
        self.journal.records["season:" + workflow._digest(old_identity)] = {"identity": old_identity, "state": "unknown"}
        report = self.run_recovery()
        self.assertEqual(report["status"], "READY_TO_RENAME", report)


if __name__ == "__main__":
    unittest.main()
