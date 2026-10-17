"""Durable operation evidence for the headless runner; never stores credentials."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SECRET = re.compile(r"cookie|authorization|(?:^|_)(?:stoken|token|password|passcode|secret)(?:$|_)|share_fid_token", re.I)


def safe_record(value):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if _SECRET.search(str(key)):
                raise ValueError("Credential fields must not enter the job journal")
            result[key] = safe_record(item)
        return result
    if isinstance(value, list):
        return [safe_record(item) for item in value]
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        parts = urlsplit(value)
        if parts.username or parts.password:
            raise ValueError("Credential URLs must not enter the job journal")
        query = [(key, "REDACTED" if key.lower() in {"pwd", "passcode", "token", "access_token"} else item)
                 for key, item in parse_qsl(parts.query, keep_blank_values=True)]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    return value


class JobJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.path.exists():
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        if self.path.stat().st_mode & 0o077:
            raise ValueError("Job journal must have private file permissions (0600)")
        self.connection = sqlite3.connect(self.path, timeout=10)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS operations(key TEXT PRIMARY KEY, record TEXT NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, key TEXT NOT NULL, record TEXT NOT NULL, created REAL NOT NULL);
        """)

    def get(self, key):
        row = self.connection.execute("SELECT record FROM operations WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, record):
        if not isinstance(record, dict):
            raise ValueError("Journal record must be an object")
        serialized = json.dumps(safe_record(record), ensure_ascii=False, allow_nan=False)
        now = time.time()
        with self.connection:
            self.connection.execute("INSERT INTO operations VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET record=excluded.record,updated=excluded.updated", (key, serialized, now))
            self.connection.execute("INSERT INTO events(key,record,created) VALUES(?,?,?)", (key, serialized, now))

    def close(self):
        self.connection.close()


@contextmanager
def run_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("另一个影库任务正在运行；本次没有开始云盘操作") from exc
        yield
    finally:
        os.close(descriptor)
