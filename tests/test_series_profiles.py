from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from media_helper.profiles import ProfileMetadata, ProfileStore


class Metadata:
    def __init__(self):
        self.series = {"id": 17, "name": "White Mafia", "year": 2026, "season_count": 1, "aliases": []}

    def get_series(self, _):
        return deepcopy(self.series)


class SeriesProfileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "profiles.json"
        self.store, self.metadata = ProfileStore(self.path), Metadata()

    def bind(self, **changes):
        return self.store.bind(**{"metadata": self.metadata, "source": "tvmaze", "series_id": "17", "year": 2026,
                                  "season": 1, "title": "白色黑手党时代", "canonical_title": "White Mafia",
                                  "aliases": ["白色黑手党"], "evidence_urls": ["https://www.tvmaze.com/shows/17/white-mafia"],
                                  "quality_preference": ["4K"], **changes})

    def test_profile_is_stable_identity_with_private_permissions(self):
        profile = self.bind()
        self.assertEqual(profile["profile_key"], "tvmaze:17:2026")
        self.assertEqual(profile["quality_preference"], ["2160p"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.match({"title": "白色黑手党"}), profile)
        self.assertIsNone(self.store.match({"title": "另一部剧"}))
        self.assertIsNone(self.store.match({"title": "白色黑手党", "year": 2025}))
        self.assertIsNone(self.store.match({"title": "白色黑手党", "series_id": 99}))

    def test_profile_checks_remote_canonical_id_and_year_before_saving(self):
        for key, value in (("id", 99), ("name", "Other Show"), ("year", 2025)):
            with self.subTest(key=key):
                original = deepcopy(self.metadata.series)
                self.metadata.series[key] = value
                with self.assertRaises(ValueError):
                    self.bind()
                self.assertFalse(self.path.exists())
                self.metadata.series = original

    def test_canonical_metadata_name_keeps_real_punctuation(self):
        self.metadata.series["name"] = "Doctor X: Mafia in White"
        profile = self.bind(canonical_title="Doctor X: Mafia in White", aliases=["Doctor X / 닥터 X"])
        self.assertEqual(profile["canonical_title"], "Doctor X: Mafia in White")
        self.assertIn("Doctor X / 닥터 X", ProfileMetadata(self.metadata, profile).get_series(17)["aliases"])

    def test_evidence_rejects_credentials_unsafe_scheme_and_secret_query(self):
        for url in ("http://example.com/show", "https://user:pass@example.com/show", "https://example.com/show?access_token=private",
                    "https://example.com/show#private", "https://example.com/show?COOKIE=private"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.bind(evidence_urls=[url])
        with self.assertRaises(ValueError):
            self.bind(evidence_urls=[])
        self.assertFalse(self.path.exists())

    def test_aliases_injected_only_after_current_id_year_canonical_verification(self):
        profile = self.bind()
        wrapped = ProfileMetadata(self.metadata, profile)
        self.assertIn("白色黑手党时代", wrapped.get_series(17)["aliases"])
        self.assertEqual(self.metadata.series["aliases"], [])
        self.metadata.series["name"] = "Changed Show"
        with self.assertRaises(ValueError):
            wrapped.get_series(17)

    def test_same_name_different_series_remains_ambiguous(self):
        self.bind()
        self.metadata.series.update(id=18, year=2025, name="Different Show")
        self.bind(series_id="18", year=2025, canonical_title="Different Show")
        with self.assertRaises(ValueError):
            self.store.match({"title": "白色黑手党时代"})
        self.assertEqual(self.store.match({"title": "白色黑手党时代", "year": 2026})["series_id"], "17")

    def test_remember_quality_is_scoped_to_verified_profile(self):
        profile = self.bind()
        self.store.remember_quality(profile, ["1080p", "720p"])
        self.assertEqual(self.store.match({"title": "白色黑手党时代"})["quality_preference"], ["1080p", "720p"])
        self.assertEqual(list(json.loads(self.path.read_text())["series"]), ["tvmaze:17:2026"])
