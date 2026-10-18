"""Per-work policies constrain labeled releases without cloud authority."""
from copy import deepcopy
import json
import math
from pathlib import Path
import tempfile
import unittest

from media_helper.version_policy import DEFAULT, VersionPolicyStore, assess, attach, normalize


REQUEST = {'title': '测试剧', 'metadata_source': 'tmdb', 'series_id': '17', 'year': 2026}


class PolicyAssessmentTests(unittest.TestCase):
    def test_fallback_must_be_explicit_even_if_lower_resolution_is_ordered(self):
        info = {'resolution': '1080p'}
        self.assertFalse(assess({}, info, size=100)['allowed'])
        self.assertTrue(assess({'allow_fallback': True}, info, size=100)['allowed'])
        self.assertFalse(assess({'allow_fallback': True}, {'resolution': '480p'}, size=100)['allowed'])

    def test_resolution_order_dominates_codec_hdr_and_all_keyword_bonuses(self):
        policy = {'resolution_order': ['2160p', '1080p'], 'allow_fallback': True,
                  'preferred_codecs': ['HEVC', 'AV1', 'H.264'], 'hdr': 'prefer',
                  'preferred_keywords': ['标签' + str(i) for i in range(12)]}
        high = assess(policy, {'resolution': '2160p'}, size=100)
        lower = assess(policy, {'resolution': '1080p', 'codec': 'HEVC', 'hdr': 'Dolby Vision'},
                       size=100, name=' '.join(policy['preferred_keywords']))
        self.assertTrue(high['allowed'] and lower['allowed'])
        self.assertGreater(high['score'], lower['score'])
        self.assertEqual(len(lower['matched_keywords']), 12)

    def test_codec_order_only_applies_within_resolution_and_required_codec_is_strict(self):
        policy = {'preferred_codecs': ['HEVC', 'H.264']}
        hevc = assess(policy, {'resolution': '2160p', 'codec': 'HEVC'}, size=100)
        avc = assess(policy, {'resolution': '2160p', 'codec': 'H.264'}, size=100)
        unknown = assess(policy, {'resolution': '2160p'}, size=100)
        self.assertGreater(hevc['score'], avc['score'])
        self.assertGreater(avc['score'], unknown['score'])
        self.assertTrue(unknown['allowed'])
        self.assertFalse(assess({**policy, 'require_codec': True}, {'resolution': '2160p'}, size=100)['allowed'])

    def test_missing_labels_do_not_become_resolution_codec_or_sdr(self):
        unknown = assess({}, {}, size=100)
        self.assertFalse(unknown['allowed'])
        self.assertIsNone(unknown['resolution'])
        self.assertIsNone(unknown['codec'])
        self.assertIsNone(unknown['hdr'])
        self.assertTrue(unknown['hdr_absence_is_not_sdr_proof'])
        self.assertEqual(unknown['basis'], 'release_labels_only')
        excluded = assess({'hdr': 'exclude'}, {'resolution': '2160p'}, size=100)
        self.assertTrue(excluded['allowed'])
        self.assertTrue(excluded['hdr_absence_is_not_sdr_proof'])

    def test_hdr_require_exclude_and_dolby_vision_are_distinct(self):
        info = {'resolution': '2160p', 'hdr': 'HDR10'}
        self.assertTrue(assess({'hdr': 'require'}, info, size=100)['allowed'])
        self.assertFalse(assess({'hdr': 'exclude'}, info, size=100)['allowed'])
        self.assertFalse(assess({'hdr': 'dolby-vision'}, info, size=100)['allowed'])
        self.assertTrue(assess({'hdr': 'dolby-vision'}, {**info, 'hdr': 'Dolby Vision'}, size=100)['allowed'])
        self.assertFalse(assess({'hdr': 'require'}, {'resolution': '2160p'}, size=100)['allowed'])

    def test_explicit_sdr_is_accepted_by_hdr_exclusion_without_hdr_preference(self):
        info = {'resolution': '2160p', 'hdr': 'SDR'}
        excluded = assess({'hdr': 'exclude'}, info, size=100)
        self.assertTrue(excluded['allowed'])
        self.assertEqual(excluded['hdr'], 'SDR')
        self.assertFalse(excluded['hdr_absence_is_not_sdr_proof'])
        self.assertFalse(assess({'hdr': 'require'}, info, size=100)['allowed'])
        self.assertFalse(assess({'hdr': 'dolby-vision'}, info, size=100)['allowed'])
        self.assertEqual(assess({'hdr': 'prefer'}, info, size=100)['score'],
                         assess({'hdr': 'prefer'}, {'resolution': '2160p'}, size=100)['score'])

    def test_size_limits_use_known_integer_bytes_and_inclusive_decimal_units(self):
        policy, info = {'min_size_mb': 500, 'max_size_gb': 1}, {'resolution': '2160p'}
        for size in (500_000_000, 1_000_000_000):
            self.assertTrue(assess(policy, info, size=size)['allowed'])
        for size in (499_999_999, 1_000_000_001, 0, -1, None, True, 500_000_000.0):
            with self.subTest(size=size):
                self.assertFalse(assess(policy, info, size=size)['allowed'])

    def test_invalid_or_credential_bearing_policy_cannot_be_normalized(self):
        for policy in ({'allow_fallback': 'yes'}, {'require_codec': True}, {'resolution_order': []},
                       {'resolution_order': ['4K']}, {'preferred_codecs': ['x265']}, {'hdr': 'SDR'},
                       {'min_size_mb': math.nan}, {'max_size_gb': math.inf}, {'min_size_mb': True},
                       {'min_size_mb': 1001, 'max_size_gb': 1}, {'existing': 'replace'},
                       {'archives': 'auto'}, {'preferred_keywords': ['cookie=fixture']},
                       {'preferred_keywords': ['https://example.org']}, {'unknown': True}):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                normalize(policy)

    def test_normalization_has_independent_defaults_and_deduplicated_order(self):
        first = normalize({'resolution_order': ['1080p', '1080p', '720p'], 'preferred_keywords': [' WEB ', 'WEB']})
        self.assertEqual(first['resolution_order'], ['1080p', '720p'])
        self.assertEqual(first['preferred_keywords'], ['WEB'])
        first['preferred_codecs'].append('HEVC')
        self.assertEqual(normalize({})['preferred_codecs'], [])
        self.assertEqual(DEFAULT['preferred_codecs'], [])


class PolicyStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / 'runtime/version-policies.json'
        self.store = VersionPolicyStore(self.path)

    def test_policy_is_scoped_to_source_id_and_year_not_title(self):
        self.store.set(REQUEST, {'resolution_order': ['1080p']})
        for changes in ({'series_id': '18'}, {'year': 2025}, {'metadata_source': 'tvmaze'}):
            self.assertIsNone(self.store.get({**REQUEST, **changes}))
        same_id = self.store.get({**REQUEST, 'title': '另一个已确认名称'})
        self.assertEqual(same_id['policy']['resolution_order'], ['1080p'])

    def test_records_are_private_and_return_values_are_independent(self):
        record = self.store.set(REQUEST, {})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        record['policy']['resolution_order'].clear()
        self.assertEqual(self.store.get(REQUEST)['policy']['resolution_order'], ['2160p', '1080p', '720p'])
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.store.all()

    def test_symlink_and_tampered_identity_are_rejected(self):
        self.store.set(REQUEST, {})
        data = json.loads(self.path.read_text())
        data['series']['tmdb:17:2026']['series_id'] = '18'
        self.path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            self.store.all()
        target = self.root / 'elsewhere.json'
        self.path.replace(target)
        self.path.symlink_to(target)
        with self.assertRaises(ValueError):
            self.store.all()

    def test_attach_refreshes_store_and_reset_does_not_restore_snapshot_policy(self):
        self.store.set(REQUEST, {'resolution_order': ['1080p']})
        old_task = attach(REQUEST, self.root)
        self.assertEqual(old_task['version_policy']['resolution_order'], ['1080p'])
        self.store.set(REQUEST, {'resolution_order': ['2160p'], 'hdr': 'require'})
        self.assertEqual(attach(old_task, self.root)['version_policy']['hdr'], 'require')
        self.store.reset(REQUEST)
        reset = attach(old_task, self.root)
        self.assertNotIn('version_policy', reset)
        self.assertNotIn('version_policy_key', reset)
        self.assertNotEqual(reset.get('quality_preference'), ['1080p'])


if __name__ == '__main__':
    unittest.main()
