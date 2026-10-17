"""Formal health command keeps cloud operations read-only and reports private."""
import argparse
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from media_helper.health_cli import add_parser, run_command
from media_helper.library_registry import LibraryRegistry, drive_reference
from tests.test_health import Storage, folder, file, Resolver


class HealthCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.storage = Storage()
        self.config = {'provider': 'quark', 'library_root_id': 'root',
                       'categories': {'tv': {'folder_id': 'tv', 'name': '电视剧'},
                                      'movie': {'folder_id': 'movie', 'name': '电影'}},
                       'credential_source': {'type': 'alist', 'storage_id': 1}}
        parser = argparse.ArgumentParser()
        add_parser(parser.add_subparsers(dest='command', required=True))
        self.parser = parser
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.default_storage = self.stack.enter_context(patch('media_helper.headless_cli._storage', return_value=self.storage))
        self.drive_storage = self.stack.enter_context(patch('media_helper.library_cli._storage', return_value=self.storage))

    def command(self, *arguments):
        return run_command(self.parser.parse_args(['health', *arguments]), self.config, self.root)

    def trial(self):
        self.storage.rows['root'].append(folder('trial', '测试影库', 'root'))
        self.storage.rows['trial'] = [folder('trial-tv', '电视剧', 'trial'), folder('trial-movie', '电影', 'trial')]
        self.storage.rows['trial-tv'] = []
        self.storage.rows['trial-movie'] = []
        registry = LibraryRegistry(self.root / 'runtime/libraries.json')
        registry.register({'library_id': 'trial', 'drive_id': 'current', 'parent_id': 'root',
                           'library_root_id': 'trial', 'name': '测试影库',
                           'categories': {'tv': {'folder_id': 'trial-tv', 'name': '电视剧'},
                                          'movie': {'folder_id': 'trial-movie', 'name': '电影'}},
                           'init_job_key': 'library-init:' + 'a' * 64, 'test_only': True})
        return registry

    def test_default_reports_are_private_json_and_markdown_without_mutations(self):
        before = deepcopy(self.storage.rows)
        report = self.command()
        self.assertEqual(report['status'], 'HEALTHY', report)
        self.assertEqual(report['library_id'], 'current')
        self.assertFalse(report['writes_attempted'])
        for path in report['report_files'].values():
            self.assertEqual(Path(path).stat().st_mode & 0o777, 0o600)
        payload = json.loads(Path(report['report_files']['json']).read_text())
        self.assertEqual(payload['status'], 'HEALTHY')
        self.assertEqual(self.storage.rows, before)

    def test_metadata_is_opt_in(self):
        with patch('media_helper.health_cli._resolver', side_effect=AssertionError('not enabled')) as resolver:
            self.command()
            resolver.assert_not_called()
        with patch('media_helper.health_cli._resolver', return_value=Resolver()) as resolver:
            report = self.command('--metadata')
            resolver.assert_called_once()
            self.assertTrue(report['coverage']['metadata_requested'])

    def test_test_only_library_is_excluded_by_default(self):
        self.trial()
        report = self.command()
        self.assertEqual(report['status'], 'HEALTHY', report)
        self.assertIn('trial', report['coverage']['excluded_ids'])
        self.assertNotIn('trial', self.storage.listed)

    def test_explicit_test_library_can_be_checked_without_activation(self):
        self.trial()
        before = deepcopy(self.config)
        report = self.command('--library-id', 'trial')
        self.assertEqual(report['status'], 'HEALTHY', report)
        self.assertEqual(report['library_root_id'], 'trial')
        self.assertEqual(self.config, before)
        self.default_storage.assert_not_called()
        self.drive_storage.assert_called_once()

    def test_missing_library_never_reads_cloud(self):
        report = self.command('--library-id', 'absent')
        self.assertEqual(report['status'], 'INCOMPLETE')
        self.default_storage.assert_not_called()
        self.drive_storage.assert_not_called()

    def test_unknown_folder_is_not_queried(self):
        report = self.command('--folder-id', 'outside')
        self.assertEqual(report['status'], 'INCOMPLETE')
        self.assertNotIn('outside', self.storage.listed)

    def test_scan_limit_is_incomplete(self):
        report = self.command('--max-entries', '1')
        self.assertEqual(report['status'], 'INCOMPLETE')
        self.assertFalse(report['coverage']['scan_complete'])

    def test_default_output_is_bounded_but_saved_reports_keep_all_findings(self):
        self.storage.rows['season'].extend(file(f'e{number}', f'示例 (2026) S01E{number:02d}.mkv', 'season', 0)
                                            for number in range(2, 27))
        report = self.command()
        self.assertEqual(len(report['findings']), 20)
        self.assertEqual(report['findings_total'], 25)
        self.assertTrue(report['findings_truncated'])
        full = json.loads(Path(report['report_files']['json']).read_text())
        self.assertEqual(len(full['findings']), 25)
        self.assertEqual(full['summary'], report['summary'])
        rendered = Path(report['report_files']['markdown']).read_text()
        self.assertIn('S01E26.mkv', rendered)
        self.assertIn('coverage', report)

    def test_details_option_returns_full_report(self):
        self.storage.rows['season'].extend(file(f'e{number}', f'示例 (2026) S01E{number:02d}.mkv', 'season', 0)
                                            for number in range(2, 27))
        report = self.command('--details')
        self.assertEqual(len(report['findings']), 25)
        self.assertNotIn('findings_truncated', report)

    def test_default_schedule_output_is_compact_and_full_schedule_remains_local(self):
        with patch('media_helper.health_cli._resolver', return_value=Resolver()):
            report = self.command('--metadata')
        self.assertNotIn('seasons', report['expected_schedule'])
        self.assertIn('season_count', report['expected_schedule'])
        full = json.loads(Path(report['report_files']['json']).read_text())
        self.assertIsInstance(full['expected_schedule']['seasons'], list)

    def test_changed_initialized_root_identity_stops(self):
        self.trial()
        self.storage.rows['root'][-1]['name'] = '更改名字'
        report = self.command('--library-id', 'trial')
        self.assertEqual(report['status'], 'INCOMPLETE')
        self.assertNotIn('trial', self.storage.listed)

    def test_no_apply_option_exists(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args(['health', '--apply'])

    def test_report_symlink_is_not_followed(self):
        reports = self.root / 'runtime/reports'
        reports.mkdir(parents=True)
        victim = self.root / 'keep.json'
        victim.write_text('keep')
        (reports / 'health-latest.json').symlink_to(victim)
        with self.assertRaises(ValueError):
            self.command()
        self.assertEqual(victim.read_text(), 'keep')


if __name__ == '__main__':
    unittest.main()
