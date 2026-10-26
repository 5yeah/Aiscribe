"""Claude Code（Anthropic）读取器。

已探明事实：
- 会话在 `~/.claude/projects/<编码后的 cwd>/<session-uuid>.jsonl`，一行一个事件
- 每行：{type: user|assistant|summary|..., message:{role, content}, timestamp(ISO8601 UTC),
        sessionId, cwd, gitBranch, isSidechain, userType}
- content 可能是字符串，也可能是块数组：text / thinking / tool_use / tool_result
  → 只取 text，工具结果与思考过程不计入日报正文
- 坑：
  * 子代理（subagent）的对话以 isSidechain=true 存在同一个文件里，默认跳过，
    否则主会话的输入会被重复统计
  * 本地命令（如 /model）会被记为一条 user 消息，正文形如 `<command-name>...`，
    以及 "Caveat: The messages below were generated..." 的说明行，都要剔除
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple

from ai_report.connectors.base import BaseConnector
from ai_report.config import get_db_path
from ai_report.connectors.common import (
    content_text,
    group_by_day,
    iter_jsonl,
    messages_payload,
    parse_iso,
    timeline,
)
from ai_report.models import UnifiedRecord
from ai_report.utils import log, safe_str, truncate

ENV_KEY = "CLAUDE_CODE_DIR"

_MSG_RAW_LIMIT = 2000
_SUMMARY_LIMIT = 400
_SNIPPET_LIMIT = 60

#: 本地命令 / 系统注入的消息开头，不属于真人输入
_SKIP_PREFIXES = (
    "<command-name>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "Caveat: The messages below were generated",
)


def _is_noise(text: str) -> bool:
    head = (text or "").lstrip()[:80]
    return head.startswith(_SKIP_PREFIXES)


class ClaudeCodeConnector(BaseConnector):
    name = "Claude Code"
    content_level = "逐条原文"

    def __init__(self, root: str | None = None) -> None:
        self.root = Path(root or get_db_path(ENV_KEY))

    def available(self) -> bool:
        return (self.root / "projects").is_dir()

    # ---- 单文件 ----
    def _parse(self, path: Path) -> Tuple[str, str, str, List[Tuple[float, str, str]]]:
        """返回 (session_id, cwd, git_branch, [(ts, role, text)])。"""
        sid = cwd = branch = ""
        messages: List[Tuple[float, str, str]] = []
        for line in iter_jsonl(path):
            kind = safe_str(line.get("type"))
            if kind not in ("user", "assistant"):
                continue  # summary / file-history-snapshot 等元事件
            if line.get("isSidechain"):
                continue  # 子代理对话，避免与主会话重复
            sid = sid or safe_str(line.get("sessionId"))
            cwd = cwd or safe_str(line.get("cwd"))
            branch = branch or safe_str(line.get("gitBranch"))
            message = line.get("message") or {}
            role = safe_str(message.get("role") or kind)
            text = content_text(message.get("content"))
            if not text:
                continue
            if role == "user" and _is_noise(text):
                continue
            messages.append((parse_iso(line.get("timestamp")), role, text))
        messages.sort(key=lambda x: x[0])
        return sid or path.stem, cwd, branch, messages

    def read(self) -> List[UnifiedRecord]:
        if not self.available():
            log(f"[{self.name}] 目录不存在，跳过：{self.root}")
            return []

        records: List[UnifiedRecord] = []
        for path in sorted(self.root.glob("projects/*/*.jsonl")):
            if not path.is_file():
                continue
            sid, cwd, branch, messages = self._parse(path)
            users = [(ts, text) for ts, role, text in messages if role == "user"]
            if not users:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0

            base_raw: Dict[str, object] = {
                "file": str(path),
                "git_branch": branch,
                "events": len(messages),
            }
            days = group_by_day(users)
            if len(days) == 1:
                items = next(iter(days.values()))
                only_day = next(iter(days))
                records.append(UnifiedRecord(
                    source=self.name,
                    session_id=sid,
                    title=truncate(items[0][1], 80),
                    cwd=cwd,
                    start_ts=min(ts for ts, _ in items) or mtime,
                    end_ts=max(ts for ts, _ in items) or mtime,
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
                    cwd=cwd,
                    start_ts=min(ts for ts, _ in items),
                    end_ts=max(ts for ts, _ in items),
                    summary=timeline(items, _SUMMARY_LIMIT, _SNIPPET_LIMIT),
                    raw=dict(base_raw, day=day, messages=messages_payload(
                        messages, day=day, limit=_MSG_RAW_LIMIT)),
                ))

        records.sort(key=lambda r: r.start_ts)
        log(f"[{self.name}] 读取到 {len(records)} 条记录")
        return records
