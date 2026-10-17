"""Conversational movie requests: parsing, exact identity and task confirmation."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from media_helper import headless_cli
from media_helper.headless_cli import _prepare_movie, parse_request
from media_helper.tasks import TaskStore, movie_confirmation_request

CONFIG = {"library_root_id": "root", "pansou_url": "http://127.0.0.1:8888", "include_media_tags": True,
          "categories": {"movie": {"folder_id": "movies", "name": "电影"}}}


def movie(identifier=693134, **changes):
    return {"id": identifier, "source_id": identifier, "source": "tmdb", "kind": "movie", "title": "沙丘2",
            "original_title": "Dune: Part Two", "year": 2024, "aliases": ["沙丘：第二部"],
            "external_ids": {}, **changes}


class Catalog:
    source = "tmdb"

    def __init__(self, rows):
        self.rows = rows

    def search_identity(self, kind, query, year=None):
        assert kind == "movie"
        return {"results": deepcopy([row for row in self.rows if year in {None, row["year"]}]), "complete": True}

    def get_identity(self, kind, identifier):
        return deepcopy(next(row for row in self.rows if row["id"] == identifier))


class ParseTests(unittest.TestCase):
    def test_movie_word_outside_title_sets_kind(self):
        result = parse_request("帮我找电影《沙丘2》 2024 存到夸克")
        self.assertEqual((result["title"], result["year"], result["kind"]), ("沙丘2", 2024, "movie"))
        self.assertTrue(result["save_requested"])

    def test_unquoted_movie_prefix_is_not_part_of_title(self):
        result = parse_request("帮我查下 电影 沙丘2 2024，存到网盘")
        self.assertEqual((result["title"], result.get("kind")), ("沙丘2", "movie"))

    def test_movie_inside_title_or_tv_request_is_not_a_movie(self):
        self.assertNotIn("kind", parse_request("帮我查下《电影之王》第2季"))
        self.assertNotIn("kind", parse_request("帮我补齐电视剧《嫁金钗》"))


class IdentityTests(unittest.TestCase):
    def prepare(self, rows, **request):
        with patch.object(headless_cli, "_configured", return_value=True), \
                patch.object(headless_cli, "_metadata", return_value=(Catalog(rows), "tmdb")):
            return _prepare_movie({"title": "沙丘2", "kind": "movie", **request}, CONFIG)

    def test_title_and_year_resolve_one_exact_movie_and_category(self):
        resolved, issue = self.prepare([movie(), movie(1, title="沙丘2", year=1990)], year=2024)
        self.assertIsNone(issue)
        self.assertEqual((resolved["movie_id"], resolved["year"], resolved["category_folder_id"]),
                         ("693134", 2024, "movies"))
        self.assertIn("Dune: Part Two", resolved["aliases"])

    def test_missing_year_lists_candidates_without_choosing(self):
        resolved, issue = self.prepare([movie(), movie(1, year=1990)])
        self.assertIsNone(resolved)
        self.assertEqual(issue["status"], "NEEDS_INPUT")
        self.assertEqual({row["id"] for row in issue["metadata_candidates"]}, {693134, 1})

    def test_same_title_and_year_twice_is_not_unique(self):
        resolved, issue = self.prepare([movie(), movie(2)], year=2024)
        self.assertIsNone(resolved)
        self.assertEqual(issue["reason"], "identity_not_unique")

    def test_tmdb_is_required_for_movies(self):
        with patch.object(headless_cli, "_configured", return_value=False), patch.dict("os.environ", {}, clear=True):
            resolved, issue = _prepare_movie({"title": "沙丘2", "year": 2024}, CONFIG)
        self.assertEqual(issue["status"], "NEEDS_AUTH")


class ConfirmationTests(unittest.TestCase):
    def task(self, candidates):
        return {"status": "NEEDS_INPUT", "resolved_request": {"kind": "movie", "movie_id": "1", "year": 2024},
                "report": {"source_candidates": candidates}}

    def test_selection_must_name_an_observed_candidate(self):
        ids = ["a" * 64, "b" * 64]
        rows = [{"candidate_id": ids[0], "fid": "x", "name": "x.mkv", "size": 1, "media_info": {"resolution": "2160p"}},
                {"candidate_id": ids[1], "fid": "y", "name": "y.mkv", "size": 1, "media_info": {"resolution": "1080p"}}]
        request, decision = movie_confirmation_request(self.task(rows), quality="1080p")
        self.assertEqual(request["source_selection"], {"movie": ids[1]})
        with self.assertRaises(ValueError):
            movie_confirmation_request(self.task(rows))
        with self.assertRaises(ValueError):
            movie_confirmation_request(self.task(rows), selection={"movie": "c" * 64})


class ExecuteTests(unittest.TestCase):
    def test_movie_task_routes_to_movie_workflow_and_keeps_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TaskStore(Path(folder) / "tasks", "root")
            task = store.create({"title": "沙丘2", "kind": "movie", "year": 2024})
            resolved = {"title": "沙丘2", "kind": "movie", "year": 2024, "movie_id": "693134",
                        "metadata_source": "tmdb", "library_root_id": "root"}
            calls = []

            def fake_run(request, storage, search, journal, apply=False):
                calls.append((request["movie_id"], apply))
                return {"status": "READY", "plan": [], "writes_performed": False}

            with patch.object(headless_cli, "_prepare_movie", return_value=(resolved, None)), \
                    patch.object(headless_cli, "_quark_ready", return_value=True), \
                    patch.object(headless_cli, "_storage", return_value=object()), \
                    patch.object(headless_cli, "ROOT", Path(folder)), \
                    patch("media_helper.movie_workflow.run", side_effect=fake_run):
                result = headless_cli._execute_task(task, store, CONFIG, apply=False)
            self.assertEqual(result["status"], "READY")
            self.assertEqual(calls, [("693134", False)])
            self.assertEqual(store.load(task["task_id"])["resolved_request"]["movie_id"], "693134")


if __name__ == "__main__":
    unittest.main()
