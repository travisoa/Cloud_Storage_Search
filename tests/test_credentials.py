"""Private credential files and file-based drive connections (no network)."""
from argparse import Namespace
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from media_helper import credentials, library_cli
from media_helper.library_registry import LibraryRegistry, file_drive_reference


class SecretDir(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.secrets = self.root / "secrets"
        patcher = patch.dict(os.environ, {credentials.ENV_DIR: str(self.secrets)})
        patcher.start()
        self.addCleanup(patcher.stop)


class CredentialFileTests(SecretDir):
    def test_write_creates_private_directories_and_file_and_reads_back(self):
        path = credentials.write_secret("drives/family.json", {"cookie": "a=b"})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.secrets.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(credentials.read_secret("drives/family.json"), {"cookie": "a=b"})
        self.assertTrue(credentials.exists("drives/family.json"))
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_world_readable_file_symlink_and_bad_names_are_refused(self):
        path = credentials.write_secret("tmdb.json", {"read_access_token": "t"})
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            credentials.read_secret("tmdb.json")
        path.chmod(0o600)
        link = self.secrets / "quark.json"
        link.symlink_to(path)
        with self.assertRaises(ValueError):
            credentials.read_secret("quark.json")
        for name in ("../x.json", "drives/../../x.json", "Quark.json", "a/b/c.json"):
            with self.assertRaises(ValueError):
                credentials.secret_path(name)

    def test_values_with_line_breaks_are_rejected(self):
        with self.assertRaises(ValueError):
            credentials.write_secret("quark.json", {"cookie": "a\nb"})

    def test_input_reads_one_stdin_line_when_not_a_terminal(self):
        with patch("sys.stdin", io.StringIO("cookie-value\n")):
            self.assertEqual(credentials.read_input("x"), "cookie-value")
        with patch("sys.stdin", io.StringIO("one\ntwo\n")), self.assertRaises(ValueError):
            credentials.read_input("x")


class Provider:
    instances = []

    def __init__(self, cookie=None, **_):
        self.cookie = cookie
        Provider.instances.append(self)

    def list_files(self, parent):
        if self.cookie == "other-account":
            return [{"fid": "x", "name": "x", "parent_id": parent, "is_dir": True}]
        return [{"fid": "lib", "name": "影库", "parent_id": parent, "is_dir": True}]


class FileDriveTests(SecretDir):
    def args(self, action, **extra):
        return Namespace(library_action=action, drive_id="family", cookie=action == "connect",
                         alist_storage_id=None, **extra)

    def run_cli(self, action, value):
        with patch("sys.stdin", io.StringIO(value + "\n")), patch("media_helper.providers.QuarkProvider", Provider):
            return library_cli.run_command(self.args(action), {"library_root_id": "root"}, self.root)

    def test_connect_verifies_read_only_then_saves_reference_without_value(self):
        result = self.run_cli("connect", "good-cookie")
        self.assertEqual(result["status"], "CONNECTED")
        self.assertEqual(result["drive"], file_drive_reference("family"))
        registry_text = (self.root / "runtime/libraries.json").read_text()
        self.assertNotIn("good-cookie", registry_text)
        self.assertNotIn("good-cookie", json.dumps(result))
        self.assertEqual(credentials.read_secret("drives/family.json"), {"cookie": "good-cookie"})
        drive = LibraryRegistry(self.root / "runtime/libraries.json").get_drive("family", {})
        with patch("media_helper.providers.QuarkProvider", Provider):
            self.assertEqual(library_cli._storage(drive).cookie, "good-cookie")

    def test_update_must_still_see_registered_libraries(self):
        self.run_cli("connect", "good-cookie")
        registry = LibraryRegistry(self.root / "runtime/libraries.json")
        registry.register({"library_id": "home", "drive_id": "family", "parent_id": "0", "library_root_id": "lib",
                           "name": "影库", "categories": {"movie": {"folder_id": "m", "name": "电影"},
                                                         "tv": {"folder_id": "t", "name": "电视剧"}},
                           "init_job_key": "library-init:" + "a" * 64, "test_only": False})
        with self.assertRaises(ValueError):
            self.run_cli("credential", "other-account")
        self.assertEqual(credentials.read_secret("drives/family.json"), {"cookie": "good-cookie"})
        self.assertEqual(self.run_cli("credential", "new-cookie")["status"], "CREDENTIAL_UPDATED")
        self.assertEqual(credentials.read_secret("drives/family.json"), {"cookie": "new-cookie"})

    def test_alist_drive_cannot_be_switched_to_file_and_unknown_drive_cannot_be_updated(self):
        with self.assertRaises(ValueError):
            self.run_cli("credential", "x")


if __name__ == "__main__":
    unittest.main()
