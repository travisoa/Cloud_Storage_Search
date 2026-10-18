from contextlib import redirect_stdout
import io
import json
import unittest
from unittest.mock import patch

from media_helper.headless_cli import choose_identity, main, parse_request, _resolve


class ConversationalRequestTests(unittest.TestCase):
    def test_user_example_extracts_title_and_authorized_save_intent(self):
        result = parse_request("帮我查下 嫁金钗 存到我的夸克网盘并重命名")
        self.assertEqual(result["title"], "嫁金钗")
        self.assertTrue(result["save_requested"])

    def test_browse_only_request_does_not_authorize_cloud_writes(self):
        self.assertFalse(parse_request("帮我查下《嫁金钗》")["save_requested"])

    def test_title_beginning_with_you_is_not_truncated(self):
        self.assertEqual(parse_request("帮我查下 你是我的荣耀 存到夸克")["title"], "你是我的荣耀")

    def test_explicit_identity_fields_are_preserved(self):
        result = parse_request("帮我补齐《示例剧》 2025 第2季存到夸克")
        self.assertEqual((result["title"], result["year"], result["season"]), ("示例剧", 2025, 2))

    def test_negative_save_instruction_never_authorizes_writes(self):
        self.assertFalse(parse_request("只查询《嫁金钗》，不要转存")["save_requested"])
        self.assertFalse(parse_request("先看《嫁金钗》的补齐计划")["save_requested"])

    def test_action_mentions_and_negated_requests_are_read_only(self):
        for text in (
            "查询《示例剧》的转存记录", "查一下《示例剧》有没有转存过",
            "看看《示例剧》是否已保存", "《示例剧》当前转存状态",
            "帮我查《示例剧》补齐情况", "查询《示例剧》转存失败原因",
            "《示例剧》保存到哪里了", "《示例剧》转存了吗",
            "《示例剧》转存是什么", "《示例剧》转存失败",
            "不要帮我转存《示例剧》", "别给我保存《示例剧》",
            "不要把《示例剧》转存到网盘", "别给《示例剧》补齐",
            "查询《示例剧》转存到哪里了", "查看《示例剧》保存到了哪里",
            "查看《示例剧》转存到夸克的记录", "查询《示例剧》保存到网盘的结果",
            "暂时不转存《示例剧》", "只看《示例剧》，保存过没有",
            "我不想转存《示例剧》", "不要追更《示例剧》", "追更《示例剧》",
            "查询《保存的故事》", "查看《补齐人生》的转存进度",
        ):
            with self.subTest(text=text):
                self.assertFalse(parse_request(text)["save_requested"])

    def test_explicit_actions_still_authorize_writes_without_apply_flag(self):
        for text in (
            "转存《示例剧》缺失集并规范命名", "请保存《示例剧》",
            "帮我补齐《示例剧》", "不要计划，直接转存《示例剧》",
            "按上次计划转存《示例剧》", "查询《示例剧》后存到网盘",
            "先核对《示例剧》，然后转存缺集", "帮我查下《示例剧》存到我的网盘",
        ):
            with self.subTest(text=text):
                self.assertTrue(parse_request(text)["save_requested"])

    def test_titles_with_negation_characters_keep_an_explicit_save(self):
        for text in ("不良人第二季转存到影库", "特别行动转存到网盘", "不完美受害人存到网盘"):
            with self.subTest(text=text):
                self.assertTrue(parse_request(text)["save_requested"])
        for text in ("别把《示例剧》转存", "先不转存《示例剧》", "请勿转存《示例剧》", "不再补齐《示例剧》"):
            with self.subTest(text=text):
                self.assertFalse(parse_request(text)["save_requested"])

    def test_default_and_library_requests_use_the_same_intent_boundary(self):
        config = {"library_root_id": "synthetic-root"}
        for text, expected in (("查询《示例剧》的转存记录", False),
                               ("直接转存《示例剧》", True)):
            with self.subTest(text=text), patch("media_helper.headless_cli._run", return_value={"status": "READY"}) as run:
                with redirect_stdout(io.StringIO()):
                    main(["request", text])
                self.assertEqual(run.call_args.kwargs["apply"], expected)
                with patch("media_helper.library_cli._registry") as registry:
                    from argparse import Namespace
                    from media_helper.library_cli import run_command
                    registry.return_value.config_for.return_value = config
                    run_command(Namespace(library_action="request", library_id="home", text=text,
                                          plan=False, kind=None), config, None)
                self.assertEqual(run.call_args.kwargs["apply"], expected)

    def test_quoted_title_is_never_interpreted_as_a_command_or_year(self):
        for title in ("请回答1988", "给我一支烟", "保存的故事"):
            result = parse_request("帮我查下《" + title + "》")
            self.assertEqual(result["title"], title)
            self.assertNotIn("year", result)
            self.assertFalse(result["save_requested"])

    def test_unquoted_reconcile_command_and_numeric_title(self):
        self.assertEqual(parse_request("帮我补齐 嫁金钗")["title"], "嫁金钗")
        self.assertEqual(parse_request("帮我查下 请回答1988")["title"], "请回答1988")

    def test_full_real_korean_request_has_only_the_work_title(self):
        result = parse_request("好的，我们先测试下 查询下韩剧 白色黑手党时代 存到我的网盘")
        self.assertEqual(result["title"], "白色黑手党时代")
        self.assertTrue(result["save_requested"])

    def test_full_request_negation_still_overrides_save_words(self):
        result = parse_request("好的，我们先测试下 查询下韩剧 白色黑手党时代 存到我的网盘，先看计划不要保存")
        self.assertEqual(result["title"], "白色黑手党时代")
        self.assertFalse(result["save_requested"])

    def test_standalone_numeric_title_is_not_a_year(self):
        for title in ("1988", "1923", "1992"):
            result = parse_request("查下 " + title + " 存到我的网盘")
            self.assertEqual(result["title"], title)
            self.assertNotIn("year", result)

    def test_unchecked_alias_candidate_prevents_false_unique_identity(self):
        items = [{"id": 1, "title": "English", "aliases": ["同名剧"], "year": 2026, "aliases_checked": True},
                 {"id": 2, "title": "Different English", "year": 2026, "aliases_checked": False}]
        self.assertIsNone(choose_identity("同名剧", items))

    def test_explicit_mismatched_id_cannot_pollute_binding(self):
        class Metadata:
            def get_series(self, _):
                return {"id": 99, "title": "另一部剧", "year": 2026}
        resolved, issue = _resolve({"title": "目标剧", "series_id": 99, "year": 2026}, Metadata(), "tvmaze")
        self.assertIsNone(resolved)
        self.assertEqual(issue["status"], "NEEDS_INPUT")

    def test_ambiguous_same_title_never_chooses_first(self):
        items = [{"id": "1", "title": "同名剧", "year": 2020}, {"id": "2", "title": "同名剧", "year": 2025}]
        self.assertIsNone(choose_identity("同名剧", items))
        self.assertEqual(choose_identity("同名剧", list(reversed(items)), 2020)["id"], "1")

    def test_multiple_seasons_require_identity_instead_of_guessing_latest(self):
        class Metadata:
            def search_tv(self, *_):
                return [{"id": 1, "title": "示例", "year": 2025}]
            def get_series(self, _):
                return {"id": 1, "title": "示例", "year": 2025, "seasons": [{"season": 1}, {"season": 2}]}
        resolved, issue = _resolve({"title": "示例"}, Metadata(), "tvmaze")
        self.assertIsNone(resolved)
        self.assertEqual(issue["status"], "NEEDS_INPUT")

    def test_doctor_reports_only_presence_and_never_opens_browser(self):
        output = io.StringIO()
        config = {"pansou_url": "http://127.0.0.1:8888", "library_root_id": "fixture-root"}
        with patch("media_helper.headless_cli._config", return_value=config), patch("media_helper.headless_cli._configured", return_value=False), patch("media_helper.headless_cli.urlopen", side_effect=OSError()), redirect_stdout(output):
            code = main(["doctor"])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertFalse(result["browser_opened"])
        self.assertFalse(result["quark_configured"])


if __name__ == "__main__":
    unittest.main()
