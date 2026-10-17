"""A process restart between save submission and polling must not resubmit."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from media_helper.journal import JobJournal
from media_helper.providers import AuthenticationError, RiskControlError
from media_helper.workflow import run
from test_workflow import Metadata, Search, Storage, request


class SubmittedStorage(Storage):
    capabilities = {"save_submission_callback": True}

    def __init__(self):
        super().__init__()
        self.after_submission = None
        self.submissions = 0
        self.polls = 0

    def save_files(self, share, files, target, *, on_submitted=None):
        self.submissions += 1
        on_submitted({"task_id": "task-1", "source_fids": [row["fid"] for row in files],
                      "target_parent_id": target})
        result = super().save_files(share, files, target)
        if self.after_submission:
            self.after_submission()
        return result

    def task_status(self, task_id):
        self.polls += 1
        return super().task_status(task_id)


class SubmissionJournalTests(unittest.TestCase):
    def test_restart_after_submit_uses_durable_id_and_never_resends_save(self):
        storage = SubmittedStorage()
        observed = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.sqlite"
            store = JobJournal(path)

            def interrupt():
                records = store.connection.execute("SELECT record FROM operations").fetchall()
                import json
                record = json.loads(records[0][0])
                observed.append(deepcopy(record))
                raise SystemExit("simulated process exit before first poll")

            storage.after_submission = interrupt
            with self.assertRaises(SystemExit):
                run(request(), storage, Metadata(), Search(), store, apply=True)
            self.assertEqual(observed[0]["operations"][0]["task_id"], "task-1")
            self.assertEqual(observed[0]["operations"][0]["state"], "save_pending")
            self.assertEqual(storage.renamed, [])
            store.close()

            store = JobJournal(path)
            storage.after_submission = None
            report = run(request(), storage, Metadata(), Search(), store, apply=True)
            self.assertEqual(report["status"], "COMPLETE", report)
            self.assertEqual(storage.submissions, 1)
            self.assertEqual(storage.polls, 1)
            self.assertTrue(report["operations"][0]["verified_files"][0]["verified"])
            store.close()

    def test_auth_or_risk_after_submit_keeps_id_and_stops(self):
        for error, status in ((AuthenticationError, "NEEDS_AUTH"), (RiskControlError, "RISK_CONTROL")):
            with self.subTest(error=error.__name__), tempfile.TemporaryDirectory() as directory:
                storage = SubmittedStorage()
                store = JobJournal(Path(directory) / "jobs.sqlite")

                def stop():
                    raise error("remote secret must not appear")

                storage.after_submission = stop
                report = run(request(), storage, Metadata(), Search(), store, apply=True)
                self.assertEqual(report["status"], status, report)
                self.assertEqual(report["operations"][0]["task_id"], "task-1")
                self.assertEqual(storage.renamed, [])
                self.assertEqual(storage.submissions, 1)
                self.assertNotIn("remote secret", str(report))
                store.close()


if __name__ == "__main__":
    unittest.main()
