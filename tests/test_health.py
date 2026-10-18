"""Read-only health evidence, bounded scans, and expected schedule separation."""
from copy import deepcopy
import unittest

from media_helper.catalog import MetadataError
from media_helper.health import inspect, markdown
from media_helper.providers import AuthenticationError, RiskControlError


def folder(fid, name, parent):
    return {'fid': fid, 'name': name, 'parent_id': parent, 'is_dir': True, 'size': 0}


def file(fid, name, parent, size=100):
    return {'fid': fid, 'name': name, 'parent_id': parent, 'is_dir': False, 'size': size}


class Storage:
    account_scope = 'health-test'

    def __init__(self):
        self.rows = {'root': [folder('tv', '电视剧', 'root'), folder('movie', '电影', 'root')],
                     'tv': [folder('show', '示例 (2026)', 'tv')], 'movie': [],
                     'show': [folder('season', 'Season 01', 'show')],
                     'season': [file('e1', '示例 (2026) S01E01 [2160p].mkv', 'season')]}
        self.listed = []

    def list_files(self, parent):
        self.listed.append(parent)
        rows = self.rows[parent]
        if isinstance(rows, Exception):
            raise rows
        return deepcopy(rows)

    def __getattr__(self, name):
        if name in {'mkdir', 'rename', 'move', 'save_files', 'delete'}:
            raise AssertionError('health must never access a mutation')
        raise AttributeError(name)


class Resolver:
    def __init__(self):
        self.providers = {'tmdb': self}
        self.source = 'tmdb'
        self.calls = []
        self.episodes = [dict(season=1, episode=n, air_date=date) for n, date in
                         [(1, '2026-10-01'), (2, '2026-10-15'), (3, '2026-10-25')]]
        self.result = {'status': 'MATCHED', 'kind': 'tv', 'title': '示例', 'year': 2026,
                       'metadata_source': 'tmdb', 'series_id': '1', 'season': 1, 'episode': 1}

    def __call__(self, row, context):
        self.calls.append(row['fid'])
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def get_season(self, identifier, season):
        self.calls.append(('season', identifier, season))
        if isinstance(self.episodes, Exception):
            raise self.episodes
        return self.episodes


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.storage = Storage()
        self.categories = {'tv': {'folder_id': 'tv', 'name': '电视剧'},
                           'movie': {'folder_id': 'movie', 'name': '电影'}}

    def inspect(self, **changes):
        return inspect(self.storage, library_root_id='root', categories=self.categories,
                       as_of='2026-10-17T10:00:00+08:00', **changes)

    def codes(self, report):
        return {item['code'] for item in report['findings']}

    def test_healthy_structure_is_readonly_and_empty_category_is_allowed(self):
        before = deepcopy(self.storage.rows)
        report = self.inspect()
        self.assertEqual(report['status'], 'HEALTHY', report)
        self.assertEqual(report['findings'], [])
        self.assertTrue(report['read_only'])
        self.assertFalse(report['writes_attempted'])
        self.assertFalse(report['writes_performed'])
        self.assertFalse(report['playability_verified'])
        self.assertEqual(self.storage.rows, before)

    def test_tv_region_layers_keep_work_inventory_and_metadata_separate(self):
        self.storage.rows['tv'] = [folder('kr', '韩剧', 'tv')]
        self.storage.rows['kr'] = [folder('a', '甲剧 (2026)', 'kr'), folder('b', '乙剧 (2026)', 'kr')]
        self.storage.rows['a'] = [file('ae', '甲剧 (2026)S01E01.mkv', 'a')]
        self.storage.rows['b'] = [file('be', '乙剧 (2026)S01E01.mkv', 'b')]

        class PerWorkResolver(Resolver):
            def __call__(self, row, context):
                title, identifier = ('甲剧', '1') if row['fid'] == 'ae' else ('乙剧', '2')
                return {**self.result, 'title': title, 'series_id': identifier}

        report = self.inspect(resolver=PerWorkResolver())
        self.assertEqual(report['status'], 'HEALTHY', report)
        self.assertEqual(report['coverage']['work_directories'], 2)
        self.assertEqual({item['work_id'] for item in report['expected_schedule']['seasons']}, {'a', 'b'})

    def test_region_group_does_not_hide_empty_work_or_orphan_subtitle(self):
        self.storage.rows['tv'] = [folder('kr', '韩剧', 'tv')]
        self.storage.rows['kr'] = [folder('a', '甲剧 (2026)', 'kr'), folder('b', '乙剧 (2026)', 'kr')]
        self.storage.rows['a'] = [file('asub', '甲剧 (2026)S01E01.zh.srt', 'a')]
        self.storage.rows['b'] = [file('be', '乙剧 (2026)S01E01.mkv', 'b')]
        report = self.inspect()
        findings = {(item['code'], item['path']) for item in report['findings']}
        self.assertIn(('empty_work_directory', '电视剧/韩剧/甲剧 (2026)'), findings)
        self.assertIn(('orphan_subtitle', '电视剧/韩剧/甲剧 (2026)/甲剧 (2026)S01E01.zh.srt'), findings)
        self.assertNotIn('duplicate_episode_versions', self.codes(report))

    def test_unknown_group_name_is_not_assumed_to_be_a_category(self):
        self.storage.rows['tv'] = [folder('group', '自定义合集', 'tv')]
        self.storage.rows['group'] = [folder('show', '示例 (2026)', 'group')]
        report = self.inspect()
        self.assertEqual(report['coverage']['work_directories'], 1)
        self.assertIn('work_year_unrecognized', self.codes(report))

    def test_compact_episode_names_keep_prefix_and_inventory_alignment(self):
        for title in ('ExampleShow (2026)', 'ExampleShow'):
            with self.subTest(title=title):
                self.storage.rows['tv'][0]['name'] = title
                self.storage.rows['season'][0]['name'] = title + 'S01E01_2160P_DV_DDP5.1.mkv'
                before = deepcopy(self.storage.rows)
                report = self.inspect()
                self.assertNotIn('title_prefix_mismatch', self.codes(report))
                self.assertNotIn('episode_unrecognized', self.codes(report))
                self.assertEqual(report['coverage']['video_files'], 1)
                self.assertEqual(self.storage.rows, before)

    def test_audio_only_movie_suffix_keeps_matching_work_prefix(self):
        self.storage.rows['movie'] = [folder('feature', 'Example Movie (2025)', 'movie')]
        for suffix in ('DDP5.1', 'TrueHD7.1.Atmos'):
            with self.subTest(suffix=suffix):
                self.storage.rows['feature'] = [file('feature-file', f'Example Movie (2025)_{suffix}.mkv', 'feature')]
                before = deepcopy(self.storage.rows)
                report = self.inspect()
                self.assertEqual(report['status'], 'HEALTHY', report)
                self.assertEqual(report['findings'], [])
                self.assertEqual(self.storage.rows, before)

    def test_same_season_episode_versions_are_detected_without_content_claim(self):
        self.storage.rows['season'].append(file('e1b', '示例 (2026) S01E01 [1080p].mkv', 'season'))
        report = self.inspect()
        finding = next(item for item in report['findings'] if item['code'] == 'duplicate_episode_versions')
        self.assertEqual(finding['fids'], ['e1', 'e1b'])
        self.assertEqual(finding['basis'], 'multiple_video_files_not_content_hash')

    def test_prefix_mismatch_unknown_episode_and_unknown_year(self):
        self.storage.rows['tv'][0]['name'] = '示例'
        self.storage.rows['season'] = [file('bad', '错误名字 S01E01.mkv', 'season'), file('bare', '02.mkv', 'season')]
        report = self.inspect()
        self.assertTrue({'title_prefix_mismatch', 'episode_unrecognized', 'work_year_unrecognized'} <= self.codes(report))

    def test_explicit_trailing_separator_year_is_recognized(self):
        for name in ('Havoc.2025', 'New.Gods.Yang.Jian.2022', '电影_2025', '电影-2025',
                     '电影 2025', '电影 (2025)', '电影（2025）', '1917 (2019)', '2001.1968'):
            with self.subTest(name=name):
                self.storage.rows['tv'][0]['name'] = name
                self.assertNotIn('work_year_unrecognized', self.codes(self.inspect()))

    def test_numeric_title_alone_never_becomes_its_own_release_year(self):
        for name in ('1917', '2001', '(2001)', '（2001）', ' 2001', '电影2025'):
            with self.subTest(name=name):
                self.storage.rows['tv'][0]['name'] = name
                self.assertIn('work_year_unrecognized', self.codes(self.inspect()))

    def test_season_conflict_detected(self):
        self.storage.rows['season'][0]['name'] = '示例 (2026) S02E01.mkv'
        self.assertIn('season_directory_conflict', self.codes(self.inspect()))

    def test_orphan_subtitle_does_not_establish_video_presence(self):
        self.storage.rows['season'].extend([file('sub1', '示例 (2026) S01E01.zh-CN.srt', 'season'),
                                            file('sub2', '示例 (2026) S01E02.srt', 'season')])
        report = self.inspect()
        orphan = [item for item in report['findings'] if item['code'] == 'orphan_subtitle']
        self.assertEqual([item['fids'] for item in orphan], [['sub2']])

    def test_zero_and_unknown_sizes_are_issues(self):
        self.storage.rows['season'][0]['size'] = 0
        self.storage.rows['season'].append(file('e2', '示例 (2026) S01E02.mkv', 'season', None))
        self.assertTrue({'zero_byte_file', 'unknown_file_size'} <= self.codes(self.inspect()))

    def test_empty_work_and_season_only_with_complete_inventory(self):
        self.storage.rows['season'] = []
        self.assertTrue({'empty_work_directory', 'empty_season_directory'} <= self.codes(self.inspect()))
        self.storage.rows['season'] = RuntimeError('secret payload must not appear')
        report = self.inspect()
        self.assertEqual(report['status'], 'INCOMPLETE')
        self.assertFalse({'empty_work_directory', 'empty_season_directory'} & self.codes(report))
        self.assertNotIn('secret payload', str(report))

    def test_archives_and_provable_part_gap_without_claiming_tail_completeness(self):
        self.storage.rows['season'].extend([file('a1', 'example.part01.rar', 'season'),
                                            file('a3', 'example.part03.rar', 'season')])
        report = self.inspect()
        gap = next(item for item in report['findings'] if item['code'] == 'archive_part_gap')
        self.assertEqual(gap['missing_parts'], [2])
        self.assertEqual(gap['tail_completeness'], 'unverified')
        self.assertIn('archive_remaining', self.codes(report))

    def test_legacy_archive_requires_anchor(self):
        self.storage.rows['season'].append(file('a', 'example.r00', 'season'))
        gap = next(item for item in self.inspect()['findings'] if item['code'] == 'archive_part_gap')
        self.assertTrue(gap['anchor_missing'])

    def test_scan_bounds_incomplete_and_never_healthy(self):
        for changes in ({'max_entries': 1}, {'max_depth': 0}):
            report = self.inspect(**changes)
            self.assertEqual(report['status'], 'INCOMPLETE')
            self.assertFalse(report['coverage']['scan_complete'])

    def test_scope_must_be_proven_and_never_lists_external_folder(self):
        report = self.inspect(folder_id='outside')
        self.assertEqual(report['status'], 'INCOMPLETE')
        self.assertFalse(report['coverage']['scope_verified'])
        self.assertNotIn('outside', self.storage.listed)
        report = self.inspect(folder_id='season')
        self.assertEqual(report['status'], 'HEALTHY', report)
        self.assertEqual(report['coverage']['video_files'], 1)

    def test_empty_selected_season_does_not_claim_whole_work_empty(self):
        self.storage.rows['show'].append(folder('season2', 'Season 02', 'show'))
        self.storage.rows['season2'] = []
        report = self.inspect(folder_id='season2')
        self.assertIn('empty_season_directory', self.codes(report))
        self.assertNotIn('empty_work_directory', self.codes(report))

    def test_excluded_test_library_is_not_scanned_or_flagged(self):
        self.storage.rows['root'].append(folder('test', '影库初始化测试', 'root'))
        self.storage.rows['test'] = AssertionError('excluded library read')
        report = self.inspect(excluded_ids=['test'])
        self.assertEqual(report['status'], 'HEALTHY')
        self.assertNotIn('test', self.storage.listed)

    def test_bad_parent_duplicate_id_and_wrong_type_are_incomplete(self):
        for broken in (dict(self.storage.rows['season'][0], parent_id='wrong'),
                       dict(self.storage.rows['season'][0], is_dir=1)):
            self.storage.rows['season'] = [broken]
            self.assertEqual(self.inspect()['status'], 'INCOMPLETE')
        self.storage.rows['season'] = [file('e1', '示例 (2026) S01E01.mkv', 'season')] * 2
        self.assertEqual(self.inspect()['status'], 'INCOMPLETE')

    def test_auth_and_risk_stop_further_provider_and_metadata_requests(self):
        for error, status in ((AuthenticationError('secret'), 'NEEDS_AUTH'),
                              (RiskControlError('secret'), 'RISK_CONTROL')):
            self.storage.listed.clear()
            self.storage.rows['tv'] = error
            resolver = Resolver()
            report = self.inspect(resolver=resolver)
            self.assertEqual(report['status'], status)
            self.assertEqual(self.storage.listed, ['root', 'tv'])
            self.assertEqual(resolver.calls, [])
            self.assertNotIn('secret', str(report))

    def test_expected_missing_and_future_remain_distinct_from_actual_inventory(self):
        resolver = Resolver()
        report = self.inspect(resolver=resolver)
        self.assertEqual(report['status'], 'HEALTHY', report)
        self.assertEqual(report['summary']['expected_missing'], 1)
        self.assertEqual(report['summary']['expected_future'], 1)
        self.assertEqual(report['coverage']['video_files'], 1)
        self.assertFalse(report['expected_schedule']['availability_verified'])
        self.assertEqual(report['expected_schedule']['missing'][0]['episode_key'], 'S01E02')

    def test_metadata_unknown_or_network_failure_is_not_healthy(self):
        for value in ({'status': 'NEEDS_INPUT'}, MetadataError('tmdb', 'upstream_error', status=503)):
            resolver = Resolver()
            resolver.result = value
            report = self.inspect(resolver=resolver)
            self.assertEqual(report['status'], 'INCOMPLETE')
            self.assertFalse(report['coverage']['metadata_complete'])

    def test_metadata_risk_stops_remaining_resolutions(self):
        self.storage.rows['season'].append(file('e2', '示例 (2026) S01E02.mkv', 'season'))
        resolver = Resolver()
        resolver.result = MetadataError('tmdb', 'risk_control', status=429)
        report = self.inspect(resolver=resolver)
        self.assertEqual(report['status'], 'RISK_CONTROL')
        self.assertEqual(resolver.calls, ['e1'])

    def test_deterministic_findings_and_markdown(self):
        self.storage.rows['season'].append(file('a', 'pack.7z.003', 'season'))
        a, b = self.inspect(), self.inspect()
        self.assertEqual(a, b)
        rendered = markdown(a)
        self.assertIn('云盘写入 0', rendered)
        self.assertIn('archive_part_gap', rendered)


if __name__ == '__main__':
    unittest.main()
