"""Production identity preparation uses exact TMDB facts before cloud access."""
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from media_helper.headless_cli import _prepare_identity
from media_helper.catalog import MetadataError
from media_helper.identity_match import MatchedMetadata
from media_helper.profiles import ProfileStore
from tests.test_identity_match import Catalog, identity
from tests.test_match_cli import Primary


class ExactHeadlessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = {'library_root_id': 'root', 'metadata_source': 'tmdb',
                       'categories': {'tv': {'folder_id': 'tv-root'}}, 'include_media_tags': True}
        self.provider = Catalog()
        self.provider.details[7]['seasons'] = [{'season': 1}]
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch('media_helper.headless_cli.ROOT', self.root))
        self.metadata = stack.enter_context(patch('media_helper.headless_cli._metadata', return_value=(self.provider, 'tmdb')))
        self.storage = stack.enter_context(patch('media_helper.headless_cli._storage', side_effect=AssertionError('identity preparation must not construct cloud provider')))

    def prepare(self, **changes):
        return _prepare_identity({'title': '示例剧', 'year': 2026, **changes}, self.config)

    def assert_no_binding(self):
        self.assertFalse((self.root / 'runtime/series-bindings.json').exists())
        self.storage.assert_not_called()

    def test_new_title_year_uses_complete_identity_search_and_live_wrapper(self):
        resolved, issue, provider, profile = self.prepare()
        self.assertIsNone(issue)
        self.assertIsNone(profile)
        self.assertIsInstance(provider, MatchedMetadata)
        self.assertEqual((resolved['series_id'], resolved['metadata_source'], resolved['season']), ('7', 'tmdb', 1))
        self.assertEqual(resolved['category_folder_id'], 'tv-root')
        self.assertTrue(resolved['identity_confirmed'])
        self.assertTrue(any(call[0] == 'search' for call in self.provider.calls))
        binding_path = self.root / 'runtime/series-bindings.json'
        saved = next(iter(json.loads(binding_path.read_text()).values()))
        self.assertEqual((saved['series_id'], saved['metadata_source']), ('7', 'tmdb'))
        self.assertEqual(binding_path.stat().st_mode & 0o777, 0o600)
        self.storage.assert_not_called()

    def test_explicit_tmdb_id_reads_same_id_aliases_without_title_search(self):
        resolved, issue, provider, _ = self.prepare(title='示例电视剧', series_id='7', season=1)
        self.assertIsNone(issue)
        self.assertEqual(resolved['series_id'], '7')
        self.assertEqual(provider.get_series(7)['aliases'], ['示例电视剧'])
        self.assertFalse(any(call[0] == 'search' for call in self.provider.calls))

    def test_title_without_year_does_not_use_old_first_result_resolution(self):
        resolved, issue, _, _ = self.prepare(year=None)
        self.assertIsNone(resolved)
        self.assertEqual(issue['reason'], 'release_year_or_explicit_id_required')
        self.assertEqual(self.provider.calls, [])
        self.assert_no_binding()

    def test_explicit_id_can_supply_unknown_year_only_after_live_details(self):
        resolved, issue, _, _ = self.prepare(series_id='7', year=None, season=1)
        self.assertIsNone(issue)
        self.assertEqual(resolved['year'], 2026)
        self.assertFalse(any(call[0] == 'search' for call in self.provider.calls))

    def test_ambiguous_same_title_year_and_incomplete_search_never_save_binding(self):
        self.provider.rows.append(identity(8))
        self.provider.details[8] = identity(8)
        resolved, issue, _, _ = self.prepare(season=1)
        self.assertIsNone(resolved)
        self.assertEqual(issue['reason'], 'identity_not_unique')
        self.assert_no_binding()
        self.provider.rows.pop()
        self.provider.complete = False
        resolved, issue, _, _ = self.prepare(season=1)
        self.assertIsNone(resolved)
        self.assertEqual(issue['reason'], 'identity_search_incomplete')
        self.assert_no_binding()

    def test_explicit_id_title_or_year_conflict_never_save_binding(self):
        for changes in ({'title': '另一部剧'}, {'year': 2025}):
            with self.subTest(changes=changes):
                resolved, issue, _, _ = self.prepare(series_id='7', season=1, **changes)
                self.assertIsNone(resolved)
                self.assertEqual(issue['status'], 'NEEDS_INPUT')
                self.assertEqual(issue['reason'], 'explicit_identity_conflict')
                self.assert_no_binding()

    def test_external_id_constraint_on_replayed_request_cannot_be_ignored(self):
        resolved, issue, _, _ = self.prepare(series_id='7', season=1, external_ids={'imdb_id': 'tt9999999'})
        self.assertIsNone(resolved)
        self.assertEqual(issue['status'], 'NEEDS_INPUT')
        self.assertEqual(issue['conflicts'][0]['field'], 'imdb_id')
        self.assert_no_binding()

    def test_metadata_details_changed_after_exact_match_cannot_bind(self):
        original = self.provider.get_identity
        count = 0
        def changed(kind, identifier):
            nonlocal count
            count += 1
            row = original(kind, identifier)
            if count > 1:
                row['year'] = 2025
            return row
        self.provider.get_identity = changed
        with self.assertRaisesRegex(MetadataError, 'matched_identity_changed'):
            self.prepare(series_id='7', season=1)
        self.assert_no_binding()

    def test_multiple_seasons_still_require_season_confirmation_after_work_match(self):
        self.provider.details[7]['seasons'] = [{'season': 1}, {'season': 2}]
        resolved, issue, _, _ = self.prepare()
        self.assertIsNone(resolved)
        self.assertEqual(issue['status'], 'NEEDS_INPUT')
        self.assertEqual(issue['seasons'], [1, 2])
        self.assert_no_binding()

    def test_existing_tvmaze_profile_keeps_primary_id_without_tmdb_search(self):
        primary = Primary()
        store = ProfileStore(self.root / 'runtime/series-profiles.json')
        store.bind(metadata=primary, source='tvmaze', series_id=9, year=2026, season=1,
                   title='示例剧', canonical_title='Example Show', aliases=['示例电视剧'],
                   evidence_urls=['https://www.tvmaze.com/shows/9/example'], quality_preference=['2160p'])
        self.metadata.side_effect = lambda source: (primary, 'tvmaze') if source == 'tvmaze' else (self.provider, 'tmdb')
        resolved, issue, provider, profile = self.prepare(title='示例电视剧', metadata_source='auto')
        self.assertIsNone(issue)
        self.assertEqual((resolved['metadata_source'], resolved['series_id'], resolved['title']), ('tvmaze', '9', '示例剧'))
        self.assertEqual(profile['profile_key'], 'tvmaze:9:2026')
        self.assertEqual(provider.source, 'tvmaze')
        self.assertEqual(self.metadata.call_args.args, ('tvmaze',))
        self.assertEqual(self.provider.calls, [])
        self.storage.assert_not_called()

    def test_wrong_library_scope_stops_before_any_metadata_or_bindings(self):
        with self.assertRaises(ValueError):
            self.prepare(library_root_id='another-root')
        self.metadata.assert_not_called()
        self.assert_no_binding()


if __name__ == '__main__':
    unittest.main()
