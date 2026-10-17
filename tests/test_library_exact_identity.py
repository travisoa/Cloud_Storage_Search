"""Library scans use the same ID-scoped TMDB identity rules as requests."""
import unittest

from media_helper.catalog import MetadataError
from media_helper.library_identity import MediaResolver
from tests.test_identity_match import Catalog, identity


class LibraryExactIdentity(unittest.TestCase):
    def resolve(self, provider, name='示例剧 (2026) S01E01.mkv', **options):
        return MediaResolver(provider, **options)({'name': name}, {})

    def test_same_id_detail_alias_can_match_when_search_display_name_differs(self):
        provider = Catalog([identity(title='Example Show', original_title='Other', aliases=[])])
        provider.details[7]['aliases'] = ['示例剧']
        result = self.resolve(provider)
        self.assertEqual((result['status'], result['series_id']), ('MATCHED', '7'))
        self.assertEqual(result['title'], 'Example Show')
        self.assertTrue(any(call[0] == 'search' for call in provider.calls))

    def test_incomplete_search_cannot_turn_one_candidate_into_identity(self):
        result = self.resolve(Catalog(complete=False))
        self.assertEqual(result['status'], 'NEEDS_INPUT')
        self.assertEqual(result['reason'], 'identity_search_incomplete')
        self.assertFalse(result['search_complete'])

    def test_multiple_same_id_scoped_alias_matches_remain_ambiguous(self):
        provider = Catalog([identity(7, title='First', original_title='First', aliases=[]),
                            identity(8, title='Second', original_title='Second', aliases=[])])
        for row in provider.details.values():
            row['aliases'] = ['示例剧']
        result = self.resolve(provider)
        self.assertEqual(result['reason'], 'identity_not_unique')
        self.assertEqual(len(result['candidates']), 2)

    def test_auth_failures_still_escape_to_stop_library_scan(self):
        for status in (401, 403, 429):
            with self.subTest(status=status), self.assertRaises(MetadataError) as raised:
                provider = Catalog()
                provider.failure = MetadataError('tmdb', 'http_error', status=status)
                self.resolve(provider)
            self.assertEqual(raised.exception.status, status)

    def test_movie_keeps_movie_namespace_and_public_detail_identity(self):
        provider = Catalog([identity(kind='movie')])
        result = self.resolve(provider, '示例剧 (2026).mkv')
        self.assertEqual((result['status'], result['kind'], result['series_id']), ('MATCHED', 'movie', '7'))
        self.assertTrue(all(call[1] == 'movie' for call in provider.calls))

    def test_bound_alias_skips_search_but_still_rechecks_live_canonical(self):
        binding = {'metadata_source': 'tmdb', 'series_id': '7', 'year': 2026, 'title': '常用片名',
                   'canonical_title': '示例剧', 'aliases': ['本地别名']}
        provider = Catalog()
        result = self.resolve(provider, '本地别名 (2026) S01E01.mkv', bindings=[binding])
        self.assertEqual((result['status'], result['title']), ('MATCHED', '常用片名'))
        self.assertFalse(any(call[0] == 'search' for call in provider.calls))
        provider.details[7]['title'] = '另一个作品'
        result = self.resolve(provider, '本地别名 (2026) S01E01.mkv', bindings=[binding])
        self.assertEqual(result['reason'], 'confirmed_binding_identity_changed')


if __name__ == '__main__':
    unittest.main()
