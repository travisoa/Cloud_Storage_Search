"""Policy CLI edits only local private records, without constructing providers."""
import argparse
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from media_helper.policy_cli import add_parser, run_command
from media_helper.profiles import ProfileStore
from media_helper.tasks import TaskStore
from media_helper.version_policy import VersionPolicyStore


class PolicyCommands(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = {'library_root_id': 'root'}
        self.request = {'title': '测试剧', 'metadata_source': 'tmdb', 'series_id': '17', 'year': 2026,
                        'season': 1, 'library_root_id': 'root', 'quality_preference': ['2160p']}
        self.task = TaskStore(self.root / 'runtime/tasks', 'root').create(self.request)
        self.parser = argparse.ArgumentParser()
        add_parser(self.parser.add_subparsers(dest='command', required=True))
        self.store = VersionPolicyStore(self.root / 'runtime/version-policies.json')

    def command(self, *args):
        parsed = self.parser.parse_args(['policy', *args])
        with patch('media_helper.headless_cli._storage', side_effect=AssertionError('policy must not construct cloud provider')), \
             patch('media_helper.headless_cli._metadata', side_effect=AssertionError('policy must not contact metadata API')):
            return run_command(parsed, self.config, self.root)

    def source(self):
        return ['--task-id', self.task['task_id']]

    def test_set_show_list_reset_only_touch_local_policy(self):
        saved = self.command('set', *self.source(), '--resolution', '2160p', '--resolution', '1080p', '--allow-fallback',
                             '--codec', 'HEVC', '--prefer-codec', '--hdr', 'prefer', '--keyword', 'WEB-DL',
                             '--min-size-mb', '500', '--max-size-gb', '12', '--archives', 'manual')
        self.assertEqual(saved['status'], 'POLICY_SAVED')
        self.assertFalse(saved['cloud_writes_performed'])
        self.assertFalse(saved['browser_opened'])
        self.assertEqual(saved['existing_files_action'], 'keep')
        policy = saved['record']['policy']
        self.assertEqual(policy['resolution_order'], ['2160p', '1080p'])
        self.assertTrue(policy['allow_fallback'])
        self.assertFalse(policy['require_codec'])
        self.assertEqual((policy['min_size_mb'], policy['max_size_gb']), (500, 12))
        shown = self.command('show', *self.source())
        self.assertEqual(shown['record'], saved['record'])
        listed = self.command('list')
        self.assertEqual(list(listed['series']), ['tmdb:17:2026'])
        self.assertEqual(self.command('reset', *self.source())['status'], 'POLICY_RESET')
        self.assertIsNone(self.command('show', *self.source())['record'])

    def test_partial_update_keeps_previous_rules_and_explicitly_disables_fallback(self):
        self.command('set', *self.source(), '--resolution', '2160p', '--resolution', '1080p', '--allow-fallback', '--hdr', 'require')
        changed = self.command('set', *self.source(), '--no-fallback', '--codec', 'AV1', '--require-codec')['record']['policy']
        self.assertEqual(changed['resolution_order'], ['2160p', '1080p'])
        self.assertFalse(changed['allow_fallback'])
        self.assertEqual(changed['hdr'], 'require')
        self.assertEqual(changed['preferred_codecs'], ['AV1'])
        self.assertTrue(changed['require_codec'])

    def test_clear_size_max_is_explicit_and_conflicting_flags_do_not_change_record(self):
        self.command('set', *self.source(), '--max-size-gb', '12')
        with self.assertRaises(ValueError):
            self.command('set', *self.source(), '--max-size-gb', '10', '--clear-max-size')
        self.assertEqual(self.store.get(self.request)['policy']['max_size_gb'], 12)
        cleared = self.command('set', *self.source(), '--clear-max-size')
        self.assertIsNone(cleared['record']['policy']['max_size_gb'])

    def test_invalid_settings_or_secret_keywords_leave_existing_policy_unchanged(self):
        self.command('set', *self.source())
        before = self.store.path.read_bytes()
        for flags in (['--max-size-gb', 'nan'], ['--min-size-mb', '-1'], ['--keyword', 'token=fixture'], ['--require-codec']):
            with self.subTest(flags=flags), self.assertRaises(ValueError):
                self.command('set', *self.source(), *flags)
            self.assertEqual(self.store.path.read_bytes(), before)

    def test_task_bound_to_another_root_cannot_change_policy(self):
        with self.assertRaises(ValueError):
            parsed = self.parser.parse_args(['policy', 'set', *self.source()])
            run_command(parsed, {'library_root_id': 'another-root'}, self.root)
        self.assertEqual(self.store.all(), {})

    def test_confirmed_profile_can_select_policy_without_any_cloud_request(self):
        class Metadata:
            def get_series(self, identifier):
                return {'id': 17, 'title': 'Example', 'year': 2026}
        profile = ProfileStore(self.root / 'runtime/series-profiles.json').bind(
            metadata=Metadata(), source='tmdb', series_id=17, year=2026, season=1,
            title='测试剧', canonical_title='Example', evidence_urls=['https://www.themoviedb.org/tv/17'])
        saved = self.command('set', '--profile-key', profile['profile_key'], '--resolution', '1080p')
        self.assertEqual(saved['record']['series_id'], '17')
        self.assertEqual(saved['record']['policy']['resolution_order'], ['1080p'])
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
