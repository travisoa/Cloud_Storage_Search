import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from media_helper.providers import (AuthenticationError, IncompleteListingError,
                                    ProviderError, QuarkProvider, QuotaError, RiskControlError,
                                    UnknownOutcome)


def ok(data=None, *, total=None):
    result = {"status": 200, "code": 0, "data": data or {}}
    if total is not None:
        result["metadata"] = {"_total": total}
    return result


def file(fid, name="Show.S01E01.mkv", *, is_dir=False, **extra):
    return {"fid": fid, "file_name": name, "dir": is_dir, "size": 123, **extra}


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, *, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": dict(headers),
                           "payload": json.loads(body) if body is not None else None, "timeout": timeout})
        if not self.responses:
            raise AssertionError("unexpected additional network request")
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class QuarkProviderTests(unittest.TestCase):
    def provider(self, *responses, **settings):
        transport = FakeTransport(*responses)
        return QuarkProvider(cookie="fake_cookie=do-not-print", transport=transport,
                             sleep=lambda _: None, **settings), transport

    def test_full_directory_pagination_and_file_info(self):
        provider, transport = self.provider(ok({"list": [file("f1")]}, total=2),
                                            ok({"list": [file("f2", "Season 01", is_dir=True)]}, total=2),
                                            ok({"list": [file("f1"), file("f2", "Season 01", is_dir=True)]}, total=2))
        result = provider.list_files("root")
        self.assertEqual([item["fid"] for item in result], ["f1", "f2"])
        self.assertTrue(result[1]["is_dir"])
        self.assertEqual(result[0]["parent_id"], "root")
        pages = [parse_qs(urlsplit(call["url"]).query)["_page"] for call in transport.calls]
        self.assertEqual(pages, [["1"], ["2"]])
        self.assertEqual(provider.file_info("f2")["name"], "Season 01")
        self.assertTrue(all(call["method"] == "GET" for call in transport.calls))

    def test_missing_total_continues_until_empty_page(self):
        provider, transport = self.provider(ok({"list": [file("f1")]}), ok({"list": [file("f2")]}), ok({"list": []}))
        self.assertEqual(len(provider.list_files("0")), 2)
        self.assertEqual(len(transport.calls), 3)

    def test_never_returns_a_partial_or_repeated_listing(self):
        scenarios = [
            [ok({"list": [file("f1")]}, total=2), ok({"list": []}, total=2)],
            [ok({"list": [file("f1")]}), ok({"list": [file("f1")]})],
            [ok({"list": [file("f1")]}, total=3), ok({"list": [file("f2")]}, total=4)],
        ]
        for responses in scenarios:
            with self.subTest(responses=len(responses)):
                provider, _ = self.provider(*responses)
                with self.assertRaises(IncompleteListingError):
                    provider.list_files("0")

    def test_share_token_and_pagination_preserve_source_directory(self):
        provider, transport = self.provider(
            {"status": 200, "data": {"stoken": "sensitive-share-token"}},
            ok({"list": [file("s1", share_fid_token="sensitive-file-token")]}, total=2),
            ok({"list": [file("s2", "Folder", is_dir=True, share_fid_token="second-token")]}, total=2))
        result = provider.share_files("https://pan.quark.cn/s/abc123?pwd=1234", parent_id="source-dir")
        self.assertEqual(result["share_ref"]["parent_id"], "source-dir")
        self.assertEqual(result["files"][0]["share_fid_token"], "sensitive-file-token")
        self.assertEqual(transport.calls[0]["payload"], {"pwd_id": "abc123", "passcode": "1234"})
        self.assertEqual(parse_qs(urlsplit(transport.calls[-1]["url"]).query)["pdir_fid"], ["source-dir"])

    def test_share_tree_reuses_token_only_within_one_enumeration(self):
        from media_helper.workflow import _share_tree
        root = ok({"list": [file("show", "Show", is_dir=True)]}, total=1)
        show = ok({"list": [file("quality", "4K", is_dir=True)]}, total=1)
        quality = ok({"list": [file("episode", share_fid_token="file-token")]}, total=1)
        provider, transport = self.provider(ok({"stoken": "first-token"}), root, show, quality,
                                            ok({"stoken": "second-token"}), root, show, quality)
        hit = {"url": "https://pan.quark.cn/s/abc123", "parent_id": "quality", "path": "Show/4K"}
        first = _share_tree(provider, hit)
        second = _share_tree(provider, hit)
        self.assertEqual(first[0]["share_ref"]["parent_id"], "quality")
        self.assertEqual(second[0]["file"]["fid"], "episode")
        token_calls = [call for call in transport.calls if call["method"] == "POST"]
        self.assertEqual(len(token_calls), 2)
        detail_calls = [parse_qs(urlsplit(call["url"]).query) for call in transport.calls if call["method"] == "GET"]
        self.assertEqual([query["pdir_fid"] for query in detail_calls],
                         [["0"], ["show"], ["quality"], ["0"], ["show"], ["quality"]])
        self.assertEqual([query["stoken"] for query in detail_calls],
                         [["first-token"]] * 3 + [["second-token"]] * 3)

    def test_save_polls_but_submits_only_one_post_and_returns_real_new_fids(self):
        provider, transport = self.provider(ok({"task_id": "task1"}), ok({"status": 0}),
                                            ok({"status": 2, "save_as": {"save_as_top_fids": ["new1"]}}))
        result = provider.save_files({"share_id": "share1", "stoken": "secret", "parent_id": "source-dir"},
                                     [{"fid": "old1", "share_fid_token": "file-secret", "parent_id": "source-dir"}], "target1")
        self.assertEqual(result["saved_fids"], ["new1"])
        self.assertEqual(result["source_fids"], ["old1"])
        self.assertEqual(result["task_id"], "task1")
        self.assertEqual([call["method"] for call in transport.calls], ["POST", "GET", "GET"])
        payload = transport.calls[0]["payload"]
        self.assertEqual(payload["pdir_fid"], "source-dir")
        self.assertEqual(payload["fid_list"], ["old1"])
        self.assertEqual(payload["fid_token_list"], ["file-secret"])

    def test_save_submission_callback_runs_before_any_status_request_with_safe_evidence(self):
        provider, transport = self.provider(ok({"task_id": "task1"}),
                                            ok({"status": 2, "save_as": {"save_as_top_fids": ["new1"]}}))
        submissions = []

        def record(evidence):
            self.assertEqual([call["method"] for call in transport.calls], ["POST"])
            self.assertEqual(evidence, {"task_id": "task1", "source_fids": ["old1"],
                                        "target_parent_id": "target1"})
            submissions.append(dict(evidence))

        result = provider.save_files({"share_id": "share1", "stoken": "secret", "parent_id": "source-dir"},
                                     [{"fid": "old1", "share_fid_token": "file-secret", "parent_id": "source-dir"}],
                                     "target1", on_submitted=record)
        self.assertTrue(provider.capabilities["save_submission_callback"])
        self.assertEqual(len(submissions), 1)
        self.assertEqual(result["saved_fids"], ["new1"])
        self.assertEqual([call["method"] for call in transport.calls], ["POST", "GET"])
        self.assertNotIn("secret", json.dumps(submissions))
        self.assertNotIn("share1", json.dumps(submissions))
        self.assertNotIn("cookie", json.dumps(submissions).lower())

    def test_save_submission_callback_cannot_mutate_result_or_polling_evidence(self):
        provider, transport = self.provider(ok({"task_id": "task1"}), ok({"status": 2}))

        def mutate(evidence):
            evidence["task_id"] = "different-task"
            evidence["source_fids"].append("different-source")
            evidence["target_parent_id"] = "different-target"

        result = provider.save_files({"share_id": "share1", "stoken": "secret"},
                                     [{"fid": "old1", "share_fid_token": "file-secret"}], "target1",
                                     on_submitted=mutate)
        self.assertEqual(result["task_id"], "task1")
        self.assertEqual(result["source_fids"], ["old1"])
        self.assertEqual(result["target_parent_id"], "target1")
        self.assertEqual(parse_qs(urlsplit(transport.calls[1]["url"]).query)["task_id"], ["task1"])

    def test_save_submission_persistence_failure_stops_without_poll_or_resubmit(self):
        provider, transport = self.provider(ok({"task_id": "task1"}))

        def fail(_):
            raise OSError("fake_cookie=do-not-print stoken=secret file-secret")

        with self.assertRaises(UnknownOutcome) as caught:
            provider.save_files({"share_id": "share1", "stoken": "secret"},
                                [{"fid": "old1", "share_fid_token": "file-secret"}], "target1",
                                on_submitted=fail)
        self.assertEqual(caught.exception.task_id, "task1")
        self.assertEqual(caught.exception.evidence, {"task_id": "task1", "source_fids": ["old1"],
                                                    "target_parent_id": "target1"})
        self.assertEqual([call["method"] for call in transport.calls], ["POST"])
        safe_error = str(caught.exception) + json.dumps(caught.exception.evidence)
        self.assertNotIn("secret", safe_error)
        self.assertNotIn("do-not-print", safe_error)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_save_submission_interruption_can_resume_by_persisted_task_without_another_post(self):
        provider, transport = self.provider(ok({"task_id": "task1"}),
                                            ok({"status": 2, "save_as": {"save_as_top_fids": ["new1"]}}))
        with tempfile.TemporaryDirectory() as directory:
            record_path = Path(directory) / "submitted.json"

            def interrupt_after_persisting(evidence):
                record_path.write_text(json.dumps(evidence), encoding="utf-8")
                raise KeyboardInterrupt("simulated interruption")

            with self.assertRaises(UnknownOutcome) as caught:
                provider.save_files({"share_id": "share1", "stoken": "secret"},
                                    [{"fid": "old1", "share_fid_token": "file-secret"}], "target1",
                                    on_submitted=interrupt_after_persisting)
            self.assertEqual(caught.exception.task_id, "task1")
            self.assertEqual([call["method"] for call in transport.calls], ["POST"])
            evidence = json.loads(record_path.read_text(encoding="utf-8"))
            resumed = provider.task_status(evidence["task_id"])
            self.assertEqual(resumed["saved_fids"], ["new1"])
            self.assertEqual([call["method"] for call in transport.calls], ["POST", "GET"])

    def test_save_submission_callback_invalid_before_post_or_unusable_task_id(self):
        for callback in (False, 1, [], {}, "not-callable"):
            with self.subTest(callback=callback):
                provider, transport = self.provider()
                with self.assertRaises(ProviderError):
                    provider.save_files({"share_id": "share1", "stoken": "secret"},
                                        [{"fid": "old1", "share_fid_token": "file-secret"}], "target1",
                                        on_submitted=callback)
                self.assertEqual(transport.calls, [])
        provider, transport = self.provider(ok({"task_id": "invalid task"}))
        submissions = []
        with self.assertRaises(UnknownOutcome):
            provider.save_files({"share_id": "share1", "stoken": "secret"},
                                [{"fid": "old1", "share_fid_token": "file-secret"}], "target1",
                                on_submitted=submissions.append)
        self.assertEqual(submissions, [])
        self.assertEqual([call["method"] for call in transport.calls], ["POST"])

    def test_missing_new_fids_never_reuses_source_fids(self):
        provider, _ = self.provider(ok({"task_id": "task1"}), ok({"status": 2}))
        result = provider.save_files({"share_id": "share1", "stoken": "secret"},
                                     [{"fid": "old1", "share_fid_token": "file-secret"}], "target1")
        self.assertEqual(result["saved_fids"], [])

    def test_invalid_success_marker_or_duplicate_saved_ids_is_not_success(self):
        provider, transport = self.provider({"status": 200, "code": "not-a-code", "data": {}})
        with self.assertRaises(UnknownOutcome):
            provider.rename("fid1", "Show S01E01.mkv")
        self.assertEqual(len(transport.calls), 1)
        provider, transport = self.provider(ok({"task_id": "task1"}),
                                            ok({"status": 2, "save_as": {"save_as_top_fids": ["new1", "new1"]}}))
        with self.assertRaises(UnknownOutcome) as caught:
            provider.save_files({"share_id": "share1", "stoken": "secret"},
                                [{"fid": "old1", "share_fid_token": "file-secret"}], "target1")
        self.assertEqual(caught.exception.task_id, "task1")

    def test_bounded_page_limit_does_not_return_a_truncated_list(self):
        provider, transport = self.provider(ok({"list": [file("f1")]}), max_pages=1)
        with self.assertRaises(IncompleteListingError):
            provider.list_files("0")
        self.assertEqual(len(transport.calls), 1)

    def test_write_timeout_is_unknown_and_has_safe_evidence_without_retry(self):
        provider, transport = self.provider(TimeoutError("fake_cookie=do-not-print sensitive-file-token"))
        with self.assertRaises(UnknownOutcome) as caught:
            provider.rename("fid1", "Show S01E01.mkv")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(caught.exception.step, "rename")
        self.assertEqual(caught.exception.evidence["fid"], "fid1")
        self.assertIn("request_id", caught.exception.evidence)
        self.assertNotIn("do-not-print", str(caught.exception))
        self.assertNotIn("cookie", json.dumps(caught.exception.evidence).lower())

    def test_poll_timeout_keeps_task_id_and_never_resubmits_save(self):
        provider, transport = self.provider(ok({"task_id": "task1"}), TimeoutError("stoken=private"))
        with self.assertRaises(UnknownOutcome) as caught:
            provider.save_files({"share_id": "share1", "stoken": "secret"},
                                [{"fid": "old1", "share_fid_token": "file-secret"}], "target1")
        self.assertEqual(caught.exception.task_id, "task1")
        self.assertEqual(caught.exception.evidence["target_parent_id"], "target1")
        self.assertEqual([call["method"] for call in transport.calls], ["POST", "GET"])
        self.assertNotIn("stoken", str(caught.exception))

    def test_bounded_polling_and_unrecognized_or_failed_task_states(self):
        for state, error in ((0, UnknownOutcome), (3, UnknownOutcome), (4, ProviderError)):
            with self.subTest(state=state):
                provider, transport = self.provider(ok({"task_id": "task1"}), ok({"status": state}), poll_attempts=1)
                with self.assertRaises(error) as caught:
                    provider.save_files({"share_id": "share1", "stoken": "secret"},
                                        [{"fid": "old1", "share_fid_token": "file-secret"}], "target1")
                self.assertEqual(caught.exception.task_id, "task1")
                self.assertEqual(len(transport.calls), 2)

    def test_authentication_and_risk_errors_stop_without_exposing_remote_message(self):
        for body, error in (({"code": 31001, "message": "cookie=private login expired"}, AuthenticationError),
                            ({"code": 999, "message": "captcha: token=private"}, RiskControlError)):
            with self.subTest(error=error):
                provider, transport = self.provider(body)
                with self.assertRaises(error) as caught:
                    provider.list_files("0")
                self.assertNotIn("private", str(caught.exception))
                self.assertEqual(len(transport.calls), 1)

    def test_unavailable_share_is_not_misclassified_as_expired_login(self):
        for body in ({"_http_status": 403, "status": 403, "code": 41031, "message": "分享者用户封禁链接查看受限"},
                     {"status": 400, "code": 41011, "message": "分享地址已失效"}):
            with self.subTest(code=body["code"]):
                provider, transport = self.provider(body)
                with self.assertRaises(ProviderError) as caught:
                    provider.share_files("https://pan.quark.cn/s/abc123")
                self.assertNotIsInstance(caught.exception, AuthenticationError)
                self.assertEqual(caught.exception.code, body["code"])
                self.assertEqual(len(transport.calls), 1)
        provider, _ = self.provider({"_http_status": 401, "status": 401, "code": 41031, "message": "请先登录"})
        with self.assertRaises(AuthenticationError):
            provider.share_files("https://pan.quark.cn/s/abc123")

    def test_storage_quota_errors_are_not_misclassified_as_expired_login(self):
        for code in (32003, 32004):
            with self.subTest(code=code):
                provider, transport = self.provider({"status": 400, "code": code, "message": "private backend details"})
                with self.assertRaises(QuotaError) as caught:
                    provider.save_files({"share_id": "share1", "stoken": "secret"},
                                        [{"fid": "old1", "share_fid_token": "file-secret"}], "target1")
                self.assertNotIsInstance(caught.exception, AuthenticationError)
                self.assertEqual(caught.exception.code, code)
                self.assertIn("space", str(caught.exception))
                self.assertNotIn("private", str(caught.exception))
                self.assertEqual(len(transport.calls), 1)

    def test_mkdir_rename_and_read_only_task_resume(self):
        provider, transport = self.provider(ok({"fid": "dir1"}), ok(),
                                            ok({"status": 2, "save_as": {"save_as_top_fids": ["new1"]}}))
        result = provider.mkdir("root", "Season 01")
        self.assertTrue(result["is_dir"])
        self.assertEqual(result["fid"], "dir1")
        self.assertEqual(provider.rename("new1", "Show S01E01.mkv")["status"], "done")
        self.assertEqual(provider.task_status("task1")["saved_fids"], ["new1"])
        self.assertEqual([call["method"] for call in transport.calls], ["POST", "POST", "GET"])

    def test_credentials_only_explicit_file_or_environment_and_repr_is_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quark.json"
            path.write_text(json.dumps({"cookie": "file_cookie=private"}))
            path.chmod(0o600)
            with patch.dict(os.environ, {}, clear=True):
                provider = QuarkProvider(credential_path=path, transport=FakeTransport())
                self.assertEqual(provider.credential_source, "file")
                self.assertNotIn("private", repr(provider))
                path.chmod(0o644)
                with self.assertRaises(AuthenticationError):
                    QuarkProvider(credential_path=path)
            with patch.dict(os.environ, {"QUARK_COOKIE": "env_cookie=private"}, clear=True):
                provider = QuarkProvider(credential_path=Path(directory) / "absent", transport=FakeTransport())
                self.assertEqual(provider.credential_source, "environment")

    def test_validation_failure_performs_no_network(self):
        provider, transport = self.provider()
        for action in (lambda: provider.share_files("https://pan.quark.cn.evil.example/s/id"),
                       lambda: provider.mkdir("root", "../escape"),
                       lambda: provider.rename("fid1", "bad\nname"),
                       lambda: provider.file_info("unseen"),
                       lambda: provider.save_files({"share_id": "s1", "stoken": "private"},
                           [{"fid": "f1", "share_fid_token": "private"}, {"fid": "f1", "share_fid_token": "private"}], "root")):
            with self.assertRaises(ProviderError):
                action()
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
