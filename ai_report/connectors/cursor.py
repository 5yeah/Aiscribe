"""Cursor 读取器。

已探明事实：
- 数据在 `%APPDATA%/Cursor/User/globalStorage/state.vscdb`，表 `cursorDiskKV(key, value)`
- 两类键：
  * `composerData:<composerId>`：会话本身，JSON 含 name / createdAt / lastUpdatedAt /
    conversation(_Map) / workspaceIdentifier
  * `bubbleId:<composerId>:<bubbleId>`：单条消息，JSON 含 type（1=用户 2=助手）、
    text、richText、createdAt
- 正文以 `text` 为主；富文本对话里 text 可能为空，退到 `richText`（Lexical JSON）里抽
- 项目目录从 workspaceIdentifier.uri 的 fsPath / external(file:// URI) 推断
- 版本差异大，全部字段都按「有就用、没有就跳过」处理，解析失败不影响其它会话
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote

from ai_report.config import get_db_path
from ai_report.connectors.base import BaseConnector
from ai_report.connectors.common import group_by_day, timeline
from ai_report.models import UnifiedRecord
from ai_report.utils import log, parse_ts, safe_str, truncate

ENV_KEY = "CURSOR_DB"

_MSG_RAW_LIMIT = 2000
_SUMMARY_LIMIT = 400
_SNIPPET_LIMIT = 60

#: richText 里正文片段的键名（Lexical 节点用 text）
_RICH_KEYS = ("text", "content")


def _text_of(value) -> str:
    """从 text / richText 里抽出纯文本。"""
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.startswith("{"):
            try:
                return _text_of(json.loads(text))
            except json.JSONDecodeError:
                return text
        return text
    if isinstance(value, dict):
        parts: List[str] = []
        for key in ("root", "children", "content"):
            if key in value:
                got = _text_of(value[key])
                if got:
                    parts.append(got)
        if not parts:
            for key in _RICH_KEYS:
                got = _text_of(value.get(key))
                if got:
                    parts.append(got)
        return "\n".join(p for p in parts if p).strip()
    if isinstance(value, list):
        return "\n".join(filter(None, (_text_of(v) for v in value))).strip()
    return ""


def _cwd_of(composer: dict) -> str:
    """从 workspaceIdentifier 推断项目目录。"""
    ident = composer.get("workspaceIdentifier") or {}
    uri = ident.get("uri") or ident
    if isinstance(uri, dict):
        for key in ("fsPath", "path", "external"):
            raw = safe_str(uri.get(key)).strip()
            if not raw:
                continue
            if raw.startswith("file:///"):
                return unquote(raw[8:])
            if raw.startswith("file://"):
                return unquote(raw[7:])
            return unquote(raw)
    return ""


class CursorConnector(BaseConnector):
    name = "Cursor"
    content_level = "逐条原文"

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = Path(db_path or get_db_path(ENV_KEY))

    def available(self) -> bool:
        return self.db_path.is_file()

    def _connect(self) -> Optional[sqlite3.Connection]:
        try:
            return sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=3.0)
        except (sqlite3.Error, OSError):
            return None

    @staticmethod
    def _decode(value) -> Optional[dict]:
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        if not isinstance(value, str):
            return None
        try:
            data = json.loads(value)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def _composers(self, conn: sqlite3.Connection) -> List[Tuple[str, dict]]:
        out = []
        for key, value in conn.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
        ):
            data = self._decode(value)
            if data is not None:
                out.append((safe_str(key).split(":", 1)[-1], data))
        return out

    def _bubbles(self, conn: sqlite3.Connection, composer_id: str) -> List[dict]:
        rows = conn.execute(
            "SELECT value FROM cursorDiskKV WHERE key LIKE ?",
            (f"bubbleId:{composer_id}:%",),
        )
        out = []
        for (value,) in rows:
            data = self._decode(value)
            if data is not None:
                out.append(data)
        return out

    def _messages(self, conn: sqlite3.Connection, composer_id: str,
                  composer: dict) -> List[Tuple[float, str, str]]:
        """优先读 bubbles；老版本退到 composer.conversation。"""
        messages: List[Tuple[float, str, str]] = []
        for bubble in self._bubbles(conn, composer_id):
            kind = bubble.get("type")
            role = {1: "user", 2: "assistant"}.get(kind)
            if role is None:
                role = "user" if safe_str(bubble.get("role")) == "user" else "assistant"
            text = _text_of(bubble.get("text")) or _text_of(bubble.get("richText"))
            if not text:
                continue
            messages.append((parse_ts(bubble.get("createdAt")), role, text))
        if not messages:
            conversation = composer.get("conversation") or []
            if isinstance(conversation, list):
                for item in conversation:
                    if not isinstance(item, dict):
                        continue
                    role = {1: "user", 2: "assistant"}.get(item.get("type"))
                    if role is None:
                        continue
                    text = _text_of(item.get("text")) or _text_of(item.get("richText"))
                    if text:
                        messages.append((parse_ts(item.get("createdAt")), role, text))
        messages.sort(key=lambda x: x[0])
        return messages

    def read(self) -> List[UnifiedRecord]:
        if not self.available():
            log(f"[{self.name}] 数据库不存在，跳过：{self.db_path}")
            return []

        conn = self._connect()
        if conn is None:
            return []
        records: List[UnifiedRecord] = []
        try:
            for composer_id, composer in self._composers(conn):
                messages = self._messages(conn, composer_id, composer)
                users = [(ts, text) for ts, role, text in messages if role == "user"]
                if not users:
                    continue
                cwd = _cwd_of(composer)
                raw_title = safe_str(composer.get("name")).strip()
                created = parse_ts(composer.get("createdAt"))
                updated = parse_ts(composer.get("lastUpdatedAt")) or created

                base_raw: Dict[str, object] = {
                    "composer_id": composer_id,
                    "message_count": len(messages),
                }
                days = group_by_day(users)
                if len(days) == 1:
                    items = next(iter(days.values()))
                    records.append(UnifiedRecord(
                        source=self.name,
                        session_id=composer_id,
                        title=truncate(raw_title or items[0][1], 80),
                        cwd=cwd,
                        start_ts=min(ts for ts, _ in items) or created,
                        end_ts=max(ts for ts, _ in items) or updated,
                        summary=timeline(items, _SUMMARY_LIMIT, _SNIPPET_LIMIT),
                        raw=dict(base_raw, messages=[
                            {"ts": ts, "text": truncate(text, _MSG_RAW_LIMIT)}
                            for ts, text in items
                        ]),
                    ))
                    continue

                for day, items in sorted(days.items()):
                    records.append(UnifiedRecord(
                        source=self.name,
                        session_id=f"{composer_id}:{day}",
                        title=truncate(raw_title or items[0][1], 80),
                        cwd=cwd,
                        start_ts=min(ts for ts, _ in items),
                        end_ts=max(ts for ts, _ in items),
                        summary=timeline(items, _SUMMARY_LIMIT, _SNIPPET_LIMIT),
                        raw=dict(base_raw, day=day, messages=[
                            {"ts": ts, "text": truncate(text, _MSG_RAW_LIMIT)}
                            for ts, text in items
                        ]),
                    ))
        except sqlite3.Error as exc:
            log(f"[{self.name}] 读取失败：{exc}")
        finally:
            conn.close()

        records.sort(key=lambda r: r.start_ts)
        log(f"[{self.name}] 读取到 {len(records)} 条记录")
        return records
