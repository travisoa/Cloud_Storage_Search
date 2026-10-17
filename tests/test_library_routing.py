"""An initialized library is an explicit request scope, never a global switch."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from media_helper.headless_cli import main
from media_helper.library_registry import LibraryRegistry, drive_reference


class LibraryRoutingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.base = {'provider': 'quark', 'credential_source': {'type': 'alist', 'storage_id': 1},
                     'library_root_id': 'old-root', 'categories': {'tv': {'folder_id': 'old-tv'}},
                     'metadata_source': 'auto', 'pansou_url': 'http://127.0.0.1:8888'}
        self.original = deepcopy(self.base)
        self.registry = LibraryRegistry(self.root / 'runtime/libraries.json')
        self.registry.connect(drive_reference('new-drive', 2))
        self.registry.register({'library_id': 'family', 'drive_id': 'new-drive',
            'parent_id': '0', 'library_root_id': 'new-root', 'name': '影库',
            'categories': {'tv': {'folder_id': 'new-tv', 'name': '电视剧'},
                           'movie': {'folder_id': 'new-movie', 'name': '电影'}},
            'init_job_key': 'library-init:' + 'a' * 64, 'test_only': False})

    def command(self, args):
        output = io.StringIO()
        with patch('media_helper.headless_cli.ROOT', self.root), patch('media_helper.headless_cli._config', return_value=self.base), redirect_stdout(output):
            code = main(args)
        return code, json.loads(output.getvalue())

    def test_library_request_uses_new_root_and_account_with_plan_dominance(self):
        with patch('media_helper.headless_cli._run', return_value={'status': 'READY'}) as run:
            code, _ = self.command(['library', 'request', 'family', '帮我查下《测试剧》，存到我的网盘', '--plan'])
        self.assertEqual(code, 0)
        self.assertFalse(run.call_args.kwargs['apply'])
        selected = run.call_args.kwargs['config']
        self.assertEqual(selected['library_root_id'], 'new-root')
        self.assertEqual(selected['credential_source']['storage_id'], 2)
        self.assertEqual(selected['categories']['tv']['folder_id'], 'new-tv')
        self.assertEqual(self.base, self.original)

    def test_selected_library_recovery_is_routed_without_changing_default(self):
        with patch('media_helper.headless_cli._task_command', return_value={'status': 'COMPLETE'}) as command:
            code, _ = self.command(['reconcile', 'a' * 32, '--library-id', 'family'])
        self.assertEqual(code, 0)
        self.assertEqual(command.call_args.kwargs['config']['library_root_id'], 'new-root')
        self.assertEqual(self.base, self.original)

    def test_omitted_library_recovery_keeps_existing_default(self):
        with patch('media_helper.headless_cli._task_command', return_value={'status': 'COMPLETE'}) as command:
            self.command(['reconcile', 'a' * 32])
        self.assertNotIn('config', command.call_args.kwargs)


if __name__ == '__main__':
    unittest.main()
