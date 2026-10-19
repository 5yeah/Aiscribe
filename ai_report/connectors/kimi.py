"""Kimi（月之暗面）桌面端读取器。

已探明事实：
- 会话正文在云端，本地只有元数据：kimi-agent/*.json（状态、上下文用量、文件编辑）
- logs/main.log 记录每次 RPC 事件，含北京时间与 conversation:<uuid>，可还原首末活动
- conversation-context-usage.json 的 updatedAt 是会话最近活跃时间（ISO8601 UTC）
- file-edit-journal.json 按会话记录改过的文件路径，可据此定位项目目录
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ai_report.config import get_db_path
from ai_report.connectors.base import BaseConnector
from ai_report.models import UnifiedRecord
from ai_report.utils import log

ENV_KEY = "KIMI_DIR"

# 项目根标记：找到最近一层含标记的目录，作为 cwd 供 L2 分类
_MARKERS = (".git", "package.json", "pyproject.toml", "requirements.txt",
            "CMakeLists.txt", "Cargo.toml", "go.mod", ".venv", ".idea")

_LINE_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.\d+\]")
_KEY_RE = re.compile(r"conversation:([0-9a-f\-]{8,})")


def _local_ts(text: str) -> float:
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return 0.0


def _iso_ts(text: str) -> float:
    """'2026-10-10T03:24:11.586Z' -> epoch 秒。"""
    if not text:
        return 0.0
    try:
        dt = datetime.strptime(text[:26], "%Y-%m-%dT%H:%M:%S.%f")
    except ValueError:
        try:
            dt = datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return 0.0
    return dt.replace(tzinfo=timezone.utc).timestamp()


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log(f"[Kimi] 读取 {path.name} 失败：{exc}")
        return default


def _project_root(file_path: str) -> str:
    """从被编辑文件向上找项目根；找不到就退回文件所在目录。"""
    p = Path(file_path)
    if not p.is_absolute():
        return str(p.parent)
    cur = p.parent
    for _ in range(6):
        if any((cur / m).exists() for m in _MARKERS):
            return str(cur)
        if cur.parent == cur:
            break
        cur = cur.parent
    return str(p.parent)


def _match_id(prefix: str, ids: List[str]) -> Optional[str]:
    """日志里的 uuid 可能被 <redacted> 截断，用前缀回配完整 id。"""
    if prefix in ids:
        return prefix
    for cid in ids:
        if cid.startswith(prefix) or prefix.startswith(cid):
            return cid
    return None


class KimiConnector(BaseConnector):
    name = "Kimi"
    content_level = "元数据"

    def __init__(self, root: str | None = None) -> None:
        self.root = Path(root or get_db_path(ENV_KEY))

    def available(self) -> bool:
        return (self.root / "kimi-agent").is_dir()

    def read(self) -> List[UnifiedRecord]:
        if not self.available():
            log(f"[{self.name}] 数据目录不存在，跳过：{self.root}")
            return []

        agent = self.root / "kimi-agent"
        usage = _load_json(agent / "conversation-context-usage.json", {})
        statuses = _load_json(agent / "conversation-statuses.json", {})
        journals = _load_json(agent / "file-edit-journal.json", {})

        ids = [k.split("conversation:")[-1] for k in usage if "conversation:" in k]
        ids += [k.split("conversation:")[-1]
                for k in statuses if "conversation:" in k and k.split("conversation:")[-1] not in ids]
        if not ids:
            return []

        # main.log：每会话的首末活动时间与消息数
        activity: Dict[str, Dict[str, float]] = {}
        log_path = self.root / "logs" / "main.log"
        if log_path.is_file():
            try:
                with log_path.open("r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        m = _LINE_RE.match(line)
                        if not m:
                            continue
                        k = _KEY_RE.search(line)
                        if not k:
                            continue
                        cid = _match_id(k.group(1), ids)
                        if not cid:
                            continue
                        ts = _local_ts(m.group(1))
                        if not ts:
                            continue
                        slot = activity.setdefault(cid, {"first": ts, "last": ts, "msgs": 0})
                        slot["first"] = min(slot["first"], ts)
                        slot["last"] = max(slot["last"], ts)
                        if "send_message" in line:
                            slot["msgs"] += 1
            except OSError as exc:
                log(f"[{self.name}] 读取 main.log 失败：{exc}")

        records: List[UnifiedRecord] = []
        for cid in ids:
            key = f"agent:main:main:conversation:{cid}"
            ctx_ts = _iso_ts((usage.get(key) or {}).get("updatedAt", ""))
            act = activity.get(cid, {})
            start = min([t for t in (act.get("first", 0.0), ctx_ts) if t] or [0.0])
            end = max([t for t in (act.get("last", 0.0), ctx_ts) if t] or [0.0])
            if not start:
                continue

            cwd, title = self._locate(journals.get(key), cid)
            msgs = int(act.get("msgs", 0))
            records.append(
                UnifiedRecord(
                    source=self.name,
                    session_id=cid,
                    title=title,
                    status=str(statuses.get(key, "")),
                    cwd=cwd,
                    start_ts=start,
                    end_ts=end,
                    summary=f"发送 {msgs} 条消息" if msgs else title,
                    raw={"messages": msgs, "model": (usage.get(key) or {}).get("model", "")},
                )
            )

        records.sort(key=lambda r: r.start_ts)
        return records

    @staticmethod
    def _locate(journal, cid: str) -> Tuple[str, str]:
        """从文件编辑日志定位项目目录；没有编辑就退回通用标题。"""
        raw = journal if isinstance(journal, str) else None
        if not raw:
            return "", f"Kimi 会话 {cid[:8]}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return "", f"Kimi 会话 {cid[:8]}"

        for turn in data.get("turns", []):
            for entry in turn.get("entries", []):
                path = entry.get("path") or ""
                if path:
                    root = _project_root(path)
                    name = Path(root).name or root
                    return root, f"{name}（代码会话）"
        return "", f"Kimi 会话 {cid[:8]}"
