"""Codex（OpenAI Codex CLI / Codex Desktop）读取器 —— 原生实现，不依赖 Cass。

已探明事实（本机 0.146 alpha 实测）：
- 元数据在 `state_5.sqlite` 的 `threads` 表：title / cwd / created_at / updated_at /
  first_user_message / model / git_branch / rollout_path
  * cwd 带 Windows 扩展长度前缀 `\\\\?\\`，展示前要剥掉
  * `rollout_path` 指向 `rollout-*.jsonl`，磁盘上实际是 `rollout-*.jsonl.zst`（zstd）
- 投影历史在 `thread_history_1.sqlite` 的 `thread_items` 表：
  item_type 为 userMessage / agentMessage，item_json 里是 {content:[{type:"text",text}]}
  —— 有它就不必解压 rollout，读取最快
- 原始日志 `sessions/<年>/<月>/<日>/rollout-*.jsonl.zst`：
  逐行 {timestamp, type, payload}；正文在 type=response_item 且 payload.type=message 里，
  role 为 user / assistant；`type=session_meta` 给出 cwd
- 坑：注入的 `<environment_context>` / `<app-context>` 也会以 role=user 出现，必须剔除
  否则日报里全是环境噪音

读取策略：sqlite 元数据 +（投影历史 优先，rollout 兜底）逐条正文；
按发送日切分记录，与 CodeBuddy CN 的口径保持一致。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ai_report.config import get_db_path
from ai_report.connectors.base import BaseConnector
from ai_report.connectors.common import (
    content_text,
    group_by_day,
    is_injected_user_text,
    iter_jsonl,
    parse_iso,
    strip_long_path,
    timeline,
)
from ai_report.models import UnifiedRecord
from ai_report.utils import log, parse_ts, safe_str, truncate

ENV_KEY = "CODEX_DIR"

_MSG_RAW_LIMIT = 2000   # raw 中单条消息保留的最大字符数
_SUMMARY_LIMIT = 400    # summary 时间线最大字符数
_SNIPPET_LIMIT = 60     # 时间线里单条消息的截断长度

# item_type -> 角色（投影历史里的类型名）
_ITEM_ROLES = {"usermessage": "user", "agentmessage": "assistant"}


def _connect(db: Path) -> Optional[sqlite3.Connection]:
    """只读打开 sqlite；失败返回 None（不抛异常）。"""
    try:
        return sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3.0)
    except (sqlite3.Error, OSError):
        return None


def _rows_as_dicts(cur) -> List[dict]:
    """把游标结果按列名转成 dict 列表。"""
    cols = [d[0] for d in (cur.description or [])]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _rollout_candidates(raw_path: str) -> List[Path]:
    """rollout_path 记录的是 .jsonl，磁盘上可能是 .jsonl.zst / .jsonl.zstd。"""
    base = Path(strip_long_path(raw_path))
    out = [base]
    if base.suffix == ".jsonl":
        out += [base.with_suffix(".jsonl.zst"), base.with_suffix(".jsonl.zstd")]
    return out


class CodexConnector(BaseConnector):
    name = "Codex"
    content_level = "逐条原文"

    def __init__(self, root: str | None = None) -> None:
        self.root = Path(root or get_db_path(ENV_KEY))

    # ---- 路径 ----
    @property
    def _state_db(self) -> Path:
        return self.root / "state_5.sqlite"

    @property
    def _history_db(self) -> Path:
        return self.root / "thread_history_1.sqlite"

    @property
    def _sessions_dir(self) -> Path:
        return self.root / "sessions"

    def available(self) -> bool:
        return self._state_db.is_file() or self._sessions_dir.is_dir()

    # ---- 元数据 ----
    def _threads(self) -> List[dict]:
        """读 threads 表；老版本没有这张表时返回空，由 rollout 扫描兜底。"""
        if not self._state_db.is_file():
            return []
        conn = _connect(self._state_db)
        if conn is None:
            return []
        try:
            return _rows_as_dicts(conn.execute("SELECT * FROM threads"))
        except sqlite3.Error as exc:
            log(f"[{self.name}] 读取 threads 表失败：{exc}")
            return []
        finally:
            conn.close()

    # ---- 正文：投影历史（无需解压） ----
    def _messages_from_history(self, thread_id: str) -> List[Tuple[float, str, str]]:
        """返回 [(epoch 秒, 角色, 正文)]，按 rollout_ordinal 升序。"""
        if not self._history_db.is_file():
            return []
        conn = _connect(self._history_db)
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT created_at_ms, item_type, item_json FROM thread_items"
                " WHERE thread_id=? ORDER BY rollout_ordinal, created_at_ms",
                (thread_id,),
            ).fetchall()
        except sqlite3.Error:
            return []
        finally:
            conn.close()

        out: List[Tuple[float, str, str]] = []
        for created_ms, item_type, item_json in rows:
            role = _ITEM_ROLES.get(safe_str(item_type).lower())
            if not role:
                continue  # reasoning / functionCall / functionCallOutput 都不要
            try:
                item = json.loads(item_json)
            except (json.JSONDecodeError, TypeError):
                continue
            text = content_text(item.get("content"))
            if not text:
                continue
            if role == "user" and is_injected_user_text(text):
                continue
            out.append((parse_ts(created_ms), role, text))
        return out

    # ---- 正文：rollout（zstd 兜底） ----
    def _messages_from_rollout(self, rollout_path: str) -> List[Tuple[float, str, str]]:
        path = next((p for p in _rollout_candidates(rollout_path) if p.is_file()), None)
        if path is None:
            return []
        out: List[Tuple[float, str, str]] = []
        for line in iter_jsonl(path):
            if line.get("type") != "response_item":
                continue
            payload = line.get("payload") or {}
            if payload.get("type") != "message":
                continue
            role = safe_str(payload.get("role")).lower()
            if role not in ("user", "assistant"):
                continue  # developer / system 是提示词，不是对话
            text = content_text(payload.get("content"))
            if not text:
                continue
            if role == "user" and is_injected_user_text(text):
                continue
            ts = parse_iso(line.get("timestamp"))
            out.append((ts, role, text))
        out.sort(key=lambda x: x[0])
        return out

    # ---- 主流程 ----
    def read(self) -> List[UnifiedRecord]:
        if not self.available():
            log(f"[{self.name}] 数据目录不存在，跳过：{self.root}")
            return []

        records: List[UnifiedRecord] = []
        threads = self._threads()

        for thread in threads:
            tid = safe_str(thread.get("id") or thread.get("thread_id")).strip()
            if not tid:
                continue
            messages = self._messages_from_history(tid)
            if not messages and thread.get("rollout_path"):
                messages = self._messages_from_rollout(safe_str(thread["rollout_path"]))
            records.extend(self._to_records(thread, tid, messages))

        # 没有 state 库（或库里没登记）时，直接从 rollout 目录重建
        if not threads:
            records.extend(self._records_from_rollouts())

        records.sort(key=lambda r: r.start_ts)
        log(f"[{self.name}] 读取到 {len(records)} 条记录")
        return records

    def _to_records(self, thread: dict, tid: str,
                    messages: List[Tuple[float, str, str]]) -> List[UnifiedRecord]:
        """一个 Codex 线程 -> 一条或多条（跨天则按天切）UnifiedRecord。"""
        cwd = strip_long_path(safe_str(thread.get("cwd")))
        raw_title = safe_str(thread.get("title")).strip()
        first_msg = safe_str(thread.get("first_user_message")).strip()
        created = parse_ts(thread.get("created_at_ms") or thread.get("created_at"))
        updated = parse_ts(thread.get("updated_at_ms") or thread.get("updated_at")) or created

        users = [(ts, text) for ts, role, text in messages if role == "user"]
        base_raw: Dict[str, object] = {
            "thread_id": tid,
            "model": safe_str(thread.get("model")),
            "source": safe_str(thread.get("source")),
            "git_branch": safe_str(thread.get("git_branch")),
            "tokens_used": thread.get("tokens_used") or 0,
            "archived": bool(thread.get("archived")),
        }

        # 没读到正文：退回元数据（标题 / 首条用户消息）
        if not users:
            title = raw_title or first_msg or f"Codex 会话 {tid[:8]}"
            return [UnifiedRecord(
                source=self.name,
                session_id=tid,
                title=truncate(title, 80),
                status="",
                cwd=cwd,
                start_ts=created,
                end_ts=updated,
                summary=truncate(first_msg or title, _SUMMARY_LIMIT),
                raw=base_raw,
            )]

        records: List[UnifiedRecord] = []
        days = group_by_day(users)
        for day, items in sorted(days.items()):
            title = raw_title or truncate(items[0][1], 50)
            start = min(ts for ts, _ in items) or created
            end = max(ts for ts, _ in items) or updated
            rec_raw = dict(base_raw)
            rec_raw["day"] = day
            rec_raw["messages"] = [
                {"ts": ts, "role": "user", "text": truncate(text, _MSG_RAW_LIMIT)}
                for ts, text in items
            ]
            records.append(UnifiedRecord(
                source=self.name,
                session_id=tid if len(days) == 1 else f"{tid}:{day}",
                title=truncate(title, 80),
                status="",
                cwd=cwd,
                start_ts=start,
                end_ts=end,
                summary=timeline(items, _SUMMARY_LIMIT, _SNIPPET_LIMIT),
                raw=rec_raw,
            ))
        return records

    def _records_from_rollouts(self) -> List[UnifiedRecord]:
        """兜底：只扫 sessions 目录，从 session_meta 取 cwd、从正文取消息。"""
        records: List[UnifiedRecord] = []
        if not self._sessions_dir.is_dir():
            return records
        for path in sorted(self._sessions_dir.rglob("rollout-*")):
            if not path.is_file():
                continue
            sid, cwd, messages = "", "", []
            for line in iter_jsonl(path):
                kind = line.get("type")
                payload = line.get("payload") or {}
                if kind == "session_meta":
                    sid = safe_str(payload.get("id") or payload.get("session_id"))
                    cwd = strip_long_path(safe_str(payload.get("cwd")))
                elif kind == "response_item" and payload.get("type") == "message":
                    role = safe_str(payload.get("role")).lower()
                    if role not in ("user", "assistant"):
                        continue
                    text = content_text(payload.get("content"))
                    if role == "user" and is_injected_user_text(text):
                        continue
                    if text:
                        messages.append((parse_iso(line.get("timestamp")), role, text))
            if not sid:
                continue
            thread = {"id": sid, "cwd": cwd, "rollout_path": str(path)}
            records.extend(self._to_records(thread, sid, messages))
        return records
