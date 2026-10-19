"""TRAE（字节跳动 IDE）读取器。

已探明事实：
- 会话正文有两个本地来源：
  1. `.trae-cn/memory/projects/<项目>/<日期>/session_memory_<会话id>.jsonl`
     每行一条消息级摘要（intent/actions/outcome + message_summary_time），
     同目录 topics.md 有会话级摘要 —— 内容最全，优先使用
  2. 桌面端 workspaceStorage/<hash>/state.vscdb（旧版 Trae CN 全 IDE）：
     ChatStore.state.turnsHeight 的键即会话 id，前 8 位十六进制是 epoch 秒，
     只有元数据，最后活跃时间粗略取 db 修改时间
- 会话 id 前 8 位十六进制 = 创建时刻的 epoch 秒
- ai-agent/database.db 为加密存储，不可读，不作为数据源
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import unquote

from ai_report.config import get_db_path
from ai_report.connectors.base import BaseConnector
from ai_report.models import UnifiedRecord
from ai_report.utils import log

ENV_KEY = "TRAE_ROOTS"
ENV_MEMORY_KEY = "TRAE_MEMORY_DIR"

_HEX = re.compile(r"^[0-9a-f]{8}$")
_SID_RE = re.compile(r"^session_memory_([0-9a-f]{24,})\.jsonl$")
_TS_MIN = 1577836800  # 2020-01-01
_TS_MAX = 4102444800  # 2100-01-01
_TOPIC_RE = re.compile(
    r"^\[session_id:\s*([0-9a-f]+)\s*\|\s*topic_summary_time:\s*"
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\](.*)$"
)


def _folder_of(work_dir: Path) -> str:
    """workspace.json 的 file:/// 协议路径 -> 普通路径。"""
    wj = work_dir / "workspace.json"
    try:
        raw = json.loads(wj.read_text("utf-8")).get("folder", "")
    except (OSError, json.JSONDecodeError):
        return ""
    if raw.startswith("file:///"):
        return unquote(raw[8:])
    if raw.startswith("file://"):
        return unquote(raw[7:])
    return unquote(raw)


def _session_times(db: Path) -> Dict[str, float]:
    """会话 id -> 创建时间。TurnsHeight 的键是 `<会话id>-<轮次偏移>`，
    会话 id 的前 8 位十六进制即创建时刻的 epoch 秒。"""
    out: Dict[str, float] = {}
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3.0)
        try:
            row = conn.execute(
                "SELECT value FROM ItemTable WHERE key='ChatStore'"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log(f"[TRAE] 打开 {db} 失败：{exc}")
        return out
    if not row or not row[0]:
        return out
    try:
        state = json.loads(row[0]).get("state", {})
    except (json.JSONDecodeError, AttributeError):
        return out
    keys = state.get("turnsHeight", {}) if isinstance(state, dict) else ()
    for turn_key in keys:
        sid = turn_key.split("-")[0]
        head = sid[:8]
        if not _HEX.match(head):
            continue
        ts = int(head, 16)
        if _TS_MIN <= ts <= _TS_MAX:
            out[sid] = float(ts)
    return out


def _decode_project(encoded: str) -> str:
    """项目目录名还原为路径：`-f-DSH-Aiscribe--p2-<hash>` -> `f:\\DSH\\Aiscribe`。"""
    name = re.sub(r"--p2-[0-9a-f]+$", "", encoded).strip("-")
    return name.replace("-", "\\") if name else ""


def _parse_time(value) -> Optional[float]:
    """'2026-10-10 11:45:54' 或 epoch 毫秒 -> epoch 秒。"""
    if isinstance(value, str):
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            return None
    if isinstance(value, (int, float)) and value:
        ts = float(value) / 1000 if value > 1e12 else float(value)
        return ts if _TS_MIN <= ts <= _TS_MAX else None
    return None


def _hex_ts(sid: str) -> Optional[float]:
    """会话 id 前 8 位十六进制即创建时刻的 epoch 秒。"""
    head = sid[:8]
    if not _HEX.match(head):
        return None
    ts = int(head, 16)
    return float(ts) if _TS_MIN <= ts <= _TS_MAX else None


def _topic_summaries(date_dir: Path) -> Dict[str, str]:
    """topics.md -> {会话 id: 会话级摘要}。"""
    out: Dict[str, str] = {}
    try:
        lines = (date_dir / "topics.md").read_text("utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        m = _TOPIC_RE.match(line.strip())
        if m:
            out[m.group(1)] = m.group(3).strip()
    return out


def _memory_records(memory_dir: Path) -> List[UnifiedRecord]:
    """读 `.trae-cn/memory/projects/<项目>/<日期>/session_memory_*.jsonl`。

    每个日期目录一条记录（不跨日期合并）：同一会话跨天续聊时，
    各天的记录按各自日期入当日日报，避免前一天的内容被算进当天。
    时间取该日期目录内消息的最早/最晚；summary 优先用同目录 topics.md
    的会话级摘要（topic），没有则退回首条 intent。
    """
    records: List[UnifiedRecord] = []
    for proj_dir in sorted(p for p in memory_dir.iterdir() if p.is_dir()):
        cwd = _decode_project(proj_dir.name)
        proj_name = Path(cwd.rstrip("\\/")).name or proj_dir.name
        for date_dir in sorted(p for p in proj_dir.iterdir() if p.is_dir()):
            if not re.match(r"^\d{8}$", date_dir.name):
                continue
            topics = _topic_summaries(date_dir)
            fallback = datetime.strptime(date_dir.name, "%Y%m%d").timestamp()
            for jl in sorted(date_dir.glob("session_memory_*.jsonl")):
                m = _SID_RE.match(jl.name)
                if not m:
                    continue
                sid = m.group(1)
                try:
                    raw_lines = jl.read_text("utf-8", errors="replace").splitlines()
                except OSError:
                    continue
                intents, times, outcome = [], [], ""
                for raw in raw_lines:
                    try:
                        item = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    intent = str(item.get("intent") or "").strip()
                    if intent:
                        intents.append(intent)
                    outcome = str(item.get("outcome") or "").strip() or outcome
                    for key in ("message_summary_time",):
                        ts = _parse_time(item.get(key))
                        if ts:
                            times.append(ts)
                    meta = item.get("compact_summary_meta")
                    if isinstance(meta, dict):
                        ts = _parse_time(meta.get("created_at_ms"))
                        if ts:
                            times.append(ts)
                start = min(times) if times else (_hex_ts(sid) or fallback)
                end = max(times) if times else start
                title = intents[0] if intents else f"{proj_name} 会话"
                title = title[:80]
                # summary 优先 topics.md 的会话级长摘要，退回首条 intent
                topic = topics.get(sid, "")
                rec = UnifiedRecord(
                    source="TRAE",
                    session_id=f"{sid}:{date_dir.name}",
                    title=title,
                    status="completed" if outcome else "",
                    cwd=cwd,
                    start_ts=start,
                    end_ts=end,
                    summary=topic or title,
                    raw={"memory": str(jl), "project": proj_name,
                         "topic": topic},
                )
                records.append(rec)
    return records


def _workspace_records(roots: List[Path], name: str) -> List[UnifiedRecord]:
    """读旧版桌面端 workspaceStorage/<hash>/state.vscdb 的会话元数据。"""
    records: List[UnifiedRecord] = []
    for root in roots:
        storage = root / "User" / "workspaceStorage"
        if not storage.is_dir():
            continue
        for work_dir in sorted(p for p in storage.iterdir() if p.is_dir()):
            db = work_dir / "state.vscdb"
            if not db.is_file():
                continue
            sessions = _session_times(db)
            if not sessions:
                continue
            folder = _folder_of(work_dir)
            folder_name = Path(folder.replace("\\", "/").rstrip("/")).name or work_dir.name
            try:
                last_seen = float(db.stat().st_mtime)
            except OSError:
                last_seen = 0.0
            # state.vscdb 的修改时间是整个工作区的，只有最新会话能据此延后
            latest = max(sessions, key=lambda s: sessions[s])
            for sid, start in sessions.items():
                end = max(start, last_seen) if sid == latest else start
                hhmm = datetime.fromtimestamp(start).strftime("%H:%M")
                records.append(
                    UnifiedRecord(
                        source=name,
                        session_id=f"{root.name}:{sid}",
                        title=f"{folder_name} 会话 {hhmm}",
                        status="",
                        cwd=folder,
                        start_ts=start,
                        end_ts=end,
                        summary=folder or folder_name,
                        raw={"workspace": folder, "db": str(db)},
                    )
                )
    return records


class TraeConnector(BaseConnector):
    name = "TRAE"
    content_level = "逐条原文"

    def __init__(self, roots: Optional[List[str]] = None,
                 memory_dir: Optional[str] = None) -> None:
        if roots is None:
            roots = [r for r in get_db_path(ENV_KEY).split(";") if r]
        self.roots = [Path(r) for r in roots]
        self.memory_dir = Path(memory_dir or get_db_path(ENV_MEMORY_KEY))

    def available(self) -> bool:
        return self.memory_dir.is_dir() or any(r.is_dir() for r in self.roots)

    def read(self) -> List[UnifiedRecord]:
        if not self.available():
            log(f"[{self.name}] 数据目录不存在，跳过：{self.memory_dir} / {self.roots}")
            return []
        records: List[UnifiedRecord] = []
        if self.memory_dir.is_dir():
            try:
                records += _memory_records(self.memory_dir)
            except OSError as exc:
                log(f"[{self.name}] 读取记忆目录失败：{exc}")
        records += _workspace_records(self.roots, self.name)
        log(f"[{self.name}] 读取到 {len(records)} 条会话")
        records.sort(key=lambda r: r.start_ts)
        return records
