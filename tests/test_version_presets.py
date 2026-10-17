"""Library-wide version presets: definitions, precedence, storage and CLI."""
from argparse import Namespace
import json
from pathlib import Path
import tempfile
import unittest

from media_helper import library_cli
from media_helper.library_registry import LibraryRegistry, drive_reference
from media_helper.movie_workflow import run as run_movie
from media_helper.naming import media_info
from media_helper.tasks import confirmation_request, movie_confirmation_request
from media_helper.version_policy import (PRESETS, VersionPolicyStore, assess, attach, normalize,
                                         preset_policy)

GB = 10 ** 9
TV = {"title": "示例剧", "metadata_source": "tmdb", "series_id": "7", "year": 2026, "season": 1}


def score(preset, name, size, kind="movie"):
    return assess(preset_policy(preset, kind), media_info(name), size=size, name=name)


class PresetDefinitionTests(unittest.TestCase):
    def test_three_presets_are_valid_policies_for_movies_and_episodes(self):
        self.assertEqual(set(PRESETS), {"quality", "balanced", "storage"})
        for name in PRESETS:
            for kind in ("movie", "tv"):
                policy = preset_policy(name, kind)
                self.assertEqual(normalize(policy), policy)
        self.assertEqual(preset_policy("storage", "tv")["max_size_gb"], 2)
        self.assertEqual(preset_policy("storage", "movie")["max_size_gb"], 8)
        self.assertIsNone(preset_policy("quality", "movie")["max_size_gb"])

    def test_quality_prefers_larger_and_storage_prefers_smaller_within_one_resolution(self):
        remux, web = ("Film.2160p.REMUX.mkv", 70 * GB), ("Film.2160p.WEB-DL.mkv", 16 * GB)
        self.assertGreater(score("quality", *remux)["score"], score("quality", *web)["score"])
        small, large = ("Film.1080p.x265.mkv", 2 * GB), ("Film.1080p.x265.mkv", 6 * GB)
        self.assertGreater(score("storage", *small)["score"], score("storage", *large)["score"])

    def test_balanced_caps_size_and_falls_back_to_1080p(self):
        self.assertFalse(score("balanced", "Film.2160p.HEVC.mkv", 70 * GB)["allowed"])
        self.assertTrue(score("balanced", "Film.1080p.HEVC.mkv", 8 * GB)["allowed"])
        self.assertTrue(score("balanced", "Film.2160p.HEVC.mkv", 20 * GB)["allowed"])

    def test_storage_ranks_1080p_above_small_4k_and_rejects_large_files(self):
        self.assertGreater(score("storage", "Film.1080p.mkv", 5 * GB)["score"],
                           score("storage", "Film.2160p.mkv", 5 * GB)["score"])
        self.assertFalse(score("storage", "Show.S01E01.1080p.mkv", 3 * GB, "tv")["allowed"])

    def test_resolution_still_dominates_size(self):
        self.assertGreater(score("quality", "Film.2160p.mkv", 4 * GB)["score"],
                           score("quality", "Film.1080p.mkv", 40 * GB)["score"])


class PrecedenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_preset_applies_when_nothing_more_specific_exists_and_refreshes(self):
        first = attach(dict(TV), self.root, preset="storage")
        self.assertEqual(first["version_policy_key"], "preset:storage")
        self.assertEqual(first["quality_preference"], ["1080p", "720p", "2160p"])
        changed = attach(first, self.root, preset="quality")
        self.assertEqual(changed["quality_preference"], ["2160p", "1080p"])
        cleared = attach(changed, self.root)
        self.assertNotIn("version_policy", cleared)
        self.assertNotIn("quality_preference", cleared)

    def test_series_policy_and_explicit_quality_outrank_the_preset(self):
        VersionPolicyStore(self.root / "runtime/version-policies.json").set(TV, {"resolution_order": ["720p"]})
        self.assertEqual(attach(dict(TV), self.root, preset="quality")["quality_preference"], ["720p"])
        explicit = attach({**TV, "series_id": "8", "quality_preference": ["2160p"]}, self.root, preset="storage")
        self.assertNotIn("version_policy", explicit)

    def test_movie_requests_get_movie_size_limits(self):
        movie = attach({"kind": "movie", "title": "片", "movie_id": "1", "year": 2024}, self.root, preset="balanced")
        self.assertEqual(movie["version_policy"]["max_size_gb"], 25)

    def test_stored_policies_written_before_size_preference_still_load(self):
        path = self.root / "runtime/version-policies.json"
        store = VersionPolicyStore(path)
        store.set(TV, {"resolution_order": ["1080p"]})
        data = json.loads(path.read_text())
        for row in data["series"].values():
            row["policy"].pop("prefer_size")
        path.write_text(json.dumps(data))
        path.chmod(0o600)
        self.assertEqual(len(store.all()), 1)

    def test_explicit_confirmation_overrides_the_library_preset(self):
        request = attach({**TV, "library_root_id": "root"}, self.root, preset="storage")
        candidate = {"candidate_id": "a" * 64, "fid": "f", "name": "示例剧.S01E01.2160p.mkv", "size": 9 * GB,
                     "episode_key": "S01E01", "media_info": {"resolution": "2160p"}}
        task = {"status": "NEEDS_INPUT", "resolved_request": request,
                "report": {"source_candidates": [candidate], "missing": [{"episode_key": "S01E01"}]}}
        confirmed, _ = confirmation_request(task, quality="2160p")
        self.assertTrue(confirmed["library_preset_overridden"])
        self.assertNotIn("version_policy_key", attach(confirmed, self.root, preset="storage"))
        movie = attach({"kind": "movie", "movie_id": "1", "year": 2024, "title": "片"}, self.root, preset="storage")
        movie_task = {"status": "NEEDS_INPUT", "resolved_request": movie,
                      "report": {"source_candidates": [{**candidate, "excluded_by_version_policy": True}]}}
        chosen, _ = movie_confirmation_request(movie_task, selection={"movie": "a" * 64})
        self.assertEqual(chosen["source_selection"], {"movie": "a" * 64})
        self.assertNotIn("version_policy", attach(chosen, self.root, preset="storage"))


class MovieSelectionTests(unittest.TestCase):
    def test_quality_preset_resolves_equal_4k_versions_by_size(self):
        from test_movie_workflow import Journal, Search, Storage, file, request
        storage = Storage()
        storage.shared["s-dir"].append(file("v4k-big", "沙丘2.2024.2160p.REMUX.mkv", "s-dir", size=70 * GB))
        policy = preset_policy("quality", "movie")
        report = run_movie(request(version_policy=policy), storage, Search(), Journal())
        self.assertEqual(report["status"], "READY", report)
        self.assertEqual(report["plan"][0]["source"]["fid"], "v4k-big")
        storage_policy = preset_policy("storage", "movie")
        small = run_movie(request(version_policy=storage_policy), Storage(), Search(), Journal())
        self.assertEqual(small["plan"][0]["source"]["fid"], "v1080")


class RegistryAndCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "config").mkdir(mode=0o700)
        self.config = {"library_root_id": "root", "provider": "quark",
                       "credential_source": {"type": "alist", "storage_id": 1}}
        (self.root / "config/headless.json").write_text(json.dumps(self.config))
        (self.root / "config/headless.json").chmod(0o600)

    def library(self, **changes):
        return {"library_id": "family", "drive_id": "new-drive", "parent_id": "0",
                "library_root_id": "library-root", "name": "影库",
                "categories": {"movie": {"folder_id": "movies", "name": "电影"},
                               "tv": {"folder_id": "shows", "name": "电视剧"}},
                "init_job_key": "library-init:" + "a" * 64, "test_only": False, **changes}

    def test_registry_keeps_validates_and_passes_the_preset(self):
        registry = LibraryRegistry(self.root / "runtime/libraries.json")
        registry.connect(drive_reference("new-drive", 2))
        registry.register(self.library(version_preset="balanced"))
        self.assertEqual(registry.config_for("family", self.config)["version_preset"], "balanced")
        self.assertEqual(registry.set_preset("family", "storage")["version_preset"], "storage")
        self.assertNotIn("version_preset", registry.set_preset("family", None))
        with self.assertRaises(ValueError):
            registry.set_preset("family", "tiny")

    def test_default_library_preset_command_writes_only_local_config(self):
        args = Namespace(library_action="preset", library_id=None, preset="quality", clear=False)
        result = library_cli.run_command(args, self.config, self.root)
        self.assertEqual((result["status"], result["version_preset"]), ("PRESET_SAVED", "quality"))
        self.assertFalse(result["cloud_writes_performed"])
        saved = json.loads((self.root / "config/headless.json").read_text())
        self.assertEqual(saved["version_preset"], "quality")
        shown = library_cli.run_command(Namespace(library_action="preset", library_id=None, preset=None, clear=False),
                                        saved, self.root)
        self.assertEqual(len(shown["choices"]), 3)


if __name__ == "__main__":
    unittest.main()
