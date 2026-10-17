"""Search archives remain visible hints and never become automatic media."""
import json
import unittest
from unittest.mock import Mock, patch

from media_helper.workflow import _digest, run
from tests.test_workflow import Journal, Metadata, Search, Storage, file, request


class WorkflowArchiveCandidatesTests(unittest.TestCase):
    def setUp(self):
        self.storage, self.metadata, self.search, self.journal = Storage(), Metadata(), Search(), Journal()
        self.storage.extract_archive = Mock(side_effect=AssertionError('workflow must never extract automatically'))

    def run_workflow(self, **changes):
        with patch('media_helper.archives.extract', side_effect=AssertionError('no implicit archive staging')) as extract:
            report = run(request(**changes), self.storage, self.metadata, self.search, self.journal, apply=True)
            extract.assert_not_called()
        self.storage.extract_archive.assert_not_called()
        return report

    def assert_archive_not_video(self, report, action):
        self.assertEqual(len(report['archive_candidates']), 1, report)
        archive = report['archive_candidates'][0]
        self.assertEqual(archive['fid'], 'archive-2')
        self.assertEqual(archive['policy_action'], action)
        self.assertEqual(archive['handling'], 'explicit_selection_required')
        self.assertEqual(archive['classification']['basis'], 'filename_only')
        self.assertFalse(archive['automatically_saved'])
        self.assertNotIn('archive-2', [row['fid'] for row in report['source_candidates']])
        self.assertNotIn('archive-2', [row['source']['fid'] for row in report.get('plan', [])])
        self.assertNotIn('do-not-persist', json.dumps(report))

    def test_default_skip_records_archive_hint_without_creating_video_or_writes(self):
        self.storage.shared = [file('archive-2', '测试剧.S01E02.2160p.zip')]
        report = self.run_workflow()
        self.assert_archive_not_video(report, 'skip')
        self.assertEqual(report['status'], 'NEEDS_INPUT', report)
        self.assertEqual(report['source_candidates'], [])
        self.assertEqual((self.storage.saved, self.storage.renamed, self.storage.created), ([], [], []))
        self.assertFalse(report['writes_attempted'])

    def test_per_series_manual_policy_remains_explicit_without_transfer_or_extract(self):
        self.storage.shared = [file('archive-2', '测试剧.S01E02.2160p.part01.rar')]
        report = self.run_workflow(version_policy={'archives': 'manual'})
        self.assert_archive_not_video(report, 'manual')
        self.assertTrue(report['archive_candidates'][0]['classification']['multipart'])
        self.assertEqual(report['status'], 'NEEDS_INPUT', report)
        self.assertEqual((self.storage.saved, self.storage.renamed, self.storage.created), ([], [], []))
        self.assertFalse(report['writes_attempted'])

    def test_archive_does_not_compete_with_real_video_under_either_policy(self):
        for policy, action in ((None, 'skip'), ({'archives': 'manual'}, 'manual')):
            with self.subTest(action=action):
                self.setUp()
                self.storage.shared = [file('archive-2', '测试剧.S01E02.4320p.7z'),
                                       file('video-2', '测试剧.S01E02.2160p.mkv')]
                report = self.run_workflow(**({'version_policy': policy} if policy is not None else {}))
                self.assert_archive_not_video(report, action)
                self.assertEqual(report['status'], 'COMPLETE', report)
                self.assertEqual(self.storage.saved, [['video-2']])
                self.assertEqual([row['fid'] for row in report['source_candidates']], ['video-2'])
                self.assertEqual(self.storage.folders['season'][0]['fid'], 'old-1')

    def test_selecting_archive_candidate_id_cannot_force_video_or_automatic_extraction(self):
        self.storage.shared = [file('archive-2', '测试剧.S01E02.2160p.zip'),
                               file('video-2', '测试剧.S01E02.2160p.mkv')]
        archive_id = _digest((_digest('https://pan.quark.cn/s/fake-share'), 'archive-2'))
        report = self.run_workflow(version_policy={'archives': 'manual'},
                                   source_selection={'S01E02': archive_id})
        self.assert_archive_not_video(report, 'manual')
        self.assertEqual(report['status'], 'NEEDS_INPUT', report)
        self.assertEqual((self.storage.saved, self.storage.renamed, self.storage.created), ([], [], []))
        self.assertFalse(report['writes_attempted'])


if __name__ == '__main__':
    unittest.main()
