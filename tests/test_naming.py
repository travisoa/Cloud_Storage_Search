"""Release-label provenance and consistent show/episode titles."""

import unittest

from media_helper.naming import episode_filename, media_info, quality_score, series_prefix


class NamingTests(unittest.TestCase):
    def test_known_resolution_labels_are_canonical(self):
        for label, expected in (("4K", "2160p"), ("UHD", "2160p"),
                                ("2160P", "2160p"), ("1080P", "1080p"), ("720p", "720p")):
            with self.subTest(label=label):
                info = media_info(f"剧名.S01E01.{label}.mkv")
                self.assertEqual(info["resolution"], expected)
                self.assertEqual(info["basis"], "filename")
                self.assertEqual(info["evidence"][0]["source"], "filename")

    def test_filename_beats_parent_but_both_label_sources_remain_visible(self):
        info = media_info("剧名.S01E01.1080p.mkv", "剧名 4K/Season 01")
        self.assertEqual(info["resolution"], "1080p")
        self.assertEqual(info["basis"], "filename")
        self.assertEqual({row["value"] for row in info["evidence"]}, {"1080p", "2160p"})

    def test_nearest_labelled_parent_wins(self):
        info = media_info("01.mp4", "剧名 UHD/1080P/Season 01")
        self.assertEqual(info["resolution"], "1080p")
        self.assertEqual(info["basis"], "parent_directory")

    def test_conflicting_same_level_labels_remain_unknown(self):
        for name, path, expected_basis in (("01.4K.1080p.mkv", "UHD", "conflicting_filename_labels"),
                                           ("01.mp4", "剧名 4K 1080p", "conflicting_parent_directory_labels")):
            with self.subTest(name=name, path=path):
                info = media_info(name, path)
                self.assertIsNone(info["resolution"])
                self.assertEqual(info["basis"], expected_basis)
                self.assertEqual(quality_score(info, ["2160p", "1080p"]), 0)

    def test_labels_are_not_arbitrary_substrings_or_resolution_guesses(self):
        for name in ("show.12160p.mkv", "show.UHDTV.mkv", "show.1080i.mkv", "show.4kbit.mkv", "01.mp4"):
            with self.subTest(name=name):
                self.assertIsNone(media_info(name)["resolution"])

    def test_codec_and_hdr_require_explicit_labels(self):
        info = media_info("剧名.S01E01.2160p.x265.HDR10+.mkv")
        self.assertEqual((info["codec"], info["hdr"]), ("HEVC", "HDR10+"))
        self.assertEqual(info["inspection"], "release_labels_only")
        self.assertEqual(episode_filename("剧名 (2026)", 1, 2, ".mkv", info),
                         "剧名 (2026) S01E02 [2160p HEVC HDR10+].mkv")
        self.assertIsNone(media_info("01.mp4")["codec"])
        self.assertIsNone(media_info("01.mp4")["hdr"])

    def test_observed_show_directory_is_exact_prefix(self):
        for observed in ("剧名", "剧名 (2026)", "English Alias [2026]"):
            with self.subTest(observed=observed):
                prefix = series_prefix("剧名", 2026, observed)
                self.assertEqual(prefix, observed)
                self.assertEqual(episode_filename(prefix, 1, 2, ".MKV"), f"{observed} S01E02.MKV")

    def test_missing_or_season_directory_never_becomes_show_prefix(self):
        for observed in (None, "Season 01", "season_1", "第1季"):
            with self.subTest(observed=observed):
                self.assertEqual(series_prefix("剧名", 2026, observed), "剧名 (2026)")

    def test_no_label_no_fabricated_suffix_and_tags_may_be_disabled(self):
        self.assertEqual(episode_filename("剧名 (2026)", 1, 1, ".mp4", media_info("01.mp4")),
                         "剧名 (2026) S01E01.mp4")
        self.assertEqual(episode_filename("剧名 (2026)", 1, 1, ".mp4", media_info("01.4K.mp4"), False),
                         "剧名 (2026) S01E01.mp4")

    def test_names_and_extensions_cannot_introduce_paths(self):
        for observed in ("show/other", "show\\other", "show\nother"):
            with self.subTest(observed=observed):
                with self.assertRaises(ValueError):
                    series_prefix("剧名", 2026, observed)
        with self.assertRaises(ValueError):
            episode_filename("剧名", 1, 1, ".mkv/other")


if __name__ == "__main__":
    unittest.main()
