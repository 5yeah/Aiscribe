"""dsh（DeepSeek Harness）读取器 —— 原生实现，不依赖任何索引器。

已探明事实（本机 v4 实测）：
- 会话在 `~/.dsh/sessions/<编码后的 cwd>/session-<uuid>/session.v4.jsonl.zstd`
- 文件是**追加式**日志：多个 zstd 帧首尾拼接，只解第一帧只能拿到头部；
  必须流式跨帧解压（common.read_bytes 已处理）
- 事件是带 `time`(epoch 毫秒) 的 JSON 行，正文只有这几类：
  * `user/message`：data.content + data.source.kind —— kind 为 "user" 才是真人输入，
    "runtime-context" / "system" 等都是注入的上下文，必须剔除
  * `assistant/message`：data.message.content，块类型 text / reasoning / tool-call
  * `session/title`：data.title，会出现多条（先 fallback 后模型生成），取最后一条
  * `session`：头部，含 id / cwd / createdAt
- `~/.dsh/storages/session_projcache/sessions/<id>.json` 是投影缓存（纯 JSON），
  含 title / 首条输入 / lastPromptAt —— 没有 zstd 能力时可据此退化出元数据记录
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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

ENV_KEY = "DSH_SESSIONS_DIR"

_MSG_RAW_LIMIT = 2000
_SUMMARY_LIMIT = 400
_SNIPPET_LIMIT = 60


def _projcache(sessions_root: Path) -> Dict[str, dict]:
    """读投影缓存：{session-id: {title, first_text, last_at, cwd}}。

    缓存位于 `~/.dsh/storages/session_projcache/sessions/`，相对 sessions 目录是
    `../../storages/...`；路径算不准时直接放弃（它只是增强，不是必需）。
    """
    cache_dir = sessions_root.parent / "storages" / "session_projcache" / "sessions"
    out: Dict[str, dict] = {}
    if not cache_dir.is_dir():
        return out
    for path in sorted(cache_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text("utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        record = data.get("record") or {}
        rows = record.get("rows") or {}
        identity = record.get("identity") or {}
        title = safe_str(((rows.get("title") or {}).get("val"))).strip()
        first = ((rows.get("titleInput") or {}).get("val") or {}).get("first") or {}
        first_text = safe_str(first.get("text")).strip()
        meta = ((rows.get("sessionListMetadata") or {}).get("val") or {})
        out[path.stem] = {
            "title": title,
            "first_text": first_text,
            "last_at": parse_ts(meta.get("lastPromptAt")),
            "cwd": safe_str(identity.get("cwd")),
            "created": parse_ts(identity.get("createdAt")),
        }
    return out


class DshConnector(BaseConnector):
    name = "dsh"
    content_level = "逐条原文"

    def __init__(self, root: str | None = None) -> None:
        self.root = Path(root or get_db_path(ENV_KEY))

    def available(self) -> bool:
        return self.root.is_dir()

    # ---- 单个会话 ----
    @staticmethod
    def _parse_session(path: Path) -> Tuple[str, str, float, List[Tuple[float, str, str]], str]:
        """解析一个 session.v4.jsonl.zstd。

        返回 (session_id, cwd, created_ts, [(ts, role, text)], title)。
        """
        sid = cwd = title = ""
        created = 0.0
        messages: List[Tuple[float, str, str]] = []
        for event in iter_jsonl(path):
            kind = safe_str(event.get("type"))
            data = event.get("data") or {}
            ts = parse_ts(event.get("time"))
            if kind == "session":
                sid = safe_str(event.get("id"))
                cwd = safe_str(event.get("cwd"))
                created = parse_ts(event.get("createdAt"))
            elif kind == "session/title":
                new_title = safe_str(data.get("title")).strip()
                if new_title:
                    title = new_title  # 后写覆盖先写：模型生成的标题更准
            elif kind == "user/message":
                source = data.get("source") or {}
                if safe_str(source.get("kind")) != "user":
                    continue  # 注入的运行时上下文 / 技能内容，不是真人输入
                text = content_text(data.get("content"))
                if text:
                    messages.append((ts, "user", text))
            elif kind == "assistant/message":
                text = content_text((data.get("message") or {}).get("content"))
                if text:
                    messages.append((ts, "assistant", text))
        messages.sort(key=lambda x: x[0])
        return sid, cwd, created, messages, title

    def _records(self, sid: str, cwd: str, created: float,
                 messages: List[Tuple[float, str, str]], title: str,
                 cache: Optional[dict]) -> List[UnifiedRecord]:
        cache = cache or {}
        users = [(ts, text) for ts, role, text in messages if role == "user"]
        title = title or cache.get("title", "")
        start = created or cache.get("created", 0.0) or (users[0][0] if users else 0.0)
        end = (users[-1][0] if users else 0.0) or cache.get("last_at", 0.0) or start
        if not start:
            return []

        if not users:
            # 没有正文（缺 zstd 或会话为空）：退化成元数据
            text = title or cache.get("first_text", "")
            if not text:
                return []  # 完全空的会话（既没正文也没标题）不值得进日报
            return [UnifiedRecord(
                source=self.name,
                session_id=sid,
                title=truncate(text, 80),
                cwd=cwd or cache.get("cwd", ""),
                start_ts=start,
                end_ts=end or start,
                summary=truncate(cache.get("first_text") or text, _SUMMARY_LIMIT),
                raw={"messages": 0, "partial": not messages},
            )]

        days = group_by_day(users)
        if len(days) == 1:
            only_day, items = next(iter(days.items()))
            return [UnifiedRecord(
                source=self.name,
                session_id=sid,
                title=truncate(title or items[0][1], 80),
                cwd=cwd or cache.get("cwd", ""),
                start_ts=min(ts for ts, _ in items),
                end_ts=max(ts for ts, _ in items),
                summary=timeline(items, _SUMMARY_LIMIT, _SNIPPET_LIMIT),
                raw={
                    "messages": [
                        {"ts": ts, "role": "user", "text": truncate(text, _MSG_RAW_LIMIT)}
                        for ts, text in items
                    ],
                    "message_count": len(users),
                    "day": only_day,
                },
            )]

        # 跨天会话按天切，避免前一天的内容算进今天
        records: List[UnifiedRecord] = []
        for day, items in sorted(days.items()):
            records.append(UnifiedRecord(
                source=self.name,
                session_id=f"{sid}:{day}",
                title=truncate(title or items[0][1], 80),
                cwd=cwd or cache.get("cwd", ""),
                start_ts=min(ts for ts, _ in items),
                end_ts=max(ts for ts, _ in items),
                summary=timeline(items, _SUMMARY_LIMIT, _SNIPPET_LIMIT),
                raw={
                    "messages": [
                        {"ts": ts, "role": "user", "text": truncate(text, _MSG_RAW_LIMIT)}
                        for ts, text in items
                    ],
                    "message_count": len(users),
                    "day": day,
                },
            ))
        return records

    def read(self) -> List[UnifiedRecord]:
        if not self.available():
            log(f"[{self.name}] 会话目录不存在，跳过：{self.root}")
            return []

        cache = _projcache(self.root)
        records: List[UnifiedRecord] = []
        for session_dir in sorted(p for p in self.root.glob("*/*") if p.is_dir()):
            if not session_dir.name.startswith("session-"):
                continue
            payload = next(iter(sorted(session_dir.glob("session.v4.jsonl*"))), None)
            if payload is None:
                continue
            sid, cwd, created, messages, title = self._parse_session(payload)
            sid = sid or session_dir.name
            records.extend(
                self._records(sid, cwd, created, messages, title, cache.get(sid))
            )

        records.sort(key=lambda r: r.start_ts)
        log(f"[{self.name}] 读取到 {len(records)} 条记录")
        return records
