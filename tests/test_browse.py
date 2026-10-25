"""会话浏览接口测试：筛选 / 搜索 / 分页 / 详情。

用假记录替换 index.collect()，避免依赖本机真实安装；再真起一个回环服务验证路由可达。
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from ai_report import ui
from ai_report.models import UnifiedRecord

# 2026-10-09 10:00 与 2026-10-10 11:00（北京时间）
TS1 = 1791511200.0
TS2 = TS1 + 86400 + 3600


def _records():
    return [
        UnifiedRecord(
            source="dsh", session_id="session-aaa", title="安装 Aiscribe 最新代码",
            project="Aiscribe", cwd=r"E:\DSH\Aiscribe", start_ts=TS2, end_ts=TS2 + 900,
            summary="10:00 帮我安装最新代码；10:10 再生成一份日报",
            raw={"messages": [
                {"ts": TS2, "text": "帮我安装最新代码"},
                {"ts": TS2 + 600, "text": "再生成一份日报"},
            ]},
        ),
        UnifiedRecord(
            source="TRAE", session_id="t1:20261009", title="端子检测联调",
            project="zhongchuan2.0", cwd=r"f:\work\zhongchuan2.0",
            start_ts=TS1, end_ts=TS1 + 1800, summary="端子检测阈值调优",
            raw={"topic": "端子检测阈值调优"},
        ),
        UnifiedRecord(
            source="WorkBuddy", session_id="w1", title="每日 AI 新闻推送",
            project="WorkBuddy", cwd="2026-10-09-09-02-18",
            start_ts=TS1 + 60, end_ts=TS1 + 120, summary="每日 AI 新闻推送",
            raw={},
        ),
    ]


class _Base(unittest.TestCase):
    def setUp(self):
        ui._BROWSE_CACHE["records"] = []
        ui._BROWSE_CACHE["ts"] = 0.0
        self._patch = mock.patch.object(ui.index, "collect", side_effect=_records)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        ui._BROWSE_CACHE["records"] = []
        ui._BROWSE_CACHE["ts"] = 0.0


class TestBrowse(_Base):
    def test_facets_and_order(self):
        data = ui.browse()
        self.assertEqual(data["total"], 3)
        # 按时间倒序：最近的 dsh 在最前
        self.assertEqual(data["sessions"][0]["id"], "dsh|session-aaa")
        sources = {s["name"]: s["count"] for s in data["facets"]["sources"]}
        self.assertEqual(sources, {"dsh": 1, "TRAE": 1, "WorkBuddy": 1})
        projects = {p["name"]: p["count"] for p in data["facets"]["projects"]}
        self.assertEqual(projects["Aiscribe"], 1)

    def test_filter_by_source_and_project(self):
        self.assertEqual(ui.browse(source="TRAE")["total"], 1)
        self.assertEqual(ui.browse(source="不存在")["total"], 0)
        self.assertEqual(ui.browse(project="zhongchuan2.0")["total"], 1)

    def test_search_hits_message_text(self):
        # 搜索命中「正文」而不只是标题
        data = ui.browse(q="再生成")
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["sessions"][0]["id"], "dsh|session-aaa")
        # 搜索命中 cwd / 项目名
        self.assertEqual(ui.browse(q="zhongchuan")["total"], 1)
        self.assertEqual(ui.browse(q="完全不存在的词")["total"], 0)

    def test_date_range(self):
        data = ui.browse(start_value="2026-10-09", end_value="2026-10-09")
        names = {s["source"] for s in data["sessions"]}
        self.assertEqual(names, {"TRAE", "WorkBuddy"})

    def test_pagination(self):
        first = ui.browse(limit=2, offset=0)
        self.assertEqual(len(first["sessions"]), 2)
        self.assertEqual(first["total"], 3)
        second = ui.browse(limit=2, offset=2)
        self.assertEqual(len(second["sessions"]), 1)
        self.assertEqual(first["sessions"][0]["id"], "dsh|session-aaa")
        self.assertEqual(second["sessions"][0]["id"], "TRAE|t1:20261009")

    def test_row_marks_missing_body(self):
        rows = {r["source"]: r for r in ui.browse()["sessions"]}
        self.assertTrue(rows["dsh"]["has_body"])
        self.assertEqual(rows["dsh"]["messages"], 2)
        self.assertFalse(rows["TRAE"]["has_body"])
        self.assertEqual(rows["TRAE"]["content_level"], "逐条原文")


class TestSessionDetail(_Base):
    def test_detail_returns_body(self):
        d = ui.session_detail("dsh|session-aaa")
        self.assertEqual(d["title"], "安装 Aiscribe 最新代码")
        self.assertEqual(len(d["body"]), 2)
        self.assertEqual(d["body"][0]["text"], "帮我安装最新代码")
        self.assertEqual(d["body"][0]["role"], "user")
        self.assertTrue(d["body"][0]["time"])          # 有 HH:MM
        self.assertEqual(d["note"], "")                # 有正文时不提示

    def test_detail_without_body_explains(self):
        d = ui.session_detail("TRAE|t1:20261009")
        self.assertEqual(d["body"], [])
        self.assertIn("只有标题", d["note"])

    def test_unknown_id_raises(self):
        with self.assertRaises(KeyError):
            ui.session_detail("nope|x")


class TestBrowseRoutes(_Base):
    """真起回环服务，确认 /browse 与 /api/browse 可达。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls._env = mock.patch.dict(
            os.environ, {"AIREREPORT_TRIMS": str(Path(cls._tmp.name) / "trims.json")})
        cls._env.start()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ui._Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._env.stop()
        cls._tmp.cleanup()

    def test_browse_page_served(self):
        with urllib.request.urlopen(self.base + "/browse", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            text = resp.read().decode("utf-8")
        self.assertIn("浏览历史会话", text)
        self.assertIn("/api/browse", text)

    def test_wizard_links_to_browse(self):
        """引导页要有进浏览页的入口，否则用户找不到。"""
        with urllib.request.urlopen(self.base + "/", timeout=5) as resp:
            text = resp.read().decode("utf-8")
        self.assertIn('href="/browse"', text)

    def test_browse_api(self):
        with urllib.request.urlopen(self.base + "/api/browse?limit=2", timeout=10) as resp:
            data = json.loads(resp.read())
        self.assertEqual(data["total"], 3)
        self.assertEqual(len(data["sessions"]), 2)
        self.assertIn("facets", data)

    def test_session_api(self):
        rid = "dsh%7Csession-aaa"
        with urllib.request.urlopen(self.base + "/api/session?id=" + rid, timeout=10) as resp:
            data = json.loads(resp.read())
        self.assertEqual(len(data["body"]), 2)


if __name__ == "__main__":
    unittest.main()
