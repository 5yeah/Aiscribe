"""连接器测试：用临时 SQLite 库验证读取、归一化与 < 系统事件过滤。"""

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from ai_report.connectors.cass import CassConnector
from ai_report.connectors.codebuddy_cn import CodeBuddyCNConnector
from ai_report.connectors.trae import TraeConnector
from ai_report.connectors.workbuddy import WorkBuddyConnector

# 2026-10-09 10:00 北京时间的 epoch 毫秒
TS_MS = 1791511200000


class TestCodeBuddy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "sessions.vscdb")
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
        rows = [
            ("session:aaa", json.dumps({
                "conversationId": "aaa", "cwd": "f:/work/demo-vision",
                "title": "视觉调试", "status": "completed",
                "createdAt": TS_MS, "updatedAt": TS_MS + 600000,
            })),
            ("session:bbb", json.dumps({
                "conversationId": "bbb", "cwd": "f:/other",
                "title": "坏数据", "status": "working",
                "createdAt": TS_MS, "updatedAt": TS_MS,
            })),
            ("other:key", json.dumps({"x": 1})),  # 非 session 键，应被 SQL 过滤
        ]
        conn.executemany("INSERT INTO ItemTable(key, value) VALUES (?, ?)", rows)
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_read(self):
        c = CodeBuddyCNConnector(db_path=self.db)
        self.assertTrue(c.available())
        recs = c.read()
        self.assertEqual(len(recs), 2)  # 非 session: 键被过滤
        by_id = {r.session_id: r for r in recs}
        self.assertEqual(by_id["aaa"].title, "视觉调试")
        self.assertEqual(by_id["aaa"].cwd, "f:/work/demo-vision")
        self.assertEqual(by_id["aaa"].summary, "视觉调试")
        self.assertEqual(by_id["aaa"].start_ts, TS_MS / 1000)

    def test_missing_db_returns_empty(self):
        c = CodeBuddyCNConnector(db_path=str(Path(self.tmp.name) / "nope.vscdb"))
        self.assertFalse(c.available())
        self.assertEqual(c.read(), [])


class TestWorkBuddy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "workbuddy.db")
        conn = sqlite3.connect(self.db)
        conn.execute(
            "CREATE TABLE sessions (id TEXT, title TEXT, created_at INTEGER,"
            " updated_at INTEGER, status TEXT, cwd TEXT)"
        )
        # cwd 是工作区目录（非项目目录），只能靠 title 分类
        conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?)",
            ("s1", "铜线周报整理", TS_MS, TS_MS, "completed",
             "2026-10-09-09-02-18"),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_read(self):
        c = WorkBuddyConnector(db_path=self.db)
        self.assertTrue(c.available())
        recs = c.read()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0].source, "WorkBuddy")
        self.assertEqual(recs[0].cwd, "2026-10-09-09-02-18")
        self.assertEqual(recs[0].start_ts, TS_MS / 1000)


class TestCass(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "agent_search.db")
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE conversations (id TEXT, title TEXT,"
                     " created_at INTEGER, updated_at INTEGER, workspace_id TEXT)")
        conn.execute("CREATE TABLE messages (id INTEGER, conversation_id TEXT,"
                     " role TEXT, content TEXT, created_at INTEGER)")
        conn.execute("CREATE TABLE workspaces (id TEXT, name TEXT, path TEXT)")
        conn.execute("INSERT INTO workspaces VALUES ('w1','ws','f:/work/terminal-detect')")
        rows = [
            ("c1", "端子检测联调", TS_MS, TS_MS, "w1"),
            ("c2", "<external_codex_apps_open_page>", TS_MS, TS_MS, "w1"),
            ("c3", "正常会话", TS_MS, TS_MS, "w1"),
        ]
        conn.executemany("INSERT INTO conversations VALUES (?,?,?,?,?)", rows)
        conn.execute("INSERT INTO messages VALUES (1,'c1','user','帮我调端子检测参数',?)",
                     (TS_MS,))
        conn.execute("INSERT INTO messages VALUES (2,'c1','assistant','好的',?)",
                     (TS_MS + 1000,))
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_filters_system_events(self):
        c = CassConnector(db_path=self.db)
        recs = c.read()
        self.assertEqual(len(recs), 2)  # "<" 开头的系统事件被过滤
        c1 = next(r for r in recs if r.session_id == "c1")
        self.assertEqual(c1.cwd, "f:/work/terminal-detect")
        # summary 取首条 user 消息
        self.assertEqual(c1.summary, "帮我调端子检测参数")
        self.assertNotIn("<", c1.title)


class TestTraeMemory(unittest.TestCase):
    """TRAE 连接器的 .trae-cn/memory 记忆目录读取。"""

    SID = "68e6f000abcdef1234567890"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mem = str(Path(self.tmp.name) / "projects")
        self.proj = Path(self.mem) / "-f-work-MyProj--p2-0011aabbccdd"
        (self.proj / "20261010").mkdir(parents=True)
        (self.proj / "20261009").mkdir(parents=True)
        lines = [
            {"intent": "修复检测算法的阈值问题",
             "outcome": "阈值已调整并验证通过",
             "message_summary_time": "2026-10-10 11:00:00"},
            {"intent": "继续调参",
             "outcome": "",
             "message_summary_time": "2026-10-10 11:30:00"},
        ]
        jl = self.proj / "20261010" / f"session_memory_{self.SID}.jsonl"
        jl.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines),
                      encoding="utf-8")
        # 同一会话在前一天也有副本，应被合并
        early = [{"intent": "修复检测算法的阈值问题",
                  "message_summary_time": "2026-10-09 20:00:00"}]
        jl2 = self.proj / "20261009" / f"session_memory_{self.SID}.jsonl"
        jl2.write_text(json.dumps(early[0], ensure_ascii=False), encoding="utf-8")
        (self.proj / "20261010" / "topics.md").write_text(
            f"[session_id: {self.SID} | topic_summary_time: 2026-10-10 14:10:50]"
            "阈值调优讨论\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_read_splits_by_date_and_uses_topic(self):
        c = TraeConnector(roots=[], memory_dir=self.mem)
        self.assertTrue(c.available())
        recs = c.read()
        # 每个日期目录一条：同一会话跨天不合并，避免昨日内容算进今天
        self.assertEqual(len(recs), 2)
        by_sid = {r.session_id: r for r in recs}
        later = by_sid[f"{self.SID}:20261010"]
        earlier = by_sid[f"{self.SID}:20261009"]
        self.assertEqual(later.source, "TRAE")
        self.assertEqual(later.cwd, r"f\work\MyProj")
        self.assertEqual(later.title, "修复检测算法的阈值问题")
        # summary 优先用 topics.md 的会话级摘要
        self.assertEqual(later.summary, "阈值调优讨论")
        self.assertEqual(later.status, "completed")
        self.assertEqual(later.raw["topic"], "阈值调优讨论")
        # 各天各取本目录内时间，不再跨日期合并
        self.assertEqual(
            datetime.fromtimestamp(later.start_ts).strftime("%Y-%m-%d %H:%M"),
            "2026-10-10 11:00")
        self.assertEqual(
            datetime.fromtimestamp(later.end_ts).strftime("%Y-%m-%d %H:%M"),
            "2026-10-10 11:30")
        self.assertEqual(
            datetime.fromtimestamp(earlier.start_ts).strftime("%Y-%m-%d %H:%M"),
            "2026-10-09 20:00")
        self.assertEqual(
            datetime.fromtimestamp(earlier.end_ts).strftime("%Y-%m-%d %H:%M"),
            "2026-10-09 20:00")

    def test_missing_memory_dir(self):
        c = TraeConnector(roots=[str(Path(self.tmp.name) / "nope")],
                          memory_dir=str(Path(self.tmp.name) / "nope2"))
        self.assertFalse(c.available())
        self.assertEqual(c.read(), [])


if __name__ == "__main__":
    unittest.main()
