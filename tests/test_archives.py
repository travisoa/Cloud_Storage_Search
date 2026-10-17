"""Safe staging behavior with mock cloud storage and durable evidence."""
from copy import deepcopy
import json
import unittest

from media_helper.archives import classify_archive, classify_archives, extract, inspect
from media_helper.providers import AuthenticationError, ProviderError, QuarkProvider, RiskControlError, UnknownOutcome
from tests.test_library_init import Journal
from tests.test_providers import FakeTransport


def item(fid, name, parent="source", size=123, directory=False):
    return {"fid": fid, "name": name, "parent_id": parent, "size": size, "is_dir": directory}


class ArchiveStorage:
    account_scope = "quark:account-one"
    capabilities = {"archive_extract": True}
    _sleep = staticmethod(lambda seconds: None)

    def __init__(self, journal=None):
        self.rows = {"root": [item("source", "待处理", "root", directory=True)],
                     "source": [item("archive1", "测试剧.zip")]}
        self.journal = journal
        self.created, self.submissions, self.queries = [], [], []
        self.hide_target = 0
        self.mkdir_failure = self.extract_failure = None
        self.skip_callback = False
        self.result = {"task_id": "task1", "status": "done", "extracted_fids": ["output1"]}

    def list_files(self, parent):
        rows = deepcopy(self.rows[parent])
        if self.hide_target and parent == "root" and self.created:
            self.hide_target -= 1
            rows = [row for row in rows if row["fid"] != self.created[-1]["fid"]]
        return rows

    def mkdir(self, parent, name):
        if self.journal:
            assert self.journal.events[-1]["status"] == "TARGET_SUBMITTING"
        row = item("isolated1", name, parent, directory=True)
        self.rows[parent].append(row)
        self.rows["isolated1"] = []
        self.created.append(row)
        if self.mkdir_failure:
            raise self.mkdir_failure
        return row

    def extract_archive(self, fid, target, *, on_submitted):
        if self.journal:
            assert self.journal.events[-1]["status"] == "SUBMITTING"
        self.submissions.append((fid, target))
        if not self.skip_callback:
            on_submitted({"source_fid": fid, "target_parent_id": target, "task_id": "task1"})
            if self.journal:
                assert self.journal.events[-1]["task_id"] == "task1"
        if self.extract_failure:
            raise self.extract_failure
        self.rows[target] = [item("output1", "测试剧.S01E01.mkv", target, size=500)]
        return deepcopy(self.result)

    def archive_task_status(self, task_id):
        self.queries.append(task_id)
        return deepcopy(self.result)


class ArchiveInspectionTests(unittest.TestCase):
    def test_filename_hints_cover_single_compound_and_multivolume_archives(self):
        for name, format_name in (("movie.ZIP", "zip"), ("movie.tar.gz", "tar.gz"), ("movie.tar.bz2", "tar.bz2"),
                                  ("movie.tar.xz", "tar.xz"), ("movie.rar", "rar"), ("movie.7z", "7z")):
            result = classify_archive(name)
            self.assertEqual(result["format"], format_name)
            self.assertFalse(result["multipart"])
            self.assertEqual(result["basis"], "filename_only")
        for name in ("show.part01.rar", "show.7z.001", "show.zip.001", "show.r00", "show.z01", "show.zx01"):
            self.assertTrue(classify_archive(name)["multipart"], name)
        self.assertFalse(classify_archive("movie.iso")["supported_format"])
        self.assertIsNone(classify_archive("movie.mkv"))

    def test_winzip_terminal_zip_and_z01_volumes_are_one_manual_group(self):
        for names in (("show.zip", "show.z01", "show.z02"), ("show.zipx", "show.zx01", "show.zx02")):
            groups = classify_archives([item(str(index), name) for index, name in enumerate(names)])
            self.assertEqual(len(groups), 1, groups)
            self.assertTrue(groups[0]["multipart"])
            self.assertEqual(groups[0]["status"], "NEEDS_INPUT")
            self.assertEqual(groups[0]["volume_check"]["missing_numbers"], [])
        missing_terminal = classify_archives([item("one", "show.z01")])[0]
        self.assertEqual(missing_terminal["volume_check"]["missing_numbers"], [0])

    def test_split_zip_terminal_member_is_not_submitted_as_standalone_zip(self):
        journal = Journal()
        storage = ArchiveStorage(journal)
        storage.rows["source"] = [item("archive1", "show.zip"), item("volume1", "show.z01")]
        report = extract(storage, fid="archive1", parent_id="source", library_root_id="root",
                         journal=journal, job_key="archive:" + "a" * 64, apply=True)
        self.assertEqual(report["status"], "NEEDS_INPUT", report)
        self.assertEqual((storage.created, storage.submissions), ([], []))

    def test_observed_number_sequence_does_not_claim_last_volume_or_complete(self):
        for names in (("show.part01.rar", "show.part02.rar"), ("show.7z.001", "show.7z.002"),
                      ("show.zip.001", "show.zip.002"), ("show.rar", "show.r00", "show.r01")):
            groups = classify_archives([item(str(index), name) for index, name in enumerate(names)])
            self.assertEqual(len(groups), 1, groups)
            self.assertEqual(groups[0]["status"], "NEEDS_INPUT")
            volume = groups[0]["volume_check"]
            self.assertFalse(volume["terminal_volume_known"])
            self.assertFalse(volume["complete"])
            self.assertTrue(volume["starts_at_expected_first"])
            self.assertEqual(volume["missing_numbers"], [])

    def test_missing_initial_middle_and_duplicate_volumes_are_reported(self):
        for names, missing in ((("show.7z.002",), [1]),
                               (("show.part01.rar", "show.part03.rar"), [2]),
                               (("show.r00",), [0])):
            result = classify_archives([item(str(index), name) for index, name in enumerate(names)])[0]
            self.assertEqual(result["volume_check"]["missing_numbers"], missing)
            self.assertIn("missing_archive_volume", result["reasons"])
        result = classify_archives([item("one", "show.part01.rar"), item("two", "show.part1.rar")])[0]
        self.assertTrue(result["volume_check"]["duplicate_numbers"])

    def test_inspect_is_readonly_and_refuses_outside_library_folder(self):
        storage = ArchiveStorage()
        result = inspect(storage, folder_id="source", library_root_id="root")
        self.assertEqual(result["status"], "INSPECTED")
        self.assertEqual(len(result["archives"]), 1)
        self.assertFalse(result["writes_attempted"])
        self.assertEqual((storage.created, storage.submissions), ([], []))
        with self.assertRaises(ValueError):
            inspect(storage, folder_id="outside", library_root_id="root")


class ArchiveExtractionTests(unittest.TestCase):
    def setUp(self):
        self.journal = Journal()
        self.storage = ArchiveStorage(self.journal)
        self.key = "archive:" + "a" * 64

    def run_archive(self, **options):
        return extract(self.storage, fid="archive1", parent_id="source", library_root_id="root",
                       journal=self.journal, job_key=self.key, **options)

    def test_default_plan_does_not_create_target_or_write_journal(self):
        report = self.run_archive()
        self.assertEqual(report["status"], "PLAN", report)
        self.assertIsNone(report["target"]["folder_id"])
        self.assertFalse(report["writes_attempted"])
        self.assertEqual((self.storage.created, self.storage.submissions, self.journal.events), ([], [], []))

    def test_preexisting_snapshot_covers_entire_authorized_library(self):
        self.storage.rows["root"].append(item("other", "另一剧集", "root", directory=True))
        self.storage.rows["other"] = [item("old-video", "old.mkv", "other", size=777)]
        self.assertEqual(self.run_archive(apply=True)["status"], "EXTRACTED")
        state = self.journal.get(self.key)
        self.assertTrue(state["before_inventory"]["complete"])
        self.assertTrue({"root", "source", "archive1", "other", "old-video"} <= set(state["before_fids"]))
        self.assertNotIn("isolated1", state["before_fids"])

    def test_incomplete_global_inventory_or_limits_block_before_any_cloud_write(self):
        for changes in ({"max_entries": 1}, {"max_depth": 0}):
            report = self.run_archive(apply=True, **changes)
            self.assertEqual(report["status"], "NEEDS_INPUT", report)
            self.assertEqual((self.storage.created, self.storage.submissions), ([], []))
        self.storage.rows["root"].append(item("unreadable", "无法读取", "root", directory=True))
        report = self.run_archive(apply=True)
        self.assertEqual(report["status"], "NEEDS_INPUT", report)
        self.assertEqual((self.storage.created, self.storage.submissions), ([], []))

    def test_nested_output_cannot_adopt_an_old_library_fid(self):
        self.storage.rows["source"].append(item("old-video", "old.mkv", "source", size=777))
        original = self.storage.extract_archive
        def moved_old_file(*args, **kwargs):
            original(*args, **kwargs)
            self.storage.rows["isolated1"] = [item("new-dir", "展开目录", "isolated1", directory=True)]
            self.storage.rows["new-dir"] = [item("old-video", "old.mkv", "new-dir", size=777)]
            self.storage.rows["source"] = [row for row in self.storage.rows["source"] if row["fid"] != "old-video"]
            return {"task_id": "task1", "status": "done", "extracted_fids": ["new-dir"]}
        self.storage.extract_archive = moved_old_file
        report = self.run_archive(apply=True)
        self.assertEqual(report["status"], "NEEDS_INPUT", report)
        self.assertIn("extracted_nested_identifier_is_not_new", report["reasons"])

    def test_valid_nested_output_requires_complete_recursive_identity_checks(self):
        original = self.storage.extract_archive
        def nested(*args, **kwargs):
            original(*args, **kwargs)
            self.storage.rows["isolated1"] = [item("new-dir", "展开目录", "isolated1", directory=True)]
            self.storage.rows["new-dir"] = [item("new-video", "new.mkv", "new-dir", size=777)]
            return {"task_id": "task1", "status": "done", "extracted_fids": ["new-dir"]}
        self.storage.extract_archive = nested
        report = self.run_archive(apply=True)
        self.assertEqual(report["status"], "EXTRACTED", report)
        self.assertEqual(report["verified_output_count"], 2)

    def test_extract_stages_new_files_preserves_source_and_does_not_claim_import(self):
        before = deepcopy(self.storage.rows["source"])
        report = self.run_archive(apply=True)
        self.assertEqual(report["status"], "EXTRACTED", report)
        self.assertTrue(report["writes_performed"])
        self.assertEqual(self.storage.rows["source"], before)
        self.assertEqual(self.storage.created[0]["parent_id"], "root")
        self.assertEqual(self.storage.submissions, [("archive1", "isolated1")])
        self.assertEqual([row["fid"] for row in report["new_files"]], ["output1"])
        self.assertFalse(report["imported"])
        self.assertTrue(report["source_preserved"])
        statuses = [row["status"] for row in self.journal.events]
        self.assertEqual(statuses[:3], ["TARGET_SUBMITTING", "TARGET_CREATED", "TARGET_READY"])
        self.assertIn("SUBMITTED", statuses)

    def test_second_apply_only_checks_known_task_and_real_new_files(self):
        self.assertEqual(self.run_archive(apply=True)["status"], "EXTRACTED")
        report = self.run_archive(apply=True)
        self.assertEqual(report["status"], "EXTRACTED", report)
        self.assertFalse(report["writes_attempted"])
        self.assertEqual(len(self.storage.created), 1)
        self.assertEqual(len(self.storage.submissions), 1)
        self.assertEqual(self.storage.queries, ["task1"])

    def test_reconcile_always_readonly_even_with_apply(self):
        report = self.run_archive(apply=True, reconcile=True)
        self.assertEqual(report["status"], "PLAN", report)
        self.assertEqual(self.storage.submissions, [])
        self.assertEqual(self.storage.created, [])

    def test_every_multipart_or_unsupported_archive_stays_pending_without_mkdir(self):
        for name in ("show.part01.rar", "show.7z.001", "show.zip.001", "show.r00", "show.iso"):
            self.storage.rows["source"][0]["name"] = name
            report = self.run_archive(apply=True)
            self.assertEqual(report["status"], "NEEDS_INPUT", report)
            self.assertEqual((self.storage.created, self.storage.submissions), ([], []))

    def test_existing_unowned_target_name_is_never_reused(self):
        report = self.run_archive()
        target = item("old-target", report["target"]["name"], "root", directory=True)
        self.storage.rows["root"].append(target)
        self.storage.rows["old-target"] = []
        result = self.run_archive(apply=True)
        self.assertEqual(result["status"], "NEEDS_INPUT", result)
        self.assertEqual(self.storage.created, [])

    def test_unknown_mkdir_never_uses_same_named_folder_or_repeats(self):
        self.storage.mkdir_failure = UnknownOutcome("password=secret")
        self.assertEqual(self.run_archive(apply=True)["status"], "UNKNOWN")
        self.storage.mkdir_failure = None
        for options in ({"apply": True}, {"reconcile": True}, {}):
            result = self.run_archive(**options)
            self.assertEqual(result["status"], "UNKNOWN", result)
            self.assertFalse(result["writes_attempted"])
            self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(len(self.storage.created), 1)
        self.assertEqual(self.storage.submissions, [])

    def test_known_created_target_recovers_after_visibility_delay_without_second_mkdir(self):
        original = self.storage.mkdir
        def delayed(parent, name):
            row = original(parent, name)
            self.storage.hide_target = 99
            return row
        self.storage.mkdir = delayed
        self.assertEqual(self.run_archive(apply=True)["status"], "UNKNOWN")
        self.storage.hide_target = 0
        report = self.run_archive(reconcile=True)
        self.assertEqual(report["status"], "READY_TO_EXTRACT", report)
        self.assertEqual(self.storage.submissions, [])
        self.assertEqual(self.run_archive(apply=True)["status"], "EXTRACTED")
        self.assertEqual(len(self.storage.created), 1)

    def test_unknown_submission_without_task_id_is_latched_and_never_resubmitted(self):
        self.storage.skip_callback = True
        self.storage.extract_failure = UnknownOutcome("cookie=secret")
        self.assertEqual(self.run_archive(apply=True)["status"], "UNKNOWN")
        self.storage.extract_failure = None
        report = self.run_archive(apply=True)
        self.assertEqual(report["status"], "UNKNOWN", report)
        self.assertEqual(len(self.storage.submissions), 1)
        self.assertFalse(report["writes_attempted"])

    def test_known_task_unknown_outcome_reconciles_get_only_and_verifies_fids(self):
        self.storage.extract_failure = UnknownOutcome("do not leak", task_id="task1")
        self.assertEqual(self.run_archive(apply=True)["status"], "UNKNOWN")
        self.storage.extract_failure = None
        self.storage.rows["isolated1"] = [item("output1", "真实视频.mkv", "isolated1", size=400)]
        report = self.run_archive(reconcile=True)
        self.assertEqual(report["status"], "EXTRACTED", report)
        self.assertFalse(report["writes_attempted"])
        self.assertEqual(self.storage.queries, ["task1"])
        self.assertEqual(len(self.storage.submissions), 1)

    def test_task_without_observed_fids_or_unexpected_target_files_is_not_success(self):
        self.storage.result["extracted_fids"] = ["not-present"]
        self.assertEqual(self.run_archive(apply=True)["status"], "UNKNOWN")
        self.storage.result["extracted_fids"] = ["output1"]
        self.storage.rows["isolated1"].append(item("unrelated", "external.mkv", "isolated1", size=7))
        report = self.run_archive(reconcile=True)
        self.assertEqual(report["status"], "NEEDS_INPUT", report)

    def test_source_name_size_parent_and_account_are_bound_before_resume(self):
        self.run_archive(apply=True)
        original = deepcopy(self.storage.rows["source"][0])
        for change in ({"name": "改名.zip"}, {"size": 999}):
            self.storage.rows["source"][0] = {**original, **change}
            self.assertEqual(self.run_archive(apply=True)["status"], "NEEDS_INPUT")
        self.storage.rows["source"][0] = original
        self.storage.account_scope = "another-account"
        self.assertEqual(self.run_archive(apply=True)["status"], "NEEDS_INPUT")
        self.assertEqual(len(self.storage.submissions), 1)

    def test_target_must_stay_exact_new_directory(self):
        self.run_archive(apply=True)
        self.storage.rows["root"][-1]["name"] = "renamed-target"
        report = self.run_archive(reconcile=True)
        self.assertEqual(report["status"], "NEEDS_INPUT", report)

    def test_changed_completed_task_identifiers_are_not_adopted(self):
        self.run_archive(apply=True)
        self.storage.result["extracted_fids"] = ["replacement"]
        self.storage.rows["isolated1"] = [item("replacement", "other.mkv", "isolated1", size=300)]
        report = self.run_archive(reconcile=True)
        self.assertEqual(report["status"], "NEEDS_INPUT", report)
        self.assertIn("completed_archive_task_identifiers_changed", report["reasons"])

    def test_original_archive_is_reverified_after_task_completion(self):
        original = self.storage.extract_archive
        def removed_source(*args, **kwargs):
            result = original(*args, **kwargs)
            self.storage.rows["source"] = []
            return result
        self.storage.extract_archive = removed_source
        report = self.run_archive(apply=True)
        self.assertEqual(report["status"], "NEEDS_INPUT", report)
        self.assertFalse(report["source_preserved"])
        self.assertFalse(report["imported"])

    def test_authentication_risk_and_membership_stop_without_retry(self):
        for exception, status in ((AuthenticationError("cookie=secret"), "NEEDS_AUTH"),
                                  (RiskControlError("password=secret"), "RISK_CONTROL"),
                                  (ProviderError("private", code=31001), "NEEDS_INPUT")):
            self.setUp()
            self.storage.extract_failure = exception
            result = self.run_archive(apply=True)
            self.assertEqual(result["status"], status, result)
            self.assertEqual(len(self.storage.submissions), 1)
            self.assertNotIn("secret", json.dumps(result))

    def test_real_provider_membership_rejection_maps_to_pending_and_does_not_retry(self):
        transport = FakeTransport({"_http_status": 200, "status": 400, "code": 31001,
                                   "message": "SVIP required private-server-text"})
        provider = QuarkProvider(cookie="fake=private", transport=transport, sleep=lambda seconds: None)
        self.storage.extract_archive = provider.extract_archive
        result = self.run_archive(apply=True)
        self.assertEqual(result["status"], "NEEDS_INPUT", result)
        self.assertIn("membership_required", result["reasons"])
        self.assertEqual(self.run_archive(apply=True)["status"], "NEEDS_INPUT")
        self.assertEqual([row["method"] for row in transport.calls], ["POST"])
        self.assertNotIn("private-server-text", json.dumps(result))

    def test_real_http_auth_is_not_relabelled_as_archive_membership(self):
        for http_status in (401, 403):
            self.setUp()
            transport = FakeTransport({"_http_status": http_status, "code": 31001, "message": "SVIP required"})
            provider = QuarkProvider(cookie="fake=private", transport=transport)
            self.storage.extract_archive = provider.extract_archive
            result = self.run_archive(apply=True)
            self.assertEqual(result["status"], "NEEDS_AUTH", result)
            self.assertEqual([row["method"] for row in transport.calls], ["POST"])

    def test_real_provider_processing_without_task_is_unknown_and_never_reposted(self):
        transport = FakeTransport({"_http_status": 200, "code": 23008, "message": "doloading"})
        provider = QuarkProvider(cookie="fake=private", transport=transport)
        self.storage.extract_archive = provider.extract_archive
        first = self.run_archive(apply=True)
        self.assertEqual(first["status"], "UNKNOWN", first)
        for options in ({"apply": True}, {"reconcile": True}):
            again = self.run_archive(**options)
            self.assertEqual(again["status"], "UNKNOWN", again)
            self.assertFalse(again["writes_attempted"])
        self.assertEqual([row["method"] for row in transport.calls], ["POST"])

    def test_password_failure_is_pending_and_never_collects_or_saves_password(self):
        self.storage.result = {"task_id": "task1", "status": "failed", "extracted_fids": [], "reason": "password_required"}
        report = self.run_archive(apply=True)
        self.assertEqual(report["status"], "NEEDS_INPUT", report)
        self.assertIn("password_required", report["reasons"])
        self.assertFalse(report["imported"])
        self.assertEqual(len(self.storage.submissions), 1)


if __name__ == "__main__":
    unittest.main()
