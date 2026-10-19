"""llm.py 测试：骨架槽位改写（rewrite）、JSON 解析、失败降级、格式校验退回草稿，
以及 key/模型的多级来源解析（参数 > 环境变量 > llm.json）与 last_error 回报。"""

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ai_report import llm
from ai_report.llm import ReportSynthesizer

OUTLINE = {
    "title": "AI 日报 2026-10-16",
    "sources": "> 数据来源：CodeBuddy CN（本地聚合，隐私优先）",
    "sections": [
        {"name": "铜线检测项目", "items": [
            {"title": "现场重编译环境确认", "body": "09:14 想让他重新编译一下，我要如何操作？"},
        ]},
        {"name": "端子检测项目", "items": [
            {"title": "结果图结论行错误",
             "body": "11:05 [图片] 这个绿色的结论是错的 我需要你删除这几行结论"},
        ]},
    ],
}

REWRITTEN = {
    "sections": [
        {"name": "铜线检测项目", "items": [
            {"title": "现场重编译与运行环境确认",
             "body": "核对现场电脑的重编译流程与 conda 运行环境，确认操作步骤。"},
        ]},
        {"name": "端子检测项目", "items": [
            {"title": "结果图结论标注修正",
             "body": "针对结果图中绿色结论行标注错误的问题，调整结果图输出逻辑。"},
        ]},
    ],
}


class _FakeResp(io.BytesIO):
    """模拟 urlopen 返回的响应对象（支持上下文管理器）。"""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def fake_urlopen(payload: dict, captured: dict | None = None):
    def _inner(req, timeout=None):
        if captured is not None:
            captured["url"] = req.full_url
            captured["headers"] = dict(req.header_items())
            captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp(json.dumps(payload).encode("utf-8"))
    return _inner


def reply(content: str, captured: dict | None = None):
    """模型正常回复（content 为字符串）时的 urlopen 桩。"""
    return fake_urlopen({"choices": [{"message": {"content": content}}]}, captured)


class StubSynth:
    """polish_outline 用的最小归纳器桩（只关心 rewrite / last_error）。"""

    def __init__(self, result, error: str = ""):
        self.result = result
        self.last_error = error
        self.available = True
        self.missing = ""
        self.calls = 0

    def rewrite(self, outline):
        self.calls += 1
        return self.result


class TestNoKeyFallback(unittest.TestCase):
    def test_missing_key_returns_none_and_records_reason(self):
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": ""}, clear=False):
            os.environ.pop("DEEPSEEK_API_KEY", None)
            synth = ReportSynthesizer(provider="deepseek", api_key="")
            self.assertFalse(synth.available)
            self.assertIsNone(synth.rewrite(OUTLINE))
            self.assertEqual(synth.last_error, "未配置 DEEPSEEK_API_KEY")


class TestPromptGuards(unittest.TestCase):
    """提示词必须明确「只能改文字、结构不许动」，否则模型会照抄或改坏排版。"""

    def test_prompt_forbids_copying_and_demands_rewrite(self):
        p = llm.SYSTEM_PROMPT
        self.assertIn("照抄", p)
        self.assertIn("严禁", p)
        self.assertIn("[图片]", p)
        self.assertIn("改写", p)

    def test_prompt_pins_the_structure(self):
        p = llm.SYSTEM_PROMPT
        self.assertIn("结构一个字都不能动", p)
        self.assertIn("不许增删", p)
        self.assertIn("顺序", p)
        self.assertIn("sections", p)
        self.assertIn("项目名", p)
        self.assertIn("JSON", p)

    def test_prompt_tells_it_to_drop_item_meta_lines(self):
        """条目下的「时间/来源/状态」元信息行必须被删掉（报告要简洁）。"""
        p = llm.SYSTEM_PROMPT
        self.assertIn("时间：", p)
        self.assertIn("来源：", p)
        self.assertIn("状态：", p)
        self.assertIn("元信息行", p)

    def test_prompt_forbids_fabrication(self):
        self.assertIn("不得编造", llm.SYSTEM_PROMPT)

    def test_prompt_separates_user_claims_from_verified_facts(self):
        """用户把现象/猜测当结论讲时，不能改写成「已定位/已验证」的结论句。"""
        p = llm.SYSTEM_PROMPT
        self.assertIn("用户的判断", p)
        self.assertIn("已定位到", p)
        self.assertIn("严禁改写成", p)

    def test_prompt_forbids_inventing_for_empty_body(self):
        """body 原本为空的条目必须留空，否则就是凭空加戏。"""
        self.assertIn("绝不替它编内容", llm.SYSTEM_PROMPT)

    def test_draft_lead_discourages_echoing_and_restructuring(self):
        self.assertIn("不要照抄", llm.DRAFT_LEAD)
        self.assertIn("不要改结构", llm.DRAFT_LEAD)


class TestRewriteSuccess(unittest.TestCase):
    def test_parses_json_and_sends_slots_only(self):
        captured = {}
        with mock.patch("ai_report.llm.urllib.request.urlopen",
                        side_effect=reply(json.dumps(REWRITTEN, ensure_ascii=False), captured)):
            synth = ReportSynthesizer(provider="deepseek", api_key="sk-test")
            out = synth.rewrite(OUTLINE)

        self.assertEqual(out, REWRITTEN)
        self.assertEqual(synth.last_error, "")
        self.assertEqual(captured["body"]["model"], "deepseek-chat")
        self.assertEqual(captured["body"]["temperature"], 0.3)
        user_msg = captured["body"]["messages"][1]["content"]
        self.assertTrue(user_msg.startswith(llm.DRAFT_LEAD))
        # 只发槽位文字：标题/来源/页脚不进提示词，减少模型可改坏的东西
        self.assertIn('"sections"', user_msg)
        self.assertNotIn(OUTLINE["title"], user_msg)
        self.assertNotIn("本地聚合", user_msg)
        self.assertIn("Bearer sk-test", captured["headers"].get("Authorization", ""))

    def test_fenced_json_is_accepted(self):
        with mock.patch("ai_report.llm.urllib.request.urlopen",
                        side_effect=reply("```json\n" + json.dumps(REWRITTEN,
                                                                   ensure_ascii=False) + "\n```")):
            synth = ReportSynthesizer(provider="deepseek", api_key="sk-test")
            self.assertEqual(synth.rewrite(OUTLINE), REWRITTEN)

    def test_empty_outline_short_circuits_without_calling_the_model(self):
        synth = ReportSynthesizer(provider="deepseek", api_key="sk-test")
        with mock.patch("ai_report.llm.urllib.request.urlopen") as m:
            self.assertIsNone(synth.rewrite({"title": "空", "sections": []}))
            m.assert_not_called()
        self.assertEqual(synth.last_error, "")


class TestJsonParsing(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(llm._parse_json('{"a": 1}'), {"a": 1})

    def test_fenced(self):
        self.assertEqual(llm._parse_json("```json\n{\"a\": 1}\n```"), {"a": 1})

    def test_with_surrounding_prose(self):
        self.assertEqual(llm._parse_json('好的，结果如下：{"a": 1} 请查收'), {"a": 1})

    def test_garbage_returns_none(self):
        self.assertIsNone(llm._parse_json("抱歉，我无法完成这个任务。"))
        self.assertIsNone(llm._parse_json(""))


class TestFailureFallback(unittest.TestCase):
    def test_network_error_returns_none(self):
        import urllib.error
        with mock.patch("ai_report.llm.urllib.request.urlopen",
                        side_effect=urllib.error.URLError("boom")):
            synth = ReportSynthesizer(provider="deepseek", api_key="sk-test")
            self.assertIsNone(synth.rewrite(OUTLINE))
            self.assertIn("URLError", synth.last_error)

    def test_bad_response_returns_none(self):
        with mock.patch("ai_report.llm.urllib.request.urlopen",
                        side_effect=fake_urlopen({"choices": []})):
            synth = ReportSynthesizer(provider="deepseek", api_key="sk-test")
            self.assertIsNone(synth.rewrite(OUTLINE))

    def test_non_json_reply_returns_none(self):
        with mock.patch("ai_report.llm.urllib.request.urlopen",
                        side_effect=reply("抱歉，我不能这样做。")):
            synth = ReportSynthesizer(provider="deepseek", api_key="sk-test")
            self.assertIsNone(synth.rewrite(OUTLINE))
            self.assertIn("JSON", synth.last_error)

    def test_unknown_provider_falls_back_to_deepseek(self):
        synth = ReportSynthesizer(provider="no-such", api_key="k")
        self.assertEqual(synth.provider, "deepseek")


class TestPolishOutline(unittest.TestCase):
    """「程序强制排版」：模型改文字，generator 排版，校验不过就退回草稿。"""

    def test_no_synth_is_pure_template(self):
        md, err = llm.polish_outline(OUTLINE)
        self.assertEqual(err, "")
        self.assertIn("## 一、铜线检测项目", md)
        self.assertIn("现场重编译环境确认", md)

    def test_rewritten_text_is_rendered_by_the_program(self):
        md, err = llm.polish_outline(OUTLINE, None, StubSynth(REWRITTEN))
        self.assertEqual(err, "")
        self.assertIn("现场重编译与运行环境确认", md)
        self.assertNotIn("现场重编译环境确认", md)
        # 排版仍由本机生成：项目行与编号是程序写的
        self.assertIn("## 一、铜线检测项目", md)
        self.assertIn("## 二、端子检测项目", md)

    def test_report_title_and_sources_survive_the_merge(self):
        """模型只回 sections；报告标题与来源行必须来自本机骨架（不能丢）。"""
        md, err = llm.polish_outline(OUTLINE, None, StubSynth(REWRITTEN))
        self.assertEqual(err, "")
        self.assertIn(f"# {OUTLINE['title']}", md)
        self.assertIn(OUTLINE["sources"], md)

    def test_merge_does_not_mutate_the_reference_outline(self):
        llm.polish_outline(OUTLINE, None, StubSynth(REWRITTEN))
        self.assertEqual(OUTLINE["sections"][0]["items"][0]["title"], "现场重编译环境确认")

    def test_ai_dropping_an_item_falls_back_to_draft(self):
        bad = {"sections": [{"name": "铜线检测项目", "items": []},
                            REWRITTEN["sections"][1]]}
        stub = StubSynth(bad)
        md, err = llm.polish_outline(OUTLINE, None, stub)
        self.assertIn("格式校验", err)
        self.assertEqual(stub.last_error, err)
        self.assertIn("现场重编译环境确认", md)      # 退回草稿原文
        self.assertNotIn("现场重编译与运行环境确认", md)

    def test_ai_renaming_a_project_falls_back_to_draft(self):
        bad = {"sections": [{"name": "铜线", "items": REWRITTEN["sections"][0]["items"]},
                            REWRITTEN["sections"][1]]}
        md, err = llm.polish_outline(OUTLINE, None, StubSynth(bad))
        self.assertIn("项目名被改动", err)
        self.assertIn("## 一、铜线检测项目", md)

    def test_ai_leaving_a_timestamp_falls_back_to_draft(self):
        bad = {"sections": [{"name": "铜线检测项目",
                             "items": [{"title": "复核", "body": "09:14 重新编译环境"}]},
                            REWRITTEN["sections"][1]]}
        md, err = llm.polish_outline(OUTLINE, None, StubSynth(bad))
        self.assertIn("时间戳", err)
        self.assertIn("现场重编译环境确认", md)

    def test_ai_leaving_an_image_placeholder_falls_back_to_draft(self):
        bad = {"sections": [REWRITTEN["sections"][0],
                            {"name": "端子检测项目",
                             "items": [{"title": "结论行", "body": "[图片] 结论是错的"}]}]}
        md, err = llm.polish_outline(OUTLINE, None, StubSynth(bad))
        self.assertIn("[图片]", err)
        self.assertNotIn("[图片] 结论是错的", md)

    def test_ai_emptying_a_body_falls_back_to_draft(self):
        bad = {"sections": [{"name": "铜线检测项目",
                             "items": [{"title": "复核", "body": ""}]},
                            REWRITTEN["sections"][1]]}
        md, err = llm.polish_outline(OUTLINE, None, StubSynth(bad))
        self.assertIn("正文被清空", err)

    def test_no_key_reports_reason_and_keeps_draft(self):
        stub = StubSynth(None, error="未配置 DEEPSEEK_API_KEY")
        md, err = llm.polish_outline(OUTLINE, None, stub)
        self.assertEqual(err, "未配置 DEEPSEEK_API_KEY")
        self.assertIn("现场重编译环境确认", md)

    def test_empty_outline_skips_the_synth(self):
        stub = StubSynth(REWRITTEN)
        md, err = llm.polish_outline({"title": "空", "sections": []}, None, stub)
        self.assertEqual(err, "")
        self.assertEqual(stub.calls, 0)
        self.assertIn("本期没有符合条件的 AI 会话记录", md)


class TestConfigResolution(unittest.TestCase):
    """key / 模型优先级：显式参数 > 环境变量 > llm.json（本地配置）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = Path(self._tmp.name) / "llm.json"
        self._env = mock.patch.dict(os.environ, {
            "AIREREPORT_LLM_FILE": str(self.cfg),
            "DEEPSEEK_API_KEY": "", "LLM_PROVIDER": "", "LLM_MODEL": "",
        }, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        for name in ("DEEPSEEK_API_KEY", "LLM_PROVIDER", "LLM_MODEL"):
            os.environ.pop(name, None)

    def _write(self, data: dict):
        self.cfg.write_text(json.dumps(data), encoding="utf-8")

    def test_file_supplies_key_and_model(self):
        self._write({"provider": "deepseek", "model": "deepseek-reasoner",
                     "api_key": "sk-file"})
        synth = ReportSynthesizer()
        self.assertTrue(synth.available)
        self.assertEqual(synth.api_key, "sk-file")
        self.assertEqual(synth.model, "deepseek-reasoner")
        self.assertEqual(synth.missing, "")

    def test_env_beats_file(self):
        self._write({"api_key": "sk-file", "model": "deepseek-chat"})
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-env",
                                          "LLM_MODEL": "deepseek-env"}):
            synth = ReportSynthesizer()
        self.assertEqual(synth.api_key, "sk-env")
        self.assertEqual(synth.model, "deepseek-env")

    def test_explicit_arg_beats_env(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-env"}):
            synth = ReportSynthesizer(api_key="sk-arg", model="m")
        self.assertEqual((synth.api_key, synth.model), ("sk-arg", "m"))

    def test_missing_reason_names_env_var(self):
        self._write({"provider": "deepseek"})   # 没有 key
        synth = ReportSynthesizer()
        self.assertFalse(synth.available)
        self.assertEqual(synth.missing, "未配置 DEEPSEEK_API_KEY")

    def test_broken_file_is_ignored(self):
        self.cfg.write_text("{ not json", encoding="utf-8")
        synth = ReportSynthesizer()
        self.assertEqual(synth.model, "deepseek-chat")
        self.assertFalse(synth.available)

    def test_ollama_needs_no_key_and_model_is_overridable(self):
        self._write({"provider": "ollama", "model": "qwen3.8:27b"})
        synth = ReportSynthesizer()
        self.assertTrue(synth.available)
        self.assertEqual(synth.model, "qwen3.8:27b")
        self.assertIn("11434", synth.conf["url"])

    def test_last_error_records_why_it_fell_back(self):
        synth = ReportSynthesizer(api_key="")
        self.assertIsNone(synth.rewrite(OUTLINE))
        self.assertEqual(synth.last_error, "未配置 DEEPSEEK_API_KEY")

    def test_last_error_cleared_when_call_succeeds(self):
        synth = ReportSynthesizer(api_key="")
        synth.rewrite(OUTLINE)
        self.assertTrue(synth.last_error)
        with mock.patch("ai_report.llm.urllib.request.urlopen",
                        side_effect=reply(json.dumps(REWRITTEN, ensure_ascii=False))):
            synth2 = ReportSynthesizer(api_key="sk-test")
            synth2.rewrite(OUTLINE)
        self.assertEqual(synth2.last_error, "")

    def test_status_reports_readiness(self):
        self._write({"provider": "deepseek", "api_key": "sk-file",
                     "model": "deepseek-chat"})
        st = llm.status()
        self.assertTrue(st["ready"])
        self.assertEqual(st["model"], "deepseek-chat")
        self.assertEqual(st["config_file"], str(self.cfg))
        self.assertEqual(st["missing"], "")

    def test_status_when_unconfigured(self):
        st = llm.status()
        self.assertFalse(st["ready"])
        self.assertIn("DEEPSEEK_API_KEY", st["missing"])


class TestConfigFileLocation(unittest.TestCase):
    """配置文件位置优先级：环境变量 > 仓库根 llm.json > ~/.aireport/llm.json。"""

    def test_env_wins(self):
        with mock.patch.dict(os.environ, {"AIREREPORT_LLM_FILE": "D:/x/llm.json"}):
            self.assertEqual(str(llm.llm_file()), "D:\\x\\llm.json")

    def test_repo_root_when_no_env(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AIREREPORT_LLM_FILE", None)
            found = llm.llm_file()
        if found is not None:                      # 本机可能确实没有该文件
            self.assertTrue(str(found).endswith("llm.json"))


if __name__ == "__main__":
    unittest.main()
