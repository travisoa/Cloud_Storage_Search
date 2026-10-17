"""Protocol-specific archive requests using fake HTTP responses only."""
import json
import unittest
from urllib.parse import parse_qs, urlsplit

from media_helper.providers import AuthenticationError, ProviderError, QuarkProvider, RiskControlError, UnknownOutcome
from tests.test_providers import FakeTransport, ok


def done(*fids):
    return ok({"status": 2, "unarchive_result": {"list": [{"fid": fid, "file_name": "output"} for fid in fids]}})


class ArchiveProviderTests(unittest.TestCase):
    def provider(self, *responses, **options):
        transport = FakeTransport(*responses)
        provider = QuarkProvider(cookie="test-cookie=never-persist", transport=transport,
                                 sleep=lambda seconds: None, **options)
        return provider, transport

    def test_only_proven_unarchive_parameters_and_callback_before_first_poll(self):
        provider, transport = self.provider(ok({"task_id": "task1"}), ok({"status": 1}), done("new1"))
        evidence = []
        def submitted(value):
            self.assertEqual(len(transport.calls), 1)
            self.assertEqual(transport.calls[0]["method"], "POST")
            evidence.append(value)
        result = provider.extract_archive("source1", "isolated1", on_submitted=submitted)
        self.assertEqual(transport.calls[0]["payload"], {"fid": "source1", "select_mode": 1, "to_pdir_fid": "isolated1"})
        self.assertEqual(urlsplit(transport.calls[0]["url"]).path, "/1/clouddrive/archive/unarchive")
        self.assertEqual([row["method"] for row in transport.calls], ["POST", "GET", "GET"])
        self.assertEqual(evidence, [{"source_fid": "source1", "target_parent_id": "isolated1", "task_id": "task1"}])
        self.assertEqual(result["extracted_fids"], ["new1"])
        self.assertEqual(result["status"], "done")
        self.assertNotIn("never-persist", json.dumps(result))

    def test_archive_task_reconciliation_is_get_only_and_whitelisted(self):
        provider, transport = self.provider(done("new1", "new2"))
        result = provider.archive_task_status("task1")
        self.assertEqual(result, {"task_id": "task1", "status": "done", "extracted_fids": ["new1", "new2"]})
        self.assertEqual(transport.calls[0]["method"], "GET")
        self.assertEqual(parse_qs(urlsplit(transport.calls[0]["url"]).query)["task_id"], ["task1"])

    def test_explicit_task_response_identifier_must_match_requested_task(self):
        response = done("new1")
        response["data"]["task_id"] = "another-task"
        provider, transport = self.provider(response)
        with self.assertRaises(UnknownOutcome) as caught:
            provider.archive_task_status("task1")
        self.assertEqual(caught.exception.task_id, "task1")
        self.assertEqual(len(transport.calls), 1)

    def test_callback_failure_stops_before_poll_and_keeps_known_task(self):
        provider, transport = self.provider(ok({"task_id": "task1"}))
        def fail(value):
            raise RuntimeError("password=my-private-password")
        with self.assertRaises(UnknownOutcome) as caught:
            provider.extract_archive("source1", "isolated1", on_submitted=fail)
        self.assertEqual(caught.exception.task_id, "task1")
        self.assertEqual(len(transport.calls), 1)
        self.assertNotIn("my-private", str(caught.exception))

    def test_missing_task_id_or_transport_loss_never_retries_post(self):
        for response in (ok({}), TimeoutError("cookie=do-not-print")):
            provider, transport = self.provider(response)
            with self.assertRaises(UnknownOutcome):
                provider.extract_archive("source1", "isolated1")
            self.assertEqual(len(transport.calls), 1)

    def test_poll_timeout_keeps_task_id_and_never_resubmits(self):
        provider, transport = self.provider(ok({"task_id": "task1"}), ok({"status": 1}), ok({"status": 1}), poll_attempts=2)
        with self.assertRaises(UnknownOutcome) as caught:
            provider.extract_archive("source1", "isolated1")
        self.assertEqual(caught.exception.task_id, "task1")
        self.assertEqual([row["method"] for row in transport.calls].count("POST"), 1)

    def test_poll_network_error_keeps_task_id_and_stops(self):
        provider, transport = self.provider(ok({"task_id": "task1"}), TimeoutError("token=secret"))
        with self.assertRaises(UnknownOutcome) as caught:
            provider.extract_archive("source1", "isolated1")
        self.assertEqual(caught.exception.task_id, "task1")
        self.assertEqual(len(transport.calls), 2)

    def test_authentication_and_risk_errors_stop_polling(self):
        for response, exception in (({"_http_status": 401, "code": 401}, AuthenticationError),
                                    ({"_http_status": 429, "code": 429}, RiskControlError)):
            provider, transport = self.provider(ok({"task_id": "task1"}), response)
            with self.assertRaises(exception) as caught:
                provider.extract_archive("source1", "isolated1")
            self.assertEqual(caught.exception.task_id, "task1")
            self.assertEqual(len(transport.calls), 2)

    def test_password_unsupported_multipart_membership_messages_only_return_categories(self):
        samples = (("密码 secret-value", "password_required"),
                   ("unsupported secret-value", "unsupported_archive"),
                   ("missing volume secret-value", "multipart_requires_review"),
                   ("SVIP secret-value", "membership_required"),
                   ("other secret-value", "archive_task_failed"))
        for message, reason in samples:
            provider, _ = self.provider(ok({"status": 4, "unarchive_result": {"message": message}}))
            result = provider.archive_task_status("task1")
            self.assertEqual(result["reason"], reason)
            self.assertNotIn("secret-value", json.dumps(result))

    def test_malformed_or_duplicate_result_identifiers_stay_unknown(self):
        for payload in ({"status": 2, "unarchive_result": {"list": "wrong"}},
                        {"status": 2, "unarchive_result": {"list": [{"fid": "same"}, {"fid": "same"}]}},
                        {"status": 2, "unarchive_result": {"list": [{"fid": "../bad"}]}}):
            provider, _ = self.provider(ok(payload))
            with self.assertRaises(UnknownOutcome):
                provider.archive_task_status("task1")

    def test_no_password_or_extra_parameters_accepted_by_api_signature(self):
        provider, transport = self.provider()
        with self.assertRaises(TypeError):
            provider.extract_archive("source1", "isolated1", password="never-enter-this")
        self.assertEqual(transport.calls, [])
        for source, destination in (("../bad", "target"), ("source", "../target")):
            with self.assertRaises(ProviderError):
                provider.extract_archive(source, destination)
        self.assertEqual(transport.calls, [])

    def test_archive_31001_is_membership_requirement_not_login_failure(self):
        provider, transport = self.provider({"_http_status": 200, "status": 400, "code": 31001,
                                              "message": "SVIP required private-server-text"})
        with self.assertRaises(ProviderError) as caught:
            provider.extract_archive("source1", "isolated1")
        self.assertNotIsInstance(caught.exception, AuthenticationError)
        self.assertEqual(caught.exception.code, 31001)
        self.assertNotIn("private-server-text", str(caught.exception))
        self.assertEqual([row["method"] for row in transport.calls], ["POST"])

    def test_archive_membership_code_does_not_mask_real_http_authentication(self):
        for http_status in (401, 403):
            provider, transport = self.provider({"_http_status": http_status, "code": 31001,
                                                  "message": "SVIP required"})
            with self.assertRaises(AuthenticationError):
                provider.extract_archive("source1", "isolated1")
            self.assertEqual(len(transport.calls), 1)
        provider, _ = self.provider({"_http_status": 200, "code": 31001, "message": "login required"})
        with self.assertRaises(AuthenticationError):
            provider.extract_archive("source1", "isolated1")

    def test_archive_membership_override_does_not_change_other_endpoint_auth_codes(self):
        provider, transport = self.provider({"_http_status": 200, "code": 31001, "message": "rejected"})
        with self.assertRaises(AuthenticationError):
            provider.list_files("source1")
        self.assertEqual([row["method"] for row in transport.calls], ["GET"])

    def test_archive_23008_processing_is_unknown_with_one_post_only(self):
        provider, transport = self.provider({"_http_status": 200, "code": 23008,
                                              "message": "doloading private-server-text"})
        with self.assertRaises(UnknownOutcome) as caught:
            provider.extract_archive("source1", "isolated1")
        self.assertEqual(caught.exception.code, 23008)
        self.assertIsNone(caught.exception.task_id)
        self.assertEqual([row["method"] for row in transport.calls], ["POST"])
        self.assertNotIn("private-server-text", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
