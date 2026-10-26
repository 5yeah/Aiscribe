"""CodeBuddy CN（腾讯 IDE）读取器。

已探明事实：
- SQLite 单表 ItemTable(key TEXT, value TEXT)，key 形如 session:<uuid>
- value 为 JSON：{conversationId, cwd, title, status, createdAt, updatedAt}（epoch 毫秒）
- 会话库只有元数据（标题是会话创建时的旧话题，不能代表当天工作内容）
- 真实发送的用户输入在本地 logs/<时间戳>/**/*.log 中：
  `onAgentStart: userInput=<正文> agent=.. mode=.. conversationId=<cid>`（正文可跨多行）
  据此按“发送日”生成记录，summary 为当日真实输入时间线
- 没有任何发送记录的会话退化为标题级 summary（老行为）
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

from ai_report.config import get_db_path
from ai_report.connectors.base import BaseConnector
from ai_report.models import UnifiedRecord
from ai_report.utils import BJ_TZ, log, parse_ts, safe_str, to_bj, truncate

ENV_KEY = "CODEBUDDY_CN_DB"

# 日志中每次真实发送的用户输入事件（正文可跨多行，尾行以 agent=.. conversationId= 收尾）
_AGENT_START = "onAgentStart: userInput="
_AGENT_TAIL = re.compile(r" agent=\S+ mode=\S+ conversationId=([0-9a-f]{32})")
_LOG_TS = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})")
_IMAGE_REF = re.compile(r"@image:\S+")

_MSG_RAW_LIMIT = 2000     # raw 中单条消息保留的最大字符数
_SUMMARY_LIMIT = 400      # summary 时间线最大字符数
_SNIPPET_LIMIT = 60       # 时间线里单条消息的截断长度


def _clean_text(text: str) -> str:
    """压缩消息正文：图片引用转占位符、换行折叠为单空格。"""
    return " ".join(_IMAGE_REF.sub("[图片]", text or "").split())


def _parse_log_ts(head: str) -> float:
    """日志行首时间戳（北京时间）-> epoch 秒；解析失败返回 0.0。"""
    m = _LOG_TS.match(head)
    if not m:
        return 0.0
    try:
        dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        return 0.0
    return dt.replace(tzinfo=BJ_TZ).timestamp()


def parse_sent_messages(logs_dir: Path) -> Dict[str, List[Tuple[float, str]]]:
    """扫描 CodeBuddy CN 日志，还原每个会话真实发送的用户输入。

    返回 {conversationId: [(epoch秒, 正文), ...]}，按时间升序。
    """
    if not logs_dir.is_dir():
        return {}
    messages: Dict[str, List[Tuple[float, str]]] = {}
    seen = set()  # 轮转日志可能重复记录同一事件
    for path in sorted(logs_dir.rglob("*.log")):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        i = 0
        while i < len(lines):
            line = lines[i]
            if _AGENT_START not in line:
                i += 1
                continue
            head, body = line.split(_AGENT_START, 1)
            ts = _parse_log_ts(head)
            if not ts:
                i += 1
                continue
            chunk = [body]
            m = _AGENT_TAIL.search(body)
            cid = m.group(1) if m else None
            if m:
                chunk[0] = body[: m.start()]
            j = i + 1
            # 正文含换行时，conversationId 出现在后续某行行尾
            while cid is None and j < len(lines) and j < i + 400:
                m = _AGENT_TAIL.search(lines[j])
                if m:
                    cid = m.group(1)
                    chunk.append(lines[j][: m.start()])
                    break
                chunk.append(lines[j])
                j += 1
            if cid:
                text = "\n".join(chunk).strip()
                key = (round(ts, 3), cid, text)
                if text and key not in seen:
                    seen.add(key)
                    messages.setdefault(cid, []).append((ts, text))
            i = j + 1 if cid else i + 1
    for items in messages.values():
        items.sort(key=lambda x: x[0])
    return messages


def _group_by_day(msgs: List[Tuple[float, str]]) -> Dict[str, List[Tuple[float, str]]]:
    """按北京时间（YYYY-MM-DD）分组。"""
    days: Dict[str, List[Tuple[float, str]]] = {}
    for ts, text in msgs:
        days.setdefault(to_bj(ts).date().isoformat(), []).append((ts, text))
    return days


def _timeline(items: List[Tuple[float, str]]) -> str:
    """当日输入渲染为单行时间线：'14:41 xxx；14:43 yyy…'。

    超长时截断中间、保留首尾（开头的诉求与最新进展都要在），
    完整正文保留在 raw["messages"] 里。
    """
    parts = [
        f"{to_bj(ts).strftime('%H:%M')} {truncate(_clean_text(text), _SNIPPET_LIMIT)}"
        for ts, text in items
    ]
    joined = "；".join(parts)
    if len(joined) <= _SUMMARY_LIMIT or len(parts) < 3:
        return truncate(joined, _SUMMARY_LIMIT)
    first, last = parts[0], parts[-1]
    budget = _SUMMARY_LIMIT - len(first) - len(last) - 1
    if budget <= 20:
        return truncate(joined, _SUMMARY_LIMIT)
    mid = truncate("；".join(parts[1:-1]), budget)
    return f"{first}；{mid}；{last}"


class CodeBuddyCNConnector(BaseConnector):
    name = "CodeBuddy CN"
    content_level = "逐条原文"

    def __init__(self, db_path: str | None = None) -> None:
        # 优先级：构造参数 > 环境变量 CODEBUDDY_CN_DB > 自动发现的默认路径
        self.db_path = db_path or get_db_path(ENV_KEY)

    def available(self) -> bool:
        return Path(self.db_path).is_file()

    def _logs_dir(self) -> Path:
        return Path(self.db_path).parent / "logs"

    def read(self) -> List[UnifiedRecord]:
        if not self.available():
            log(f"[{self.name}] 数据库不存在，跳过：{self.db_path}")
            return []

        try:
            # read-only 打开，避免锁库；超时 3 秒防止被 IDE 长时间占用
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=3.0)
            try:
                rows = conn.execute(
                    "SELECT key, value FROM ItemTable WHERE key LIKE 'session:%'"
                ).fetchall()
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as exc:
            log(f"[{self.name}] 打开数据库失败：{exc}")
            return []

        messages = parse_sent_messages(self._logs_dir())
        records: List[UnifiedRecord] = []
        session_meta: Dict[str, dict] = {}

        for key, value in rows:
            try:
                data = json.loads(value) if isinstance(value, str) else dict(value or {})
            except (json.JSONDecodeError, TypeError):
                continue  # 单条损坏不影响整体

            session_id = safe_str(data.get("conversationId")) or safe_str(key).split(":", 1)[-1]
            meta = {
                "key": key,
                "title": safe_str(data.get("title")).strip() or "(无标题会话)",
                "status": safe_str(data.get("status")),
                "cwd": safe_str(data.get("cwd")),
                "start_ts": parse_ts(data.get("createdAt")),
                "end_ts": parse_ts(data.get("updatedAt")) or parse_ts(data.get("createdAt")),
            }
            session_meta[session_id] = meta

            if session_id in messages:
                continue  # 有真实输入 → 下面按“发送日”生成记录

            # 无任何发送记录：退回标题级元数据
            records.append(
                UnifiedRecord(
                    source=self.name,
                    session_id=session_id,
                    title=meta["title"],
                    project="",  # 留空，由 L2 统一分类
                    status=meta["status"],
                    cwd=meta["cwd"],
                    start_ts=meta["start_ts"],
                    end_ts=meta["end_ts"],
                    summary=meta["title"],
                    raw={"key": key, "status": meta["status"], "cwd": meta["cwd"]},
                )
            )

        # 有真实输入的会话：每个“有发送记录的自然日”一条记录
        for cid, msgs in messages.items():
            meta = session_meta.get(cid, {})
            title = meta.get("title", "")
            for day, items in _group_by_day(msgs).items():
                if not title:
                    title = truncate(_clean_text(items[0][1]), 50)
                records.append(
                    UnifiedRecord(
                        source=self.name,
                        session_id=f"{cid}:{day}",
                        title=title,
                        project="",
                        status=meta.get("status", ""),
                        cwd=meta.get("cwd", ""),
                        start_ts=items[0][0],
                        end_ts=items[-1][0],
                        summary=_timeline(items),
                        raw={
                            "key": meta.get("key", ""),
                            "status": meta.get("status"),
                            "cwd": meta.get("cwd"),
                            "day": day,
                            "messages": [
                                {"ts": ts, "role": "user",
                                 "text": truncate(text, _MSG_RAW_LIMIT)}
                                for ts, text in items
                            ],
                        },
                    )
                )

        with_input = sum(1 for r in records if "messages" in r.raw)
        log(
            f"[{self.name}] 读取到 {len(records)} 条记录"
            f"（{with_input} 条含真实输入，{len(records) - with_input} 条仅元数据）"
        )
        return records
