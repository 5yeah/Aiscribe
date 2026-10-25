"""VS Code 插件系 AI 编码代理读取器：Cline / Roo Code / Kilo Code。

已探明事实（三者同源，格式一致）：
- 数据在编辑器的 globalStorage 下，按插件 id 分目录：
  Cline `saoudrizwan.claude-dev`、Roo Code `rooveterinaryinc.roo-cline`、
  Kilo Code `kilocode.kilo-code`
- 每个任务一个目录 `<globalStorage>/<插件 id>/tasks/<taskId>/`：
  * `api_conversation_history.json`：[{role: user|assistant, content: [...块...], ts: <ms>}]
    —— 块类型 text / tool_use / tool_result，只有 text 是正文
  * `task_metadata.json`：模型用量、上下文文件等；有 cwd / workspace 就拿来定位项目
- 兜底：任务目录的 mtime 作为时间（老版本没有 ts 时）
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from ai_report.config import get_db_path
from ai_report.connectors.base import BaseConnector
from ai_report.connectors.common import (
    content_text,
    group_by_day,
    timeline,
)
from ai_report.models import UnifiedRecord
from ai_report.utils import log, parse_ts, safe_str, truncate

ENV_KEY = "VSCODE_GLOBAL_STORAGE"

_MSG_RAW_LIMIT = 2000
_SUMMARY_LIMIT = 400
_SNIPPET_LIMIT = 60

#: 插件 id -> 展示名
EXTENSIONS = {
    "saoudrizwan.claude-dev": "Cline",
    "rooveterinaryinc.roo-cline": "Roo Code",
    "kilocode.kilo-code": "Kilo Code",
}


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return default


class _VsCodeAgentConnector(BaseConnector):
    """Cline / Roo / Kilo 的公共实现，子类只指定插件 id。"""

    ext_id = ""
    #: 默认扫描的编辑器 globalStorage 目录名（%APPDATA% 下）
    editor_dirs: Tuple[str, ...] = (
        "Code", "Code - Insiders", "VSCodium", "Cursor", "Trae CN", "Windsurf"
    )

    def __init__(self, bases: Optional[Iterable[str]] = None) -> None:
        if bases is None:
            raw = get_db_path(ENV_KEY)
            bases = [p for p in raw.split(";") if p]
        self.bases = [Path(b) for b in bases]

    def _task_roots(self) -> List[Path]:
        """返回所有插件 tasks 目录。"""
        out = []
        for base in self.bases:
            for editor in self.editor_dirs:
                tasks = base / editor / "User" / "globalStorage" / self.ext_id / "tasks"
                if tasks.is_dir():
                    out.append(tasks)
        return out

    def available(self) -> bool:
        return bool(self._task_roots())

    def location(self) -> str:
        roots = self._task_roots()
        if roots:
            return ";".join(str(p) for p in roots)
        # 没装过：把「会去哪里找」显示出来，方便排查
        return ";".join(str(b / e / "User" / "globalStorage" / self.ext_id / "tasks")
                        for b in self.bases for e in self.editor_dirs[:1])

    # ---- 单个任务 ----
    def _parse_task(self, task_dir: Path) -> Tuple[List[Tuple[float, str, str]], str, float]:
        """返回 ([(ts, role, text)], cwd, 目录 mtime)。"""
        history = _load_json(task_dir / "api_conversation_history.json", [])
        meta = _load_json(task_dir / "task_metadata.json", {})
        cwd = safe_str(
            (meta or {}).get("cwd") or (meta or {}).get("workspace") or ""
        ).strip()

        messages: List[Tuple[float, str, str]] = []
        if isinstance(history, list):
            for item in history:
                if not isinstance(item, dict):
                    continue
                role = safe_str(item.get("role")).lower()
                if role not in ("user", "assistant"):
                    continue
                text = content_text(item.get("content"))
                if not text:
                    continue
                messages.append((parse_ts(item.get("ts")), role, text))
        messages.sort(key=lambda x: x[0])
        try:
            mtime = task_dir.stat().st_mtime
        except OSError:
            mtime = 0.0
        return messages, cwd, mtime

    def read(self) -> List[UnifiedRecord]:
        roots = self._task_roots()
        if not roots:
            log(f"[{self.name}] 未找到 {self.ext_id} 的任务目录")
            return []

        records: List[UnifiedRecord] = []
        for tasks in roots:
            for task_dir in sorted(p for p in tasks.iterdir() if p.is_dir()):
                messages, cwd, mtime = self._parse_task(task_dir)
                users = [(ts, text) for ts, role, text in messages if role == "user"]
                if not users:
                    continue
                base_raw: Dict[str, object] = {
                    "task_id": task_dir.name,
                    "dir": str(task_dir),
                }
                days = group_by_day(users)
                if len(days) == 1:
                    items = next(iter(days.values()))
                    records.append(UnifiedRecord(
                        source=self.name,
                        session_id=task_dir.name,
                        title=truncate(items[0][1], 80),
                        cwd=cwd,
                        start_ts=min(ts for ts, _ in items) or mtime,
                        end_ts=max(ts for ts, _ in items) or mtime,
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
                        session_id=f"{task_dir.name}:{day}",
                        title=truncate(items[0][1], 80),
                        cwd=cwd,
                        start_ts=min(ts for ts, _ in items),
                        end_ts=max(ts for ts, _ in items),
                        summary=timeline(items, _SUMMARY_LIMIT, _SNIPPET_LIMIT),
                        raw=dict(base_raw, day=day, messages=[
                            {"ts": ts, "text": truncate(text, _MSG_RAW_LIMIT)}
                            for ts, text in items
                        ]),
                    ))

        records.sort(key=lambda r: r.start_ts)
        log(f"[{self.name}] 读取到 {len(records)} 条记录")
        return records


class ClineConnector(_VsCodeAgentConnector):
    name = "Cline"
    content_level = "逐条原文"
    ext_id = "saoudrizwan.claude-dev"


class RooCodeConnector(_VsCodeAgentConnector):
    name = "Roo Code"
    content_level = "逐条原文"
    ext_id = "rooveterinaryinc.roo-cline"


class KiloCodeConnector(_VsCodeAgentConnector):
    name = "Kilo Code"
    content_level = "逐条原文"
    ext_id = "kilocode.kilo-code"
