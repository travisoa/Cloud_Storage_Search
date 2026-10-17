"""Local named-library configuration and account-boundary tests."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from media_helper.library_registry import LibraryRegistry, drive_reference


def library(**changes):
    return {"library_id": "family", "drive_id": "new-drive", "parent_id": "0",
            "library_root_id": "library-root", "name": "影库",
            "categories": {"movie": {"folder_id": "movies", "name": "电影"},
                           "tv": {"folder_id": "shows", "name": "电视剧"}},
            "init_job_key": "library-init:" + "a" * 64, "test_only": False, **changes}


class LibraryRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.path = self.directory / "private" / "libraries.json"
        self.registry = LibraryRegistry(self.path)
        self.base = {"provider": "quark", "library_root_id": "existing-library",
                     "credential_source": {"type": "alist", "storage_id": 1,
                                           "container": "alist", "mount_path": "/Quark"},
                     "categories": {"tv": {"folder_id": "existing-tv", "name": "电视剧📺"}},
                     "timezone": "Asia/Shanghai", "metadata_source": "auto",
                     "overwrite": False, "delete": False, "open_browser": False}

    def connect(self, drive_id="new-drive", storage_id=2, **options):
        return self.registry.connect(drive_reference(drive_id, storage_id, **options))

    def register(self, **changes):
        self.connect()
        return self.registry.register(library(**changes))

    def test_missing_registry_is_empty_and_does_not_create_file(self):
        self.assertEqual(self.registry.read(), {"schema_version": 1, "drives": {}, "libraries": {}})
        self.assertFalse(self.path.exists())

    def test_drive_reference_is_only_a_public_alist_reference(self):
        reference = drive_reference("new-drive", 2, boundary_root_id="0")
        self.assertEqual(reference, {"drive_id": "new-drive", "provider": "quark",
                                    "credential_source": {"type": "alist", "storage_id": 2},
                                    "account_scope": "quark:alist-storage:2", "boundary_root_id": "0"})
        self.assertNotIn("cookie", json.dumps(reference).lower())
        for value in (None, True, 0, -1, "2"):
            with self.subTest(storage_id=value), self.assertRaises(ValueError):
                drive_reference("new-drive", value)

    def test_credential_fields_and_alternate_connection_fields_rejected_before_write(self):
        for field in ("cookie", "token", "password", "credential_path", "container", "mount_path"):
            reference = drive_reference("new-drive", 2)
            reference["credential_source"][field] = "sensitive-private-value"
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.registry.connect(reference)
            self.assertFalse(self.path.exists())
        for field, value in (("provider", "another"), ("account_scope", "account-substitution"),
                             ("extra", "opaque")):
            reference = drive_reference("new-drive", 2)
            reference[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.registry.connect(reference)

    def test_saved_drive_and_library_are_private_and_not_caller_aliases(self):
        reference = self.connect()
        reference["credential_source"]["storage_id"] = 99
        record = self.registry.register(library())
        record["categories"]["tv"]["name"] = "mutated-return"
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        data = self.registry.read()
        self.assertEqual(data["drives"]["new-drive"]["credential_source"]["storage_id"], 2)
        self.assertEqual(data["libraries"]["family"]["categories"]["tv"]["name"], "电视剧")

    def test_same_drive_name_cannot_replace_account_or_boundary(self):
        self.connect(boundary_root_id="0")
        before = self.path.read_bytes()
        for storage_id, boundary in ((3, "0"), (2, "another-root")):
            with self.assertRaises(ValueError):
                self.connect(storage_id=storage_id, boundary_root_id=boundary)
            self.assertEqual(self.path.read_bytes(), before)
        self.connect(boundary_root_id="0")
        self.assertEqual(self.registry.read()["drives"]["new-drive"]["credential_source"]["storage_id"], 2)

    def test_reserved_current_cannot_be_connected(self):
        with self.assertRaises(ValueError):
            self.connect(drive_id="current")
        self.assertFalse(self.path.exists())

    def test_existing_library_name_is_idempotent_and_cannot_change_scope(self):
        self.register()
        before = self.path.read_bytes()
        self.registry.register(library())
        for changes in ({"library_root_id": "other-library"}, {"parent_id": "another-parent"},
                        {"name": "替换影库"}, {"test_only": True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.registry.register(library(**changes))
            self.assertEqual(self.path.read_bytes(), before)

    def test_same_physical_root_cannot_be_registered_twice_on_same_drive(self):
        self.register()
        with self.assertRaises(ValueError):
            self.registry.register(library(library_id="alias"))
        self.assertEqual(set(self.registry.read()["libraries"]), {"family"})

    def test_account_alias_cannot_duplicate_same_physical_library(self):
        self.register()
        with self.assertRaises(ValueError):
            self.connect(drive_id="account-alias", storage_id=2)
        self.assertEqual(set(self.registry.read()["drives"]), {"new-drive"})

    def test_test_only_library_is_never_a_transfer_or_watch_target(self):
        self.register(test_only=True)
        before = deepcopy(self.base)
        with self.assertRaises(ValueError):
            self.registry.config_for("family", self.base)
        self.assertEqual(self.base, before)
        self.assertTrue(self.registry.get_library("family")["test_only"])

    def test_config_for_selected_library_deepcopies_and_never_edits_base(self):
        self.register()
        before = deepcopy(self.base)
        config = self.registry.config_for("family", self.base)
        self.assertEqual(config["library_root_id"], "library-root")
        self.assertEqual(config["credential_source"], {"type": "alist", "storage_id": 2})
        self.assertEqual(config["categories"]["tv"]["folder_id"], "shows")
        self.assertEqual(config["timezone"], "Asia/Shanghai")
        self.assertFalse(config["overwrite"])
        config["categories"]["tv"]["name"] = "changed"
        config["credential_source"]["storage_id"] = 999
        self.assertEqual(self.base, before)
        self.assertEqual(self.registry.get_library("family")["categories"]["tv"]["name"], "电视剧")

    def test_current_reference_is_root_bounded_and_drops_extra_base_fields(self):
        before = deepcopy(self.base)
        reference = self.registry.get_drive("current", self.base)
        self.assertEqual(reference["boundary_root_id"], "existing-library")
        self.assertEqual(reference["credential_source"], {"type": "alist", "storage_id": 1})
        self.assertEqual(self.base, before)
        file_mode = self.registry.get_drive("current", {**self.base, "credential_source": {"type": "file"}})
        self.assertEqual(file_mode["credential_source"], {"type": "file", "name": "quark.json"})
        self.assertEqual(file_mode["boundary_root_id"], "existing-library")
        for bad in ({**self.base, "provider": "other"},
                    {**self.base, "credential_source": {"type": "environment"}}):
            with self.assertRaises(ValueError):
                self.registry.get_drive("current", bad)

    def test_current_library_cannot_be_registered_as_a_production_target(self):
        record = library(drive_id="current", parent_id="existing-library")
        with self.assertRaises(ValueError):
            self.registry.register(record)
        self.registry.register({**record, "test_only": True})
        with self.assertRaises(ValueError):
            self.registry.config_for("family", self.base)

    def test_register_rejects_extra_fields_and_invalid_boolean_before_persisting(self):
        self.connect()
        before = self.path.read_bytes()
        for changes in ({"extra": "opaque-field"}, {"credential_path": "private-path"},
                        {"test_only": "false"}, {"test_only": 0}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.registry.register(library(**changes))
            self.assertEqual(self.path.read_bytes(), before)

    def test_unknown_drives_missing_categories_and_repeated_fids_rejected(self):
        self.connect()
        samples = [library(drive_id="unknown"), library(categories={}),
                   library(categories={"tv": {"folder_id": "shows", "name": "电视剧"}}),
                   library(categories={"tv": {"folder_id": "same", "name": "电视剧"},
                                       "movie": {"folder_id": "same", "name": "电影"}}),
                   library(categories={"tv": {"folder_id": "library-root", "name": "电视剧"},
                                       "movie": {"folder_id": "movies", "name": "电影"}})]
        for record in samples:
            with self.subTest(record=record), self.assertRaises(ValueError):
                self.registry.register(record)
        self.assertEqual(self.registry.read()["libraries"], {})

    def test_world_readable_file_symlink_and_parent_symlink_are_rejected(self):
        self.connect()
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.registry.read()
        self.path.chmod(0o600)
        symlink = self.directory / "link.json"
        symlink.symlink_to(self.path)
        with self.assertRaises(ValueError):
            LibraryRegistry(symlink).read()
        parent_link = self.directory / "linked-parent"
        parent_link.symlink_to(self.path.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            LibraryRegistry(parent_link / self.path.name).read()

    def test_tampered_public_reference_is_rejected_on_read(self):
        self.register()
        data = self.registry.read()
        data["drives"]["new-drive"]["credential_source"]["cookie"] = "never-accept-this"
        self.path.write_text(json.dumps(data), encoding="utf-8")
        self.path.chmod(0o600)
        with self.assertRaises(ValueError):
            self.registry.read()


if __name__ == "__main__":
    unittest.main()
