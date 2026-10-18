"""Version policy decisions survive workflow and subscription boundaries."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from media_helper.workflow import run
from media_helper.watching import run_subscription, select_ready
from tests.test_workflow import Journal, Metadata, Search, Storage, file, request
from tests.test_watching import SOURCE, WatchStorage, candidate, subscription


class WorkflowPolicyTests(unittest.TestCase):
    def setUp(self):
        self.storage, self.metadata, self.search, self.journal = Storage(), Metadata(), Search(), Journal()

    def plan(self, policy, *, apply=False):
        return run(request(version_policy=policy), self.storage, self.metadata, self.search, self.journal, apply=apply)

    def test_explicit_fallback_controls_whether_only_1080_can_be_saved(self):
        self.storage.shared = [file('lower', '测试剧.S01E02.1080p.mkv')]
        blocked = self.plan({}, apply=True)
        self.assertEqual(blocked['status'], 'NEEDS_INPUT', blocked)
        self.assertEqual(self.storage.saved, [])
        self.assertFalse(blocked['source_candidates'][0]['version_decision']['allowed'])
        allowed = self.plan({'allow_fallback': True}, apply=True)
        self.assertEqual(allowed['status'], 'COMPLETE', allowed)
        self.assertEqual(self.storage.saved, [['lower']])
        self.assertEqual(self.storage.folders['season'][0]['fid'], 'old-1')

    def test_resolution_wins_over_lower_quality_with_secondary_preferences(self):
        self.storage.shared = [file('high', '测试剧.S01E02.2160p.mkv'),
                               file('lower', '测试剧.S01E02.1080p.HEVC.HDR10.WEB-DL.mkv')]
        report = self.plan({'allow_fallback': True, 'preferred_codecs': ['HEVC'], 'hdr': 'prefer', 'preferred_keywords': ['WEB-DL']})
        self.assertEqual(report['status'], 'READY', report)
        self.assertEqual([row['source']['fid'] for row in report['plan']], ['high'])
        self.assertEqual(self.storage.saved, [])

    def test_codec_and_size_constraints_filter_before_any_selection_or_write(self):
        self.storage.shared = [file('small', '测试剧.S01E02.2160p.HEVC.mkv', size=10),
                               file('wrongcodec', '测试剧.S01E02.2160p.H264.mkv', size=2_000_000),
                               file('eligible', '测试剧.S01E02.2160p.HEVC.mkv', size=2_000_000)]
        report = self.plan({'preferred_codecs': ['HEVC'], 'require_codec': True, 'min_size_mb': 1})
        self.assertEqual(report['status'], 'READY', report)
        self.assertEqual([row['source']['fid'] for row in report['plan']], ['eligible'])
        self.assertEqual(sum(row['version_decision']['allowed'] for row in report['source_candidates']), 1)

    def test_unknown_tags_never_get_filled_from_preferences(self):
        self.storage.shared = [file('unknown', '测试剧.S01E02.mkv')]
        report = self.plan({'preferred_codecs': ['HEVC'], 'hdr': 'prefer'})
        self.assertEqual(report['status'], 'NEEDS_INPUT', report)
        info = report['source_candidates'][0]['media_info']
        self.assertIsNone(info.get('resolution'))
        self.assertIsNone(info.get('codec'))
        self.assertIsNone(info.get('hdr'))
        self.assertEqual(self.storage.saved, [])

    def test_existing_episode_is_kept_even_when_lower_than_policy(self):
        self.metadata.episodes = self.metadata.episodes[:1]
        report = self.plan({'resolution_order': ['2160p'], 'preferred_codecs': ['HEVC'], 'require_codec': True}, apply=True)
        self.assertEqual(report['status'], 'UP_TO_DATE', report)
        self.assertEqual((self.storage.saved, self.storage.renamed, self.storage.created), ([], [], []))
        self.assertEqual(self.search.calls, 0)


class WatchingPolicySelectionTests(unittest.TestCase):
    def choose(self, rows, policy):
        sub = subscription()
        req = {**sub['request'], 'version_policy': policy}
        return select_ready(req, {'missing': [{'episode_key': 'S01E02'}], 'source_candidates': rows}, sub)

    def explicit(self, identifier, **changes):
        return candidate(identifier, name='测试剧.S01E02.mkv', episode_key='S01E02', suggested_episode=None, **changes)

    def test_policy_resolution_beats_preferred_source_rank(self):
        lower = self.explicit('preferred-low', media_info={'resolution': '1080p'})
        high = self.explicit('fallback-high', media_info={'resolution': '2160p'}, parent_id='other',
                             share_source={'source_hash': 'b' * 64})
        selected, _, issues = self.choose([lower, high], {'allow_fallback': True})
        self.assertEqual(selected, {'S01E02': 'fallback-high'})
        self.assertEqual(issues, [])

    def test_same_resolution_still_prefers_confirmed_source(self):
        preferred = self.explicit('preferred', media_info={'resolution': '2160p'})
        fallback = self.explicit('fallback', media_info={'resolution': '2160p', 'codec': 'HEVC'}, parent_id='other',
                                 share_source={'source_hash': 'b' * 64})
        selected, _, _ = self.choose([preferred, fallback], {'preferred_codecs': ['HEVC']})
        self.assertEqual(selected, {'S01E02': 'preferred'})

    def test_no_fallback_and_size_or_codec_rejection_leave_no_automatic_choice(self):
        cases = [(self.explicit('low', media_info={'resolution': '1080p'}), {}),
                 (self.explicit('unknown-codec', media_info={'resolution': '2160p'}), {'preferred_codecs': ['HEVC'], 'require_codec': True}),
                 (self.explicit('small', media_info={'resolution': '2160p'}), {'min_size_mb': 1})]
        for row, policy in cases:
            with self.subTest(policy=policy):
                self.assertEqual(self.choose([row], policy), ({}, {}, []))


class WatchingPolicyCycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = {'library_root_id': 'root', 'pansou_url': 'http://127.0.0.1:8888'}
        self.storage, self.primary = WatchStorage(), Metadata()
        self.primary.source = 'tmdb'
        self.fallback = Mock()
        self.fallback.search.return_value = []
        self.updates = {'status': 'ok', 'automatic_transfer_allowed': True,
                        'series': {'id': 17, 'title': '测试剧', 'year': 2026, 'source': 'tmdb'},
                        'episodes': [dict(row, episode_key=f'S01E{row["episode"]:02d}') for row in self.primary.episodes],
                        'conflicts': [], 'errors': []}
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch('media_helper.headless_cli._prepare_identity', side_effect=lambda req, cfg: (deepcopy(req), None, self.primary, None)))
        stack.enter_context(patch('media_helper.headless_cli._storage', return_value=self.storage))
        stack.enter_context(patch('media_helper.watching.collect_updates', side_effect=lambda *args: deepcopy(self.updates)))
        stack.enter_context(patch('media_helper.watching.PanSouSearch', return_value=self.fallback))

    def cycle(self, policy):
        sub = subscription()
        sub['request']['version_policy'] = policy
        return run_subscription(sub, self.config, self.root, apply=True,
                                as_of=datetime(2026, 10, 17, tzinfo=timezone.utc))

    def test_live_cycle_applies_explicit_fallback_and_saves_only_missing(self):
        self.storage.shared = [file('lower', '测试剧.S01E02.1080p.mkv', size=2_000_000)]
        report = self.cycle({'allow_fallback': True})
        self.assertEqual(report['status'], 'COMPLETE', report)
        self.assertEqual(self.storage.saved, [['lower']])
        self.assertEqual(self.storage.renamed, [('new-lower', '测试剧 (2026)S01E02_1080P.mkv')])
        self.assertEqual(report['missing'], [])

    def test_live_cycle_waits_for_allowed_version_without_writing(self):
        self.storage.shared = [file('lower', '测试剧.S01E02.1080p.mkv', size=2_000_000)]
        report = self.cycle({'allow_fallback': False})
        self.assertEqual(report['status'], 'WAITING', report)
        self.assertEqual((self.storage.saved, self.storage.renamed, self.storage.created), ([], [], []))
        self.assertFalse(report['writes_attempted'])


if __name__ == '__main__':
    unittest.main()
