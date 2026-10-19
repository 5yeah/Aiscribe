"""引导页测试：共享取数/素材负载、指令生成、时间剪辑路由、HTTP 路由冒烟。"""

import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from ai_report import index, trim, ui
from ai_report.models import UnifiedRecord


def _rec(source="CodeBuddy CN", session_id="cid:2026-10-16", title="新版部署确认"):
    return UnifiedRecord(
        source=source, session_id=session_id, title=title, project="",
        status="completed", cwd="D:/project/demo",
        start_ts=1792100000.0, end_ts=1792103600.0,
        summary=title, raw={"messages": [{"ts": 1792100000.0, "text": "请覆盖部署"}]},
    )


def _passthrough_llm():
    """把 ui.llm.ReportSynthesizer 换成不联网的桩：原样返回骨架（等于没改写）。

    本机可能真的配好了 llm.json，不打桩就会在单测里联网调 DeepSeek。
    """
    inst = mock.Mock()
    inst.rewrite.side_effect = lambda outline: outline
    inst.available = False
    inst.last_error = ""
    inst.provider, inst.model = "deepseek", "deepseek-chat"
    return mock.patch.object(ui.llm, "ReportSynthesizer", return_value=inst)


def _rewriting_llm(title: str, body: str):
    """归纳桩：结构原样保留，只把每条的文字换掉（模拟「AI 只改文字」）。"""
    def _rewrite(outline):
        return {"sections": [
            {"name": sec["name"],
             "items": [{"title": title, "body": body} for _ in (sec.get("items") or [])]}
            for sec in (outline.get("sections") or [])
        ]}
    return _rewrite


class TestRecordsPayload(unittest.TestCase):
    def test_payload_has_stable_id_and_summary(self):
        payload = index.records_payload([_rec()], detail="summary")
        self.assertEqual(payload["total_records"], 1)
        entry = payload["projects"][0]["records"][0]
        self.assertEqual(entry["id"], "CodeBuddy CN|cid:2026-10-16")
        self.assertIn("summary", entry)
        self.assertNotIn("messages", entry)

    def test_payload_full_detail_keeps_messages(self):
        entry = index.records_payload([_rec()], detail="full")["projects"][0]["records"][0]
        self.assertEqual(entry["messages"][0]["text"], "请覆盖部署")

    def test_payload_filters_by_ids(self):
        keep = index.record_id(_rec())
        payload = index.records_payload([_rec(), _rec(session_id="other:2026-10-16")],
                                        ids=[keep])
        self.assertEqual(payload["total_records"], 1)
        self.assertEqual(payload["projects"][0]["records"][0]["id"], keep)

    def test_payload_empty_ids_means_all(self):
        payload = index.records_payload([_rec()], ids=[])
        self.assertEqual(payload["total_records"], 1)


class TestInstruction(unittest.TestCase):
    def test_instruction_carries_ids_and_targets(self):
        text = ui.build_instruction(["CodeBuddy CN|a:2026-10-16"],
                                    mode="daily", date_value="2026-10-16", fmt="docx")
        self.assertIn("get_conversations", text)
        self.assertIn("CodeBuddy CN|a:2026-10-16", text)
        self.assertIn('format: "docx"', text)
        self.assertIn("日报_2026-10-16", text)

    def test_instruction_weekly_keeps_single_date(self):
        text = ui.build_instruction(["a"], mode="weekly", date_value="2026-10-16")
        self.assertIn('get_conversations(mode="weekly", date="2026-10-16"', text)
        self.assertIn("周报_2026-10-16", text)
        self.assertIn("2026-10-12 ~ 2026-10-18", text)

    def test_instruction_range_uses_start_and_end(self):
        text = ui.build_instruction(["a"], mode="range",
                                    start="2026-10-01", end="2026-10-07", fmt="html")
        self.assertIn('get_conversations(mode="range", start="2026-10-01", '
                      'end="2026-10-07"', text)
        self.assertIn('format: "html"', text)
        self.assertIn("AI报告_2026-10-01_2026-10-07", text)
        # CLI 兜底也走 range，不能退化成 daily --date
        self.assertIn("ai_report range --start 2026-10-01 --end 2026-10-07", text)


class TestUiRoutes(unittest.TestCase):
    """真起一个回环 HTTP 服务，验证路由与页面可达（端口用 0 由系统分配）。"""

    @classmethod
    def setUpClass(cls):
        # 剪辑落盘改到临时文件，避免污染仓库里的 trims.json
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

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as resp:
            return resp.status, resp.read()

    def _post(self, path, payload):
        req = urllib.request.Request(
            self.base + path, method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())

    def test_index_page_served(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        # 六屏向导：启动屏 + 四个步骤屏 + 生成等待页
        self.assertIn("选择要读的 AI 工具", text)
        self.assertIn("定下报告的时间窗", text)
        self.assertIn("生成", text)

    def test_sources_lists_registry(self):
        status, body = self._get("/api/sources")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(any(item["name"] == "WorkBuddy" for item in data))
        self.assertTrue(all("content_level" in item for item in data))

    def test_unknown_source_reports_400(self):
        try:
            self._get("/api/source?name=%E4%B8%8D%E5%AD%98%E5%9C%A8")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
        else:  # pragma: no cover - 正常不会走到这里
            self.fail("未注册的数据源应返回 400")

    def test_instruction_requires_ids(self):
        req = urllib.request.Request(
            self.base + "/api/instruction", method="POST",
            data=json.dumps({"ids": []}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
        else:  # pragma: no cover
            self.fail("空 ids 应返回 400")

    def test_instruction_post_returns_text(self):
        req = urllib.request.Request(
            self.base + "/api/instruction", method="POST",
            data=json.dumps({"ids": ["CodeBuddy CN|a:2026-10-16"],
                             "mode": "daily", "date": "2026-10-16"}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        self.assertIn("save_report", data["text"])

    # ---- 时间剪辑 ----

    def test_page_exposes_trim_ui(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn("时间剪辑", text)
        self.assertIn("/api/trim", text)

    def test_page_exposes_custom_range_mode(self):
        """引导页要能在「自定义时间」下用 从/到 两个日期。"""
        _, body = self._get("/")
        text = body.decode("utf-8")
        self.assertIn("自定义时间", text)
        self.assertIn("mode === 'range'", text)
        self.assertIn("从", text)
        self.assertIn("到", text)

    def test_conversations_range_route(self):
        status, body = self._get(
            "/api/conversations?mode=range&start=2026-10-01&end=2026-10-07")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["range"],
                         {"start": "2026-10-01", "end": "2026-10-07"})

    def test_instruction_mentions_trim(self):
        rid = "CodeBuddy CN|a:2026-10-16"
        trim.set_trim(rid, "09:00", "12:00")
        self.addCleanup(trim.clear_trim, rid)
        data = self._post("/api/instruction", {"ids": [rid], "mode": "daily",
                                               "date": "2026-10-16"})
        self.assertIn("时间剪辑", data["text"])

    def test_trim_sets_then_clears_one_record(self):
        rid = "CodeBuddy CN|a:2026-10-16"
        data = self._post("/api/trim", {"id": rid, "start": "9:00", "end": "12:00"})
        self.assertEqual(data["trim"], {"start": "09:00", "end": "12:00"})
        self.assertEqual(data["count"], 1)
        cleared = self._post("/api/trim", {"id": rid, "start": "", "end": ""})
        self.assertIsNone(cleared["trim"])
        self.assertEqual(cleared["count"], 0)

    def test_trim_clear_all(self):
        self._post("/api/trim", {"id": "a", "start": "09:00", "end": "10:00"})
        data = self._post("/api/trim", {"clear": True})
        self.assertEqual(data["count"], 0)

    def test_trim_requires_id(self):
        req = urllib.request.Request(
            self.base + "/api/trim", method="POST",
            data=json.dumps({"start": "09:00"}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
        else:  # pragma: no cover
            self.fail("缺 id 应返回 400")

    def test_trim_invalid_time_reports_400(self):
        req = urllib.request.Request(
            self.base + "/api/trim", method="POST",
            data=json.dumps({"id": "a", "start": "25:00"}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
        else:  # pragma: no cover
            self.fail("非法时间应返回 400")


class TestGenerateReport(unittest.TestCase):
    """等待页「直接生成」：按勾选过滤、剪辑生效、落盘到 reports/ 并能取回。

    落盘目录改到临时目录（config.REPORTS_DIR 是导入期读环境变量的模块常量，
    所以要直接打补丁），取数用假记录替换，避免依赖机器上真实存在哪些会话。
    归纳器一律打桩：本机若配了 llm.json，不打桩就会真的联网调 DeepSeek，测试既慢又不确定。
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        base = Path(cls._tmp.name)
        cls.reports = base / "reports"
        cls._env = mock.patch.dict(
            os.environ, {"AIREREPORT_TRIMS": str(base / "trims.json")})
        cls._env.start()
        cls._dir = mock.patch.object(ui.config, "REPORTS_DIR", cls.reports)
        cls._dir.start()
        # 两条素材：一条带时间戳（能被剪辑剪空），一条不带
        cls.timed = _rec()
        cls.timed.summary = "09:30 写部署脚本；21:40 联调补丁"
        cls.other = _rec(source="TRAE", session_id="t:2026-10-16", title="别的活")
        cls._sel = mock.patch.object(
            ui.index, "select_records",
            side_effect=lambda mode="daily", date_value=None,
            start_value=None, end_value=None: [cls.timed, cls.other])
        cls._sel.start()
        cls._llm = _passthrough_llm()
        cls._llm.start()

    @classmethod
    def tearDownClass(cls):
        cls._llm.stop()
        cls._sel.stop()
        cls._dir.stop()
        cls._env.stop()
        cls._tmp.cleanup()

    def test_empty_ids_rejected(self):
        with self.assertRaises(ValueError):
            ui.generate_report([])

    def test_only_checked_records_land_in_report(self):
        data = ui.generate_report(
            ["CodeBuddy CN|cid:2026-10-16"], mode="daily", date_value="2026-10-16")
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["projects"], 1)
        self.assertIn("新版部署确认", data["text"])
        self.assertNotIn("别的活", data["text"])
        path = Path(data["path"])
        self.assertTrue(path.is_file())
        self.assertEqual(path.parent, self.reports)
        self.assertEqual(data["name"], "AI日报_2026-10-16.md")
        self.assertEqual(path.read_text("utf-8").strip(), data["text"].strip())

    def test_trim_empties_a_record_out_of_report(self):
        rid = "CodeBuddy CN|cid:2026-10-16"
        trim.set_trim(rid, "03:00", "04:00")   # 两段都在窗口外 → 剪空
        self.addCleanup(trim.clear_trim, rid)
        data = ui.generate_report([rid, "TRAE|t:2026-10-16"],
                                  mode="daily", date_value="2026-10-16")
        self.assertEqual(data["total"], 1)
        self.assertNotIn("新版部署确认", data["text"])
        self.assertIn("别的活", data["text"])

    def test_weekly_and_range_names(self):
        ids = ["CodeBuddy CN|cid:2026-10-16", "TRAE|t:2026-10-16"]
        weekly = ui.generate_report(ids, mode="weekly", date_value="2026-10-16")
        self.assertEqual(weekly["name"], "AI周报_2026-10-16.md")
        self.assertIn("AI 周报", weekly["text"])
        rng = ui.generate_report(ids, mode="range",
                                 start="2026-10-01", end="2026-10-31")
        self.assertEqual(rng["name"], "AI报告_2026-10-01_2026-10-31.md")

    def test_range_requires_both_ends(self):
        with self.assertRaises(ValueError):
            ui.generate_report(["a"], mode="range", start="2026-10-01")

    def test_html_format_lands_as_html(self):
        data = ui.generate_report(["TRAE|t:2026-10-16"], mode="daily",
                                  date_value="2026-10-16", fmt="html")
        self.assertEqual(data["format"], "html")
        self.assertTrue(data["name"].endswith(".html"))
        self.assertTrue(Path(data["path"]).is_file())


class TestGenerateReportPolish(unittest.TestCase):
    """「生成报告」的归纳环节：接了 LLM 就归纳，没接就如实退回草稿并说明原因。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        base = Path(cls._tmp.name)
        cls._env = mock.patch.dict(
            os.environ, {"AIREREPORT_TRIMS": str(base / "trims.json")})
        cls._env.start()
        cls._dir = mock.patch.object(ui.config, "REPORTS_DIR", base / "reports")
        cls._dir.start()
        cls._sel = mock.patch.object(
            ui.index, "select_records",
            side_effect=lambda mode="daily", date_value=None,
            start_value=None, end_value=None: [_rec()])
        cls._sel.start()
        cls._llm = _passthrough_llm()
        cls._llm.start()

    @classmethod
    def tearDownClass(cls):
        cls._llm.stop()
        cls._sel.stop()
        cls._dir.stop()
        cls._env.stop()
        cls._tmp.cleanup()

    def test_polish_uses_rewritten_slots_and_marks_polished(self):
        """归纳只改文字：项目行/编号仍由本机 generator 排版，文件与返回值一致。"""
        with mock.patch.object(ui.llm, "ReportSynthesizer") as cls:
            inst = cls.return_value
            inst.rewrite.side_effect = _rewriting_llm("归纳后的事项", "归纳后的正文")
            inst.available = True
            inst.last_error = ""
            inst.provider, inst.model = "deepseek", "deepseek-chat"
            data = ui.generate_report(["CodeBuddy CN|cid:2026-10-16"],
                                      mode="daily", date_value="2026-10-16")
        inst.rewrite.assert_called_once()
        self.assertTrue(data["polished"])
        self.assertEqual(data["llm_error"], "")
        self.assertIn("归纳后的事项", data["text"])
        self.assertIn("归纳后的正文", data["text"])
        self.assertNotIn("新版部署确认", data["text"])   # 原文字被替换掉
        self.assertEqual(Path(data["path"]).read_text("utf-8").strip(),
                         data["text"].strip())

    def test_broken_structure_falls_back_to_template_text(self):
        """AI 改了结构（增删条目）→ 校验不通过 → 如实退回模板草稿并说明原因。"""
        def _dropping(outline):
            return {"sections": [{"name": s["name"], "items": []}
                                 for s in (outline.get("sections") or [])]}

        with mock.patch.object(ui.llm, "ReportSynthesizer") as cls:
            inst = cls.return_value
            inst.rewrite.side_effect = _dropping
            inst.available = True
            inst.last_error = ""
            inst.provider, inst.model = "deepseek", "deepseek-chat"
            data = ui.generate_report(["CodeBuddy CN|cid:2026-10-16"],
                                      mode="daily", date_value="2026-10-16")
        self.assertFalse(data["polished"])
        self.assertIn("格式校验", data["llm_error"])
        self.assertIn("新版部署确认", data["text"])

    def test_no_key_falls_back_and_reports_why(self):
        with mock.patch.object(ui.llm, "ReportSynthesizer") as cls:
            inst = cls.return_value
            inst.rewrite.return_value = None
            inst.available = False
            inst.last_error = "未配置 DEEPSEEK_API_KEY"
            inst.provider, inst.model = "deepseek", "deepseek-chat"
            data = ui.generate_report(["CodeBuddy CN|cid:2026-10-16"],
                                      mode="daily", date_value="2026-10-16")
        self.assertFalse(data["polished"])
        self.assertEqual(data["llm_error"], "未配置 DEEPSEEK_API_KEY")
        self.assertIn("新版部署确认", data["text"])

    def test_polish_false_skips_the_llm(self):
        data = ui.generate_report(["CodeBuddy CN|cid:2026-10-16"], mode="daily",
                                  date_value="2026-10-16", polish=False)
        self.assertFalse(data["polished"])
        self.assertEqual(data["llm_error"], "")


class TestGenerateRoutes(unittest.TestCase):
    """「直接生成」的 HTTP 链路：POST /api/generate + GET /api/report 取回成品。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        base = Path(cls._tmp.name)
        cls._env = mock.patch.dict(
            os.environ, {"AIREREPORT_TRIMS": str(base / "trims.json")})
        cls._env.start()
        cls._dir = mock.patch.object(ui.config, "REPORTS_DIR", base / "reports")
        cls._dir.start()
        cls._sel = mock.patch.object(
            ui.index, "select_records",
            side_effect=lambda mode="daily", date_value=None,
            start_value=None, end_value=None: [
                _rec(), _rec(source="TRAE", session_id="t:2026-10-16", title="别的活")])
        cls._sel.start()
        cls._llm = _passthrough_llm()
        cls._llm.start()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ui._Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._llm.stop()
        cls._sel.stop()
        cls._dir.stop()
        cls._env.stop()
        cls._tmp.cleanup()

    def test_generate_route_honours_polish_flag(self):
        """POST /api/generate 带 polish=true 时，确实把骨架的槽位交给归纳器。"""
        with mock.patch.object(ui.llm, "ReportSynthesizer") as cls:
            inst = cls.return_value
            inst.rewrite.side_effect = _rewriting_llm("归纳过的条目", "归纳过的正文")
            inst.available = True
            inst.last_error = ""
            inst.provider, inst.model = "deepseek", "deepseek-chat"
            data = self._post("/api/generate", {
                "ids": ["CodeBuddy CN|cid:2026-10-16"], "mode": "daily",
                "date": "2026-10-16", "format": "md", "polish": True})
        inst.rewrite.assert_called_once()
        self.assertTrue(data["polished"])
        self.assertIn("归纳过的条目", data["text"])

    def _post(self, path, payload):
        req = urllib.request.Request(
            self.base + path, method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())

    def test_page_exposes_generate_ui(self):
        with urllib.request.urlopen(self.base + "/", timeout=5) as resp:
            text = resp.read().decode("utf-8")
        self.assertIn("生成报告", text)
        self.assertIn("/api/generate", text)
        self.assertIn("AI 归纳未启用", text)
        self.assertIn("/api/llm", text)

    def test_llm_status_route(self):
        fake = {"provider": "deepseek", "model": "deepseek-chat",
                "ready": False, "missing": "未配置 DEEPSEEK_API_KEY",
                "config_file": ""}
        with mock.patch.object(ui.llm, "status", return_value=fake):
            with urllib.request.urlopen(self.base + "/api/llm", timeout=5) as resp:
                self.assertEqual(json.loads(resp.read()), fake)

    def test_generate_requires_ids(self):
        req = urllib.request.Request(
            self.base + "/api/generate", method="POST",
            data=json.dumps({"ids": []}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
            self.assertIn("ids", json.loads(exc.read())["error"])
        else:  # pragma: no cover
            self.fail("空 ids 应返回 400")

    def test_generate_then_download(self):
        data = self._post("/api/generate", {
            "ids": ["CodeBuddy CN|cid:2026-10-16"], "mode": "daily",
            "date": "2026-10-16", "format": "md", "polish": False})
        self.assertEqual(data["total"], 1)
        self.assertTrue(data["url"].startswith("/api/report?name="))
        with urllib.request.urlopen(self.base + data["url"], timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/markdown", resp.headers["Content-Type"])
            self.assertIn("AI 日报", resp.read().decode("utf-8"))

    def test_report_route_rejects_path_traversal(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(
                self.base + "/api/report?name=..%2F..%2Fconfig.py", timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_report_route_404_for_missing(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.base + "/api/report?name=nope.md", timeout=5)
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
