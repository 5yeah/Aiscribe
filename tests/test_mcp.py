"""mcp_server 测试：JSON-RPC dispatch、工具调用、stdio 循环。"""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ai_report import config
from ai_report.mcp_server import _make_response, dispatch, serve_stdio


class TestDispatch(unittest.TestCase):
    def test_initialize_echoes_supported_version(self):
        out = dispatch("initialize", {"protocolVersion": "2025-06-18"})
        self.assertEqual(out["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(out["result"]["serverInfo"]["name"], "aireport")
        self.assertIn("tools", out["result"]["capabilities"])
        self.assertIn("云端模型", out["result"]["instructions"])

    def test_initialize_echoes_any_client_version(self):
        # 无条件 echo：严格校验的客户端必然支持自己声明的版本，避免断开
        out = dispatch("initialize", {"protocolVersion": "1999-01-01"})
        self.assertEqual(out["result"]["protocolVersion"], "1999-01-01")

    def test_initialize_without_version_uses_default(self):
        out = dispatch("initialize", {})
        self.assertEqual(out["result"]["protocolVersion"], "2025-06-18")

    def test_ping(self):
        self.assertEqual(dispatch("ping", None), {"result": {}})

    def test_tools_list(self):
        out = dispatch("tools/list", None)
        names = [t["name"] for t in out["result"]["tools"]]
        self.assertEqual(names, ["list_sources", "generate_report",
                                 "get_conversations", "save_report",
                                 "list_templates", "get_format_spec", "save_template"])
        by_name = {t["name"]: t for t in out["result"]["tools"]}
        schema = by_name["generate_report"]["inputSchema"]
        self.assertIn("mode", schema["properties"])
        self.assertIn("range", schema["properties"]["mode"]["enum"])
        self.assertIn("html", schema["properties"]["format"]["enum"])
        self.assertTrue(by_name["list_sources"]["annotations"]["readOnlyHint"])
        # 素材接口：供 AI 归纳用；默认先摘要，避免一次性把全文灌进上下文
        detail = by_name["get_conversations"]["inputSchema"]["properties"]["detail"]
        self.assertEqual(detail["default"], "summary")
        self.assertTrue(by_name["get_conversations"]["annotations"]["readOnlyHint"])
        self.assertIn("markdown",
                      by_name["save_report"]["inputSchema"]["properties"])
        self.assertEqual(by_name["save_report"]["inputSchema"]["required"],
                         ["markdown"])
        self.assertIn("html",
                      by_name["save_report"]["inputSchema"]["properties"]["format"]["enum"])
        # 格式克隆：模板读写
        self.assertEqual(by_name["save_template"]["inputSchema"]["required"],
                         ["name", "features"])
        self.assertIn("features", by_name["save_template"]["inputSchema"]["properties"])

    def test_unknown_method(self):
        out = dispatch("bogus/method", None)
        self.assertEqual(out["error"]["code"], -32601)

    def test_call_list_sources(self):
        out = dispatch("tools/call", {"name": "list_sources", "arguments": {}})
        data = json.loads(out["result"]["content"][0]["text"])
        self.assertIsInstance(data, list)
        self.assertTrue(any(d["name"] == "WorkBuddy" for d in data))

    def test_call_generate_report_daily(self):
        out = dispatch("tools/call", {
            "name": "generate_report",
            "arguments": {"mode": "daily", "date": "2026-10-09"},
        })
        self.assertFalse(out["result"]["isError"])
        text = out["result"]["content"][0]["text"]
        self.assertIn("# AI 日报 2026-10-09", text)

    def test_get_conversations_defaults_to_summary(self):
        """不传 detail 时只回摘要，避免默认把全文灌进模型上下文。"""
        from ai_report.models import UnifiedRecord
        from ai_report import index as idx

        rec = UnifiedRecord(
            source="TRAE", session_id="t:summary", title="标题", project="",
            status="completed", cwd="D:/demo",
            start_ts=1792022400.0, end_ts=1792026000.0,
            summary="做了一件事", raw={"messages": [{"ts": 1792022400.0, "text": "全文"}]},
        )
        with mock.patch.object(idx, "select_records", return_value=[rec]):
            out = dispatch("tools/call", {
                "name": "get_conversations",
                "arguments": {"mode": "daily", "date": "2026-10-16"},
            })
        payload = json.loads(out["result"]["content"][0]["text"])
        entry = payload["projects"][0]["records"][0]
        self.assertIn("summary", entry)
        self.assertNotIn("messages", entry)

    def test_call_generate_report_range_requires_dates(self):
        out = dispatch("tools/call", {
            "name": "generate_report",
            "arguments": {"mode": "range"},
        })
        self.assertTrue(out["result"]["isError"])
        self.assertIn("start", out["result"]["content"][0]["text"])

    def test_call_unknown_tool(self):
        out = dispatch("tools/call", {"name": "nope", "arguments": {}})
        self.assertTrue(out["result"]["isError"])

    def test_call_save_report_html(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(config, "ensure_reports_dir",
                                   return_value=Path(d)):
                out = dispatch("tools/call", {
                    "name": "save_report",
                    "arguments": {"markdown": "# 日报\n\n**1、Aiscribe：**\n\n①、修好了。",
                                  "filename": "AI日报_2026-10-16",
                                  "format": "html"}})
            text = out["result"]["content"][0]["text"]
            self.assertIn("AI日报_2026-10-16.html", text)
            page = (Path(d) / "AI日报_2026-10-16.html").read_text("utf-8")
            self.assertIn("<!DOCTYPE html>", page)
            self.assertIn("<style>", page)   # 自包含：样式内嵌
            self.assertNotIn("<script", page)

    def test_call_save_report_rejects_unknown_format(self):
        out = dispatch("tools/call", {
            "name": "save_report",
            "arguments": {"markdown": "# x", "format": "pdf"}})
        self.assertTrue(out["result"]["isError"])

    def test_generate_report_honours_format(self):
        outline = {"title": "t", "sources": "", "sections": []}
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(config, "ensure_reports_dir",
                                   return_value=Path(d)), \
                 mock.patch("ai_report.mcp_server.generator.outline_daily",
                            return_value=outline):
                out = dispatch("tools/call", {
                    "name": "generate_report",
                    "arguments": {"mode": "daily", "date": "2026-10-16",
                                  "format": "html", "save": True},
                })
            self.assertFalse(out["result"]["isError"])
            text = out["result"]["content"][0]["text"]
            self.assertIn("AI日报_2026-10-16.html", text)
            self.assertTrue((Path(d) / "AI日报_2026-10-16.html").is_file())


class TestMakeResponse(unittest.TestCase):
    def test_notification_gets_no_response(self):
        req = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        self.assertIsNone(_make_response(req))

    def test_request_with_id(self):
        req = {"jsonrpc": "2.0", "id": 7, "method": "tools/list"}
        resp = _make_response(req)
        self.assertEqual(resp["id"], 7)
        self.assertIn("result", resp)

    def test_missing_method(self):
        resp = _make_response({"jsonrpc": "2.0", "id": 3})
        self.assertIn("error", resp)

    def test_tool_exception_is_isolated(self):
        """单个工具抛异常只影响这一条请求，stdio 循环还能继续。"""
        with mock.patch("ai_report.mcp_server._tool_list_sources",
                        side_effect=RuntimeError("boom")):
            resp = _make_response({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                                   "params": {"name": "list_sources",
                                              "arguments": {}}})
        self.assertEqual(resp["id"], 8)
        # 工具异常按 MCP 约定放进 result.isError，而不是 JSON-RPC error
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("boom", resp["result"]["content"][0]["text"])
        nxt = _make_response({"jsonrpc": "2.0", "id": 9, "method": "ping"})
        self.assertEqual(nxt["result"], {})


class TestServeStdio(unittest.TestCase):
    def test_loop_responses(self):
        lines = [
            "﻿" + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": "2025-03-26"}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\r",
            "not-json{",
            "",
        ]
        stdin = io.StringIO("\n".join(lines) + "\n")
        stdout = io.StringIO()
        serve_stdio(in_stream=stdin, out_stream=stdout)

        out_lines = [l for l in stdout.getvalue().splitlines() if l.strip()]
        # 通知不回包 → 1(init) + 2(tools) + parse-error = 3 条
        self.assertEqual(len(out_lines), 3)
        first = json.loads(out_lines[0])
        self.assertEqual(first["id"], 1)
        self.assertEqual(first["result"]["protocolVersion"], "2025-03-26")
        second = json.loads(out_lines[1])
        self.assertEqual(second["id"], 2)
        third = json.loads(out_lines[2])
        self.assertEqual(third["error"]["code"], -32700)

    def test_batch_request(self):
        payload = [
            {"jsonrpc": "2.0", "id": 10, "method": "ping"},
            {"jsonrpc": "2.0", "id": 11, "method": "tools/list"},
        ]
        stdin = io.StringIO(json.dumps(payload) + "\n")
        stdout = io.StringIO()
        serve_stdio(in_stream=stdin, out_stream=stdout)
        out_lines = stdout.getvalue().splitlines()
        self.assertEqual(len(out_lines), 2)
        ids = [json.loads(l)["id"] for l in out_lines]
        self.assertEqual(ids, [10, 11])


if __name__ == "__main__":
    unittest.main()
