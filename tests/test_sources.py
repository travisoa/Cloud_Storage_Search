"""Stable source reuse stays local, scoped, and free of share credentials."""
from copy import deepcopy
import json
import unittest

from media_helper.search import PreferredSourceSearch, SearchError
from media_helper.sources import extract_selected_sources, safe_source, source_digest, source_ref
from media_helper.workflow import _digest


URL = "https://pan.quark.cn/s/0a1b2c3d4e5f"
OTHER = "https://pan.quark.cn/s/123abcd"


class Search:
    def __init__(self, hits=(), error=None):
        self.hits, self.error, self.calls = list(hits), error, []

    def search(self, query):
        self.calls.append(query)
        if self.error:
            raise self.error
        return deepcopy(self.hits)


class SourceReferences(unittest.TestCase):
    def test_audit_digest_excludes_password_query_fragment_and_userinfo(self):
        self.assertEqual(source_digest(URL), _digest(URL))
        for url in (URL + "?pwd=1234", URL + "?pwd=9876&tracking=x", URL + "#1234",
                    URL.replace("pan.quark.cn", "user:secret@pan.quark.cn")):
            with self.subTest(url=url):
                self.assertEqual(source_digest(url), _digest(URL))
                self.assertNotEqual(source_digest(url), _digest(url))

    def test_safe_reference_preserves_only_reusable_fields_and_existing_hash(self):
        value = safe_source({"url": URL, "title": "测试剧 4K", "source": "pansou", "verified": False,
                             "parent_id": "a57f2192a111444da0c1eccf4d629433", "path": "测试剧/4K",
                             "quality_tags": ["2160p", "HEVC", "2160p"]})
        self.assertEqual(value, {"url": URL, "title": "测试剧 4K", "source_hash": _digest(URL),
                                "parent_id": "a57f2192a111444da0c1eccf4d629433", "path": "测试剧/4K",
                                "quality_tags": ["2160p", "HEVC"]})
        self.assertEqual(safe_source(value), value)
        self.assertEqual(source_ref(value), {"url": URL, "title": "测试剧 4K", "source_hash": _digest(URL)})

    def test_rejects_noncanonical_or_credential_bearing_urls_without_silently_stripping(self):
        for url in (URL + "?pwd=1234", URL + "?tracking=x", URL + "#1234", URL + "/",
                    URL.replace("https://", "http://"), URL.replace("pan.quark.cn", "pan.quark.cn.evil"),
                    URL.replace("pan.quark.cn", "pan.quark.cn:443"),
                    URL.replace("pan.quark.cn", "user:secret@pan.quark.cn"),
                    URL.replace("/s/", "/s/../"), URL + "%2f", " " + URL, URL + "\n"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                safe_source(url)

    def test_rejects_sensitive_fields_even_when_nested_or_empty(self):
        for extra in ({"password": ""}, {"stoken": "share-secret"}, {"share_fid_token": "file-secret"},
                      {"extra": [{"authorization": "hidden"}]}, {"api_key": None}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                safe_source({"url": URL, **extra})

    def test_rejects_password_hints_and_invalid_scope_or_labels(self):
        for extra in ({"title": "测试剧 提取码：1234"}, {"path": "测试剧/密码abcd"},
                      {"parent_id": "../evil"}, {"quality_tags": ["unverified-secret"]},
                      {"quality_tags": "2160p"}, {"source_hash": "0" * 64}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                safe_source({"url": URL, **extra})

    def test_extracts_only_chosen_plan_sources_and_deduplicates_subtitles(self):
        source = safe_source(URL, title="测试剧", parent_id="4kfolder", path="测试剧/4K", quality_tags=["2160p"])
        report = {"source_refs": [source_ref(source), source_ref(safe_source(OTHER))],
                  "source_candidates": [{"source_ref": safe_source(OTHER)}],
                  "plan": [{"source_ref": source}, {"source_ref": source}]}
        self.assertEqual(extract_selected_sources({"report": report}), [source])
        report["plan"] = []
        self.assertEqual(extract_selected_sources(report), [])

    def test_extracts_hash_reference_with_observed_parent_and_quality_labels(self):
        report = {"source_refs": [safe_source(URL, title="测试剧")],
                  "plan": [{"share_source": {"source_hash": _digest(URL)},
                            "source": {"parent_id": "4kfolder", "path": "测试剧/4K/测试剧.S01E01.mkv"},
                            "media_info": {"resolution": "2160p", "codec": "HEVC", "hdr": None}}]}
        self.assertEqual(extract_selected_sources(report),
                         [safe_source(URL, title="测试剧", parent_id="4kfolder", path="测试剧/4K",
                                      quality_tags=["2160p", "HEVC"])])

    def test_unsafe_or_malformed_plan_does_not_create_a_source(self):
        report = {"source_refs": [{"url": URL + "?pwd=1234"}],
                  "plan": [{"source_ref": {"url": URL, "stoken": "secret"}},
                           {"share_source": {"source_hash": _digest(URL)}}]}
        self.assertEqual(extract_selected_sources(report), [])
        self.assertEqual(extract_selected_sources({"source_refs": None, "plan": None}), [])


class PreferredSearch(unittest.TestCase):
    def test_saved_source_does_not_call_pansou_until_explicit_fallback(self):
        fallback = Search(error=AssertionError("PanSou must not run"))
        source = safe_source(URL, title="测试剧", parent_id="4kfolder", path="测试剧/4K")
        hits = PreferredSourceSearch([source], fallback).search("测试剧")
        self.assertEqual(fallback.calls, [])
        self.assertEqual((hits[0]["url"], hits[0]["parent_id"], hits[0]["path"], hits[0]["source"]),
                         (URL, "4kfolder", "测试剧/4K", "preferred"))
        self.assertFalse(hits[0]["verified"])

    def test_fallback_only_when_flag_enabled_merges_and_deduplicates(self):
        fallback = Search([{"url": URL, "title": "重复"}, {"url": OTHER, "title": "测试剧 第二源"},
                           {"url": OTHER, "title": "重复"}])
        hits = PreferredSourceSearch([safe_source(URL, title="测试剧")], fallback,
                                     include_fallback=True).search("测试剧")
        self.assertEqual(fallback.calls, ["测试剧"])
        self.assertEqual([hit["url"] for hit in hits], [URL, OTHER])
        self.assertEqual([hit["source"] for hit in hits], ["preferred", "search"])

    def test_multiple_confirmed_folders_in_same_share_are_retained(self):
        fallback = Search([{"url": URL, "title": "重复"}])
        a = safe_source(URL, title="测试剧", parent_id="part1")
        b = safe_source(URL, title="测试剧", parent_id="part2")
        hits = PreferredSourceSearch([a, a, b], fallback, include_fallback=True).search("测试剧")
        self.assertEqual([hit["parent_id"] for hit in hits], ["part1", "part2"])

    def test_unsafe_config_is_rejected_and_unsafe_fallback_is_excluded(self):
        fallback = Search([{"url": OTHER + "?pwd=1234", "title": "测试剧"},
                           {"url": OTHER, "title": "测试剧", "password": "1234"},
                           {"url": OTHER, "title": "测试剧 提取码1234"}])
        with self.assertRaises(ValueError):
            PreferredSourceSearch([{"url": URL, "stoken": "secret"}], fallback)
        self.assertEqual(fallback.calls, [])
        hits = PreferredSourceSearch([], fallback, include_fallback=True).search("测试剧")
        self.assertEqual(hits, [])
        self.assertNotIn("1234", json.dumps(hits))

    def test_fallback_failure_is_reported_without_switching_service(self):
        fallback = Search(error=SearchError("fixed backend unavailable"))
        with self.assertRaises(SearchError):
            PreferredSourceSearch([safe_source(URL)], fallback, include_fallback=True).search("测试剧")
        self.assertEqual(fallback.calls, ["测试剧"])


if __name__ == "__main__":
    unittest.main()
