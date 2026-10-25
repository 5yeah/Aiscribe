"""原生连接器测试：Codex / dsh / Claude Code / Gemini CLI / Copilot / Cline / Cursor。

全部用临时目录构造最小样本，不依赖真实安装，也不需要 zstd（dsh 用未压缩的
`.jsonl` 变体覆盖同一段解析逻辑）。
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ai_report.connectors.claude_code import ClaudeCodeConnector
from ai_report.connectors.cline import ClineConnector
from ai_report.connectors.codex import CodexConnector
from ai_report.connectors.cursor import CursorConnector
from ai_report.connectors.dsh import DshConnector
from ai_report.connectors.gemini_cli import GeminiCliConnector
from ai_report.connectors.vscode_chat import VSCodeChatConnector

# 2026-10-09 10:00 北京时间
TS_MS = 1791511200000
TS_S = TS_MS / 1000
DAY1 = "2026-10-09T02:00:00.000Z"   # UTC -> 北京 10:00


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()


class TestCodex(_Tmp):
    """sqlite 元数据 + 投影历史，注入上下文必须被剔除，跨天要按天切分。"""

    def setUp(self):
        super().setUp()
        db = self.root / "state_5.sqlite"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE threads (id TEXT, rollout_path TEXT, created_at INTEGER,"
            " updated_at INTEGER, cwd TEXT, title TEXT, first_user_message TEXT,"
            " model TEXT, source TEXT, git_branch TEXT, tokens_used INTEGER, archived INTEGER)"
        )
        conn.execute(
            "INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("t1", "", int(TS_S), int(TS_S) + 3600, r"\\?\E:\DSH\Aiscribe",
             "安装 Aiscribe", "帮我安装最新代码", "gpt-5.6", "vscode", "main", 1234, 0),
        )
        conn.commit()
        conn.close()

        hist = self.root / "thread_history_1.sqlite"
        conn = sqlite3.connect(hist)
        conn.execute(
            "CREATE TABLE thread_items (thread_id TEXT, turn_id TEXT, item_id TEXT,"
            " rollout_ordinal INTEGER, created_at_ms INTEGER, item_json TEXT,"
            " item_type TEXT, updated_at_ordinal INTEGER DEFAULT 0)"
        )
        rows = [
            ("item-1", 1, TS_MS,
             {"type": "userMessage", "content": [{"type": "text", "text": "帮我安装最新代码"}]},
             "userMessage"),
            ("item-2", 2, TS_MS + 60000,
             {"type": "agentMessage", "content": [{"type": "text", "text": "好的"}]},
             "agentMessage"),
            # 注入的运行时上下文：role 也是 user，但必须剔除
            ("item-3", 3, TS_MS + 120000,
             {"type": "userMessage", "content": [
                 {"type": "text", "text": "<environment_context>\n<cwd>E:\\x</cwd>"}]},
             "userMessage"),
            # 第二天又来一条真实输入 -> 应切成第二条记录
            ("item-4", 4, TS_MS + 86400000,
             {"type": "userMessage", "content": [{"type": "text", "text": "再生成一份日报"}]},
             "userMessage"),
        ]
        conn.executemany(
            "INSERT INTO thread_items (thread_id, turn_id, item_id, rollout_ordinal,"
            " created_at_ms, item_json, item_type) VALUES ('t1','turn1',?,?,?,?,?)",
            [(item_id, ordinal, ts, json.dumps(payload, ensure_ascii=False), kind)
             for item_id, ordinal, ts, payload, kind in rows],
        )
        conn.commit()
        conn.close()

    def test_reads_history_and_filters_injected(self):
        c = CodexConnector(root=str(self.root))
        self.assertTrue(c.available())
        recs = c.read()
        self.assertEqual(len(recs), 2)  # 两个自然日
        by_day = {r.session_id: r for r in recs}
        first = by_day["t1:2026-10-09"]
        self.assertEqual(first.source, "Codex")
        self.assertEqual(first.cwd, r"E:\DSH\Aiscribe")  # 去掉 \\?\ 前缀
        self.assertEqual(first.title, "安装 Aiscribe")
        self.assertIn("帮我安装最新代码", first.summary)
        self.assertNotIn("environment_context", first.summary)
        self.assertEqual(first.raw["messages"][0]["text"], "帮我安装最新代码")
        # 第二天是独立记录
        self.assertTrue(by_day["t1:2026-10-10"].summary.startswith("10:00"))


class TestDsh(_Tmp):
    """dsh 事件流：source.kind=user 才是真人输入，标题取最后一条 session/title。"""

    def setUp(self):
        super().setUp()
        sess = self.root / "--E-DSH--" / "session-abc123"
        sess.mkdir(parents=True)
        events = [
            {"type": "session", "version": 4, "id": "session-abc123",
             "createdAt": TS_MS, "cwd": r"E:\DSH\Aiscribe"},
            {"type": "user/message", "time": TS_MS,
             "data": {"content": [{"type": "text", "text": "帮我整理日报"}],
                      "source": {"kind": "user"}}},
            {"type": "user/message", "time": TS_MS + 1000,
             "data": {"content": [{"type": "text", "text": "Current runtime context: ..."}],
                      "source": {"kind": "runtime-context"}}},
            {"type": "assistant/message", "time": TS_MS + 2000,
             "data": {"message": {"role": "assistant", "content": [
                 {"type": "reasoning", "text": "思考中"},
                 {"type": "text", "text": "好的，这就整理"}]}}},
            {"type": "session/title", "time": TS_MS + 3000,
             "data": {"title": "整理日报"}},
        ]
        (sess / "session.v4.jsonl").write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8")

    def test_reads_events(self):
        c = DshConnector(root=str(self.root))
        self.assertTrue(c.available())
        recs = c.read()
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec.source, "dsh")
        self.assertEqual(rec.title, "整理日报")
        self.assertEqual(rec.cwd, r"E:\DSH\Aiscribe")
        self.assertIn("帮我整理日报", rec.summary)
        self.assertNotIn("Current runtime context", rec.summary)
        self.assertEqual(len(rec.raw["messages"]), 1)  # 注入消息被剔除


class TestClaudeCode(_Tmp):
    def setUp(self):
        super().setUp()
        proj = self.root / "projects" / "-E-DSH-Aiscribe"
        proj.mkdir(parents=True)
        lines = [
            {"type": "user", "sessionId": "s1", "cwd": r"E:\DSH\Aiscribe",
             "gitBranch": "main", "timestamp": DAY1,
             "message": {"role": "user", "content": "帮我加个连接器"}},
            {"type": "assistant", "sessionId": "s1", "cwd": r"E:\DSH\Aiscribe",
             "timestamp": "2026-10-09T02:00:05.000Z",
             "message": {"role": "assistant", "content": [
                 {"type": "thinking", "thinking": "内部思考"},
                 {"type": "text", "text": "好的，先看格式"}]}},
            # 子代理对话：必须跳过
            {"type": "user", "sessionId": "s1", "isSidechain": True, "timestamp": DAY1,
             "message": {"role": "user", "content": "子代理的输入"}},
            # 本地命令噪音：必须跳过
            {"type": "user", "sessionId": "s1", "timestamp": DAY1,
             "message": {"role": "user", "content": "<command-name>/model</command-name>"}},
        ]
        (proj / "s1.jsonl").write_text(
            "\n".join(json.dumps(x, ensure_ascii=False) for x in lines), encoding="utf-8")

    def test_reads_and_skips_noise(self):
        c = ClaudeCodeConnector(root=str(self.root))
        self.assertTrue(c.available())
        recs = c.read()
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec.title, "帮我加个连接器")
        self.assertEqual(rec.cwd, r"E:\DSH\Aiscribe")
        self.assertEqual(rec.raw["git_branch"], "main")
        self.assertNotIn("子代理", rec.summary)
        self.assertNotIn("command-name", rec.summary)


class TestGeminiCli(_Tmp):
    def setUp(self):
        super().setUp()
        chat = self.root / "tmp" / "abc123" / "chats"
        chat.mkdir(parents=True)
        data = {
            "sessionId": "g1",
            "startTime": DAY1,
            "lastUpdated": "2026-10-09T02:30:00.000Z",
            "messages": [
                {"type": "user", "timestamp": DAY1,
                 "content": [{"text": "解释一下这个函数"}]},
                {"type": "gemini", "timestamp": "2026-10-09T02:05:00.000Z",
                 "content": [{"text": "它做的是..."}]},
                {"type": "info", "timestamp": DAY1, "content": [{"text": "运行日志"}]},
            ],
        }
        (chat / "session-2026-10-09T02-00-g1.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_reads_chat_file(self):
        c = GeminiCliConnector(root=str(self.root))
        self.assertTrue(c.available())
        recs = c.read()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].title, "解释一下这个函数")
        self.assertEqual(recs[0].session_id, "g1")
        self.assertNotIn("运行日志", recs[0].summary)


class TestVSCodeChat(_Tmp):
    def setUp(self):
        super().setUp()
        work = self.root / "workspaceStorage" / "w1"
        (work / "chatSessions").mkdir(parents=True)
        (work / "workspace.json").write_text(
            json.dumps({"folder": "file:///e%3A/DSH/Aiscribe"}), encoding="utf-8")
        session = {"kind": 0, "v": {"version": 3, "creationDate": TS_MS,
                                    "sessionId": "c1", "requests": [
                                        {"message": {"text": "帮我看看报表模板"},
                                         "timestamp": TS_MS}]}}
        (work / "chatSessions" / "c1.jsonl").write_text(
            json.dumps(session, ensure_ascii=False), encoding="utf-8")
        # 空会话：应被丢弃
        (work / "chatSessions" / "c2.jsonl").write_text(
            json.dumps({"kind": 0, "v": {"requests": []}}), encoding="utf-8")

    def test_reads_workspace_chat(self):
        c = VSCodeChatConnector(roots=[str(self.root)])
        self.assertTrue(c.available())
        recs = c.read()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].title, "帮我看看报表模板")
        self.assertEqual(recs[0].cwd, "e:/DSH/Aiscribe")


class TestCline(_Tmp):
    def setUp(self):
        super().setUp()
        task = (self.root / "Code" / "User" / "globalStorage" /
                "saoudrizwan.claude-dev" / "tasks" / "task1")
        task.mkdir(parents=True)
        history = [
            {"role": "user", "ts": TS_MS, "content": [
                {"type": "text", "text": "把这个脚本改成异步"}]},
            {"role": "assistant", "ts": TS_MS + 1000, "content": [
                {"type": "tool_use", "name": "read_file", "input": {}},
                {"type": "text", "text": "改好了"}]},
            {"role": "user", "ts": TS_MS + 2000, "content": [
                {"type": "tool_result", "content": "文件内容"}]},
        ]
        (task / "api_conversation_history.json").write_text(
            json.dumps(history, ensure_ascii=False), encoding="utf-8")
        (task / "task_metadata.json").write_text(
            json.dumps({"cwd": r"E:\DSH\Aiscribe"}), encoding="utf-8")

    def test_reads_task_history(self):
        c = ClineConnector(bases=[str(self.root)])
        self.assertTrue(c.available())
        recs = c.read()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].source, "Cline")
        self.assertEqual(recs[0].title, "把这个脚本改成异步")
        self.assertEqual(recs[0].cwd, r"E:\DSH\Aiscribe")
        self.assertEqual(len(recs[0].raw["messages"]), 1)  # tool_result 不算正文


class TestCursor(_Tmp):
    def setUp(self):
        super().setUp()
        db = self.root / "state.vscdb"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)")
        composer = {"composerId": "c1", "name": "报表重构", "createdAt": TS_MS,
                    "lastUpdatedAt": TS_MS + 60000,
                    "workspaceIdentifier": {"uri": {"fsPath": r"E:\DSH\Aiscribe"}}}
        conn.executemany("INSERT INTO cursorDiskKV VALUES (?,?)", [
            ("composerData:c1", json.dumps(composer, ensure_ascii=False)),
            ("bubbleId:c1:b1", json.dumps(
                {"type": 1, "text": "把日报模板换掉", "createdAt": TS_MS},
                ensure_ascii=False)),
            ("bubbleId:c1:b2", json.dumps(
                {"type": 2, "text": "已换好", "createdAt": TS_MS + 1000},
                ensure_ascii=False)),
        ])
        conn.commit()
        conn.close()

    def test_reads_composer(self):
        c = CursorConnector(db_path=str(self.root / "state.vscdb"))
        self.assertTrue(c.available())
        recs = c.read()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].title, "报表重构")
        self.assertEqual(recs[0].cwd, r"E:\DSH\Aiscribe")
        self.assertIn("把日报模板换掉", recs[0].summary)


if __name__ == "__main__":
    unittest.main()
