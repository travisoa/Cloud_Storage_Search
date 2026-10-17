"""Offline integration scenarios: fake HTTP providers, real reconciliation engine."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from media_helper.workflow import _digest, run
from media_helper.providers import AuthenticationError, RiskControlError


def file(fid, name, parent="season", size=123, directory=False):
    return {"fid": fid, "name": name, "parent_id": parent, "size": size,
            "is_dir": directory}


def request(**changes):
    return {"title": "测试剧", "year": 2026, "season": 1, "series_id": 17,
            "metadata_source": "tmdb", "library_root_id": "root",
            "as_of": "2026-10-16T20:00:00+08:00", **changes}


def confirmed_source(fid="source-2", name="02.mkv", size=123, episode=2, season=1):
    identifier = _digest((_digest("https://pan.quark.cn/s/fake-share"), fid))
    return identifier, {"season": season, "episode": episode, "name": name, "size": size}


class Journal:
    def __init__(self):
        self.records, self.events = {}, []

    def get(self, key):
        return deepcopy(self.records.get(key))

    def put(self, key, record):
        self.events.append(deepcopy(record))
        self.records[key] = deepcopy(record)


class Metadata:
    def __init__(self, episodes=(1, 2)):
        self.episodes = [{"season": 1, "episode": number, "air_date": "2026-10-15",
                          "source": "tmdb", "date_precision": "date"} for number in episodes]

    def get_series(self, identifier):
        return {"id": identifier, "title": "测试剧", "year": 2026, "source": "tmdb"}

    def get_season(self, identifier, season):
        return deepcopy(self.episodes)

    def search_tv(self, title, year=None):
        return [{"id": 17, "title": title, "year": year or 2026, "source": "tmdb"},
                {"id": 18, "title": title, "year": 2000, "source": "tmdb"}]


class Search:
    def __init__(self, hits=None):
        self.hits = hits or [{"title": "测试剧", "url": "https://pan.quark.cn/s/fake-share", "provider": "quark"}]
        self.calls = 0

    def search(self, query):
        self.calls += 1
        return deepcopy(self.hits)


class UnknownSave(RuntimeError):
    task_id = "task-1"


class Storage:
    def __init__(self):
        self.folders = {
            "root": [file("series", "测试剧 (2026)", "root", directory=True)],
            "series": [file("season", "Season 01", "series", directory=True)],
            "season": [file("old-1", "测试剧 S01E01.MKV")],
        }
        self.shared = [file("source-1", "测试剧.S01E01.1080p.mkv"),
                       file("source-2", "测试剧.S01E02.1080p.MKV"),
                       file("subtitle-2", "测试剧.S01E02.1080p.zh-Hans.forced.ASS", size=7)]
        self.saved, self.renamed, self.created = [], [], []
        self.fail_save = self.fail_rename = self.no_task_fids = self.conflict_after_save = False
        self.task_ids = []

    def list_files(self, parent):
        return deepcopy(self.folders[parent])

    def share_files(self, url, password=None, parent_id="0"):
        return {"share_ref": {"provider": "quark", "share_id": "share-1", "parent_id": parent_id,
                              "stoken": "do-not-persist-this"},
                "files": [dict(deepcopy(row), share_fid_token="do-not-persist-this-either") for row in self.shared]}

    def save_files(self, share, files, target):
        self.saved.append([row["fid"] for row in files])
        self.task_ids = []
        for source in files:
            new_id = "new-" + source["fid"]
            self.task_ids.append(new_id)
            self.folders[target].append(file(new_id, source["name"], target, size=source["size"]))
        if self.conflict_after_save:
            self.folders[target].append(file("concurrent", "测试剧 (2026) S01E02 [1080p].MKV"))
        if self.fail_save:
            raise UnknownSave("secret transport URL and Cookie value")
        return {"task_id": "task-1", "status": "done",
                "saved_fids": [] if self.no_task_fids else self.task_ids}

    def task_status(self, task_id):
        return {"status": "done", "task_id": task_id, "saved_fids": self.task_ids}

    def rename(self, fid, name):
        if self.fail_rename:
            raise RuntimeError("do not print raw provider response")
        self.renamed.append((fid, name))
        for rows in self.folders.values():
            for row in rows:
                if row["fid"] == fid:
                    row["name"] = name
                    return {"fid": fid, "name": name, "status": "done"}
        raise KeyError(fid)

    def mkdir(self, parent, name):
        fid = "dir-" + str(len(self.created))
        row = file(fid, name, parent, directory=True)
        self.folders[parent].append(row)
        self.folders[fid] = []
        self.created.append(row)
        return deepcopy(row)


class WorkflowScenarios(unittest.TestCase):
    def setUp(self):
        self.storage, self.metadata, self.search, self.journal = Storage(), Metadata(), Search(), Journal()

    def run_workflow(self, apply=False, **changes):
        return run(request(**changes), self.storage, self.metadata, self.search, self.journal, apply=apply)

    def test_plan_lists_missing_only_and_never_writes_cloud_or_journal(self):
        report = self.run_workflow()
        self.assertEqual(report["status"], "READY", report)
        self.assertEqual([row["episode_key"] for row in report["missing"]], ["S01E02"])
        self.assertEqual([row["source"]["fid"] for row in report["plan"]], ["source-2", "subtitle-2"])
        self.assertFalse(report["writes_performed"])
        self.assertEqual((self.storage.saved, self.storage.renamed, self.journal.events), ([], [], []))

    def test_selected_plan_retains_safe_share_directory_and_label_references(self):
        from media_helper.sources import extract_selected_sources
        url = "https://pan.quark.cn/s/0a1b2c3d4e5f"
        self.search.hits = [{"url": url, "title": "测试剧", "parent_id": "4kfolder", "path": "测试剧/4K"}]
        self.storage.shared = [file("source-2", "测试剧.S01E02.mkv"),
                               file("subtitle-2", "测试剧.S01E02.zh-Hans.srt", size=7)]
        calls = []
        original = self.storage.share_files
        def listing(url, password=None, parent_id="0"):
            calls.append(parent_id)
            value = original(url, password, parent_id)
            if parent_id == "0":
                value["files"] = [file("share-series", "测试剧", "0", directory=True)]
            elif parent_id == "share-series":
                value["files"] = [file("4kfolder", "4K", "share-series", directory=True)]
            return value
        self.storage.share_files = listing
        report = self.run_workflow()
        self.assertEqual(report["status"], "READY", report)
        self.assertEqual(calls, ["0", "share-series", "4kfolder"])
        self.assertEqual(report["source_refs"], [{"url": url, "title": "测试剧", "source_hash": _digest(url)}])
        for candidate in report["source_candidates"]:
            self.assertEqual(candidate["share_source"]["source_hash"], report["source_refs"][0]["source_hash"])
        sources = extract_selected_sources(report)
        self.assertEqual(sources, [{"url": url, "title": "测试剧", "source_hash": _digest(url),
                                   "parent_id": "4kfolder", "path": "测试剧/4K", "quality_tags": ["2160p"]}])
        self.assertEqual(len(report["plan"]), 2)
        self.assertEqual(report["plan"][0]["source_ref"], report["plan"][1]["source_ref"])
        self.assertNotIn("stoken", json.dumps(report))
        self.assertNotIn("do-not-persist", json.dumps(report))

    def test_remembered_share_labels_require_observed_ancestor_names_and_exact_parent_fid(self):
        url = "https://pan.quark.cn/s/0a1b2c3d4e5f"
        for problem in ("renamed", "replaced", "unreadable", "duplicate", "missing_path", "invented_root_path"):
            with self.subTest(problem=problem):
                storage, journal = Storage(), Journal()
                folders = {
                    "0": [file("share-series", "测试剧", "0", directory=True)],
                    "share-series": [file("4kfolder", "4K", "share-series", directory=True)],
                    "4kfolder": [file("source-2", "测试剧.S01E02.mkv", "4kfolder")],
                }
                hit = {"url": url, "title": "测试剧", "parent_id": "4kfolder", "path": "测试剧/4K"}
                if problem == "renamed":
                    folders["share-series"][0]["name"] = "1080p"
                elif problem == "replaced":
                    folders["share-series"][0]["fid"] = "replacement-4k"
                elif problem == "duplicate":
                    folders["share-series"].append(file("other-4k", "4K", "share-series", directory=True))
                elif problem == "missing_path":
                    hit.pop("path")
                elif problem == "invented_root_path":
                    hit.update(parent_id="0", path="4K")
                calls = []
                def listing(url, password=None, parent_id="0"):
                    calls.append(parent_id)
                    if problem == "unreadable" and parent_id == "0":
                        raise RuntimeError("share root unavailable")
                    return {"share_ref": {"share_id": "share-1", "parent_id": parent_id},
                            "files": deepcopy(folders[parent_id])}
                storage.share_files = listing
                report = run(request(quality_preference=["2160p"]), storage, self.metadata,
                             Search([hit]), journal, apply=True)
                self.assertEqual(report["status"], "NEEDS_INPUT", report)
                self.assertEqual(report["resource_checks"][0]["status"], "unavailable")
                self.assertEqual(report["source_candidates"], [])
                self.assertNotIn("4kfolder", calls)
                self.assertEqual((storage.saved, storage.renamed, storage.created), ([], [], []))

    def test_password_share_can_be_ordinary_task_but_is_not_a_reusable_source(self):
        from media_helper.sources import extract_selected_sources
        self.search.hits = [{"url": "https://pan.quark.cn/s/0a1b2c3d4e5f?pwd=secret1234", "title": "测试剧"}]
        report = self.run_workflow()
        self.assertEqual(report["status"], "READY", report)
        self.assertEqual(report["source_refs"], [])
        self.assertTrue(report["plan"])
        self.assertTrue(all("source_ref" not in row for row in report["plan"]))
        self.assertEqual(extract_selected_sources(report), [])
        self.assertNotIn("secret1234", json.dumps(report))

    def test_source_refs_do_not_turn_unchosen_versions_into_preferred_sources(self):
        from media_helper.sources import extract_selected_sources
        first, second = "https://pan.quark.cn/s/111aaa", "https://pan.quark.cn/s/222bbb"
        self.search.hits = [{"url": first, "title": "测试剧"}, {"url": second, "title": "测试剧"}]
        choice = _digest((_digest(first), "source-2"))
        report = self.run_workflow(source_selection={"S01E02": choice})
        self.assertEqual(report["status"], "READY", report)
        self.assertEqual(len(report["source_refs"]), 2)
        self.assertEqual([source["url"] for source in extract_selected_sources(report)], [first])

    def test_apply_saves_missing_and_associated_subtitle_and_renames_only_new_fids(self):
        before = deepcopy(self.storage.folders["season"][0])
        report = self.run_workflow(apply=True)
        self.assertEqual(report["status"], "COMPLETE", report)
        self.assertEqual(self.storage.saved, [["source-2", "subtitle-2"]])
        self.assertEqual(self.storage.renamed, [("new-source-2", "测试剧 (2026) S01E02 [1080p].MKV"),
                                               ("new-subtitle-2", "测试剧 (2026) S01E02 [1080p].zh-Hans.forced.ASS")])
        self.assertEqual(self.storage.folders["season"][0], before)
        self.assertEqual([row["state"] for row in report["operations"]], ["complete"])
        serialized = json.dumps([report, self.journal.events])
        self.assertNotIn("do-not-persist", serialized)
        self.assertNotIn("stoken", serialized)
        self.assertNotIn("share_fid_token", serialized)

    def test_repeated_completed_request_does_not_save_again(self):
        self.run_workflow(apply=True)
        report = self.run_workflow(apply=True)
        self.assertEqual(report["status"], "UP_TO_DATE", report)
        self.assertEqual(len(self.storage.saved), 1)
        self.assertEqual(self.search.calls, 1)

    def test_title_only_returns_identity_candidates_without_searching_resources(self):
        report = self.run_workflow(series_id=None, year=None, season=None)
        self.assertEqual(report["status"], "NEEDS_INPUT")
        self.assertEqual(len(report["metadata_candidates"]), 2)
        self.assertEqual(self.search.calls, 0)

    def test_existing_bare_absolute_number_cannot_be_guessed_or_duplicated(self):
        self.storage.folders["season"].append(file("absolute", "001.MKV"))
        report = self.run_workflow(apply=True)
        self.assertEqual(report["status"], "NEEDS_INPUT", report)
        self.assertIn("existing_library_episode_numbering_ambiguous", report["reasons"])
        self.assertEqual(self.storage.saved, [])

    def test_future_and_unknown_dates_do_not_become_missing(self):
        self.metadata.episodes.extend([{"season": 1, "episode": 3, "air_date": "2026-10-17"},
                                       {"season": 1, "episode": 4, "air_date": None}])
        report = self.run_workflow()
        self.assertEqual([row["episode"] for row in report["missing"]], [2])
        self.assertEqual([row["episode"] for row in report["future"]], [3])
        self.assertEqual([row["episode"] for row in report["unknown_schedule"]], [4])
        self.assertEqual(report["metadata_basis"], "expected_schedule_only")

    def test_equal_versions_require_selection_not_first_result(self):
        self.storage.shared.append(file("alternate", "测试剧.S01E02.other.1080p.mkv"))
        report = self.run_workflow(apply=True)
        self.assertEqual(report["status"], "NEEDS_INPUT", report)
        self.assertEqual(len(report["unresolved_episodes"]["S01E02"]), 2)
        self.assertEqual(self.storage.saved, [])

    def test_explicit_version_selection_resolves_tie(self):
        self.storage.shared.append(file("alternate", "测试剧.S01E02.other.1080p.mkv"))
        pending = self.run_workflow()
        selected = next(row["candidate_id"] for row in pending["unresolved_episodes"]["S01E02"] if row["fid"] == "alternate")
        report = self.run_workflow(apply=True, source_selection={"S01E02": selected})
        self.assertEqual(report["status"], "COMPLETE", report)
        self.assertEqual(self.storage.saved, [["alternate"]])

    def test_bare_numeric_share_still_requires_explicit_episode_confirmation(self):
        self.storage.shared = [file("source-2", "02.mkv")]
        report = self.run_workflow(apply=True)
        self.assertEqual(report["status"], "NEEDS_INPUT", report)
        self.assertEqual(report["source_review"][0]["reasons"], ["episode_unknown_no_sort_order_inference"])
        candidate = report["source_candidates"][0]
        self.assertEqual((candidate["name"], candidate["fid"], candidate["suggested_episode"]),
                         ("02.mkv", "source-2", 2))
        self.assertNotIn("episode_key", candidate)
        self.assertEqual(candidate["share_source"], {"source_hash": _digest(self.search.hits[0]["url"])})
        self.assertNotIn("https://", json.dumps(report["source_candidates"]))
        self.assertEqual(self.storage.saved, [])

    def test_confirmed_bare_numeric_episode_keeps_real_source_through_save_and_verification(self):
        self.storage.shared = [file("source-2", "02.mkv")]
        identifier, confirmed = confirmed_source()
        received = []
        original_save = self.storage.save_files

        def capture_save(share, files, target):
            received.extend(deepcopy(files))
            return original_save(share, files, target)

        self.storage.save_files = capture_save
        report = self.run_workflow(apply=True, source_episode_mapping={identifier: confirmed},
                                   source_selection={"S01E02": identifier})
        self.assertEqual(report["status"], "COMPLETE", report)
        self.assertEqual([(row["fid"], row["name"], row["size"]) for row in received],
                         [("source-2", "02.mkv", 123)])
        self.assertEqual(self.storage.renamed, [("new-source-2", "测试剧 (2026) S01E02.mkv")])
        source = report["operations"][0]["files"][0]
        self.assertEqual((source["source_fid"], source["name"], source["size"]), ("source-2", "02.mkv", 123))
        evidence = report["source_episode_mapping_applied"]
        self.assertEqual(len(evidence), 1)
        self.assertEqual((evidence[0]["candidate_id"], evidence[0]["episode_key"], evidence[0]["name"]),
                         (identifier, "S01E02", "02.mkv"))
        self.assertNotIn("https://", json.dumps(evidence))
        self.assertNotIn("do-not-persist", json.dumps([report, self.journal.events]))

    def test_confirmed_bare_numeric_versions_still_use_explicit_source_selection(self):
        self.storage.shared = [file("source-2", "02.mkv"), file("alternate", "02.mkv", size=456)]
        first_id, first = confirmed_source()
        second_id, second = confirmed_source("alternate", size=456)
        report = self.run_workflow(apply=True, source_episode_mapping={first_id: first, second_id: second},
                                   source_selection={"S01E02": second_id})
        self.assertEqual(report["status"], "COMPLETE", report)
        self.assertEqual(self.storage.saved, [["alternate"]])
        self.assertEqual([row["candidate_id"] for row in report["source_episode_mapping_applied"]], [second_id])

    def test_invalid_source_episode_mapping_fails_before_resource_search(self):
        identifier, valid = confirmed_source()
        invalid = [[], {"not-a-candidate": valid}, {identifier: []},
                   {identifier: {**valid, "url": "https://example.invalid"}}]
        invalid.extend({identifier: {**valid, **change}} for change in
                       ({"season": 2}, {"season": True}, {"episode": 0}, {"episode": 1000},
                        {"episode": True}, {"size": 0}, {"size": True}, {"name": None}))
        for mapping in invalid:
            with self.subTest(mapping=mapping):
                report = self.run_workflow(apply=True, source_episode_mapping=mapping)
                self.assertEqual(report["status"], "NEEDS_INPUT", report)
                self.assertEqual(self.search.calls, 0)
                self.assertEqual(self.storage.saved, [])

    def test_stale_source_episode_mapping_blocks_all_writes(self):
        for change in ({"name": "02.MKV"}, {"size": 124}, {"fid": "gone"}):
            with self.subTest(change=change):
                self.storage.shared = [file("source-2", "02.mkv"), file("explicit", "测试剧.S01E02.mkv")]
                identifier, confirmed = confirmed_source(**change)
                report = self.run_workflow(apply=True, source_episode_mapping={identifier: confirmed})
                self.assertEqual(report["status"], "NEEDS_INPUT", report)
                self.assertIn("source_episode_mapping_requires_reconfirmation", report["reasons"])
                self.assertEqual(self.storage.saved, [])

    def test_mapping_cannot_override_explicit_or_other_ambiguous_episode_names(self):
        for name in ("测试剧.S01E02.mkv", "测试剧.S01E01-E02.mkv", "测试剧02.mkv", "02-03.mkv", "02.zip", "01.mkv"):
            with self.subTest(name=name):
                self.storage.shared = [file("source-2", name)]
                identifier, confirmed = confirmed_source(name=name)
                report = self.run_workflow(apply=True, source_episode_mapping={identifier: confirmed})
                self.assertEqual(report["status"], "NEEDS_INPUT", report)
                self.assertIn("source_episode_mapping_requires_reconfirmation", report["reasons"])
                self.assertEqual(self.storage.saved, [])

    def test_source_episode_mapping_does_not_change_existing_inventory_parsing(self):
        self.storage.folders["season"].append(file("old-unknown", "01.mkv"))
        self.storage.shared = [file("source-2", "02.mkv")]
        identifier, confirmed = confirmed_source()
        report = self.run_workflow(apply=True, source_episode_mapping={identifier: confirmed})
        self.assertEqual(report["status"], "NEEDS_INPUT", report)
        self.assertIn("existing_library_episode_numbering_ambiguous", report["reasons"])
        self.assertEqual(self.storage.saved, [])

    def test_quality_choice_is_explainable_and_independent_of_result_order(self):
        self.storage.shared.insert(0, file("lower", "测试剧.S01E02.720p.mkv"))
        report = self.run_workflow()
        self.assertEqual(report["plan"][0]["source"]["fid"], "source-2")

    def test_candidates_include_existing_episode_sources_and_media_provenance(self):
        report = self.run_workflow()
        self.assertEqual([row["fid"] for row in report["source_candidates"]], ["source-1", "source-2"])
        source = report["source_candidates"][1]
        self.assertEqual(source["episode_key"], "S01E02")
        self.assertEqual(source["media_info"]["resolution"], "1080p")
        self.assertEqual(source["media_info"]["basis"], "filename")
        self.assertEqual(source["media_info"]["inspection"], "release_labels_only")

    def test_parent_quality_labels_select_4k_and_are_included_in_episode_name(self):
        folders = {"0": [file("1080-dir", "测试剧 1080P", directory=True),
                          file("4k-dir", "测试剧 4K", directory=True)],
                   "1080-dir": [file("hd-source", "测试剧.S01E02.mkv")],
                   "4k-dir": [file("uhd-source", "测试剧.S01E02.mkv")]}

        def share(url, password=None, parent_id="0"):
            return {"share_ref": {"share_id": "share-1", "parent_id": parent_id},
                    "files": deepcopy(folders[parent_id])}

        self.storage.share_files = share
        report = self.run_workflow()
        self.assertEqual(report["status"], "READY", report)
        self.assertEqual(report["plan"][0]["source"]["fid"], "uhd-source")
        self.assertEqual(report["plan"][0]["target_name"], "测试剧 (2026) S01E02 [2160p].mkv")
        candidate = next(row for row in report["source_candidates"] if row["fid"] == "uhd-source")
        self.assertEqual(candidate["media_info"]["basis"], "parent_directory")
        self.assertEqual(candidate["parent_id"], "4k-dir")
        self.assertEqual(candidate["parent"], "测试剧 4K")

    def test_media_tags_can_be_disabled_without_losing_quality_choice(self):
        self.storage.shared.append(file("uhd-source", "测试剧.S01E02.4K.mkv"))
        report = self.run_workflow(include_media_tags=False)
        self.assertEqual(report["plan"][0]["source"]["fid"], "uhd-source")
        self.assertEqual(report["plan"][0]["target_name"], "测试剧 (2026) S01E02.mkv")

    def test_configured_quality_preference_accepts_4k_alias_and_hd_first(self):
        self.storage.shared.append(file("uhd-source", "测试剧.S01E02.4K.mkv"))
        report = self.run_workflow(quality_preference=["1080P", "4K"])
        self.assertEqual(report["plan"][0]["source"]["fid"], "source-2")

    def test_no_real_new_fids_never_claims_verified_or_renames(self):
        self.storage.no_task_fids = True
        report = self.run_workflow(apply=True)
        self.assertEqual(report["status"], "UNKNOWN", report)
        self.assertEqual(self.storage.renamed, [])

    def test_unknown_save_can_resume_through_task_get_without_duplicate_post(self):
        self.storage.fail_save = True
        report = self.run_workflow(apply=True)
        self.assertEqual(report["status"], "UNKNOWN", report)
        self.assertNotIn("secret transport", json.dumps(report))
        self.storage.fail_save = False
        resumed = self.run_workflow(apply=True)
        self.assertEqual(resumed["status"], "COMPLETE", resumed)
        self.assertEqual(len(self.storage.saved), 1)
        self.assertEqual(len(self.storage.renamed), 2)

    def test_unknown_save_without_recoverable_task_never_reposts(self):
        class NoTaskError(RuntimeError):
            pass

        def fail(*args):
            self.storage.saved.append(["attempted"])
            raise NoTaskError("uncertain")

        self.storage.save_files = fail
        first = self.run_workflow(apply=True)
        second = self.run_workflow(apply=True)
        self.assertEqual((first["status"], second["status"]), ("UNKNOWN", "UNKNOWN"))
        self.assertEqual(self.storage.saved, [["attempted"]])

    def test_unknown_operation_identity_survives_alias_and_destination_edits(self):
        def fail(*args):
            self.storage.saved.append(["attempted"])
            raise RuntimeError("unknown save without a recoverable task")

        self.storage.save_files = fail
        first = self.run_workflow(apply=True)
        for changes, field in (({"title": "English Alias"}, "title"),
                               ({"year": 2025}, "year"),
                               ({"target_folder_id": "elsewhere"}, "target_folder_id"),
                               ({"category_folder_id": "another-category"}, "category_folder_id")):
            with self.subTest(field=field):
                rerun = self.run_workflow(apply=True, **changes)
                self.assertEqual(rerun["job_key"], first["job_key"])
                self.assertEqual(rerun["status"], "NEEDS_INPUT", rerun)
                self.assertIn(field, rerun["conflicting_fields"])
        # Explicitly selecting its already recorded destination still finds
        # the unknown operation, rather than becoming a new job.
        same_destination = self.run_workflow(apply=True, target_folder_id="season")
        self.assertEqual(same_destination["job_key"], first["job_key"])
        self.assertEqual(same_destination["status"], "UNKNOWN", same_destination)
        self.assertEqual(self.storage.saved, [["attempted"]])

    def test_job_identity_normalizes_numeric_ids_season_and_source(self):
        def fail(*args):
            self.storage.saved.append(["attempted"])
            raise RuntimeError("unknown")

        self.storage.save_files = fail
        first = self.run_workflow(apply=True)
        second = self.run_workflow(apply=True, series_id="0017", season="01", metadata_source="TMDB")
        self.assertEqual(second["job_key"], first["job_key"])
        self.assertEqual(second["status"], "UNKNOWN", second)
        self.assertEqual(self.storage.saved, [["attempted"]])

    def test_plan_for_unfinished_operation_stops_without_mutations_or_journal_update(self):
        self.storage.fail_save = True
        first = self.run_workflow(apply=True)
        before = deepcopy((self.storage.folders, self.storage.saved, self.storage.renamed, self.journal.events))

        def no_recovery_in_plan(task_id):
            self.fail("A read-only plan may stop UNKNOWN without executing recovery")

        self.storage.task_status = no_recovery_in_plan
        report = self.run_workflow()
        self.assertEqual(report["status"], "UNKNOWN", report)
        self.assertEqual(report["job_key"], first["job_key"])
        self.assertFalse(report["writes_attempted"])
        self.assertEqual((self.storage.folders, self.storage.saved, self.storage.renamed, self.journal.events), before)

    def test_rename_failure_is_durable_and_resumes_only_known_new_files(self):
        self.storage.fail_rename = True
        first = self.run_workflow(apply=True)
        self.assertEqual(first["status"], "PARTIAL", first)
        self.storage.fail_rename = False
        resumed = self.run_workflow(apply=True)
        self.assertEqual(resumed["status"], "COMPLETE", resumed)
        self.assertEqual(len(self.storage.saved), 1)

    def test_resume_only_finishes_submitted_uhd_episode_without_searching_or_saving_new_hd_episode(self):
        self.storage.shared = [file("source-2", "测试剧.S01E02.2160p.mkv")]
        self.storage.fail_rename = True
        first = self.run_workflow(apply=True, quality_preference=["2160p"])
        self.assertEqual(first["status"], "PARTIAL", first)
        self.assertEqual(self.storage.saved, [["source-2"]])
        self.assertEqual(self.storage.renamed, [])
        # While E02 awaits its durable rename, another episode becomes due.
        # The only E03 source is HD, which the unattended watch policy rejects.
        self.metadata.episodes = Metadata((1, 2, 3)).episodes
        self.storage.shared.append(file("source-3", "测试剧.S01E03.1080p.mkv"))
        self.storage.fail_rename = False
        self.search.calls = 0

        resumed = run(request(quality_preference=["2160p"]), self.storage, self.metadata,
                      self.search, self.journal, apply=True, resume_only=True)

        self.assertEqual(resumed["status"], "COMPLETE", resumed)
        self.assertIn("previous_execution_reconciled", resumed["reasons"])
        self.assertEqual(self.search.calls, 0)
        self.assertEqual(self.storage.saved, [["source-2"]])
        self.assertEqual(self.storage.renamed,
                         [("new-source-2", "测试剧 (2026) S01E02 [2160p].mkv")])
        self.assertNotIn("new-source-3", {row["fid"] for row in self.storage.folders["season"]})
        self.assertEqual([row["episode_key"] for operation in resumed["operations"]
                          for row in operation["verified_files"]], ["S01E02"])
        self.assertEqual(self.journal.get(first["job_key"])["state"], "complete")

    def test_normal_resume_still_plans_and_saves_new_missing_episodes_after_durable_rename(self):
        self.storage.shared = [file("source-2", "测试剧.S01E02.2160p.mkv")]
        self.storage.fail_rename = True
        first = self.run_workflow(apply=True, quality_preference=["2160p"])
        self.assertEqual(first["status"], "PARTIAL", first)
        self.metadata.episodes = Metadata((1, 2, 3)).episodes
        self.storage.shared.append(file("source-3", "测试剧.S01E03.1080p.mkv"))
        self.storage.fail_rename = False
        self.search.calls = 0

        resumed = self.run_workflow(apply=True, quality_preference=["2160p"])

        self.assertEqual(resumed["status"], "COMPLETE", resumed)
        self.assertEqual(self.search.calls, 1)
        self.assertEqual(self.storage.saved, [["source-2"], ["source-3"]])
        self.assertEqual(self.storage.renamed,
                         [("new-source-2", "测试剧 (2026) S01E02 [2160p].mkv"),
                          ("new-source-3", "测试剧 (2026) S01E03 [1080p].mkv")])

    def test_successful_rename_with_lost_response_resumes_by_fid_without_repeating_save_or_rename(self):
        rename = self.storage.rename

        def lost_response(fid, name):
            result = rename(fid, name)
            if fid == "new-subtitle-2":
                raise RuntimeError("rename succeeded but response was lost")
            return result

        self.storage.rename = lost_response
        first = self.run_workflow(apply=True)
        self.assertEqual(first["status"], "PARTIAL", first)
        before = deepcopy((self.storage.saved, self.storage.renamed))
        self.storage.rename = rename
        report = self.run_workflow(apply=True)
        self.assertEqual(report["status"], "COMPLETE", report)
        self.assertEqual((self.storage.saved, self.storage.renamed), before)
        self.assertFalse(report["writes_attempted"])

    def test_concurrent_target_collision_preserves_every_file_and_stops_rename(self):
        self.storage.conflict_after_save = True
        report = self.run_workflow(apply=True)
        self.assertEqual(report["status"], "PARTIAL", report)
        self.assertEqual(self.storage.renamed, [])
        self.assertEqual({row["fid"] for row in self.storage.folders["season"]},
                         {"old-1", "new-source-2", "new-subtitle-2", "concurrent"})

    def test_unknown_target_folder_cannot_escape_configured_library(self):
        report = self.run_workflow(apply=True, target_folder_id="elsewhere")
        self.assertEqual(report["status"], "NEEDS_INPUT", report)
        self.assertEqual(self.storage.saved, [])

    def test_library_root_is_not_silently_treated_as_series_destination(self):
        report = self.run_workflow(apply=True, target_folder_id="root")
        self.assertEqual(report["status"], "NEEDS_INPUT", report)
        self.assertIn("library_root_is_not_a_confirmed_series_destination", report["reasons"])
        self.assertEqual(self.storage.saved, [])

    def test_new_series_directory_is_only_created_under_confirmed_root(self):
        self.storage.folders = {"root": [file("tv", "电视剧", "root", directory=True)], "tv": []}
        self.metadata = Metadata((2,))
        report = self.run_workflow(apply=True)
        self.assertEqual(report["status"], "COMPLETE", report)
        self.assertEqual([(row["parent_id"], row["name"]) for row in self.storage.created],
                         [("tv", "测试剧 (2026)"), ("dir-0", "Season 01")])
        self.assertEqual(report["plan"][0]["target_name"], "测试剧 (2026) S01E02 [1080p].MKV")

    def test_mkdir_visibility_lag_retries_only_reads_and_records_returned_fid_first(self):
        self.storage.folders = {"root": [file("tv", "电视剧", "root", directory=True)], "tv": []}
        self.metadata = Metadata((2,))
        original_list = self.storage.list_files
        hidden_once = set()

        def delayed_list(parent):
            rows = original_list(parent)
            just_created = [row for row in self.storage.created if row["parent_id"] == parent and row["fid"] not in hidden_once]
            if just_created:
                returned = just_created[-1]
                latest = next(iter(self.journal.records.values()))
                self.assertEqual(latest["directory"]["returned"]["fid"], returned["fid"])
                self.assertEqual(latest["state"], "mkdir_pending")
                hidden_once.add(returned["fid"])
                rows = [row for row in rows if row["fid"] != returned["fid"]]
            return rows

        self.storage.list_files = delayed_list
        with patch("media_helper.workflow.time.sleep") as sleep:
            report = self.run_workflow(apply=True)
        self.assertEqual(report["status"], "COMPLETE", report)
        self.assertEqual(len(self.storage.created), 2)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual([row["read_attempts"] for row in report["directory_verifications"]], [2, 2])
        self.assertEqual([row["fid"] for row in report["directory_verifications"]], ["dir-0", "dir-1"])

    def test_mkdir_stays_unknown_when_returned_fid_remains_invisible_without_reposting(self):
        self.storage.folders = {"root": [file("tv", "电视剧", "root", directory=True)], "tv": []}
        self.metadata = Metadata((2,))
        original_list = self.storage.list_files

        def hidden_list(parent):
            created_fids = {row["fid"] for row in self.storage.created}
            return [row for row in original_list(parent) if row["fid"] not in created_fids]

        self.storage.list_files = hidden_list
        with patch("media_helper.workflow.time.sleep"):
            first = self.run_workflow(apply=True)
            second = self.run_workflow(apply=True)
        self.assertEqual((first["status"], second["status"]), ("UNKNOWN", "UNKNOWN"))
        self.assertIn("created_directory_not_observed", first["reasons"])
        self.assertEqual(len(self.storage.created), 1)
        self.assertEqual(self.storage.saved, [])
        self.assertFalse(second["writes_attempted"])
        pending = next(iter(self.journal.records.values()))
        self.assertEqual(pending["directory"]["returned"],
                         {"fid": "dir-0", "name": "测试剧 (2026)", "parent_id": "tv", "is_dir": True})

    def test_persisted_mkdir_fid_resumes_from_read_only_verification_without_duplicate_creation(self):
        self.storage.folders = {"root": [file("tv", "电视剧", "root", directory=True)], "tv": []}
        self.metadata = Metadata((2,))
        original_list = self.storage.list_files

        def hidden_list(parent):
            return [row for row in original_list(parent) if row["fid"] != "dir-0"]

        self.storage.list_files = hidden_list
        with patch("media_helper.workflow.time.sleep"):
            first = self.run_workflow(apply=True)
        self.assertEqual(first["status"], "UNKNOWN", first)
        self.storage.list_files = original_list
        resumed = self.run_workflow(apply=True)
        self.assertEqual(resumed["status"], "COMPLETE", resumed)
        self.assertEqual([(row["parent_id"], row["name"]) for row in self.storage.created],
                         [("tv", "测试剧 (2026)"), ("dir-0", "Season 01")])
        evidence = next(row for row in resumed["directory_verifications"] if row["phase"] == "resume")
        self.assertEqual((evidence["fid"], evidence["parent_id"], evidence["name"]),
                         ("dir-0", "tv", "测试剧 (2026)"))
        self.assertIn(evidence, next(iter(self.journal.records.values()))["directory_verifications"])

    def test_mkdir_without_returned_fid_does_not_adopt_matching_name_on_resume(self):
        self.storage.folders = {"root": [file("tv", "电视剧", "root", directory=True)], "tv": []}
        self.metadata = Metadata((2,))
        original_mkdir = self.storage.mkdir

        def lost_response(parent, name):
            original_mkdir(parent, name)
            raise RuntimeError("response lost after creation")

        self.storage.mkdir = lost_response
        first = self.run_workflow(apply=True)
        self.storage.mkdir = original_mkdir
        second = self.run_workflow(apply=True)
        self.assertEqual((first["status"], second["status"]), ("UNKNOWN", "UNKNOWN"))
        self.assertIn("directory_creation_outcome_requires_reconciliation", second["reasons"])
        self.assertEqual(len(self.storage.created), 1)
        self.assertEqual(self.storage.saved, [])

    def test_missing_series_category_never_creates_parallel_library_at_root(self):
        self.storage.folders = {"root": []}
        report = self.run_workflow(apply=True)
        self.assertEqual(report["status"], "NEEDS_INPUT", report)
        self.assertIn("unique_existing_series_category_required", report["reasons"])
        self.assertEqual(self.storage.created, [])

    def test_library_directory_scan_stops_after_five_levels_and_does_not_assume_absence(self):
        self.storage.folders = {"root": [file("tv", "电视剧", "root", directory=True)]}
        chain = ["tv", "a", "b", "c", "d", "e"]
        names = {"a": "国产剧", "b": "2026", "c": "连载剧", "d": "港台剧", "e": "美剧"}
        for parent, child in zip(chain, chain[1:]):
            self.storage.folders[parent] = [file(child, names[child], parent, directory=True)]
        self.storage.folders["e"] = []
        listed = []
        list_files = self.storage.list_files

        def track(parent):
            listed.append(parent)
            return list_files(parent)

        self.storage.list_files = track
        report = self.run_workflow(apply=True)
        self.assertEqual(report["status"], "NEEDS_INPUT", report)
        self.assertIn("library_directory_scan_incomplete_no_new_series_created", report["reasons"])
        self.assertNotIn("d", listed)
        self.assertEqual((self.storage.created, self.storage.saved), ([], []))

    def test_default_tv_discovery_never_opens_movie_or_learning_categories(self):
        self.storage.folders["root"] = [file("tv", "电视剧📺", "root", directory=True),
                                         file("movies", "电影🎬", "root", directory=True),
                                         file("learning", "学习强国", "root", directory=True)]
        self.storage.folders["tv"] = [file("series", "测试剧 (2026)", "tv", directory=True)]
        self.storage.folders["movies"] = []
        self.storage.folders["learning"] = []
        listed = []
        list_files = self.storage.list_files

        def restricted(parent):
            self.assertNotIn(parent, {"movies", "learning"}, "TV discovery must not open unrelated categories")
            listed.append(parent)
            return list_files(parent)

        self.storage.list_files = restricted
        report = self.run_workflow()
        self.assertEqual(report["status"], "READY", report)
        self.assertEqual(report["target_folder_id"], "season")
        self.assertIn("tv", listed)
        self.assertEqual([row["source"]["fid"] for row in report["plan"]], ["source-2", "subtitle-2"])

    def test_unique_bare_tv_directory_is_reused_with_named_episode_evidence(self):
        self.storage.folders["root"] = [file("tv", "电视剧📺", "root", directory=True)]
        self.storage.folders["tv"] = [file("series", "测试剧", "tv", directory=True)]
        report = self.run_workflow(category_folder_id="tv")
        self.assertEqual(report["status"], "READY", report)
        self.assertEqual(report["target_folder_id"], "season")
        self.assertEqual(report["directories"], [])
        self.assertEqual(report["directory_match_basis"]["method"], "unique_bare_title_with_existing_episode_evidence")
        self.assertEqual(report["directory_match_basis"]["evidence"][0]["fid"], "old-1")
        self.assertEqual(self.storage.folders["tv"][0]["name"], "测试剧")
        self.assertEqual(report["plan"][0]["target_name"], "测试剧 S01E02 [1080p].MKV")

    def test_direct_show_match_never_opens_other_shows_even_for_explicit_season_target(self):
        for name, target in (("测试剧", None), ("测试剧 (2026)", None), ("测试剧", "season")):
            with self.subTest(name=name, target=target):
                self.storage = Storage()
                self.storage.folders["root"] = [file("tv", "电视剧📺", "root", directory=True)]
                self.storage.folders["tv"] = [file("series", name, "tv", directory=True),
                                               file("other-show", "另一部电视剧 (2026)", "tv", directory=True)]
                list_files = self.storage.list_files

                def restricted(parent):
                    self.assertNotEqual(parent, "other-show", "Matched-show discovery must not inspect its peers")
                    return list_files(parent)

                self.storage.list_files = restricted
                options = {"category_folder_id": "tv"}
                if target:
                    options["target_folder_id"] = target
                report = self.run_workflow(**options)
                self.assertEqual(report["status"], "READY", report)
                self.assertEqual(report["target_folder_id"], "season")
                self.assertEqual(report["plan"][0]["target_name"], f"{name} S01E02 [1080p].MKV")

    def test_bare_title_with_duplicate_or_competing_year_directory_requires_input(self):
        for competing in ("测试剧", "测试剧 (2025)", "测试剧 (2026)"):
            with self.subTest(competing=competing):
                self.storage = Storage()
                self.storage.folders["root"] = [file("tv", "电视剧📺", "root", directory=True)]
                self.storage.folders["tv"] = [file("series", "测试剧", "tv", directory=True),
                                               file("competing", competing, "tv", directory=True)]
                self.storage.folders["competing"] = []
                report = self.run_workflow(apply=True, category_folder_id="tv")
                self.assertEqual(report["status"], "NEEDS_INPUT", report)
                self.assertIn("bare_series_directory_has_competing_identity_or_year", report["reasons"])
                self.assertEqual(self.storage.saved, [])

    def test_bare_title_needs_matching_video_title_and_unambiguous_episode_numbering(self):
        for filename in ("另一作品.S01E01.mkv", "测试剧.001.mkv"):
            with self.subTest(filename=filename):
                self.storage = Storage()
                self.storage.folders["root"] = [file("tv", "电视剧📺", "root", directory=True)]
                self.storage.folders["tv"] = [file("series", "测试剧", "tv", directory=True)]
                self.storage.folders["season"] = [file("old-1", filename)]
                report = self.run_workflow(apply=True, category_folder_id="tv")
                self.assertEqual(report["status"], "NEEDS_INPUT", report)
                self.assertIn("bare_series_directory_lacks_unambiguous_named_episode_evidence", report["reasons"])
                self.assertEqual(self.storage.saved, [])

    def test_new_series_stays_in_unique_direct_tv_category_without_reading_other_categories(self):
        self.storage.folders = {"root": [file("tv", "电视剧📺", "root", directory=True),
                                            file("movies", "电影🎬", "root", directory=True),
                                            file("learning", "学习强国", "root", directory=True)], "tv": []}
        list_files = self.storage.list_files

        def restricted(parent):
            self.assertNotIn(parent, {"movies", "learning"})
            return list_files(parent)

        self.storage.list_files = restricted
        self.metadata = Metadata((2,))
        report = self.run_workflow(apply=True)
        self.assertEqual(report["status"], "COMPLETE", report)
        self.assertEqual(self.storage.created[0]["parent_id"], "tv")

    def test_explicit_category_root_can_create_new_series(self):
        self.storage.folders = {"root": []}
        self.metadata = Metadata((2,))
        report = self.run_workflow(apply=True, library_root_is_series_category=True)
        self.assertEqual(report["status"], "COMPLETE", report)
        self.assertEqual(self.storage.created[0]["parent_id"], "root")

    def test_auth_and_risk_control_stop_without_trying_other_shares(self):
        for error_class, status in ((AuthenticationError, "NEEDS_AUTH"), (RiskControlError, "RISK_CONTROL")):
            with self.subTest(error_class=error_class):
                calls = []

                def fail(url, **kwargs):
                    calls.append(url)
                    raise error_class("stopped", step="share")

                self.storage.share_files = fail
                self.search.hits = [dict(self.search.hits[0]), dict(self.search.hits[0], url="https://pan.quark.cn/s/another")]
                report = self.run_workflow(apply=True)
                self.assertEqual(report["status"], status, report)
                self.assertEqual(len(calls), 1)
                self.assertEqual(self.storage.saved, [])


if __name__ == "__main__":
    unittest.main()
