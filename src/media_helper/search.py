"""PanSou search adapter for background execution; no browser or public fallback."""
from __future__ import annotations

import json
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

from .sources import safe_source


class SearchError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PanSouSearch:
    def __init__(self, base_url="http://127.0.0.1:8888", timeout=35, transport=None):
        parts = urlsplit(base_url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("Configure one fixed PanSou HTTP endpoint without credentials")
        self.url = base_url.rstrip("/") + "/api/search"
        self.timeout = timeout
        self.transport = transport

    def search(self, query):
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200:
            raise ValueError("Search title must contain 1–200 characters")
        params = {"kw": query.strip(), "cloud_types": "quark", "res": "merge"}
        try:
            if self.transport:
                body = self.transport(self.url, params)
            else:
                opener = build_opener(ProxyHandler({}), _NoRedirect())
                with opener.open(Request(self.url + "?" + urlencode(params), headers={"Accept": "application/json"}), timeout=self.timeout) as response:
                    body = json.load(response)
        except Exception as exc:
            raise SearchError("后台搜索服务不可用；没有切换到其他公开服务") from None
        if not isinstance(body, dict) or body.get("success") is False or body.get("code", 0) != 0:
            raise SearchError("后台搜索服务返回失败")
        payload = body.get("data", body)
        if not isinstance(payload, dict) or not isinstance(payload.get("merged_by_type"), dict):
            raise SearchError("后台搜索响应缺少可核验的网盘结果")
        output, seen = [], set()
        candidates = payload["merged_by_type"].get("quark", [])
        if not isinstance(candidates, list):
            raise SearchError("后台搜索响应格式无效")
        for item in candidates:
            if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                continue
            parts = urlsplit(item["url"])
            if parts.scheme not in {"http", "https"} or parts.hostname != "pan.quark.cn" or not parts.path.startswith("/s/") or parts.username or parts.password:
                continue
            if item["url"] in seen:
                continue
            seen.add(item["url"])
            output.append({"url": item["url"], "title": item.get("note", ""), "source": item.get("source", "unknown"),
                           "published_at": item.get("datetime"), "verified": False})
        return output


class PreferredSourceSearch:
    """Enumerate saved sources first; wider search is an explicit second pass.

    This adapter does not decide whether a share has failed or is incomplete.
    Its caller inspects the workflow plan before setting include_fallback=True.
    Preferred sources are validated before any network call. Unsafe fallback
    hits are excluded from unattended watch runs, never stripped of passwords.
    """
    def __init__(self, preferred_sources, fallback_search, include_fallback=False):
        if not isinstance(preferred_sources, list):
            raise ValueError("优先来源必须是安全来源记录列表")
        if type(include_fallback) is not bool:
            raise ValueError("是否扩大搜索须为布尔值")
        self.preferred_sources = [safe_source(source) for source in preferred_sources]
        self.fallback_search = fallback_search
        self.include_fallback = include_fallback

    def search(self, query):
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 200:
            raise ValueError("Search title must contain 1–200 characters")
        result, seen, preferred_seen = [], set(), set()
        for source in self.preferred_sources:
            identity = (source["url"], source.get("parent_id", "0"))
            if identity not in preferred_seen:
                result.append(dict(source, provider="quark", source="preferred", verified=False))
                seen.add(source["url"])
                preferred_seen.add(identity)
        if self.include_fallback:
            hits = self.fallback_search.search(query)
            for hit in hits:
                if not isinstance(hit, dict) or hit.get("provider", "quark") != "quark":
                    continue
                try:
                    source = safe_source(hit)
                except (ValueError, TypeError):
                    continue
                if source["url"] not in seen:
                    result.append(dict(source, provider="quark", source="search", verified=False))
                    seen.add(source["url"])
        return result
