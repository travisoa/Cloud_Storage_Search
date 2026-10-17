"""Integration regressions from the exact identity and policy replay review."""
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from media_helper.headless_cli import _prepare_identity
from media_helper.profiles import ProfileStore
from media_helper.subscriptions import SubscriptionStore
from media_helper.version_policy import VersionPolicyStore
from tests.test_identity_match import Catalog
from tests.test_match_cli import Primary


class IdentityPolicyReplay(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = {'library_root_id': 'root', 'metadata_source': 'tmdb'}
        self.provider = Catalog()
        self.provider.details[7]['seasons'] = [{'season': 1}]
        self.profiles = ProfileStore(self.root / 'runtime/series-profiles.json')
        self.profile = self.profiles.bind(metadata=self.provider, source='tmdb', series_id=7,
            year=2026, season=1, title='示例剧', canonical_title='示例剧',
            evidence_urls=['https://www.themoviedb.org/tv/7'], quality_preference=['2160p'])
        self.request = {'title': '示例剧', 'series_id': '7', 'year': 2026, 'season': 1,
                        'metadata_source': 'tmdb', 'library_root_id': 'root'}
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch('media_helper.headless_cli.ROOT', self.root))
        stack.enter_context(patch('media_helper.headless_cli._metadata', return_value=(self.provider, 'tmdb')))
        stack.enter_context(patch('media_helper.headless_cli._storage', side_effect=AssertionError('identity review must not access a drive')))

    def test_existing_profile_does_not_override_conflicting_replayed_external_id(self):
        before = self.profiles.path.read_bytes()
        resolved, issue, _, _ = _prepare_identity({**self.request, 'external_ids': {'imdb_id': 'tt9999999'}}, self.config)
        self.assertIsNone(resolved)
        self.assertEqual(issue['status'], 'NEEDS_INPUT')
        self.assertEqual(issue['conflicts'][0]['field'], 'imdb_id')
        self.assertEqual(self.profiles.path.read_bytes(), before)
        self.assertFalse((self.root / 'runtime/series-bindings.json').exists())

    def test_policy_derived_subscription_quality_cannot_revive_after_reset(self):
        policies = VersionPolicyStore(self.root / 'runtime/version-policies.json')
        policies.set(self.request, {'resolution_order': ['1080p']})
        resolved, issue, _, _ = _prepare_identity(self.request, self.config)
        self.assertIsNone(issue)
        self.assertEqual(resolved['quality_preference'], ['1080p'])
        self.assertIn('version_policy_key', resolved)
        subscriptions = SubscriptionStore(self.root / 'runtime/subscriptions.json', 'root')
        record = subscriptions.add(resolved)
        policies.reset(self.request)
        after, issue, _, _ = _prepare_identity(record['request'], self.config)
        self.assertIsNone(issue)
        self.assertNotIn('version_policy', after)
        self.assertEqual(after['quality_preference'], ['2160p'])


class ConfirmedLinkRebind(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.primary, self.tmdb = Primary(), Catalog()
        self.store = ProfileStore(self.root / 'profiles.json')
        self.profile = self.bind()
        result = self.store.bind_tmdb(self.profile, primary=self.primary, tmdb=self.tmdb, tmdb_id=7)
        self.assertEqual(result['status'], 'MATCHED')
        self.link = self.store.all()[self.profile['profile_key']]['tmdb_link']

    def bind(self, **changes):
        return self.store.bind(metadata=self.primary, source='tvmaze', series_id=9, year=2026,
                               season=1, title='示例剧', canonical_title='Example Show',
                               evidence_urls=['https://www.tvmaze.com/shows/9/example'], **changes)

    def test_rebinding_same_verified_identity_retains_confirmed_secondary_link(self):
        profile = self.bind(aliases=['新增已确认别名'])
        self.assertEqual(profile.get('tmdb_link'), self.link)
        self.assertEqual((profile['metadata_source'], profile['series_id']), ('tvmaze', '9'))

    def test_rebind_does_not_preserve_link_when_primary_public_ids_have_changed(self):
        before = self.store.path.read_bytes()
        self.primary.get_external_ids = lambda identifier: {'imdb_id': 'tt9999999', 'tvdb_id': 55}
        with self.assertRaises(ValueError):
            self.bind(aliases=['新增已确认别名'])
        self.assertEqual(self.store.path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
