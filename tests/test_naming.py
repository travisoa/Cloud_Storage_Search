"""Release-label provenance and consistent show/episode titles."""

import unittest

from media_helper.naming import episode_filename, media_info, movie_filename, quality_score, series_prefix


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
                         "剧名 (2026)S01E02_2160P_HDR10+.mkv")
        self.assertIsNone(media_info("01.mp4")["codec"])
        self.assertIsNone(media_info("01.mp4")["hdr"])

    def test_observed_show_directory_is_exact_prefix(self):
        for observed in ("剧名", "剧名 (2026)", "English Alias [2026]"):
            with self.subTest(observed=observed):
                prefix = series_prefix("剧名", 2026, observed)
                self.assertEqual(prefix, observed)
                self.assertEqual(episode_filename(prefix, 1, 2, ".MKV"), f"{observed}S01E02.MKV")

    def test_missing_or_season_directory_never_becomes_show_prefix(self):
        for observed in (None, "Season 01", "season_1", "第1季"):
            with self.subTest(observed=observed):
                self.assertEqual(series_prefix("剧名", 2026, observed), "剧名 (2026)")

    def test_no_label_no_fabricated_suffix_and_tags_may_be_disabled(self):
        self.assertEqual(episode_filename("剧名 (2026)", 1, 1, ".mp4", media_info("01.mp4")),
                         "剧名 (2026)S01E01.mp4")
        self.assertEqual(episode_filename("剧名 (2026)", 1, 1, ".mp4", media_info("01.4K.mp4"), False),
                         "剧名 (2026)S01E01.mp4")

    def test_audio_aliases_channels_and_atmos_are_explicit_release_labels(self):
        for label, expected in (("AAC.2.0", "AAC2.0"), ("DD+.5.1", "DDP5.1"),
                                ("E-AC-3.5.1", "DDP5.1"), ("DDP5.1", "DDP5.1"),
                                ("AC3.5.1", "AC3.5.1"), ("TrueHD.7.1.Atmos", "TrueHD7.1.Atmos"),
                                ("DTS-HD.MA.5.1", "DTS-HD.MA5.1"), ("DTS:X", "DTS-X"),
                                ("FLAC.2.0", "FLAC2.0"), ("Opus", "Opus"), ("Atmos", "Atmos")):
            with self.subTest(label=label):
                info = media_info(f"剧.S01E01.{label}.mkv")
                self.assertEqual(info["audio"], expected)
                self.assertEqual(info["audio_basis"], "filename")
                self.assertEqual(info["audio_evidence"][0]["value"], expected)
                target = episode_filename("剧", 1, 1, ".mkv", info)
                self.assertEqual(target, f"剧S01E01_{expected}.mkv")
                self.assertEqual(media_info(target)["audio"], expected)

    def test_optional_fields_are_omitted_independently_and_codec_is_not_in_filename(self):
        for source, expected in (
                ("01.1080p.HDR10.DDP5.1.mkv", "剧S01E01_1080P_HDR10_DDP5.1.mkv"),
                ("01.SDR.mkv", "剧S01E01_SDR.mkv"),
                ("01.Dolby.Vision.mkv", "剧S01E01_DV.mkv"),
                ("01.HEVC.mkv", "剧S01E01.mkv"),
                ("01.mkv", "剧S01E01.mkv")):
            with self.subTest(source=source):
                self.assertEqual(episode_filename("剧", 1, 1, ".mkv", media_info(source)), expected)

    def test_directory_audio_is_used_without_overriding_filename_or_inventing_channels(self):
        info = media_info("01.mkv", "剧 1080P SDR DDP5.1/Season 01")
        self.assertEqual((info["resolution"], info["hdr"], info["audio"]), ("1080p", "SDR", "DDP5.1"))
        self.assertEqual(info["audio_basis"], "parent_directory")
        self.assertEqual(media_info("01.AAC.mkv", "TrueHD7.1.Atmos")["audio"], "AAC")
        self.assertEqual(media_info("01.mkv", "AAC2.0/DDP5.1/Season 01")["audio"], "DDP5.1")

    def test_conflicting_audio_is_omitted_without_falling_back_to_parent(self):
        info = media_info("01.AAC2.0.DDP5.1.mkv", "TrueHD7.1.Atmos")
        self.assertIsNone(info["audio"])
        self.assertEqual(info["audio_basis"], "conflicting_filename_labels")
        self.assertEqual(episode_filename("剧", 1, 1, ".mkv", info), "剧S01E01.mkv")

    def test_audio_labels_never_infer_atmos_or_come_from_arbitrary_substrings(self):
        for source in ("剧.DDPoster.mkv", "剧.AACaster.mkv", "剧.5.1.mkv", "剧.7.1.mkv", "01.mkv"):
            with self.subTest(source=source):
                self.assertIsNone(media_info(source)["audio"])
        self.assertEqual(media_info("剧.TrueHD7.1.mkv")["audio"], "TrueHD7.1")
        self.assertEqual(media_info("剧.EAC3.5.1.mkv")["audio"], "DDP5.1")

    def test_dts_followed_by_a_video_codec_label_is_still_dts(self):
        self.assertEqual(media_info("Movie.2010.720p.BluRay.DTS.x264-GROUP.mkv")["audio"], "DTS")
        self.assertEqual(media_info("Movie.2010.DTS-x265.mkv")["audio"], "DTS")
        self.assertEqual(media_info("Movie.2010.DTS.X.7.1.mkv")["audio"], "DTS-X7.1")

    def test_dv_and_hdr_base_labels_do_not_conflict_but_sdr_does(self):
        info = media_info("01.DV.HDR10.mkv")
        self.assertEqual(info["hdr"], "Dolby Vision")
        self.assertEqual(episode_filename("剧", 1, 1, ".mkv", info), "剧S01E01_DV.mkv")
        self.assertEqual(media_info("01.HDR.HDR10+.mkv")["hdr"], "HDR10+")
        self.assertIsNone(media_info("01.HDR.SDR.mkv", "HDR10")["hdr"])
        self.assertIsNone(media_info("01.mkv")["hdr"])

    def test_movie_uses_the_same_optional_tags_without_an_episode_marker(self):
        self.assertEqual(movie_filename("片名 (2026)", ".mkv", media_info("01.4K.DV.TrueHD7.1.Atmos.mkv")),
                         "片名 (2026)_2160P_DV_TrueHD7.1.Atmos.mkv")
        self.assertEqual(movie_filename("片名 (2026)", ".mp4", media_info("01.mp4")), "片名 (2026).mp4")
        self.assertEqual(movie_filename("片名 (2026)", ".mkv", media_info("01.1080p.SDR.AAC.mkv"), False),
                         "片名 (2026).mkv")

    def test_unrecognized_audio_cannot_add_arbitrary_text_or_paths_to_target(self):
        for audio in ("AAC/other", "AAC\nother", "unknown", "AAC2.0_secret", "AAC2.0.Atmos/other"):
            with self.subTest(audio=audio):
                self.assertEqual(episode_filename("剧", 1, 1, ".mkv", {"audio": audio}), "剧S01E01.mkv")

    def test_names_and_extensions_cannot_introduce_paths(self):
        for observed in ("show/other", "show\\other", "show\nother"):
            with self.subTest(observed=observed):
                with self.assertRaises(ValueError):
                    series_prefix("剧名", 2026, observed)
        with self.assertRaises(ValueError):
            episode_filename("剧名", 1, 1, ".mkv/other")


if __name__ == "__main__":
    unittest.main()
