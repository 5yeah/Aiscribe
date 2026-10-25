"""VS Code 系编辑器内建聊天（GitHub Copilot Chat）读取器。

已探明事实（本机 VS Code 实测）：
- 会话按工作区存放：`<User>/workspaceStorage/<哈希>/chatSessions/<sessionId>.jsonl`
- 首行是一次快照：`{"kind":0,"v":{"version":3,"creationDate":<ms>,"sessionId":"...","requests":[...]}}`
  之后每行是对该快照的增量操作（kind 非 0），requests 里才是正文
- 会话索引在 `<User>/.../state.vscdb` 的 ItemTable，键 `chat.ChatSessionStore.index`：
  {entries: {"<sessionId>": {sessionId, title, lastMessageDate, timing:{created}, isEmpty}}}
  —— 只有索引、没有正文时退化成「仅标题」记录
- 工作区目录对应哪个项目，看同目录的 `workspace.json` 里的 folder URI（file:/// 编码）
- 变量：VS Code / Insiders / Cursor / Windsurf 都是同一套布局，一并扫描
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote

from ai_report.config import get_db_path
from ai_report.connectors.base import BaseConnector
from ai_report.connectors.common import (
    content_text,
    group_by_day,
    iter_jsonl,
    timeline,
)
from ai_report.models import UnifiedRecord
from ai_report.utils import log, parse_ts, safe_str, truncate

ENV_KEY = "VSCODE_USER_DIRS"

_MSG_RAW_LIMIT = 2000
_SUMMARY_LIMIT = 400
_SNIPPET_LIMIT = 60


def _folder_of(work_dir: Path) -> str:
    """workspace.json 的 file:/// 路径 -> 普通路径。"""
    try:
        raw = json.loads((work_dir / "workspace.json").read_text("utf-8")).get("folder", "")
    except (OSError, json.JSONDecodeError):
        return ""
    if raw.startswith("file:///"):
        return unquote(raw[8:])
    if raw.startswith("file://"):
        return unquote(raw[7:])
    return unquote(raw)


def _index(state_db: Path) -> Dict[str, dict]:
    """读 chat.ChatSessionStore.index -> {sessionId: 元数据}。"""
    if not state_db.is_file():
        return {}
    try:
        conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=3.0)
    except (sqlite3.Error, OSError):
        return {}
    try:
        row = conn.execute(
            "SELECT value FROM ItemTable WHERE key='chat.ChatSessionStore.index'"
        ).fetchone()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    if not row or not row[0]:
        return {}
    try:
        entries = json.loads(row[0]).get("entries") or {}
    except (json.JSONDecodeError, AttributeError):
        return {}
    return {k: v for k, v in entries.items() if isinstance(v, dict)}


def _requests(path: Path) -> List[dict]:
    """从快照 + 增量操作里收集 chat requests。"""
    out: List[dict] = []
    for line in iter_jsonl(path):
        value = line.get("v") if "v" in line else line
        if isinstance(value, dict) and isinstance(value.get("requests"), list):
            out.extend(r for r in value["requests"] if isinstance(r, dict))
        # 增量操作可能把单条 request 包在别的字段里，宽松兜一下
        for key in ("request", "requests", "item"):
            found = line.get(key)
            if isinstance(found, dict) and ("message" in found or "response" in found):
                out.append(found)
            elif isinstance(found, list):
                out.extend(r for r in found if isinstance(r, dict))
    return out


class VSCodeChatConnector(BaseConnector):
    name = "Copilot Chat"
    content_level = "逐条原文"

    def __init__(self, roots: Optional[Iterable[str]] = None) -> None:
        if roots is None:
            raw = get_db_path(ENV_KEY)
            roots = [p for p in raw.split(";") if p]
        self.roots = [Path(r) for r in roots]

    def available(self) -> bool:
        return any(r.is_dir() for r in self.roots)

    def location(self) -> str:
        return ";".join(str(r) for r in self.roots if r.is_dir())

    # ---- 单会话 ----
    @staticmethod
    def _user_messages(requests: List[dict]) -> List[Tuple[float, str]]:
        out: List[Tuple[float, str]] = []
        for req in requests:
            text = ""
            message = req.get("message")
            if isinstance(message, dict):
                text = content_text(message.get("text") or message.get("content"))
            elif isinstance(message, str):
                text = message.strip()
            if not text:
                text = content_text(req.get("prompt") or req.get("text"))
            if not text:
                continue
            ts = parse_ts(
                req.get("timestamp")
                or (req.get("result") or {}).get("timestamp")
                or req.get("createdAt")
            )
            out.append((ts, text))
        out.sort(key=lambda x: x[0])
        return out

    def _read_root(self, root: Path) -> List[UnifiedRecord]:
        records: List[UnifiedRecord] = []
        global_index = _index(root / "globalStorage" / "state.vscdb")

        # 全局会话（不带工作区）
        for path in sorted(root.glob("globalStorage/chatSessions/*.jsonl")):
            records.extend(self._one_session(path, global_index, ""))

        for work_dir in sorted(p for p in (root / "workspaceStorage").glob("*") if p.is_dir()):
            sessions = sorted(work_dir.glob("chatSessions/*.jsonl"))
            if not sessions:
                continue
            cwd = _folder_of(work_dir)
            idx = _index(work_dir / "state.vscdb")
            for path in sessions:
                records.extend(self._one_session(path, idx, cwd))
        return records

    def _one_session(self, path: Path, index: Dict[str, dict], cwd: str) -> List[UnifiedRecord]:
        sid = path.stem
        meta = index.get(sid) or {}
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0

        users = self._user_messages(_requests(path))
        raw_title = safe_str(meta.get("title")).strip()
        if raw_title in ("New Chat", "Untitled"):
            raw_title = ""
        created = parse_ts((meta.get("timing") or {}).get("created")) or parse_ts(
            meta.get("lastMessageDate")
        )

        if not users:
            # 空会话（isEmpty=true 或没有正文）：没有信息量，直接丢掉
            if meta.get("isEmpty", True):
                return []
            title = raw_title or f"VS Code 会话 {sid[:8]}"
            return [UnifiedRecord(
                source=self.name,
                session_id=sid,
                title=truncate(title, 80),
                cwd=cwd,
                start_ts=created or mtime,
                end_ts=parse_ts(meta.get("lastMessageDate")) or created or mtime,
                summary=truncate(title, _SUMMARY_LIMIT),
                raw={"index_only": True, "cwd": cwd},
            )]

        end_hint = parse_ts(meta.get("lastMessageDate")) or mtime
        days = group_by_day(users)
        if len(days) == 1:
            items = next(iter(days.values()))
            return [UnifiedRecord(
                source=self.name,
                session_id=sid,
                title=truncate(raw_title or items[0][1], 80),
                cwd=cwd,
                start_ts=min(ts for ts, _ in items) or created or mtime,
                end_ts=max(ts for ts, _ in items) or end_hint,
                summary=timeline(items, _SUMMARY_LIMIT, _SNIPPET_LIMIT),
                raw=dict(cwd=cwd, messages=[
                    {"ts": ts, "text": truncate(text, _MSG_RAW_LIMIT)}
                    for ts, text in items
                ]),
            )]

        out: List[UnifiedRecord] = []
        for day, items in sorted(days.items()):
            out.append(UnifiedRecord(
                source=self.name,
                session_id=f"{sid}:{day}",
                title=truncate(raw_title or items[0][1], 80),
                cwd=cwd,
                start_ts=min(ts for ts, _ in items),
                end_ts=max(ts for ts, _ in items),
                summary=timeline(items, _SUMMARY_LIMIT, _SNIPPET_LIMIT),
                raw=dict(cwd=cwd, day=day, messages=[
                    {"ts": ts, "text": truncate(text, _MSG_RAW_LIMIT)}
                    for ts, text in items
                ]),
            ))
        return out

    def read(self) -> List[UnifiedRecord]:
        if not self.available():
            log(f"[{self.name}] 编辑器用户目录不存在，跳过：{self.roots}")
            return []
        records: List[UnifiedRecord] = []
        for root in self.roots:
            if root.is_dir():
                records.extend(self._read_root(root))
        records.sort(key=lambda r: r.start_ts)
        log(f"[{self.name}] 读取到 {len(records)} 条记录")
        return records
