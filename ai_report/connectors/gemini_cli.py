"""Gemini CLI 及其分支（Qwen Code / iFlow）读取器。

已探明事实（Gemini CLI 及其 fork 共用同一套本地格式）：
- 会话在 `~/.gemini/tmp/<项目哈希>/chats/session-<时间>-<id>.json`
- 单文件一个会话：
  {sessionId, startTime, lastUpdated,
   messages: [{id, timestamp, type: user|gemini|info|error, content: [{text}]}]}
- fork 关系：Qwen Code 用 `~/.qwen`，iFlow 用 `~/.iflow`，目录结构与字段一致，
  因此用同一个解析器、三个注册项（各带自己的环境变量与显示名）
- content 在不同版本里可能是字符串或块数组，统一走 common.content_text
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Tuple

from ai_report.config import get_db_path
from ai_report.connectors.base import BaseConnector
from ai_report.connectors.common import (
    content_text,
    group_by_day,
    messages_payload,
    parse_iso,
    timeline,
)
from ai_report.models import UnifiedRecord
from ai_report.utils import log, safe_str, truncate

_MSG_RAW_LIMIT = 2000
_SUMMARY_LIMIT = 400
_SNIPPET_LIMIT = 60

#: 只把这两类消息当作对话正文（info / error 是运行日志）
_ROLES = {"user": "user", "gemini": "assistant", "model": "assistant"}


class _GeminiLikeConnector(BaseConnector):
    """Gemini CLI 系连接器的公共实现，子类只改 root / 显示名 / 环境变量。"""

    env_key = ""

    def __init__(self, root: str | None = None) -> None:
        self.root = Path(root or get_db_path(self.env_key))

    def available(self) -> bool:
        return (self.root / "tmp").is_dir()

    @staticmethod
    def _parse(path: Path) -> Tuple[str, float, float, List[Tuple[float, str, str]]]:
        """返回 (session_id, start, end, [(ts, role, text)])。"""
        try:
            data = json.loads(path.read_text("utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            return "", 0.0, 0.0, []
        if not isinstance(data, dict):
            return "", 0.0, 0.0, []

        sid = safe_str(data.get("sessionId")) or path.stem
        start = parse_iso(data.get("startTime"))
        end = parse_iso(data.get("lastUpdated"))
        messages: List[Tuple[float, str, str]] = []
        for item in data.get("messages") or []:
            role = _ROLES.get(safe_str(item.get("type")).lower())
            if not role:
                continue
            text = content_text(item.get("content"))
            if not text:
                continue
            messages.append((parse_iso(item.get("timestamp")), role, text))
        messages.sort(key=lambda x: x[0])
        return sid, start, end, messages

    def read(self) -> List[UnifiedRecord]:
        if not self.available():
            log(f"[{self.name}] 目录不存在，跳过：{self.root}")
            return []

        records: List[UnifiedRecord] = []
        for path in sorted(self.root.glob("tmp/*/chats/session-*.json")):
            if not path.is_file():
                continue
            sid, start, end, messages = self._parse(path)
            users = [(ts, text) for ts, role, text in messages if role == "user"]
            if not users:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            first = start or users[0][0] or mtime
            last = end or users[-1][0] or first

            base_raw = {"file": str(path), "events": len(messages)}
            days = group_by_day(users)
            if len(days) == 1:
                items = next(iter(days.values()))
                only_day = next(iter(days))
                records.append(UnifiedRecord(
                    source=self.name,
                    session_id=sid,
                    title=truncate(items[0][1], 80),
                    cwd=str(path.parent.parent.name),  # 项目哈希目录名，仅作线索
                    start_ts=min(ts for ts, _ in items) or first,
                    end_ts=max(ts for ts, _ in items) or last,
                    summary=timeline(items, _SUMMARY_LIMIT, _SNIPPET_LIMIT),
                    raw=dict(base_raw, messages=messages_payload(
                        messages, day=only_day, limit=_MSG_RAW_LIMIT)),
                ))
                continue

            for day, items in sorted(days.items()):
                records.append(UnifiedRecord(
                    source=self.name,
                    session_id=f"{sid}:{day}",
                    title=truncate(items[0][1], 80),
                    cwd=str(path.parent.parent.name),
                    start_ts=min(ts for ts, _ in items),
                    end_ts=max(ts for ts, _ in items),
                    summary=timeline(items, _SUMMARY_LIMIT, _SNIPPET_LIMIT),
                    raw=dict(base_raw, day=day, messages=messages_payload(
                        messages, day=day, limit=_MSG_RAW_LIMIT)),
                ))

        records.sort(key=lambda r: r.start_ts)
        log(f"[{self.name}] 读取到 {len(records)} 条记录")
        return records


class GeminiCliConnector(_GeminiLikeConnector):
    name = "Gemini CLI"
    content_level = "逐条原文"
    env_key = "GEMINI_DIR"


class QwenCodeConnector(_GeminiLikeConnector):
    name = "Qwen Code"
    content_level = "逐条原文"
    env_key = "QWEN_CODE_DIR"


class IFlowConnector(_GeminiLikeConnector):
    name = "iFlow"
    content_level = "逐条原文"
    env_key = "IFLOW_DIR"
