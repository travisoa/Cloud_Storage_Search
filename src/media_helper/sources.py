"""Credential-free references to explicitly selected Quark share sources.

These records describe a source to enumerate again; they never prove that a
share is still available, that its label is a stream measurement, or that a
new episode may be saved. The workflow makes those checks on every run.
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import posixpath
import re
import unicodedata
from urllib.parse import urlsplit


_SECRET = re.compile(r"cookie|auth|token|password|passcode|secret|credential|api[-_]?key|signature|(?:^|_)(?:pwd|key|sig)(?:$|_)", re.I)
_PASSWORD_HINT = re.compile(r"提取[码碼]|密[码碼]|解[压壓][码碼]|password|passcode|(?:^|[\s:：?&])pwd\s*[=:：]", re.I)
_QUALITY_TAGS = {"4320p", "2160p", "1440p", "1080p", "720p", "576p", "480p",
                 "HEVC", "H.264", "AV1", "Dolby Vision", "HDR10+", "HDR10", "HDR", "HLG", "SDR"}


def _reject_secrets(value):
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _SECRET.search(str(key)):
                raise ValueError("分享来源不能包含认证信息、密码或提取码")
            _reject_secrets(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_secrets(item)


def _text(value, field, limit):
    if (not isinstance(value, str) or len(value) > limit
            or any(unicodedata.category(c) in {"Cc", "Cs"} for c in value)
            or _PASSWORD_HINT.search(value)):
        raise ValueError("分享来源的" + field + "无效或包含密码提示")
    return unicodedata.normalize("NFC", value.strip())


def source_digest(url):
    """Hash public URL identity only; query codes never become verifiers.

    Canonical password-free share URLs retain their existing candidate IDs.
    This is an audit identity, not permission to reuse a password share.
    """
    try:
        parts = urlsplit(url) if isinstance(url, str) else None
        public_url = (parts.scheme + "://" + (parts.hostname or "") + parts.path
                      if parts else "")
    except ValueError:
        public_url = ""
    return hashlib.sha256(json.dumps(public_url, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def safe_source(source, *, title=None, parent_id=None, path=None, quality_tags=None):
    """Return a whitelisted source, rejecting password-bearing shares.

    ``source_hash`` deliberately uses the same JSON-string digest as workflow
    candidate IDs. No URL query is discarded: query-bearing URLs are rejected
    rather than accidentally treating a password share as password-free.
    """
    if isinstance(source, str):
        source = {"url": source}
    if not isinstance(source, Mapping):
        raise ValueError("分享来源必须是链接或来源记录")
    _reject_secrets(source)
    url = source.get("url")
    if not isinstance(url, str):
        raise ValueError("分享来源需要有效夸克链接")
    try:
        parts = urlsplit(url)
        valid = (parts.scheme == "https" and parts.netloc == "pan.quark.cn"
                 and not parts.query and not parts.fragment
                 and url == "https://pan.quark.cn" + parts.path
                 and re.fullmatch(r"/s/[A-Za-z0-9]+", parts.path))
    except ValueError:
        valid = False
    if not valid or url != url.strip() or any(ord(c) < 33 for c in url):
        raise ValueError("订阅来源须为无密码、无参数的 HTTPS pan.quark.cn 分享链接")
    digest = source_digest(url)
    if source.get("source_hash") is not None and source["source_hash"] != digest:
        raise ValueError("分享来源标识与链接不一致")
    result = {"url": url, "title": _text(title if title is not None else source.get("title", ""), "标题", 500),
              "source_hash": digest}
    parent_id = parent_id if parent_id is not None else source.get("parent_id")
    if parent_id is not None:
        if not isinstance(parent_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", parent_id):
            raise ValueError("分享来源目录 ID 无效")
        result["parent_id"] = parent_id
    path = path if path is not None else source.get("path")
    if path is not None:
        result["path"] = _text(path, "目录路径", 2000)
    quality_tags = quality_tags if quality_tags is not None else source.get("quality_tags")
    if quality_tags is not None:
        if (not isinstance(quality_tags, list)
                or any(not isinstance(value, str) or value not in _QUALITY_TAGS for value in quality_tags)):
            raise ValueError("分享来源画质标签必须是已识别的媒体标签")
        result["quality_tags"] = list(dict.fromkeys(quality_tags))
    return result


def source_ref(source):
    """Return only the URL identity needed by a report's source_refs table."""
    value = safe_source(source)
    return {key: value[key] for key in ("url", "title", "source_hash")}


def extract_selected_sources(report):
    """Extract chosen plan sources, never all discovered source candidates.

    Accepts a workflow report or a TaskStore snapshot wrapping it in ``report``.
    Legacy plans with only a source_hash can use the report's source_refs table.
    Missing or unsafe references are skipped; malformed records do not become
    trusted preferred sources.
    """
    if not isinstance(report, Mapping):
        return []
    if isinstance(report.get("report"), Mapping):
        report = report["report"]
    references = {}
    refs = report.get("source_refs", [])
    for item in refs if isinstance(refs, list) else []:
        try:
            value = safe_source(item)
        except (ValueError, TypeError):
            continue
        references[value["source_hash"]] = value
    selected, seen = [], set()
    plan = report.get("plan", [])
    for item in plan if isinstance(plan, list) else []:
        if not isinstance(item, Mapping):
            continue
        source = item.get("source_ref")
        if source is None:
            share = item.get("share_source") or {}
            if not isinstance(share, Mapping):
                continue
            identifier = share.get("source_hash", item.get("source_hash"))
            source = references.get(identifier) if isinstance(identifier, str) else None
        if source is None:
            continue
        try:
            value = safe_source(source)
            observed = item.get("source")
            if isinstance(observed, Mapping) and "source_ref" not in item:
                info = item.get("media_info") or {}
                if not isinstance(info, Mapping):
                    continue
                value = safe_source(value,
                    parent_id=observed.get("parent_id", value.get("parent_id")),
                    path=posixpath.dirname(observed["path"]) if isinstance(observed.get("path"), str) else value.get("path"),
                    quality_tags=[info[field] for field in ("resolution", "codec", "hdr") if info.get(field)])
        except (ValueError, TypeError):
            continue
        identity = (value["url"], value.get("parent_id"), value.get("path"))
        if identity not in seen:
            selected.append(value)
            seen.add(identity)
    return selected
