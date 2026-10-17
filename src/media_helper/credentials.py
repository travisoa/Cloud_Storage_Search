"""Private credential files in one directory, the same on macOS and Linux.

The directory defaults to ``runtime/secrets`` in the project and can be moved
with ``MEDIA_HELPER_SECRETS_DIR`` (for example to a mounted volume on a Linux
host). Files are JSON objects with mode 0600 inside a 0700 directory owned by
the current user. Values are never printed, logged or accepted as arguments.
"""
from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
import re
import stat
import sys

ROOT = Path(__file__).resolve().parents[2]
ENV_DIR = "MEDIA_HELPER_SECRETS_DIR"
_NAME = re.compile(r"(?:drives/)?[a-z][a-z0-9_-]{0,47}\.json")


def secrets_dir():
    configured = os.environ.get(ENV_DIR)
    return Path(configured).expanduser() if configured else ROOT / "runtime/secrets"


def secret_path(name):
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError("凭据文件名无效")
    return secrets_dir() / name


def _owned_private(path, *, directory):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise ValueError("凭据路径不能是符号链接")
    if directory != stat.S_ISDIR(info.st_mode) or (not directory and not stat.S_ISREG(info.st_mode)):
        raise ValueError("凭据路径类型无效")
    if info.st_mode & 0o077:
        raise ValueError("凭据目录须为 0700、文件须为 0600")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ValueError("凭据须属于当前运行用户")


def exists(name):
    return secret_path(name).is_file()


def read_secret(name):
    path = secret_path(name)
    try:
        for directory in dict.fromkeys((secrets_dir(), path.parent)):
            _owned_private(directory, directory=True)
        _owned_private(path, directory=False)
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError("尚未配置此凭据") from None
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("凭据文件不可读或格式无效") from None
    if not isinstance(data, dict):
        raise ValueError("凭据文件格式无效")
    return data


def write_secret(name, value):
    """Atomically replace one private JSON credential file."""
    if not isinstance(value, dict) or not value or any(
            not isinstance(item, str) or not item or "\n" in item or "\r" in item for item in value.values()):
        raise ValueError("凭据内容无效")
    path, base = secret_path(name), secrets_dir()
    for directory in dict.fromkeys((base, path.parent)):
        if not directory.exists():
            directory.mkdir(mode=0o700, parents=directory == base)
        os.chmod(directory, 0o700)
        _owned_private(directory, directory=True)
    if path.is_symlink():
        raise ValueError("凭据路径不能是符号链接")
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.unlink(missing_ok=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def read_input(prompt):
    """Hidden terminal input, or one line from stdin for headless Linux setup."""
    if sys.stdin.isatty():
        value = getpass.getpass(prompt)
    else:
        value = sys.stdin.readline()
        if sys.stdin.read(1):
            raise ValueError("标准输入只能包含一行凭据")
    value = value.strip()
    if not value or "\n" in value or "\r" in value or len(value) > 65536:
        raise ValueError("凭据为空或格式无效")
    return value
