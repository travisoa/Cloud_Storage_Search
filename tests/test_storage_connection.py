"""Selected connection isolation, with synthetic credentials only."""
import os
from unittest import TestCase
from unittest.mock import patch

from media_helper import headless_cli, library_cli, storage_connection as connection
from media_helper.library_registry import LibraryRegistry, file_drive_reference


def config(name="quark.json", root="synthetic-root"):
    return {"provider": "quark", "library_root_id": root,
            "credential_source": {"type": "file", "name": name}}


class ConnectionTests(TestCase):
    def test_named_file_is_used_by_both_entry_points_even_with_default_and_environment(self):
        selected = config("drives/family.json")
        with patch.dict(os.environ, {"QUARK_COOKIE": "environment-cookie"}), \
                patch("media_helper.credentials.read_secret", return_value={"cookie": "selected-cookie"}) as read, \
                patch("media_helper.providers.QuarkProvider") as provider:
            for factory in (headless_cli._storage, library_cli._storage):
                storage = factory(selected)
                self.assertEqual(read.call_args.args, ("drives/family.json",))
                self.assertEqual(provider.call_args.kwargs, {"cookie": "selected-cookie"})
                self.assertEqual(storage.account_scope, "quark:file:family")

    def test_missing_or_empty_selected_cookie_cannot_activate_provider_fallback(self):
        for data in ({}, {"cookie": None}, {"cookie": ""}, {"cookie": "   "}):
            with self.subTest(data=data), patch.dict(os.environ, {"QUARK_COOKIE": "other-account"}), \
                    patch("media_helper.credentials.read_secret", return_value=data), \
                    patch("media_helper.providers.QuarkProvider") as provider:
                with self.assertRaises(ValueError):
                    headless_cli._storage(config("drives/family.json"))
                provider.assert_not_called()

    def test_readiness_checks_only_the_selected_named_file(self):
        with patch.dict(os.environ, {"QUARK_COOKIE": "other-account"}), \
                patch("media_helper.credentials.exists", return_value=False) as exists:
            self.assertFalse(headless_cli._quark_ready(config("drives/family.json")))
            exists.assert_called_once_with("drives/family.json")
        with patch("media_helper.credentials.exists", side_effect=lambda name: name == "drives/family.json"):
            self.assertTrue(headless_cli._quark_ready(config("drives/family.json")))

    def test_default_environment_override_remains_supported_with_distinct_scope(self):
        with patch.dict(os.environ, {"QUARK_COOKIE": "environment-cookie"}), \
                patch("media_helper.credentials.read_secret") as read, \
                patch("media_helper.providers.QuarkProvider") as provider:
            storage = headless_cli._storage(config())
            self.assertEqual(provider.call_args.kwargs, {"cookie": "environment-cookie"})
            self.assertEqual(storage.account_scope, "quark:environment:QUARK_COOKIE")
            read.assert_not_called()
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(connection.account_scope(config()), "quark:file:current")

    def test_scope_distinguishes_sources_and_survives_root_and_cookie_changes(self):
        first = {"credential_source": {"type": "alist", "storage_id": 1}, "library_root_id": "a"}
        second = {**first, "library_root_id": "b"}
        other = {**first, "credential_source": {"type": "alist", "storage_id": 2}}
        self.assertEqual(connection.account_scope(first), connection.account_scope(second))
        self.assertNotEqual(connection.account_scope(first), connection.account_scope(other))
        with patch("media_helper.providers.QuarkProvider"):
            a = connection.storage(config("drives/family.json"), cookie="old-cookie").account_scope
            b = connection.storage(config("drives/family.json"), cookie="renewed-cookie").account_scope
        self.assertEqual(a, b)

    def test_current_drive_keeps_selected_file_reference_and_reserves_its_alias(self):
        selected = config("drives/family.json")
        drive = LibraryRegistry("unused-registry.json").get_drive("current", selected)
        self.assertEqual(drive["credential_source"], selected["credential_source"])
        self.assertEqual(drive["account_scope"], "quark:file:family")
        with self.assertRaises(ValueError):
            file_drive_reference("current")
        with self.assertRaises(ValueError):
            connection.account_scope(config("drives/current.json"))

    def test_alist_cookie_is_explicit_and_does_not_use_a_default_file(self):
        selected = {"library_root_id": "root", "credential_source": {"type": "alist", "storage_id": 2}}
        with patch("media_helper.alist_credentials.AListCredentials") as reader, \
                patch("media_helper.credentials.read_secret") as read, \
                patch("media_helper.providers.QuarkProvider") as provider:
            reader.return_value.__enter__.return_value.load_cookie.return_value = "mount-cookie"
            storage = headless_cli._storage(selected)
            self.assertEqual(provider.call_args.kwargs, {"cookie": "mount-cookie"})
            self.assertEqual(storage.account_scope, "quark:alist-storage:2")
            self.assertEqual(storage.legacy_account_scopes, ("quark-root:root",))
            reader.return_value.__enter__.return_value.load_cookie.assert_called_once_with(2)
            read.assert_not_called()
