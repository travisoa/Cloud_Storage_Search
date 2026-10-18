"""Adopting a root-scoped journal record only after the current connection reads its saved fids."""
import unittest
from unittest.mock import patch

from media_helper import headless_cli
from media_helper.providers import RiskControlError
from media_helper.scope_adoption import adopt
from media_helper.workflow import run
from test_workflow import Journal, Metadata, Search, Storage, request

LEGACY = "quark-root:root"
CURRENT = "quark:alist-storage:1"


class ScopeAdoptionTests(unittest.TestCase):
    def setUp(self):
        self.storage, self.journal = Storage(), Journal()
        self.storage.account_scope = LEGACY
        saved = run(request(), self.storage, Metadata(), Search(), self.journal, apply=True)
        self.assertEqual(saved["status"], "COMPLETE", saved)
        self.storage.account_scope = CURRENT
        self.storage.legacy_account_scopes = (LEGACY,)
        blocked = run(request(), self.storage, Metadata(), Search(), self.journal)
        self.assertEqual(blocked["reasons"], ["legacy_account_scope_requires_confirmation"])
        self.legacy_key = blocked["legacy_job_key"]
        self.assertEqual(blocked["next_step"], "python3 scripts/media.py adopt-scope " + self.legacy_key)

    def adopt(self, apply=False):
        return adopt(self.storage, self.journal, self.legacy_key, apply=apply)

    def saved_row(self):
        return next(row for row in self.storage.folders["season"] if row["fid"] == "new-source-2")

    def test_check_is_read_only_and_apply_copies_the_record_under_the_current_scope(self):
        events = len(self.journal.events)
        checked = self.adopt()
        self.assertEqual(checked["status"], "READY", checked)
        self.assertEqual([row["saved_fid"] for row in checked["matched_files"]], ["new-source-2", "new-subtitle-2"])
        self.assertEqual(len(self.journal.events), events)
        original = self.journal.get(self.legacy_key)
        adopted = self.adopt(apply=True)
        self.assertEqual(adopted["status"], "ADOPTED", adopted)
        self.assertFalse(adopted["writes_attempted"])
        record = self.journal.get(adopted["job_key"])
        self.assertEqual(record["identity"]["account_scope"], CURRENT)
        self.assertEqual(record["adopted_from"]["job_key"], self.legacy_key)
        self.assertEqual(self.journal.get(self.legacy_key), original)
        resumed = run(request(), self.storage, Metadata(), Search(), self.journal)
        self.assertEqual(resumed["status"], "UP_TO_DATE", resumed)
        self.assertEqual(resumed["job_key"], adopted["job_key"])
        self.assertEqual(self.adopt(apply=True)["reasons"], ["current_scope_record_already_exists"])

    def test_renamed_file_is_still_account_evidence(self):
        self.saved_row()["name"] = "tidied.mkv"
        checked = self.adopt()
        self.assertEqual(checked["status"], "READY", checked)
        self.assertFalse(checked["matched_files"][0]["name_matches"])

    def test_size_conflict_or_no_observed_file_keeps_the_block(self):
        self.saved_row()["size"] += 1
        self.assertEqual(self.adopt(apply=True)["reasons"], ["legacy_file_identity_conflict"])
        self.storage.folders["season"] = [row for row in self.storage.folders["season"]
                                          if not row["fid"].startswith("new-")]
        self.assertEqual(self.adopt(apply=True)["reasons"], ["legacy_files_not_observed_in_current_connection"])
        self.assertIsNone(self.journal.get(self.adopt()["job_key"]))

    def test_unfinished_unowned_or_unknown_records_are_refused(self):
        self.assertEqual(adopt(self.storage, self.journal, "season:missing")["reasons"], ["legacy_record_not_found"])
        self.storage.legacy_account_scopes = ()
        self.assertEqual(self.adopt()["reasons"], ["legacy_scope_not_owned_by_current_connection"])
        self.storage.legacy_account_scopes = (LEGACY,)
        self.journal.records[self.legacy_key]["state"] = "unknown"
        self.assertEqual(self.adopt(apply=True)["reasons"], ["legacy_record_unfinished_requires_manual_reconciliation"])

    def test_risk_control_stops_without_journal_write(self):
        events = len(self.journal.events)
        with patch.object(self.storage, "list_files", side_effect=RiskControlError("captcha", step="list_files")):
            report = self.adopt(apply=True)
        self.assertEqual(report["status"], "RISK_CONTROL", report)
        self.assertEqual(len(self.journal.events), events)

    def test_cli_checks_without_apply_by_default(self):
        with patch.object(headless_cli, "_config", return_value={"library_root_id": "root"}), \
                patch.object(headless_cli, "_adopt_scope", return_value={"status": "READY"}) as command, \
                patch("builtins.print"):
            self.assertEqual(headless_cli.main(["adopt-scope", self.legacy_key]), 0)
        command.assert_called_once_with(self.legacy_key, {"library_root_id": "root"}, apply=False)


if __name__ == "__main__":
    unittest.main()
