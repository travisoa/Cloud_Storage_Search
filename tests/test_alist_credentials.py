"""All DB/WAL files here are synthetic fixtures, never the user's database."""

import json
from pathlib import Path
import sqlite3
import struct
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

from media_helper.alist_credentials import (AListCredentialError, AListCredentials,
                                            DATABASE_PATH, SnapshotBusyError,
                                            SnapshotIntegrityError, UnsupportedStorageError,
                                            committed_image)


def _sum(content, endian, seed=(0, 0)):
    a, b = seed
    for offset in range(0, len(content), 8):
        a = (a + int.from_bytes(content[offset:offset + 4], endian) + b) % (2**32)
        b = (b + int.from_bytes(content[offset + 4:offset + 8], endian) + a) % (2**32)
    return a, b


def rewrite_wal(wal, *, commits=None, endian=None):
    """Re-sign a fixture independently, including alternate checksum endian."""
    out = bytearray(wal)
    magic = int.from_bytes(out[:4], "big")
    order = endian or ("little" if magic == 0x377F0682 else "big")
    out[:4] = (0x377F0682 if order == "little" else 0x377F0683).to_bytes(4, "big")
    state = _sum(out[:24], order)
    out[24:32] = struct.pack(">II", *state)
    size = int.from_bytes(out[8:12], "big")
    for index, offset in enumerate(range(32, len(out) - size - 24 + 1, size + 24)):
        if commits is not None:
            out[offset + 4:offset + 8] = int(commits.get(index, 0)).to_bytes(4, "big")
        state = _sum(out[offset:offset + 8], order, state)
        state = _sum(out[offset + 24:offset + 24 + size], order, state)
        out[offset + 16:offset + 24] = struct.pack(">II", *state)
    return bytes(out)


class Fixture:
    def __init__(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "fixture.db"
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA wal_autocheckpoint=0")
        self.connection.execute("CREATE TABLE x_storages(id INTEGER PRIMARY KEY,mount_path TEXT,driver TEXT,addition TEXT,disabled INTEGER,status TEXT)")
        self.connection.execute("CREATE TABLE unrelated_users(id INTEGER PRIMARY KEY,private_value TEXT)")
        self.connection.execute("INSERT INTO unrelated_users VALUES(1,'must-not-query')")
        self.insert(1, "/Quark", "Quark", "old-cookie")
        self.connection.commit()
        self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.base = self.path.read_bytes()
        self.update_cookie("new-cookie")
        self.first_wal = Path(str(self.path) + "-wal").read_bytes()

    def insert(self, identifier, mount, driver, cookie=None, disabled=0, addition=None):
        addition = addition if addition is not None else {"cookie": cookie} if cookie else {"access_token": "not-cookie"}
        self.connection.execute("INSERT INTO x_storages VALUES(?,?,?,?,?,?)", (
            identifier, mount, driver, json.dumps(addition), disabled, "work"))

    def update_cookie(self, cookie):
        self.connection.execute("UPDATE x_storages SET addition=? WHERE id=1", (json.dumps({"cookie": cookie}),))
        self.connection.commit()

    def pair(self):
        return self.path.read_bytes(), Path(str(self.path) + "-wal").read_bytes()

    def close(self):
        self.connection.close()
        self.directory.cleanup()


class Runner:
    def __init__(self, pairs):
        self.pairs = pairs
        self.calls = []

    def __call__(self, command, **kwargs):
        index = len(self.calls)
        self.calls.append((command, kwargs))
        pair = self.pairs[min(index // 2, len(self.pairs) - 1)]
        content = pair[index % 2]
        return SimpleNamespace(returncode=0, stdout=content, stderr=b"")


class AListCredentialTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()

    def tearDown(self):
        self.fixture.close()

    def source(self, pair=None):
        runner = Runner([pair or self.fixture.pair()])
        return AListCredentials(runner), runner

    def test_main_database_lags_wal_but_committed_cookie_is_reconstructed_in_memory(self):
        original = self.fixture.pair()
        with AListCredentials(Runner([original])) as source:
            self.assertEqual(source.load_cookie(1), "new-cookie")
            safe = source.list_safe()
            self.assertEqual(safe, [{"storage_id": 1, "mount_path": "/Quark", "driver": "Quark", "cookie_present": True, "disabled": False}])
            self.assertNotIn("new-cookie", json.dumps(safe))
            self.assertNotIn("new-cookie", repr(source))
            self.assertEqual(source._connection.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.DatabaseError):
                source._connection.execute("SELECT * FROM unrelated_users").fetchall()
            with self.assertRaises(sqlite3.DatabaseError):
                source._connection.execute("DELETE FROM x_storages")
        self.assertEqual(self.fixture.pair(), original)

    def test_only_fixed_read_commands_capture_binary_and_no_shell(self):
        source, runner = self.source()
        source.close()
        self.assertEqual(len(runner.calls), 4)
        for index, (command, kwargs) in enumerate(runner.calls):
            self.assertEqual(command, ["container", "exec", "alist", "cat", DATABASE_PATH + ("-wal" if index % 2 else "")])
            self.assertIs(kwargs["shell"], False)
            self.assertTrue(kwargs["capture_output"])
            self.assertFalse(kwargs["check"])

    def test_checksum_both_little_and_big_endian_are_supported(self):
        database, wal = self.fixture.pair()
        for endian in ("little", "big"):
            with self.subTest(endian=endian):
                with AListCredentials(Runner([(database, rewrite_wal(wal, endian=endian))])) as source:
                    self.assertEqual(source.load_cookie("1"), "new-cookie")

    def test_valid_uncommitted_transaction_is_never_applied(self):
        database, wal = self.fixture.pair()
        pending = rewrite_wal(wal, commits={})
        with AListCredentials(Runner([(database, pending)])) as source:
            self.assertEqual(source.load_cookie(1), "old-cookie")

    def test_tail_after_last_commit_is_not_applied(self):
        database, wal = self.fixture.pair()
        size = int.from_bytes(wal[8:12], "big")
        first_frame_count = (len(wal) - 32) // (24 + size)
        original_commits = {}
        for index, offset in enumerate(range(32, len(wal) - size - 24 + 1, size + 24)):
            original_commits[index] = int.from_bytes(wal[offset + 4:offset + 8], "big")
        self.fixture.update_cookie("uncommitted-later-cookie")
        _, later = self.fixture.pair()
        self.assertGreater((len(later) - 32) // (24 + size), first_frame_count)
        pending_tail = rewrite_wal(later, commits=original_commits)
        with AListCredentials(Runner([(database, pending_tail)])) as source:
            self.assertEqual(source.load_cookie(1), "new-cookie")

    def test_header_and_active_frame_checksum_corruption_are_rejected(self):
        database, wal = self.fixture.pair()
        for position in (12, 32 + 24 + 100):
            bad = bytearray(wal)
            bad[position] ^= 1
            with self.subTest(position=position):
                with self.assertRaises(SnapshotIntegrityError):
                    AListCredentials(Runner([(database, bytes(bad))]))

    def test_stale_salt_tail_is_ignored_instead_of_applied(self):
        database, wal = self.fixture.pair()
        size = int.from_bytes(wal[8:12], "big")
        stale_frame = bytearray(wal[32:32 + size + 24])
        stale_frame[8] ^= 1
        with AListCredentials(Runner([(database, wal + bytes(stale_frame))])) as source:
            self.assertEqual(source.load_cookie(1), "new-cookie")

    def test_no_commit_and_empty_wal_use_base_database(self):
        with AListCredentials(Runner([(self.fixture.base, b"")])) as source:
            self.assertEqual(source.load_cookie(1), "old-cookie")

    def test_two_snapshot_mismatches_fail_without_selecting_credentials(self):
        first = self.fixture.pair()
        self.fixture.update_cookie("second-cookie")
        second = self.fixture.pair()
        self.fixture.update_cookie("third-cookie")
        third = self.fixture.pair()
        runner = Runner([first, second, third])
        with self.assertRaises(SnapshotBusyError) as caught:
            AListCredentials(runner)
        self.assertEqual(len(runner.calls), 6)
        self.assertNotIn("cookie", str(caught.exception).lower())

    def test_one_mismatch_can_settle_but_requires_two_identical_pairs(self):
        first = self.fixture.pair()
        self.fixture.update_cookie("settled-cookie")
        second = self.fixture.pair()
        runner = Runner([first, second, second])
        with AListCredentials(runner) as source:
            self.assertEqual(source.load_cookie(1), "settled-cookie")
        self.assertEqual(len(runner.calls), 6)

    def test_multiple_accounts_require_explicit_selection_and_disabled_or_token_drivers_are_refused(self):
        self.fixture.insert(2, "/Second", "Quark", "second-account")
        self.fixture.insert(3, "/Disabled", "Quark", "disabled-secret", disabled=1)
        self.fixture.insert(4, "/Open", "QuarkOpen", addition={"access_token": "never-return-this"})
        self.fixture.insert(5, "/Missing", "Quark", addition={})
        self.fixture.connection.commit()
        with AListCredentials(Runner([self.fixture.pair()])) as source:
            self.assertEqual(source.load_cookie(2), "second-account")
            for identifier in (None, True, 3, 4, 5, 999, "1 OR 1=1"):
                with self.subTest(identifier=identifier):
                    with self.assertRaises(UnsupportedStorageError):
                        source.load_cookie(identifier)
            safe = source.list_safe()
            self.assertTrue(safe[2]["disabled"])
            self.assertFalse(safe[3]["cookie_present"])
            self.assertNotIn("second-account", json.dumps(safe))
            self.assertNotIn("never-return-this", json.dumps(safe))

    def test_command_failure_and_timeout_are_sanitized(self):
        def bad_runner(*args, **kwargs):
            return SimpleNamespace(returncode=1, stdout=b"secret-database", stderr=b"cookie=private")
        def timed_out(*args, **kwargs):
            raise subprocess.TimeoutExpired("cookie=private", 20, output=b"secret-database")
        for runner in (bad_runner, timed_out):
            with self.subTest(runner=runner.__name__):
                with self.assertRaises(AListCredentialError) as caught:
                    AListCredentials(runner)
                self.assertNotIn("private", str(caught.exception))
                self.assertNotIn("secret-database", str(caught.exception))

    def test_database_and_path_validation_never_reads_an_arbitrary_container(self):
        runner = Runner([self.fixture.pair()])
        with self.assertRaises(AListCredentialError):
            AListCredentials(runner, container_name="alist; echo unsafe")
        with self.assertRaises(AListCredentialError):
            AListCredentials(runner, database_path="/etc/passwd")
        self.assertEqual(runner.calls, [])
        with self.assertRaises(SnapshotIntegrityError):
            committed_image(b"not-a-database", b"")


if __name__ == "__main__":
    unittest.main()
