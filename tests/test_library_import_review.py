"""Concurrent changes must not expand a selected import's write authority."""
from copy import deepcopy
import unittest

from media_helper.library_import import apply_import, plan_import, scan
from tests.test_library_import import Journal, Storage, library, resolver, row


class LibraryImportRaceReviewTests(unittest.TestCase):
    def make_plan(self, storage):
        storage.folders["inbox"] = storage.folders["inbox"][:1]
        return plan_import(scan(storage, source_id="0", excluded_ids=["library"]), library(), resolver)

    def prepare_existing_target(self, storage):
        storage.folders["tv"] = [row("show", "测试剧 (2026)", "tv", directory=True)]
        storage.folders["show"] = [row("season", "Season 01", "show", directory=True)]
        storage.folders["season"] = []
        storage.folders["outside"] = []

    def test_fid_moved_outside_target_before_rename_is_never_renamed(self):
        class ConcurrentMove(Storage):
            moved = False
            target_reads = 0

            def move(self, fid, parent):
                result = super().move(fid, parent)
                self.moved = True
                return result

            def list_files(self, parent):
                if self.moved and parent == "season":
                    self.target_reads += 1
                    if self.target_reads == 2:
                        own = next(value for value in self.folders["season"] if value["fid"] == "v1")
                        self.folders["season"].remove(own)
                        own["parent_id"] = "outside"
                        self.folders["outside"].append(own)
                return super().list_files(parent)

        storage = ConcurrentMove()
        self.prepare_existing_target(storage)
        plan = self.make_plan(storage)
        result = apply_import(plan, storage, journal=Journal(), job_key="review:rename-race", apply=True)
        self.assertFalse(any(write[0] == "rename" for write in storage.writes), result)
        self.assertEqual(storage.folders["outside"][0]["name"], "测试剧.2026.S01E01.4K.mkv")

    def test_fid_changed_by_another_client_before_rename_is_not_taken_over(self):
        class ConcurrentRename(Storage):
            moved = False
            target_reads = 0

            def move(self, fid, parent):
                result = super().move(fid, parent)
                self.moved = True
                return result

            def list_files(self, parent):
                if self.moved and parent == "season":
                    self.target_reads += 1
                    if self.target_reads == 2:
                        self.folders["season"][0]["name"] = "其他客户端已修改.mkv"
                return super().list_files(parent)

        storage = ConcurrentRename()
        self.prepare_existing_target(storage)
        plan = self.make_plan(storage)
        result = apply_import(plan, storage, journal=Journal(), job_key="review:changed-file", apply=True)
        self.assertFalse(any(write[0] == "rename" for write in storage.writes), result)
        self.assertEqual(storage.folders["season"][0]["name"], "其他客户端已修改.mkv")

    def test_category_moved_outside_library_before_creation_stops_writes(self):
        class ConcurrentCategoryMove(Storage):
            armed = False

            def list_files(self, parent):
                if self.armed and parent == "inbox":
                    self.armed = False
                    category = next(value for value in self.folders["library"] if value["fid"] == "tv")
                    self.folders["library"].remove(category)
                    category["parent_id"] = "outside"
                    self.folders["outside"].append(category)
                return super().list_files(parent)

        storage = ConcurrentCategoryMove()
        storage.folders["outside"] = []
        plan = self.make_plan(storage)
        storage.armed = True
        result = apply_import(plan, storage, journal=Journal(), job_key="review:category-race", apply=True)
        self.assertEqual(storage.writes, [], result)


if __name__ == "__main__":
    unittest.main()
