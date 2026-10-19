"""WorkBuddy 读取器。

已探明事实：
- SQLite：sessions 表存会话元数据
- cwd 是 WorkBuddy 自身工作区目录（形如 2026-10-09-09-02-18），不是项目目录
  → 项目分类必须靠 title 关键词（在 L2 的 TITLE_HINTS 处理），不能靠 cwd
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import List

from ai_report.config import get_db_path
from ai_report.connectors.base import BaseConnector
from ai_report.models import UnifiedRecord
from ai_report.utils import log, parse_ts, safe_str

ENV_KEY = "WORKBUDDY_DB"


class WorkBuddyConnector(BaseConnector):
    name = "WorkBuddy"
    content_level = "仅标题"

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or get_db_path(ENV_KEY)

    def available(self) -> bool:
        return Path(self.db_path).is_file()

    def read(self) -> List[UnifiedRecord]:
        if not self.available():
            log(f"[{self.name}] 数据库不存在，跳过：{self.db_path}")
            return []

        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=3.0)
            try:
                # 先拿到列名，兼容不同版本的字段命名
                cols = {
                    row[1].lower()
                    for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
                }
                if not cols:
                    log(f"[{self.name}] sessions 表不存在，跳过")
                    return []
                rows, col_names = self._fetch_rows(conn, cols)
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as exc:
            log(f"[{self.name}] 打开数据库失败：{exc}")
            return []

        records: List[UnifiedRecord] = []
        for row in rows:
            data = dict(zip(col_names, row))
            session_id = safe_str(
                data.get("id") or data.get("session_id") or data.get("uuid")
            )
            if not session_id:
                continue
            title = safe_str(data.get("title")).strip() or "(无标题会话)"
            start_ts = parse_ts(data.get("createdat") or data.get("created_at"))
            end_ts = parse_ts(data.get("updatedat") or data.get("updated_at")) or start_ts

            records.append(
                UnifiedRecord(
                    source=self.name,
                    session_id=session_id,
                    title=title,
                    project="",  # 留空：cwd 不是项目目录，由 L2 用标题关键词分类
                    status=safe_str(data.get("status")),
                    cwd=safe_str(data.get("cwd")),
                    start_ts=start_ts,
                    end_ts=end_ts,
                    summary=title,
                    raw={"status": data.get("status")},
                )
            )

        log(f"[{self.name}] 读取到 {len(records)} 条会话元数据")
        return records

    @staticmethod
    def _fetch_rows(conn: sqlite3.Connection, cols: set):
        """按可用列做 SELECT，兼容列名下划线/无下划线差异。"""
        candidates = [
            ["id", "title", "created_at", "updated_at", "status", "cwd"],
            ["id", "title", "createdAt", "updatedAt", "status", "cwd"],
            ["session_id", "title", "created_at", "updated_at", "status", "cwd"],
        ]
        for want in candidates:
            usable = [c for c in want if c.lower() in cols]
            if "title" in {c.lower() for c in usable} and usable:
                sql = f"SELECT {', '.join(usable)} FROM sessions"
                cur = conn.execute(sql)
                return cur.fetchall(), [d[0] for d in cur.description]
        # 兜底：全部列
        cur = conn.execute("SELECT * FROM sessions")
        return cur.fetchall(), [d[0] for d in cur.description]
