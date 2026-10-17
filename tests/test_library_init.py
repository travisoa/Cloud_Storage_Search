"""Offline cloud mkdir tests; no real credentials or remote writes."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from media_helper.journal import JobJournal
from media_helper.library_init import DEFAULT_CATEGORIES, initialize
from media_helper.providers import AuthenticationError, IncompleteListingError, RiskControlError, UnknownOutcome


def folder(fid, name, parent="0", directory=True):
    return {"fid": fid, "name": name, "parent_id": parent, "is_dir": directory}


class Journal:
    def __init__(self):
        self.data, self.events = {}, []

    def get(self, key):
        return deepcopy(self.data.get(key))

    def put(self, key, row):
        self.events.append(deepcopy(row))
        self.data[key] = deepcopy(row)


class Storage:
    account_scope = "quark-account:one"

    def __init__(self, journal=None):
        self.rows = {"0": []}
        self.created, self.listed = [], []
        self.journal = journal
        self.failure = None
        self.hide_reads = 0
        self.omit_fid = False
        self._sleep = lambda delay: None

    def list_files(self, parent):
        self.listed.append(parent)
        rows = deepcopy(self.rows[parent])
        if self.hide_reads and self.created:
            self.hide_reads -= 1
            rows = [row for row in rows if row["fid"] != self.created[-1]["fid"]]
        return rows

    def mkdir(self, parent, name):
        if self.journal:
            operation = list(self.journal.events[-1]["operations"].values())[-1]
            assert operation["status"] == "SUBMITTING"
            assert (operation["parent_id"], operation["name"]) == (parent, name)
        row = folder("folder-" + str(len(self.created) + 1), name, parent)
        self.rows[parent].append(row)
        self.rows[row["fid"]] = []
        self.created.append(row)
        if self.failure:
            raise self.failure
        return {} if self.omit_fid else deepcopy(row)


class LibraryInitializationTests(unittest.TestCase):
    def setUp(self):
        self.journal = Journal()
        self.storage = Storage(self.journal)

    def run_init(self, **options):
        return initialize(self.storage, parent_id="0", journal=self.journal,
                          job_key="library-init:one", **options)

    def seed_root(self):
        self.storage.rows["0"].append(folder("library", "影库"))
        self.storage.rows["library"] = []

    def test_empty_new_cloud_plan_never_creates_or_persists(self):
        report = self.run_init()
        self.assertEqual(report["status"], "PLAN", report)
        self.assertIsNone(report["root_id"])
        self.assertEqual(len(report["operations"]), 6)
        self.assertEqual(report["categories"], {key: {"folder_id": None, "name": name}
                                                for key, name in DEFAULT_CATEGORIES.items()})
        self.assertFalse(report["writes_attempted"])
        self.assertFalse(report["writes_performed"])
        self.assertEqual(self.storage.listed, ["0"])
        self.assertEqual((self.storage.created, self.journal.events), ([], []))

    def test_apply_empty_root_zero_creates_only_root_and_categories(self):
        report = self.run_init(apply=True)
        self.assertEqual(report["status"], "COMPLETE", report)
        self.assertEqual(len(self.storage.created), 6)
        self.assertTrue(report["writes_attempted"])
        self.assertTrue(report["writes_performed"])
        self.assertEqual(report["root_id"], "folder-1")
        self.assertEqual(set(self.storage.listed), {"0", "folder-1"})
        self.assertTrue(all(row["status"] == "VERIFIED" for row in report["operations"]))
        for row in self.journal.events:
            self.assertNotIn("cookie", json.dumps(row).lower())

    def test_unique_existing_directories_are_reused_without_writes(self):
        self.seed_root()
        for key, name in DEFAULT_CATEGORIES.items():
            self.storage.rows["library"].append(folder(key, name, "library"))
            self.storage.rows[key] = [folder("untouched-" + key, "old-file.mkv", key, False)]
        report = self.run_init(apply=True)
        self.assertEqual(report["status"], "COMPLETE", report)
        self.assertFalse(report["writes_attempted"])
        self.assertEqual(self.storage.created, [])
        self.assertEqual(report["root_id"], "library")
        self.assertEqual(report["categories"]["tv"]["folder_id"], "tv")
        self.assertEqual(set(self.storage.listed), {"0", "library"})

    def test_root_name_occupied_by_file_blocks(self):
        self.storage.rows["0"] = [folder("file", "影库", directory=False)]
        report = self.run_init(apply=True)
        self.assertEqual(report["status"], "NEEDS_INPUT")
        self.assertIn("directory_name_is_occupied_by_file", report["reasons"])
        self.assertEqual(self.storage.created, [])

    def test_duplicate_root_directories_block(self):
        self.storage.rows["0"] = [folder("one", "影库"), folder("two", "影库")]
        self.assertEqual(self.run_init(apply=True)["status"], "NEEDS_INPUT")
        self.assertEqual(self.storage.created, [])

    def test_all_category_conflicts_preflight_before_first_category_write(self):
        self.seed_root()
        self.storage.rows["library"] = [folder("file", "综艺", "library", False)]
        self.assertEqual(self.run_init(apply=True)["status"], "NEEDS_INPUT")
        self.assertEqual(self.storage.created, [])

    def test_duplicate_category_directory_blocks(self):
        self.seed_root()
        self.storage.rows["library"] = [folder("one", "电影", "library"), folder("two", "电影", "library")]
        self.assertEqual(self.run_init(apply=True)["status"], "NEEDS_INPUT")
        self.assertEqual(self.storage.created, [])

    def test_completed_run_is_idempotent_and_reverifies_real_ids(self):
        first = self.run_init(apply=True)
        count = len(self.storage.created)
        second = self.run_init(apply=True)
        self.assertEqual(second["status"], "COMPLETE", second)
        self.assertEqual(first["categories"], second["categories"])
        self.assertEqual(len(self.storage.created), count)
        self.assertFalse(second["writes_attempted"])
        self.assertGreater(len(self.storage.listed), 12)

    def test_known_fid_is_not_replaced_using_a_same_named_directory(self):
        report = self.run_init(apply=True)
        root = report["root_id"]
        self.storage.rows["0"] = [folder("replacement", "影库")]
        self.storage.rows["replacement"] = []
        second = self.run_init(apply=True)
        self.assertEqual(second["status"], "NEEDS_INPUT", second)
        self.assertEqual(len(self.storage.created), 6)
        self.assertNotEqual(root, "replacement")

    def test_scope_parent_layout_and_account_changes_stop(self):
        self.run_init(apply=True)
        count = len(self.storage.created)
        for changes in ({"root_name": "另一影库"}, {"categories": {"tv": "剧集"}}):
            report = self.run_init(apply=True, **changes)
            self.assertEqual(report["status"], "NEEDS_INPUT")
            self.assertIn("initialization_scope_or_layout_changed", report["reasons"])
        other = initialize(self.storage, parent_id="elsewhere", journal=self.journal,
                           job_key="library-init:one", apply=True)
        self.assertEqual(other["status"], "NEEDS_INPUT")
        self.storage.account_scope = "quark-account:two"
        self.assertEqual(self.run_init(apply=True)["status"], "NEEDS_INPUT")
        self.assertEqual(len(self.storage.created), count)

    def test_stable_account_scope_required_even_for_root_zero(self):
        self.storage.account_scope = None
        self.assertEqual(self.run_init(apply=True)["status"], "NEEDS_INPUT")
        self.assertEqual(self.storage.listed, [])

    def test_eventual_visibility_bounded_reads_verify_before_children(self):
        self.storage.hide_reads = 2
        report = self.run_init(apply=True)
        self.assertEqual(report["status"], "COMPLETE", report)
        self.assertEqual(self.storage.listed[:3], ["0", "0", "0"])
        root_create_events = [row["operations"]["root"]["status"] for row in self.journal.events]
        self.assertEqual(root_create_events[:3], ["SUBMITTING", "CREATED", "VERIFIED"])

    def test_unobserved_returned_fid_is_persisted_then_recovers_readonly(self):
        self.storage.hide_reads = 99
        first = self.run_init(apply=True)
        self.assertEqual(first["status"], "UNKNOWN", first)
        self.assertTrue(first["writes_performed"])
        self.assertEqual(self.journal.get("library-init:one")["operations"]["root"]["folder_id"], "folder-1")
        self.storage.hide_reads = 0
        recovered = self.run_init(apply=True, reconcile=True)
        self.assertEqual(recovered["status"], "READY_TO_CONTINUE", recovered)
        self.assertFalse(recovered["writes_attempted"])
        self.assertEqual(len(self.storage.created), 1)
        final = self.run_init(apply=True)
        self.assertEqual(final["status"], "COMPLETE", final)
        self.assertEqual(len(self.storage.created), 6)

    def test_unknown_mkdir_submission_is_never_repeated_or_guessed_by_name(self):
        self.storage.failure = UnknownOutcome("cookie=private-response", step="mkdir")
        first = self.run_init(apply=True)
        self.assertEqual(first["status"], "UNKNOWN", first)
        self.storage.failure = None
        for options in ({"apply": True}, {"reconcile": True}, {}):
            report = self.run_init(**options)
            self.assertEqual(report["status"], "UNKNOWN", report)
            self.assertFalse(report["writes_attempted"])
            self.assertNotIn("private-response", json.dumps(report))
        self.assertEqual(len(self.storage.created), 1)

    def test_return_without_fid_remains_unknown_even_when_name_is_present(self):
        self.storage.omit_fid = True
        self.assertEqual(self.run_init(apply=True)["status"], "UNKNOWN")
        self.storage.omit_fid = False
        self.assertEqual(self.run_init(apply=True)["status"], "UNKNOWN")
        self.assertEqual(len(self.storage.created), 1)

    def test_authentication_and_risk_stop_immediately_without_leaking_messages(self):
        for exception, status in ((AuthenticationError("cookie=secret"), "NEEDS_AUTH"),
                                  (RiskControlError("token=secret"), "RISK_CONTROL")):
            self.setUp()
            self.storage.failure = exception
            report = self.run_init(apply=True)
            self.assertEqual(report["status"], status, report)
            self.assertEqual(len(self.storage.created), 1)
            self.assertNotIn("secret", json.dumps(report))

    def test_incomplete_or_malformed_listing_never_creates(self):
        samples = [None, {}, [{"fid": "one", "name": "影库", "is_dir": True}],
                   [folder("one", "影库", "wrong")],
                   [folder("one", "x"), folder("one", "y")]]
        for sample in samples:
            self.storage.list_files = lambda parent: deepcopy(sample)
            self.assertEqual(self.run_init(apply=True)["status"], "NEEDS_INPUT")
            self.assertEqual(self.storage.created, [])
        def incomplete(parent):
            raise IncompleteListingError("do not expose payload")
        self.storage.list_files = incomplete
        self.assertEqual(self.run_init(apply=True)["status"], "NEEDS_INPUT")
        self.assertEqual(self.storage.created, [])

    def test_readonly_reconcile_does_not_persist_planned_nodes_as_unknown_submissions(self):
        self.seed_root()
        self.storage.rows["library"] = [folder("tv", "电视剧", "library")]
        report = self.run_init(reconcile=True, apply=True)
        self.assertEqual(report["status"], "READY_TO_CONTINUE", report)
        self.assertFalse(report["writes_attempted"])
        durable = self.journal.get("library-init:one")
        self.assertNotIn("category:movie", durable["operations"])
        final = self.run_init(apply=True)
        self.assertEqual(final["status"], "COMPLETE", final)
        self.assertEqual(len(self.storage.created), 4)

    def test_partial_category_completion_resumes_with_known_fids_only(self):
        original = self.storage.mkdir
        def delayed(parent, name):
            row = original(parent, name)
            if name == "电视剧":
                self.storage.hide_reads = 99
            return row
        self.storage.mkdir = delayed
        report = self.run_init(apply=True)
        self.assertEqual(report["status"], "UNKNOWN", report)
        self.assertEqual(len(self.storage.created), 3)
        self.storage.hide_reads = 0
        recovered = self.run_init(reconcile=True)
        self.assertEqual(recovered["status"], "READY_TO_CONTINUE", recovered)
        self.assertEqual(len(self.storage.created), 3)
        final = self.run_init(apply=True)
        self.assertEqual(final["status"], "COMPLETE", final)
        self.assertEqual(len(self.storage.created), 6)

    def test_custom_layout_and_parent_are_supported_without_subtree_scan(self):
        self.storage.rows["selected"] = []
        report = initialize(self.storage, parent_id="selected", root_name="家庭影库",
                            categories={"movie": "Movies", "tv": "TV Shows"},
                            journal=self.journal, job_key="library-init:custom", apply=True)
        self.assertEqual(report["status"], "COMPLETE", report)
        self.assertEqual(len(self.storage.created), 3)
        self.assertEqual(set(self.storage.listed), {"selected", report["root_id"]})

    def test_unsafe_or_duplicate_layout_is_rejected_before_api(self):
        for options in ({"root_name": "../影库"}, {"categories": {}},
                        {"categories": {"movie": "Same", "tv": "same"}},
                        {"categories": {"tv": "剧\n集"}}, {"categories": {"TV": "电视剧"}}):
            self.assertEqual(self.run_init(apply=True, **options)["status"], "NEEDS_INPUT")
        self.assertEqual(self.storage.listed, [])

    def test_real_private_job_journal_retains_recoverable_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "jobs.sqlite3"
            journal = JobJournal(path)
            self.storage.journal = None
            try:
                report = initialize(self.storage, parent_id="0", journal=journal,
                                    job_key="library-init:private", apply=True)
                self.assertEqual(report["status"], "COMPLETE", report)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(len(journal.get("library-init:private")["operations"]), 6)
            finally:
                journal.close()


if __name__ == "__main__":
    unittest.main()
