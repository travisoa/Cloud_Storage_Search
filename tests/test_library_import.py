"""Cloud-library import is tested against mutable fake cloud state only."""
from copy import deepcopy
import unittest

from media_helper.catalog import MetadataError
from media_helper.library_import import scan, plan_import, apply_import
from media_helper.providers import AuthenticationError, RiskControlError, UnknownOutcome


def row(fid, name, parent, *, directory=False, size=100):
    return {"fid": fid, "name": name, "parent_id": parent,
            "is_dir": directory, "size": 0 if directory else size}


class Journal:
    def __init__(self):
        self.rows, self.events = {}, []

    def get(self, key):
        return deepcopy(self.rows.get(key))

    def put(self, key, value):
        self.rows[key] = deepcopy(value)
        self.events.append(deepcopy(value))


class Storage:
    account_scope = "quark:account-test"

    def __init__(self):
        self.folders = {
            "0": [row("library", "影库", "0", directory=True), row("inbox", "待整理", "0", directory=True)],
            "library": [row("tv", "电视剧", "library", directory=True), row("movie", "电影", "library", directory=True)],
            "tv": [], "movie": [],
            "inbox": [row("v1", "测试剧.2026.S01E01.4K.mkv", "inbox"),
                      row("s1", "测试剧.2026.S01E01.4K.zh-Hans.srt", "inbox", size=9),
                      row("m1", "测试电影.2025.1080p.mp4", "inbox")],
        }
        self.reads, self.writes = [], []
        self.failure = self.move_failure = self.rename_failure = self.mkdir_failure = None

    def list_files(self, parent):
        self.reads.append(parent)
        if self.failure:
            raise self.failure
        return deepcopy(self.folders[parent])

    def mkdir(self, parent, name):
        self.writes.append(("mkdir", parent, name))
        fid = "newdir-" + str(len(self.writes))
        value = row(fid, name, parent, directory=True)
        self.folders[parent].append(value)
        self.folders[fid] = []
        if self.mkdir_failure:
            raise self.mkdir_failure
        return deepcopy(value)

    def move(self, fid, parent):
        self.writes.append(("move", fid, parent))
        for children in self.folders.values():
            for value in children:
                if value["fid"] == fid:
                    children.remove(value)
                    value["parent_id"] = parent
                    self.folders[parent].append(value)
                    if self.move_failure:
                        raise self.move_failure
                    return {"fid": fid, "target_parent_id": parent, "status": "submitted", "task_id": "move-task"}
        raise KeyError(fid)

    def rename(self, fid, name):
        self.writes.append(("rename", fid, name))
        for children in self.folders.values():
            for value in children:
                if value["fid"] == fid:
                    if self.rename_failure == "before":
                        raise UnknownOutcome("unknown rename")
                    value["name"] = name
                    if self.rename_failure:
                        raise self.rename_failure
                    return {"fid": fid, "status": "done"}
        raise KeyError(fid)


def resolver(file, context):
    if file["fid"].startswith("m"):
        return {"status": "MATCHED", "kind": "movie", "title": "测试电影", "year": 2025,
                "metadata_source": "tmdb", "series_id": "200"}
    return {"status": "MATCHED", "kind": "tv", "title": "测试剧", "year": 2026,
            "metadata_source": "tmdb", "series_id": "100", "season": 1, "episode": 1}


def library(**changes):
    return {"library_root_id": "library", "categories": {"tv": {"folder_id": "tv"}, "movie": {"folder_id": "movie"}}, **changes}


class LibraryImportTests(unittest.TestCase):
    def setUp(self):
        self.storage, self.journal = Storage(), Journal()

    def plan(self, resolve=resolver):
        snapshot = scan(self.storage, source_id="0", excluded_ids=["library"])
        return plan_import(snapshot, library(), resolve)

    def apply(self, plan, apply=True):
        return apply_import(plan, self.storage, journal=self.journal, job_key="library-import:test", apply=apply)

    def test_recursive_scan_excludes_library_and_never_writes(self):
        value = scan(self.storage, source_id="0", excluded_ids=["library"])
        self.assertEqual(value["status"], "COMPLETE")
        self.assertEqual(value["counts"], {"directory": 1, "video": 2, "subtitle": 1})
        self.assertNotIn("library", self.storage.reads)
        self.assertFalse(value["writes_performed"])
        self.assertEqual(self.storage.writes, [])

    def test_partial_listing_and_limit_fail_closed(self):
        for kwargs in ({"max_entries": 1}, {"max_depth": 0}):
            value = scan(self.storage, source_id="0", excluded_ids=["library"], **kwargs)
            self.assertFalse(value["complete"])
            self.assertEqual(plan_import(value, library(), resolver)["operations"], [])
        self.storage.folders["inbox"].append(row("0", "loop", "inbox", directory=True))
        self.assertEqual(scan(self.storage, source_id="0", excluded_ids=["library"])["status"], "INCOMPLETE")

    def test_scan_auth_and_risk_stop_without_next_request(self):
        for exception, expected in ((AuthenticationError("secret"), "NEEDS_AUTH"),
                                    (RiskControlError("secret"), "RISK_CONTROL")):
            self.storage.failure = exception
            self.storage.reads = []
            value = scan(self.storage, source_id="0", excluded_ids=["library"])
            self.assertEqual(value["status"], expected)
            self.assertEqual(self.storage.reads, ["0"])
            self.assertNotIn("secret", str(value))

    def test_cross_account_scan_rejected_before_any_request(self):
        plan = self.plan()
        self.storage.account_scope = "quark:someone-else"
        previous = len(self.storage.reads)
        with self.assertRaisesRegex(ValueError, "account_scope"):
            self.apply(plan)
        self.assertEqual(len(self.storage.reads), previous)
        self.assertEqual(self.storage.writes, [])

    def test_unknown_size_and_zero_video_stay_for_review(self):
        self.storage.folders["inbox"][0]["size"] = None
        self.storage.folders["inbox"][2]["size"] = 0
        plan = self.plan()
        self.assertEqual(plan["operations"], [])
        self.assertEqual(sum(item["reason"] == "video_size_unknown_or_zero" for item in plan["review"]), 2)

    def test_scan_root_category_context_reaches_default_resolver(self):
        from media_helper.library_identity import MediaResolver
        from tests.test_library_identity import Metadata
        metadata = Metadata()
        self.storage.folders['inbox'] = [row('m1', '示例电影.2025.mkv', 'inbox')]
        snapshot = scan(self.storage, source_id='inbox', excluded_ids=['library'],
                        source_context={'ancestors': ['电视剧'], 'kind': 'tv'})
        plan = plan_import(snapshot, library(), MediaResolver(metadata))
        self.assertEqual(plan['operations'], [])
        self.assertEqual(plan['review'][0]['reason'], 'explicit_season_and_episode_required')
        self.assertEqual(metadata.calls, [])

    def test_source_context_is_bound_to_scan_hash_and_legacy_scans_still_work(self):
        snapshot = scan(self.storage, source_id='0', excluded_ids=['library'],
                        source_context={'ancestors': [], 'kind': 'tv'})
        snapshot['source_context']['kind'] = 'movie'
        with self.assertRaisesRegex(ValueError, 'scan_snapshot_changed'):
            plan_import(snapshot, library(), resolver)
        self.assertEqual(self.plan()['status'], 'READY')

    def test_extras_and_iso_are_reviewed_before_metadata_resolution(self):
        self.storage.folders['inbox'] = [row('trailer', '测试电影.2025.Trailer.mkv', 'inbox'),
                                         row('iso', '测试电影.2025.iso', 'inbox'),
                                         row('zip', '测试电影.2025.zip', 'inbox'),
                                         row('trailers', 'Trailers', 'inbox', directory=True)]
        self.storage.folders['trailers'] = [row('extra', '测试电影.2025.mkv', 'trailers')]
        def forbidden(file, context):
            raise AssertionError('excluded media must not reach resolver')
        snapshot = scan(self.storage, source_id='0', excluded_ids=['library'])
        self.assertEqual(next(item for item in snapshot['entries'] if item['fid'] == 'iso')['kind'], 'archive')
        plan = plan_import(snapshot, library(), forbidden)
        self.assertEqual(plan['operations'], [])
        self.assertEqual({item['source']['fid']: item['reason'] for item in plan['review']},
                         {'trailer': 'extra_or_trailer_not_a_feature', 'extra': 'extra_or_trailer_not_a_feature',
                          'iso': 'archive_requires_extraction', 'zip': 'archive_requires_extraction'})

    def test_source_root_extras_are_excluded_without_a_movie_size_floor(self):
        self.storage.folders['inbox'] = [row('m1', '测试电影.2025.mkv', 'inbox', size=1)]
        snapshot = scan(self.storage, source_id='inbox', excluded_ids=['library'],
                        source_context={'ancestors': ['Trailers']})
        plan = plan_import(snapshot, library(), resolver)
        self.assertEqual(plan['operations'], [])
        self.assertEqual(plan['review'][0]['reason'], 'extra_or_trailer_not_a_feature')
        snapshot = scan(self.storage, source_id='inbox', excluded_ids=['library'])
        plan = plan_import(snapshot, library(), resolver)
        self.assertEqual(len(plan['operations']), 1)
        self.assertEqual(plan['operations'][0]['source']['size'], 1)

    def test_extras_words_inside_a_work_title_do_not_hide_its_episodes(self):
        self.storage.folders['inbox'] = [row('show', '幕后之王 (2019)', 'inbox', directory=True),
                                         row('t1', 'Extras.S01E02.mkv', 'inbox')]
        self.storage.folders['show'] = [row('e1', '幕后之王 S01E01.mkv', 'show'),
                                        row('x1', '幕后之王 花絮01.mp4', 'show'),
                                        row('extras', 'Extras', 'show', directory=True)]
        self.storage.folders['extras'] = [row('x2', '幕后之王 S01E01 片段.mkv', 'extras')]
        snapshot = scan(self.storage, source_id='inbox', excluded_ids=['library'],
                        source_context={'ancestors': ['影视', '幕后之王 (2019)']})
        plan = plan_import(snapshot, library(), resolver)
        extras = {item['source']['fid'] for item in plan['review'] if item['reason'] == 'extra_or_trailer_not_a_feature'}
        self.assertEqual(extras, {'x1', 'x2'})

    def test_plan_uses_identity_and_same_prefix_and_unique_subtitle(self):
        plan = self.plan()
        self.assertEqual(plan["status"], "READY")
        by_id = {op["source"]["fid"]: op for op in plan["operations"]}
        self.assertEqual(by_id["v1"]["target_directories"], ["测试剧 (2026)", "Season 01"])
        self.assertEqual(by_id["v1"]["target_name"], "测试剧 (2026)S01E01_2160P.mkv")
        self.assertEqual(by_id["s1"]["target_name"], "测试剧 (2026)S01E01_2160P.zh-Hans.srt")
        self.assertEqual(by_id["m1"]["target_name"], "测试电影 (2025)_1080P.mp4")
        self.assertEqual(self.storage.writes, [])

    def test_resolver_receives_full_path_and_ancestor_names(self):
        received = []
        def resolve(file, context):
            received.append(context)
            return resolver(file, context)
        self.plan(resolve)
        self.assertEqual(received[0]["ancestors"], ["待整理"])
        self.assertEqual(received[0]["path"], "待整理/测试剧.2026.S01E01.4K.mkv")

    def test_archive_ambiguity_and_orphan_subtitle_never_get_operations(self):
        self.storage.folders["inbox"].extend([
            row("z", "测试剧.part01.rar", "inbox"), row("s2", "orphan.zh.srt", "inbox"),
            row("v2", "01.mkv", "inbox")])
        def resolve(file, context):
            return {"status": "NEEDS_INPUT"} if file["fid"] == "v2" else resolver(file, context)
        plan = self.plan(resolve)
        self.assertEqual({op["source"]["fid"] for op in plan["operations"]}, {"v1", "m1", "s1"})
        self.assertEqual({op["source"]["fid"] for op in plan["review"]}, {"z", "s2", "v2"})

    def test_duplicate_targets_and_their_subtitles_stay_in_place(self):
        self.storage.folders["inbox"].append(row("v2", "same.4K.mkv", "inbox"))
        plan = self.plan()
        self.assertEqual({op["source"]["fid"] for op in plan["operations"]}, {"m1"})
        self.assertEqual({op["source"]["fid"] for op in plan["review"]}, {"v1", "v2", "s1"})

    def test_metadata_auth_aborts_whole_plan_without_next_resolution(self):
        calls = []
        def resolve(file, context):
            calls.append(file["fid"])
            raise MetadataError("tmdb", "authentication_required", status=401)
        value = self.plan(resolve)
        self.assertEqual(value["status"], "NEEDS_AUTH")
        self.assertEqual(value["operations"], [])
        self.assertEqual(len(calls), 1)

    def test_no_scan_of_destination_and_category_scope_is_verified(self):
        snapshot = scan(self.storage, source_id="0")
        with self.assertRaisesRegex(ValueError, "excluded"):
            plan_import(snapshot, library(), resolver)
        plan = self.plan()
        self.storage.folders["library"] = self.storage.folders["library"][:1]
        value = self.apply(plan)
        self.assertEqual(value["status"], "NEEDS_INPUT")
        self.assertEqual(self.storage.writes, [])

    def test_preview_is_read_only_even_when_directories_missing(self):
        value = self.apply(self.plan(), apply=False)
        self.assertEqual(value["status"], "READY")
        self.assertEqual(len(value["operations"]), 3)
        self.assertEqual(self.storage.writes, [])
        self.assertEqual(self.journal.events, [])

    def test_move_rename_and_receipts_verified_and_idempotent(self):
        plan = self.plan()
        value = self.apply(plan)
        self.assertEqual(value["status"], "COMPLETE", value)
        self.assertEqual([op["state"] for op in value["operations"]], ["complete"] * 3)
        self.assertEqual(value["operations"][0]["move_evidence"]["task_id"], "move-task")
        self.assertEqual(self.storage.folders["inbox"], [])
        previous = list(self.storage.writes)
        self.assertEqual(self.apply(plan)["status"], "COMPLETE")
        self.assertEqual(self.storage.writes, previous)
        self.assertTrue(any(record["files"].get("v1", {}).get("state") == "move_intent" for record in self.journal.events))
        self.assertTrue(any(record["files"].get("v1", {}).get("state") == "rename_intent" for record in self.journal.events))

    def test_changed_source_stops_before_creating_directories(self):
        plan = self.plan()
        self.storage.folders["inbox"][0]["size"] += 1
        value = self.apply(plan)
        self.assertEqual(value["status"], "NEEDS_INPUT")
        self.assertEqual(self.storage.writes, [])

    def test_changed_source_ancestor_is_not_followed_outside_scope(self):
        plan = self.plan()
        self.storage.folders["0"][1]["name"] = "他人目录"
        value = self.apply(plan)
        self.assertEqual(value["status"], "NEEDS_INPUT")
        self.assertEqual(self.storage.writes, [])

    def test_existing_target_conflict_does_not_overwrite_or_move_subtitle(self):
        plan = self.plan()
        self.storage.folders["tv"] = [row("show", "测试剧 (2026)", "tv", directory=True)]
        self.storage.folders["show"] = [row("season", "Season 01", "show", directory=True)]
        self.storage.folders["season"] = [row("old", "测试剧 (2026)S01E01_2160P.mkv", "season")]
        value = self.apply(plan)
        self.assertEqual(value["status"], "NEEDS_INPUT")
        self.assertEqual(self.storage.writes, [])
        self.assertEqual({row["fid"] for row in self.storage.folders["inbox"]}, {"v1", "s1", "m1"})

    def test_unknown_move_is_reconciled_without_repeat_and_then_can_continue(self):
        plan = self.plan()
        self.storage.move_failure = UnknownOutcome("secret payload", task_id="task-unknown")
        first = self.apply(plan)
        self.assertEqual(first["status"], "UNKNOWN_OUTCOME")
        self.assertEqual(first["operations"][0]["failure_evidence"]["task_id"], "task-unknown")
        writes = list(self.storage.writes)
        self.storage.move_failure = None
        second = self.apply(plan)
        self.assertEqual(second["status"], "READY_TO_CONTINUE", second)
        self.assertEqual(self.storage.writes, writes)
        self.assertFalse(second["writes_attempted"])
        third = self.apply(plan)
        self.assertEqual(third["status"], "COMPLETE", third)
        self.assertEqual(sum(write[:2] == ("move", "v1") for write in self.storage.writes), 1)
        self.assertNotIn("secret", str(first))

    def test_unknown_rename_observed_unchanged_cannot_be_reissued(self):
        plan = self.plan()
        self.storage.rename_failure = "before"
        first = self.apply(plan)
        self.assertEqual(first["status"], "UNKNOWN_OUTCOME")
        writes = list(self.storage.writes)
        self.storage.rename_failure = None
        self.assertEqual(self.apply(plan)["status"], "UNKNOWN_OUTCOME")
        self.assertEqual(self.apply(plan)["status"], "UNKNOWN_OUTCOME")
        self.assertEqual(self.storage.writes, writes)

    def test_unknown_rename_observed_done_reconciles_without_more_writes(self):
        plan = self.plan()
        self.storage.rename_failure = UnknownOutcome("network")
        self.assertEqual(self.apply(plan)["status"], "UNKNOWN_OUTCOME")
        writes = list(self.storage.writes)
        value = self.apply(plan)
        self.assertEqual(value["status"], "READY_TO_CONTINUE")
        self.assertEqual(value["operations"][0]["state"], "complete")
        self.assertEqual(self.storage.writes, writes)

    def test_unknown_mkdir_never_adopts_same_name_and_recreates(self):
        plan = self.plan()
        self.storage.mkdir_failure = UnknownOutcome("mkdir response missing")
        self.assertEqual(self.apply(plan)["status"], "UNKNOWN_OUTCOME")
        writes = list(self.storage.writes)
        self.storage.mkdir_failure = None
        self.assertEqual(self.apply(plan)["status"], "UNKNOWN_OUTCOME")
        self.assertEqual(self.storage.writes, writes)

    def test_modified_plan_and_wrong_job_binding_rejected(self):
        plan = self.plan()
        changed = deepcopy(plan)
        changed["operations"][0]["source"]["fid"] = "old"
        with self.assertRaisesRegex(ValueError, "plan_changed"):
            self.apply(changed)
        self.journal.put("library-import:test", {"plan_hash": "other"})
        with self.assertRaisesRegex(ValueError, "binding_mismatch"):
            self.apply(plan)

    def test_reused_directory_identity_is_bound_across_recovery(self):
        plan = self.plan()
        self.storage.folders["tv"] = [row("show", "测试剧 (2026)", "tv", directory=True)]
        self.storage.folders["show"] = [row("season", "Season 01", "show", directory=True)]
        self.storage.folders["season"] = []
        self.storage.rename_failure = "before"
        self.assertEqual(self.apply(plan)["status"], "UNKNOWN_OUTCOME")
        persisted = self.journal.get("library-import:test")
        self.assertEqual({record["fid"] for record in persisted["directories"].values()}, {"show", "season"})
        self.storage.folders["show"] = [row("replacement", "Season 01", "show", directory=True)]
        self.storage.folders["replacement"] = []
        writes = list(self.storage.writes)
        value = self.apply(plan)
        self.assertEqual(value["status"], "UNKNOWN_OUTCOME")
        self.assertEqual(self.storage.writes, writes)

    def test_library_root_moved_from_selected_parent_stops_before_write(self):
        snapshot = scan(self.storage, source_id="0", excluded_ids=["library"])
        plan = plan_import(snapshot, library(parent_id="0", name="影库"), resolver)
        self.storage.folders["0"] = [item for item in self.storage.folders["0"] if item["fid"] != "library"]
        value = self.apply(plan)
        self.assertEqual(value["status"], "NEEDS_INPUT")
        self.assertEqual(self.storage.writes, [])

    def test_category_name_from_initialized_library_is_not_silently_rebound(self):
        snapshot = scan(self.storage, source_id="0", excluded_ids=["library"])
        selected = library()
        selected["categories"]["tv"]["name"] = "电视剧"
        plan = plan_import(snapshot, selected, resolver)
        self.storage.folders["library"][0]["name"] = "其他用途"
        value = self.apply(plan)
        self.assertEqual(value["status"], "NEEDS_INPUT")
        self.assertEqual(self.storage.writes, [])

    def test_changed_category_after_first_write_stops_later_moves(self):
        class CategoryMovedAfterRename(Storage):
            def rename(self, fid, name):
                result = super().rename(fid, name)
                if fid == "v1":
                    category = next(item for item in self.folders["library"] if item["fid"] == "tv")
                    self.folders["library"].remove(category)
                    category["parent_id"] = "outside"
                    self.folders["outside"] = [category]
                return result
        self.storage = CategoryMovedAfterRename()
        value = self.apply(self.plan())
        self.assertEqual(value["status"], "UNKNOWN_OUTCOME")
        self.assertEqual([write[1] for write in self.storage.writes if write[0] == "move"], ["v1"])
        self.assertEqual({item["fid"] for item in self.storage.folders["inbox"]}, {"s1", "m1"})


if __name__ == "__main__":
    unittest.main()
