"""Read an explicitly selected AList Quark Cookie without copying it to disk.

The container and SQLite files are only read with ``container exec ... cat``.
Two identical consecutive DB/WAL pairs are required. Committed WAL pages are
validated and overlaid in memory according to SQLite's published file format:
https://www.sqlite.org/fileformat.html#walformat

Only list_safe() results may be printed. load_cookie() returns a secret directly
to the caller's in-memory provider; it must not be printed, repr'd or persisted.
"""

from __future__ import annotations

import json
import re
import sqlite3
import struct
import subprocess


CONTAINER_NAME = "alist"
DATABASE_PATH = "/opt/alist/data/data.db"
MAX_IMAGE_BYTES = 256 * 1024 * 1024
_SQLITE_MAGIC = b"SQLite format 3\x00"
_WAL_VERSION = 3007000


class AListCredentialError(RuntimeError):
    """A sanitized error without raw database, command or stderr content."""


class SnapshotBusyError(AListCredentialError):
    pass


class SnapshotIntegrityError(AListCredentialError):
    pass


class UnsupportedStorageError(AListCredentialError):
    pass


def _checksum(content, endian: str, seed=(0, 0)) -> tuple[int, int]:
    if len(content) % 8:
        raise SnapshotIntegrityError("SQLite WAL checksum input is incomplete")
    first, second = seed
    for x0, x1 in struct.iter_unpack(endian + "II", content):
        first = (first + x0 + second) & 0xFFFFFFFF
        second = (second + x1 + first) & 0xFFFFFFFF
    return first, second


def _database_page_size(database: bytes | bytearray) -> int:
    if len(database) < 100 or database[:16] != _SQLITE_MAGIC:
        raise SnapshotIntegrityError("AList database has an invalid SQLite header")
    size = int.from_bytes(database[16:18], "big")
    size = 65536 if size == 1 else size
    if size < 512 or size > 65536 or size & (size - 1):
        raise SnapshotIntegrityError("AList database page size is invalid")
    if len(database) % size:
        raise SnapshotIntegrityError("AList database ends in an incomplete page")
    if database[18] not in {1, 2} or database[19] not in {1, 2}:
        raise SnapshotIntegrityError("AList database format version is unsupported")
    return size


def committed_image(database: bytes, wal: bytes) -> bytearray:
    """Return a standalone memory image; uncommitted/stale WAL tails are ignored.

    A salt mismatch terminates the current WAL generation, as SQLite specifies.
    A checksum mismatch in the active generation fails closed instead of
    silently presenting an older Cookie. No original byte buffer is modified.
    """
    if not isinstance(database, bytes) or not isinstance(wal, bytes):
        raise SnapshotIntegrityError("Snapshot inputs must be binary data")
    if len(database) > MAX_IMAGE_BYTES or len(wal) > MAX_IMAGE_BYTES:
        raise SnapshotIntegrityError("AList snapshot exceeds the in-memory size limit")
    page_size = _database_page_size(database)
    image = bytearray(database)
    if wal:
        if len(wal) < 32:
            raise SnapshotIntegrityError("SQLite WAL header is incomplete")
        magic, version, wal_page_size, _, salt1, salt2, check1, check2 = struct.unpack_from(
            ">8I", wal, 0)
        if magic not in {0x377F0682, 0x377F0683} or version != _WAL_VERSION:
            raise SnapshotIntegrityError("SQLite WAL header format is unsupported")
        if wal_page_size != page_size:
            raise SnapshotIntegrityError("SQLite database and WAL page sizes differ")
        endian = "<" if magic == 0x377F0682 else ">"
        state = _checksum(memoryview(wal)[:24], endian)
        if state != (check1, check2):
            raise SnapshotIntegrityError("SQLite WAL header checksum is invalid")
        frame_size = 24 + page_size
        frames: list[tuple[int, int]] = []
        last_commit_frames = 0
        committed_pages = None
        for offset in range(32, len(wal) - frame_size + 1, frame_size):
            page_number, db_size, frame_salt1, frame_salt2, frame_check1, frame_check2 = struct.unpack_from(
                ">6I", wal, offset)
            if (frame_salt1, frame_salt2) != (salt1, salt2):
                # WAL reset can leave an older generation after the valid prefix.
                break
            if not 1 <= page_number <= 0xFFFFFFFE:
                raise SnapshotIntegrityError("SQLite WAL contains an invalid page number")
            page_offset = offset + 24
            state = _checksum(memoryview(wal)[offset:offset + 8], endian, state)
            state = _checksum(memoryview(wal)[page_offset:page_offset + page_size], endian, state)
            if state != (frame_check1, frame_check2):
                raise SnapshotIntegrityError("SQLite WAL frame checksum is invalid")
            frames.append((page_number, page_offset))
            if db_size:
                if db_size > 0xFFFFFFFE or db_size * page_size > MAX_IMAGE_BYTES:
                    raise SnapshotIntegrityError("SQLite WAL commit exceeds the in-memory size limit")
                last_commit_frames = len(frames)
                committed_pages = db_size
        if committed_pages is not None:
            final_size = committed_pages * page_size
            # Truncation is governed by the last valid commit, never a pending
            # frame. Ignore pages removed by a committed shrink transaction.
            del image[final_size:]
            if len(image) < final_size:
                image.extend(b"\x00" * (final_size - len(image)))
            for page_number, page_offset in frames[:last_commit_frames]:
                if page_number <= committed_pages:
                    begin = (page_number - 1) * page_size
                    image[begin:begin + page_size] = wal[page_offset:page_offset + page_size]
    if _database_page_size(image) != page_size:
        raise SnapshotIntegrityError("SQLite WAL changed the database page format")
    # deserialize() cannot open an in-memory WAL sidecar. Only the private copy
    # is marked rollback format after all committed pages have been applied.
    image[18] = image[19] = 1
    return image


class AListCredentials:
    def __init__(self, runner=None, *, container_name=CONTAINER_NAME,
                 database_path=DATABASE_PATH, timeout=20):
        if container_name != CONTAINER_NAME or database_path != DATABASE_PATH:
            raise AListCredentialError("Only the existing alist container and its fixed database path are supported")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 60:
            raise AListCredentialError("Invalid bounded container read timeout")
        self._runner = runner or subprocess.run
        self._timeout = timeout
        self._connection = None
        if not hasattr(sqlite3.Connection, "deserialize"):
            raise AListCredentialError("This Python runtime does not support safe in-memory SQLite deserialization")
        database, wal = self._stable_snapshot()
        image = committed_image(database, wal)
        try:
            connection = sqlite3.connect(":memory:")
            connection.deserialize(image)
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            checks = connection.execute("PRAGMA integrity_check").fetchall()
            if checks != [("ok",)]:
                raise SnapshotIntegrityError("The in-memory AList snapshot failed SQLite integrity_check")
            # Permit metadata and the explicitly requested table only. A view
            # cannot silently turn this into a read of another user's table.
            def authorizer(action, table, column, database_name, source):
                if action == sqlite3.SQLITE_READ and table not in {"x_storages", "sqlite_master", "sqlite_schema"}:
                    return sqlite3.SQLITE_DENY
                if action in {sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH}:
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            connection.set_authorizer(authorizer)
            connection.row_factory = sqlite3.Row
            # Check the exact supported schema without reading other tables.
            connection.execute("SELECT id,mount_path,driver,addition,disabled,status FROM x_storages LIMIT 0")
            self._connection = connection
        except AListCredentialError:
            if "connection" in locals():
                connection.close()
            raise
        except sqlite3.Error:
            if "connection" in locals():
                connection.close()
            raise SnapshotIntegrityError("The AList storage schema or in-memory snapshot is not readable") from None

    def __repr__(self):
        return "<AListCredentials in_memory=True secret_output=False>"

    def _read(self, path, *, missing_ok=False):
        try:
            result = self._runner(["container", "exec", CONTAINER_NAME, "cat", path],
                                  capture_output=True, check=False, timeout=self._timeout,
                                  stdin=subprocess.DEVNULL, shell=False)
        except Exception:
            raise AListCredentialError("Read-only access to the existing AList database was not available") from None
        # cat reports ENOENT without exposing file contents. Accept only its
        # exact known missing-file diagnostics for the optional fixed WAL;
        # permissions, container failures and unknown/localized errors fail closed.
        missing_errors = {
            ("cat: " + path + ": No such file or directory\n").encode(),
            ("cat: can't open '" + path + "': No such file or directory\n").encode(),
        }
        if (missing_ok and path == DATABASE_PATH + "-wal"
                and getattr(result, "returncode", None) == 1
                and getattr(result, "stdout", None) == b""
                and getattr(result, "stderr", None) in missing_errors):
            return b""
        if (getattr(result, "returncode", None) != 0
                or not isinstance(getattr(result, "stdout", None), bytes)):
            raise AListCredentialError("Read-only access to the existing AList database files failed")
        content = result.stdout
        if len(content) > MAX_IMAGE_BYTES:
            raise AListCredentialError("AList database files exceed the in-memory size limit")
        return content

    def _read_pair(self):
        return self._read(DATABASE_PATH), self._read(DATABASE_PATH + "-wal", missing_ok=True)

    def _stable_snapshot(self):
        previous = self._read_pair()
        for _ in range(2):
            current = self._read_pair()
            if current == previous:
                return current
            previous = current
        raise SnapshotBusyError("AList database changed during consecutive reads; no credential was selected")

    def _query(self, suffix="", values=()):
        if self._connection is None:
            raise AListCredentialError("The in-memory AList credential source is closed")
        try:
            return self._connection.execute(
                "SELECT id,mount_path,driver,addition,disabled,status FROM x_storages " + suffix, values).fetchall()
        except sqlite3.Error:
            raise AListCredentialError("The in-memory AList storage table could not be read") from None

    @staticmethod
    def _addition(row):
        try:
            data = json.loads(row["addition"]) if isinstance(row["addition"], str) else {}
            return data if isinstance(data, dict) else {}
        except (ValueError, TypeError):
            return {}

    @staticmethod
    def _cookie(row):
        addition = AListCredentials._addition(row)
        value = addition.get("cookie", addition.get("Cookie"))
        return value.strip() if isinstance(value, str) else ""

    @staticmethod
    def _disabled(row):
        return row["disabled"] not in (None, 0, False, "0")

    def list_safe(self) -> list[dict]:
        return [{"storage_id": row["id"], "mount_path": row["mount_path"], "driver": row["driver"],
                 "cookie_present": bool(self._cookie(row)), "disabled": self._disabled(row)}
                for row in self._query("ORDER BY id")]

    def load_cookie(self, storage_id) -> str:
        if isinstance(storage_id, bool) or not isinstance(storage_id, (int, str)) or not re.fullmatch(r"[1-9]\d{0,17}", str(storage_id)):
            raise UnsupportedStorageError("Specify one explicit positive storage_id; no account is selected by default")
        rows = self._query("WHERE id=?", (int(storage_id),))
        if len(rows) != 1:
            raise UnsupportedStorageError("The selected AList storage does not exist or is not unique")
        row = rows[0]
        if not isinstance(row["driver"], str) or row["driver"].strip().casefold() != "quark":
            raise UnsupportedStorageError("Only the Quark Cookie driver is supported; open/token drivers cannot be reused")
        if self._disabled(row):
            raise UnsupportedStorageError("The selected AList storage is disabled")
        cookie = self._cookie(row)
        if not cookie or len(cookie) > 65536 or "\n" in cookie or "\r" in cookie:
            raise UnsupportedStorageError("The selected Quark storage has no usable Cookie")
        return cookie

    def close(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

