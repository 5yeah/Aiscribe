"""Cass（coding-agent-search，索引 codex / copilot）读取器。

已探明事实：
- 表：conversations(id, title, created_at, updated_at, workspace_id, ...)
      messages(id, conversation_id, role, content, created_at, ...)
      workspaces(id, name, path)
- 时间戳 epoch 毫秒；messages 含本地正文 → summary 可取首条 user 消息
- 必须过滤标题以 "<" 开头的系统事件（如 <external_codex_apps_open_page>）
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import List

from ai_report.config import get_db_path
from ai_report.connectors.base import BaseConnector
from ai_report.models import UnifiedRecord
from ai_report.utils import log, parse_ts, safe_str

ENV_KEY = "CASS_DB"

# 摘要取首条 user 消息前 80 字
_SUMMARY_LEN = 80


class CassConnector(BaseConnector):
    name = "Cass"
    content_level = "首条正文"

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
                cols = {r[1] for r in conn.execute("PRAGMA table_info(conversations)")}
                # 真实库为 started_at/ended_at，旧版可能是 created_at/updated_at
                start_col = "started_at" if "started_at" in cols else "created_at"
                end_col = "ended_at" if "ended_at" in cols else "updated_at"
                rows = conn.execute(
                    f"""
                    SELECT c.id, c.title, c.{start_col}, c.{end_col},
                           w.path AS workspace_path,
                           (
                               SELECT m.content FROM messages m
                               WHERE m.conversation_id = c.id AND m.role = 'user'
                               ORDER BY m.created_at ASC LIMIT 1
                           ) AS first_user_msg
                    FROM conversations c
                    LEFT JOIN workspaces w ON w.id = c.workspace_id
                    """
                ).fetchall()
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as exc:
            log(f"[{self.name}] 打开数据库失败：{exc}")
            return []

        records: List[UnifiedRecord] = []
        for conv_id, title, started_at, ended_at, ws_path, first_msg in rows:
            title = safe_str(title).strip()
            # 过滤系统事件标题（形如 <external_codex_apps_open_page>）
            if not title or title.startswith("<"):
                continue

            start_ts = parse_ts(started_at)
            end_ts = parse_ts(ended_at) or start_ts
            summary = " ".join(safe_str(first_msg).split())[:_SUMMARY_LEN]

            records.append(
                UnifiedRecord(
                    source=self.name,
                    session_id=safe_str(conv_id),
                    title=title,
                    project="",  # 留空，L2 用 workspace.path 分类
                    status="",
                    cwd=safe_str(ws_path),
                    start_ts=start_ts,
                    end_ts=end_ts,
                    summary=summary or title,
                    raw={"workspace_path": safe_str(ws_path)},
                )
            )

        log(f"[{self.name}] 读取到 {len(records)} 条会话（已过滤系统事件标题）")
        return records
