"""Quark move submission contract; no real account operations."""
import unittest

from media_helper.providers import (AuthenticationError, ProviderError, QuarkProvider,
                                    RiskControlError, UnknownOutcome)
from tests.test_providers import FakeTransport, ok


class LibraryMoveTests(unittest.TestCase):
    def provider(self, response):
        transport = FakeTransport(response)
        return QuarkProvider(cookie='fake-private-cookie', transport=transport), transport

    def test_single_move_post_has_safe_submission_evidence(self):
        provider, transport = self.provider(ok({'task_id': 'move-task'}))
        result = provider.move('video', 'new-parent')
        self.assertTrue(provider.capabilities['move'])
        self.assertEqual(result, {'fid': 'video', 'target_parent_id': 'new-parent',
                                  'status': 'submitted', 'task_id': 'move-task'})
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0]['payload'], {'action_type': 1, 'exclude_fids': [],
                         'filelist': ['video'], 'to_pdir_fid': 'new-parent'})
        self.assertNotIn('fake-private-cookie', str(result))

    def test_lost_or_bad_response_never_retries(self):
        for response in (TimeoutError(), ok({'task_id': '../bad'})):
            with self.subTest(response=type(response).__name__):
                provider, transport = self.provider(response)
                with self.assertRaises(UnknownOutcome):
                    provider.move('video', 'new-parent')
                self.assertEqual(len(transport.calls), 1)

    def test_auth_risk_stop_and_invalid_ids_send_nothing(self):
        for status, expected in ((401, AuthenticationError), (429, RiskControlError)):
            provider, transport = self.provider({'_http_status': status, 'code': status})
            with self.assertRaises(expected):
                provider.move('video', 'new-parent')
            self.assertEqual(len(transport.calls), 1)
        for fid, parent in (('../video', 'parent'), ('same', 'same')):
            provider, transport = self.provider(ok())
            with self.assertRaises(ProviderError):
                provider.move(fid, parent)
            self.assertEqual(transport.calls, [])


if __name__ == '__main__':
    unittest.main()
