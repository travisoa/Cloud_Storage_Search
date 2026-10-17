"""Exact matching CLI is read-only remotely and never rebinds a primary ID."""
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from media_helper.headless_cli import main
from media_helper.profiles import ProfileStore
from media_helper.tasks import TaskStore
from tests.test_identity_match import Catalog, identity


class Primary:
    source = 'tvmaze'
    def get_series(self, identifier):
        return {'id': 9, 'title': 'Example Show', 'year': 2026, 'source': 'tvmaze'}
    def get_external_ids(self, identifier):
        return {'imdb_id': 'tt1234567', 'tvdb_id': 55}


class MatchCommandsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = {'library_root_id': 'root', 'metadata_source': 'tmdb'}
        self.provider, self.primary = Catalog(), Primary()
        self.request = {'title': '示例剧', 'canonical_title': 'Example Show', 'aliases': ['示例剧'],
                        'series_id': '9', 'metadata_source': 'tvmaze', 'year': 2026,
                        'season': 1, 'library_root_id': 'root'}
        self.profiles = ProfileStore(self.root / 'runtime/series-profiles.json')
        self.profile = self.profiles.bind(metadata=self.primary, source='tvmaze', series_id=9,
            year=2026, season=1, title='示例剧', canonical_title='Example Show', aliases=['示例剧'],
            evidence_urls=['https://www.tvmaze.com/shows/9/example'])
        self.task = TaskStore(self.root / 'runtime/tasks', 'root').create(self.request)
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch('media_helper.headless_cli.ROOT', self.root))
        stack.enter_context(patch('media_helper.headless_cli._config', return_value=self.config))
        self.metadata = stack.enter_context(patch('media_helper.headless_cli._metadata',
            side_effect=lambda source: (self.provider, 'tmdb') if source == 'tmdb' else (self.primary, 'tvmaze')))
        stack.enter_context(patch('media_helper.headless_cli._storage', side_effect=AssertionError('match must not construct cloud provider')))

    def command(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(['match', *args])
        return code, json.loads(output.getvalue())

    def source(self):
        return ['--task-id', self.task['task_id']]

    def test_explicit_id_or_url_returns_details_and_private_report_without_cloud(self):
        for value in ('7', 'https://www.themoviedb.org/tv/7'):
            code, report = self.command('--tmdb-id', value)
            self.assertEqual((code, report['status']), (0, 'MATCHED'), report)
            self.assertEqual(report['identity']['id'], 7)
            self.assertFalse(report['cloud_writes_performed'])
            self.assertFalse(report['browser_opened'])
        report_path = self.root / 'runtime/reports/match-latest.json'
        self.assertEqual(report_path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('tmdb_link', self.profiles.all()[self.profile['profile_key']])

    def test_exact_title_year_uses_complete_search_and_movie_namespace_is_distinct(self):
        code, report = self.command('--title', '示例剧', '--year', '2026')
        self.assertEqual((code, report['status']), (0, 'MATCHED'), report)
        self.assertTrue(any(call[0] == 'search' for call in self.provider.calls))
        code, report = self.command('--kind', 'movie', '--tmdb-id', 'https://www.themoviedb.org/tv/7')
        self.assertEqual((code, report['status']), (3, 'NEEDS_INPUT'), report)

    def test_binding_links_verified_tmdb_without_changing_primary_profile_or_task(self):
        task_path = self.root / 'runtime/tasks' / (self.task['task_id'] + '.json')
        before = task_path.read_bytes()
        code, report = self.command(*self.source(), '--tmdb-id', '7', '--bind')
        self.assertEqual((code, report['status']), (0, 'MATCHED'), report)
        saved = self.profiles.all()[self.profile['profile_key']]
        self.assertEqual((saved['metadata_source'], saved['series_id'], saved['profile_key']), ('tvmaze', '9', 'tvmaze:9:2026'))
        self.assertEqual(saved['tmdb_link']['series_id'], '7')
        self.assertEqual(task_path.read_bytes(), before)

    def test_explicit_title_year_or_external_id_conflicts_cannot_bind(self):
        before = self.profiles.path.read_bytes()
        for flags in (['--title', '另一部剧'], ['--year', '2025'], ['--year', '0'],
                      ['--imdb-id', 'tt9999999'], ['--tvdb-id', '99']):
            with self.subTest(flags=flags):
                code, report = self.command(*self.source(), '--tmdb-id', '7', '--bind', *flags)
                self.assertEqual((code, report['status']), (1, 'FAILED'), report)
                self.assertEqual(self.profiles.path.read_bytes(), before)

    def test_same_external_id_case_is_normalized_before_comparing_task_identity(self):
        code, report = self.command(*self.source(), '--tmdb-id', '7', '--imdb-id', 'TT1234567', '--bind')
        self.assertEqual((code, report['status']), (0, 'MATCHED'), report)

    def test_tmdb_details_conflicting_with_primary_ids_cannot_bind(self):
        self.provider.details[7]['external_ids']['tvdb_id'] = 99
        before = self.profiles.path.read_bytes()
        code, report = self.command(*self.source(), '--tmdb-id', '7', '--bind')
        self.assertEqual((code, report['status']), (3, 'NEEDS_INPUT'), report)
        self.assertEqual(report['conflicts'][0]['field'], 'tvdb_id')
        self.assertEqual(self.profiles.path.read_bytes(), before)

    def test_incomplete_search_does_not_bind_unique_first_page(self):
        self.provider.complete = False
        before = self.profiles.path.read_bytes()
        code, report = self.command('--title', '示例剧', '--year', '2026')
        self.assertEqual((code, report['status']), (3, 'NEEDS_INPUT'), report)
        self.assertFalse(report['search_complete'])
        self.assertEqual(self.profiles.path.read_bytes(), before)

    def test_bind_without_task_and_task_movie_type_are_rejected(self):
        before = self.profiles.path.read_bytes()
        for arguments in (['--tmdb-id', '7', '--bind'], [*self.source(), '--kind', 'movie', '--tmdb-id', '7', '--bind']):
            code, report = self.command(*arguments)
            self.assertEqual((code, report['status']), (1, 'FAILED'), report)
            self.assertEqual(self.profiles.path.read_bytes(), before)

    def test_task_from_another_library_root_cannot_be_used(self):
        self.config['library_root_id'] = 'other-root'
        code, report = self.command(*self.source(), '--tmdb-id', '7', '--bind')
        self.assertEqual((code, report['status']), (1, 'FAILED'), report)
        self.assertFalse(self.provider.calls)


if __name__ == '__main__':
    unittest.main()
