"""Independent boundary cases found while reviewing archive staging."""
import unittest

from media_helper.archives import extract
from media_helper.providers import QuarkProvider, UnknownOutcome
from tests.test_archives import ArchiveStorage, item
from tests.test_library_init import Journal
from tests.test_providers import FakeTransport, ok


class ArchiveReviewTests(unittest.TestCase):
    def extract(self, storage, journal):
        return extract(storage, fid='archive1', parent_id='source', library_root_id='root',
                       journal=journal, job_key='archive:' + 'a' * 64, apply=True)

    def test_nested_output_parent_must_be_verified_before_extracted_claim(self):
        journal = Journal()
        storage = ArchiveStorage(journal)
        original = storage.extract_archive

        def nested_output(fid, target, *, on_submitted):
            original(fid, target, on_submitted=on_submitted)
            storage.rows[target] = [item('output-dir', '解压目录', target, directory=True)]
            storage.rows['output-dir'] = [item('nested-video', '视频.mkv', 'outside-library', size=500)]
            return {'task_id': 'task1', 'status': 'done', 'extracted_fids': ['output-dir']}

        storage.extract_archive = nested_output
        report = self.extract(storage, journal)
        self.assertNotEqual(report['status'], 'EXTRACTED', report)
        self.assertEqual(report['new_files'], [])
        self.assertEqual(len(storage.submissions), 1)

    def test_explicit_mismatched_task_identifier_cannot_be_relabelled(self):
        transport = FakeTransport(ok({'task_id': 'different-task', 'status': 2,
                                      'unarchive_result': {'list': [{'fid': 'output'}]}}))
        provider = QuarkProvider(cookie='fake=never-print', transport=transport)
        with self.assertRaises(UnknownOutcome):
            provider.archive_task_status('task1')
        self.assertEqual([call['method'] for call in transport.calls], ['GET'])


    def test_source_identity_is_rechecked_after_final_target_observation_before_post(self):
        journal = Journal()
        storage = ArchiveStorage(journal)
        original = storage.list_files
        changed = []

        def observe(parent):
            rows = original(parent)
            ready = [event for event in journal.events if event['status'] == 'TARGET_READY']
            if parent == 'isolated1' and ready and not changed:
                storage.rows['source'][0]['size'] += 1
                changed.append(True)
            return rows

        storage.list_files = observe
        report = self.extract(storage, journal)
        self.assertTrue(changed)
        self.assertNotEqual(report['status'], 'EXTRACTED', report)
        self.assertEqual(storage.submissions, [], 'a changed source must be re-evaluated before any POST')



if __name__ == '__main__':
    unittest.main()
