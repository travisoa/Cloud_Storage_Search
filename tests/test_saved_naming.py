"""Offline saved-fid naming corrections and their independent recovery intent."""

from copy import deepcopy
import unittest

from media_helper.naming import media_info
from media_helper.saved_naming import normalize_saved
from media_helper.workflow import _digest, _execution_config, _job_identity


def file(fid, name, parent, size=123, directory=False):
    return {"fid": fid, "name": name, "parent_id": parent, "size": size, "is_dir": directory}


class Journal:
    def __init__(self):
        self.records, self.events = {}, []

    def get(self, key):
        return deepcopy(self.records.get(key))

    def put(self, key, record):
        self.records[key] = deepcopy(record)
        self.events.append((key, deepcopy(record)))


class Metadata:
    def get_series(self, identifier):
        return {"id": identifier, "source": "tmdb", "title": "测试剧", "year": 2026}


class Storage:
    def __init__(self):
        self.folders = {
            "root": [file("series", "测试剧 (2026)", "root", directory=True)],
            "series": [file("season", "Season 01", "series", directory=True)],
            "season": [file("old-1", "测试剧 S01E01.mkv", "season"),
                       file("new-2", "测试剧 S01E02.mkv", "season", size=456),
                       file("new-sub-2", "测试剧 S01E02.zh-Hans.forced.ass", "season", size=7)]}
        self.renamed = []

    def list_files(self, parent):
        return deepcopy(self.folders[parent])

    def rename(self, fid, name):
        self.renamed.append((fid, name))
        for rows in self.folders.values():
            for row in rows:
                if row["fid"] == fid:
                    row["name"] = name
                    return {"fid": fid, "name": name}
        raise KeyError(fid)


class SavedNamingTests(unittest.TestCase):
    def setUp(self):
        self.storage, self.journal, self.metadata = Storage(), Journal(), Metadata()
        self.request = {"title": "测试剧", "year": 2026, "season": 1, "series_id": 17,
                        "metadata_source": "tmdb", "library_root_id": "root"}
        self.identity = _job_identity(self.request, self.storage)
        self.key = "season:" + _digest(self.identity)
        source = {"source_fid": "source-2", "name": "02.mkv", "size": 456,
                  "episode_key": "S01E02", "target_name": "测试剧 S01E02.mkv"}
        subtitle = {"source_fid": "sub-source-2", "name": "02.zh-Hans.forced.ass", "size": 7,
                    "episode_key": "S01E02", "target_name": "测试剧 S01E02.zh-Hans.forced.ass"}
        operation = {"state": "complete", "target_parent_id": "season", "before_fids": ["old-1"],
                     "files": [source, subtitle], "saved_fids": ["new-2", "new-sub-2"],
                     "verified_files": [{**source, "saved_fid": "new-2", "verified": True},
                                        {**subtitle, "saved_fid": "new-sub-2", "verified": True}]}
        plan = [{"episode_key": row["episode_key"], "candidate_id": _digest(row["source_fid"]),
                 "source": {"fid": row["source_fid"], "name": row["name"], "size": row["size"]},
                 "target_name": row["target_name"]} for row in [source, subtitle]]
        plan[0]["media_info"] = media_info("02.mkv", "测试剧 4K")
        self.receipt = {"status": "COMPLETE", "job_key": self.key, "operations": [operation], "plan": plan}
        self.journal.records[self.key] = {"state": "complete", "identity": self.identity,
            "configuration": _execution_config(self.request), "target_folder_id": "season",
            "operations": deepcopy([operation]), "plan": deepcopy(plan)}

    def run_normalize(self, apply=False, request=None, receipt=None):
        return normalize_saved(request or self.request, self.storage, self.metadata,
                               self.journal, receipt or self.receipt, apply=apply)

    def test_default_plan_is_cloud_and_journal_read_only(self):
        before = deepcopy((self.storage.folders, self.journal.records, self.journal.events))
        result = self.run_normalize()
        self.assertEqual(result["status"], "READY", result)
        self.assertFalse(result["writes_attempted"])
        self.assertEqual(result["naming_prefix"], "测试剧 (2026)")
        self.assertEqual(result["plan"][0]["target_name"], "测试剧 (2026)S01E02_2160P.mkv")
        self.assertEqual(result["plan"][1]["target_name"], "测试剧 (2026)S01E02_2160P.zh-Hans.forced.ass")
        self.assertEqual((self.storage.folders, self.journal.records, self.journal.events), before)

    def test_apply_only_verified_new_fids_and_keep_real_source_names(self):
        old = deepcopy(self.storage.folders["season"][0])
        result = self.run_normalize(apply=True)
        self.assertEqual(result["status"], "COMPLETE", result)
        self.assertEqual(self.storage.renamed, [("new-2", "测试剧 (2026)S01E02_2160P.mkv"),
            ("new-sub-2", "测试剧 (2026)S01E02_2160P.zh-Hans.forced.ass")])
        self.assertEqual(self.storage.folders["season"][0], old)
        base = self.journal.get(self.key)
        self.assertEqual(base["operations"][0]["files"][0]["name"], "02.mkv")
        self.assertEqual(base["operations"][0]["files"][0]["target_name"], result["plan"][0]["target_name"])
        self.assertEqual(base["operations"][0]["verified_files"][0]["target_name"], result["plan"][0]["target_name"])
        self.assertEqual(base["plan"][0]["target_name"], result["plan"][0]["target_name"])
        self.assertEqual(result["updated_receipt"]["plan"][0]["target_name"], result["plan"][0]["target_name"])

    def test_every_rename_has_a_durable_new_fid_intent_before_mutation(self):
        original = self.storage.rename

        def check(fid, name):
            intents = [row for key, row in self.journal.events if key.startswith("naming:")]
            self.assertTrue(intents)
            row = intents[-1]["files"][fid]
            self.assertEqual((row["state"], row["target_name"], row["parent_id"]),
                             ("rename_pending", name, "season"))
            return original(fid, name)

        self.storage.rename = check
        self.assertEqual(self.run_normalize(apply=True)["status"], "COMPLETE")

    def test_old_fid_cannot_be_adopted_even_when_receipt_and_journal_are_modified(self):
        for operations in (self.receipt["operations"], self.journal.records[self.key]["operations"]):
            operations[0]["saved_fids"][0] = "old-1"
            operations[0]["verified_files"][0]["saved_fid"] = "old-1"
        result = self.run_normalize(apply=True)
        self.assertEqual(result["status"], "NEEDS_INPUT", result)
        self.assertEqual(self.storage.renamed, [])

    def test_forged_receipt_fid_does_not_match_journal(self):
        self.receipt["operations"][0]["saved_fids"][0] = "another-fid"
        self.receipt["operations"][0]["verified_files"][0]["saved_fid"] = "another-fid"
        self.assertEqual(self.run_normalize(apply=True)["status"], "NEEDS_INPUT")
        self.assertEqual(self.storage.renamed, [])

    def test_unverified_fid_and_changed_source_identity_are_rejected(self):
        for change in ({"verified": False}, {"source_fid": "forged-source"}, {"name": "99.mkv"}, {"size": 999}):
            with self.subTest(change=change):
                receipt = deepcopy(self.receipt)
                receipt["operations"][0]["verified_files"][0].update(change)
                self.assertEqual(self.run_normalize(apply=True, receipt=receipt)["status"], "NEEDS_INPUT")
        self.assertEqual(self.storage.renamed, [])

    def test_target_conflict_preserves_all_files(self):
        self.storage.folders["season"].append(file("external", "测试剧 (2026)S01E02_2160P.mkv", "season"))
        before = deepcopy(self.storage.folders)
        result = self.run_normalize(apply=True)
        self.assertEqual(result["status"], "NEEDS_INPUT", result)
        self.assertEqual(self.storage.folders, before)
        self.assertEqual(self.storage.renamed, [])

    def test_external_name_size_or_parent_changes_are_rejected(self):
        original = deepcopy(self.storage.folders["season"])
        for change in ({"name": "another-name.mkv"}, {"size": 0}, {"parent_id": "another-parent"}):
            with self.subTest(change=change):
                self.storage.folders["season"] = deepcopy(original)
                self.storage.folders["season"][1].update(change)
                result = self.run_normalize(apply=True)
                self.assertEqual(result["status"], "NEEDS_INPUT", result)
        self.assertEqual(self.storage.renamed, [])

    def test_wrong_root_or_receipt_job_key_cannot_rename(self):
        self.assertEqual(self.run_normalize(apply=True, request={**self.request, "library_root_id": "other-root"})["status"], "NEEDS_INPUT")
        self.assertEqual(self.run_normalize(apply=True, receipt={**self.receipt, "job_key": "season:forged"})["status"], "NEEDS_INPUT")
        self.assertEqual(self.storage.renamed, [])

    def test_unknown_success_recovers_by_fid_without_second_rename(self):
        original = self.storage.rename

        def lost(fid, name):
            original(fid, name)
            raise RuntimeError("transport response lost; raw secret must not appear")

        self.storage.rename = lost
        first = self.run_normalize(apply=True)
        self.assertEqual(first["status"], "UNKNOWN", first)
        self.assertNotIn("raw secret", str(first))
        self.storage.rename = original
        result = self.run_normalize(apply=True)
        self.assertEqual(result["status"], "COMPLETE", result)
        self.assertEqual([fid for fid, _ in self.storage.renamed], ["new-2", "new-sub-2"])

    def test_unknown_failure_requires_fresh_old_name_readback_before_retry(self):
        original = self.storage.rename
        self.storage.rename = lambda *args: (_ for _ in ()).throw(RuntimeError("unknown"))
        self.assertEqual(self.run_normalize(apply=True)["status"], "UNKNOWN")
        self.storage.rename = original
        self.assertEqual(self.run_normalize(apply=True)["status"], "COMPLETE")
        self.assertEqual(len(self.storage.renamed), 2)

    def test_repeat_with_original_or_updated_receipt_never_renames_again(self):
        first = self.run_normalize(apply=True)
        before = deepcopy(self.storage.renamed)
        self.assertEqual(self.run_normalize(apply=True)["status"], "UP_TO_DATE")
        self.assertEqual(self.run_normalize(apply=True, receipt=first["updated_receipt"])["status"], "UP_TO_DATE")
        self.assertEqual(self.storage.renamed, before)

    def test_missing_subtitle_evidence_is_not_silently_skipped(self):
        receipt = deepcopy(self.receipt)
        receipt["operations"][0]["saved_fids"].pop()
        receipt["operations"][0]["verified_files"].pop()
        receipt["operations"][0]["files"].pop()
        receipt["plan"].pop()
        self.assertEqual(self.run_normalize(apply=True, receipt=receipt)["status"], "NEEDS_INPUT")
        self.assertEqual(self.storage.renamed, [])

    def test_ambiguous_subtitle_pairing_blocks_all_renames(self):
        for record in (self.receipt, self.journal.records[self.key]):
            for row in record["operations"][0]["files"] + record["operations"][0]["verified_files"]:
                if row["source_fid"] == "sub-source-2":
                    row["name"] = "other.zh.ass"
            record["plan"][1]["source"]["name"] = "other.zh.ass"
        self.assertEqual(self.run_normalize(apply=True)["status"], "NEEDS_INPUT")
        self.assertEqual(self.storage.renamed, [])

    def test_quality_preference_does_not_create_unobserved_resolution_label(self):
        self.receipt["plan"][0].pop("media_info")
        result = self.run_normalize(request={**self.request, "quality_preference": ["2160p"]})
        self.assertEqual(result["plan"][0]["target_name"], "测试剧 (2026)S01E02.mkv")

    def test_unsubstantiated_media_claims_block_the_plan(self):
        self.receipt["plan"][0]["media_info"] = {"resolution": "2160p", "inspection": "release_labels_only"}
        result = self.run_normalize(apply=True)
        self.assertEqual(result["status"], "NEEDS_INPUT", result)
        self.assertEqual(self.storage.renamed, [])

    def test_recorded_directory_audio_labels_are_preserved_with_subtitles(self):
        self.receipt["plan"][0]["media_info"] = media_info("02.mkv", "测试剧 4K SDR DDP5.1")
        result = self.run_normalize()
        self.assertEqual(result["status"], "READY", result)
        self.assertEqual(result["plan"][0]["target_name"], "测试剧 (2026)S01E02_2160P_SDR_DDP5.1.mkv")
        self.assertEqual(result["plan"][1]["target_name"], "测试剧 (2026)S01E02_2160P_SDR_DDP5.1.zh-Hans.forced.ass")

    def test_audio_requires_matching_recorded_provenance(self):
        for audio_info in (
                {"audio": "DDP5.1"},
                {"audio": "DDP5.1", "audio_basis": "filename", "audio_evidence": [
                    {"source": "filename", "text": "02.DDP5.1.mkv", "value": "DDP5.1"}]},
                {"audio": "DDP5.1", "audio_basis": "parent_directory", "audio_evidence": [
                    {"source": "parent_directory", "text": "DDP2.0", "value": "DDP5.1"}]},
                {"audio": "DDP5.1", "audio_basis": "parent_directory", "audio_evidence": [
                    {"source": "parent_directory", "text": "DDP5.1", "value": "DDP"}]},
                {"audio": "DDP5.1", "audio_basis": "parent_directory", "audio_evidence": None}):
            with self.subTest(audio_info=audio_info):
                receipt = deepcopy(self.receipt)
                receipt["plan"][0]["media_info"].update(audio_info)
                result = self.run_normalize(apply=True, receipt=receipt)
                self.assertEqual(result["status"], "NEEDS_INPUT", result)
                self.assertEqual(result["validation_error"], "media_labels_require_recorded_provenance")
        self.assertEqual(self.storage.renamed, [])

    def test_legacy_audio_can_come_from_saved_source_filename(self):
        name = "测试剧.S01E02.DDP5.1.mkv"
        subtitle = "测试剧.S01E02.DDP5.1.zh-Hans.forced.ass"
        for record in (self.receipt, self.journal.records[self.key]):
            record["plan"][0]["source"]["name"] = name
            record["plan"][1]["source"]["name"] = subtitle
            for field in ("files", "verified_files"):
                record["operations"][0][field][0]["name"] = name
                record["operations"][0][field][1]["name"] = subtitle
        info = self.receipt["plan"][0]["media_info"]
        for field in ("audio", "audio_basis", "audio_evidence"):
            info.pop(field, None)
        result = self.run_normalize()
        self.assertEqual(result["status"], "READY", result)
        self.assertEqual(result["plan"][0]["target_name"], "测试剧 (2026)S01E02_2160P_DDP5.1.mkv")
        self.assertEqual(result["plan"][0]["media_info"]["audio_basis"], "filename")

    def test_legacy_audio_can_come_from_validated_recorded_directory_text(self):
        info = media_info("02.mkv", "测试剧 4K DDP5.1")
        for field in ("audio", "audio_basis", "audio_evidence"):
            info.pop(field, None)
        self.receipt["plan"][0]["media_info"] = info
        result = self.run_normalize()
        self.assertEqual(result["status"], "READY", result)
        self.assertEqual(result["plan"][0]["target_name"], "测试剧 (2026)S01E02_2160P_DDP5.1.mkv")
        self.assertEqual(result["plan"][0]["media_info"]["audio_basis"], "parent_directory")

    def test_unvalidated_legacy_evidence_does_not_add_audio(self):
        info = self.receipt["plan"][0]["media_info"]
        for field in ("audio", "audio_basis", "audio_evidence"):
            info.pop(field, None)
        info["codec_evidence"] = [{"source": "parent_directory", "text": "DDP5.1", "value": "HEVC"}]
        result = self.run_normalize()
        self.assertEqual(result["status"], "READY", result)
        self.assertEqual(result["plan"][0]["target_name"], "测试剧 (2026)S01E02_2160P.mkv")

    def test_conflicting_recorded_legacy_directory_audio_is_omitted(self):
        info = media_info("02.mkv", "测试剧 4K DDP5.1/HEVC AAC2.0")
        for field in ("audio", "audio_basis", "audio_evidence"):
            info.pop(field, None)
        self.receipt["plan"][0]["media_info"] = info
        result = self.run_normalize()
        self.assertEqual(result["status"], "READY", result)
        self.assertEqual(result["plan"][0]["target_name"], "测试剧 (2026)S01E02_2160P.mkv")
        self.assertIsNone(result["plan"][0]["media_info"]["audio"])


if __name__ == "__main__":
    unittest.main()
