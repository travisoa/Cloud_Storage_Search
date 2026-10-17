from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from media_helper.tasks import TaskStore, confirmation_request


def candidate(identifier, *, name="02.mkv", number=2, quality="2160p", size=100):
    return {"candidate_id": identifier * 64, "fid": "source-" + identifier, "name": name,
            "size": size, "parent_id": "shared", "path": "4K/" + name,
            "suggested_episode": number, "media_info": {"resolution": quality, "basis": "parent_directory"}}


class TaskSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "tasks"
        self.store = TaskStore(self.path, "library-root")
        self.request = {"title": "测试剧", "year": 2026, "season": 1, "series_id": 17, "metadata_source": "tvmaze"}
        self.report = {"status": "NEEDS_INPUT", "missing": [{"episode_key": "S01E02"}],
                       "unresolved_episodes": {"S01E02": []},
                       "source_candidates": [candidate("a"), candidate("b", quality="1080p")]}

    def task(self):
        return self.store.create(self.request, report=self.report)

    def test_private_task_persists_safe_resolved_scope_and_report(self):
        task = self.store.create({**self.request, "request_text": "do not persist freeform text"})
        self.store.finish(task, self.report)
        loaded = self.store.load(task["task_id"])
        self.assertEqual(loaded["library_root_id"], "library-root")
        self.assertNotIn("request_text", loaded["resolved_request"])
        self.assertEqual(loaded["report"]["task_id"], task["task_id"])
        self.assertFalse(loaded["report"]["browser_opened"])
        self.assertEqual((self.path / (task["task_id"] + ".json")).stat().st_mode & 0o777, 0o600)

    def test_confirmation_derives_real_name_size_fid_and_ids_from_snapshot(self):
        task = self.task()
        request, decision = confirmation_request(task, quality="4K", numeric_episodes=True)
        self.assertEqual(request["source_selection"], {"S01E02": "a" * 64})
        self.assertEqual(request["source_episode_mapping"], {"a" * 64: {"season": 1, "episode": 2, "name": "02.mkv", "size": 100}})
        self.assertEqual(decision["candidates"][0]["fid"], "source-a")
        self.assertEqual(request["quality_preference"], ["2160p"])
        self.assertEqual(task["resolved_request"], {**self.request, "library_root_id": "library-root"})

    def test_bare_numeric_requires_explicit_numeric_confirmation(self):
        with self.assertRaises(ValueError):
            confirmation_request(self.task(), quality="2160p")

    def test_tied_version_requires_explicit_candidate_id(self):
        self.report["source_candidates"].append(candidate("c"))
        task = self.task()
        with self.assertRaises(ValueError):
            confirmation_request(task, quality="2160p", numeric_episodes=True)
        request, _ = confirmation_request(task, quality="2160p", numeric_episodes=True, selection={"S01E02": "c" * 64})
        self.assertEqual(request["source_selection"], {"S01E02": "c" * 64})

    def test_cannot_select_unobserved_candidate_or_extra_episode(self):
        for choice in ({"S01E02": "c" * 64}, {"S01E03": "a" * 64}, {"S01E02": {"url": "https://example.invalid"}}):
            with self.subTest(choice=choice), self.assertRaises(ValueError):
                confirmation_request(self.task(), numeric_episodes=True, selection=choice)

    def test_completed_task_not_replayed_by_confirm(self):
        task = self.task()
        task["status"] = "COMPLETE"
        with self.assertRaises(ValueError):
            confirmation_request(task, quality="2160p", numeric_episodes=True)

    def test_root_change_and_arbitrary_paths_cannot_reuse_task(self):
        task = self.task()
        with self.assertRaises(ValueError):
            TaskStore(self.path, "other-root").load(task["task_id"])
        for identifier in ("../escape", "/tmp/task", "a" * 33, "a" * 32 + ".json"):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                self.store.load(identifier)
        with self.assertRaises(ValueError):
            self.store.create({**self.request, "library_root_id": "other-root"})

    def test_task_symlink_and_credentials_rejected(self):
        task = self.task()
        task["report"]["Cookie"] = "should-not-write"
        with self.assertRaises(ValueError):
            self.store.save(task)
        path = self.path / (task["task_id"] + ".json")
        path.unlink()
        path.symlink_to(Path(self.temporary.name) / "elsewhere.json")
        with self.assertRaises(ValueError):
            self.store.load(task["task_id"])
