"""连接器共用工具：zstd 解压、JSONL 读取、ISO 时间解析、正文抽取。

设计约定：
- 核心仍坚持「零三方依赖」，但 Codex / dsh 的会话是 zstd 压缩的追加式日志：
  * 有 `zstandard` 包就用它（推荐，速度快、支持多帧拼接）；
  * 没有就退回系统 `zstd` / `zstd.exe` 命令行；
  * 两者都没有时只警告一次并跳过该文件，不影响其它数据源。
  换句话说：不装任何东西也能跑，装了 zstd 才能读到 Codex / dsh 的正文。
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, List, Optional

from ai_report.utils import log, to_bj, truncate

#: 需要解压的后缀（大小写不敏感）
ZSTD_SUFFIXES = (".zst", ".zstd")

_warned: set = set()


def _warn_once(key: str, msg: str) -> None:
    """同一条提示只打印一次，避免刷屏。"""
    if key not in _warned:
        _warned.add(key)
        log(msg)


def _zstd_via_module(data: bytes) -> Optional[bytes]:
    """用 zstandard 包解压；支持多个 zstd 帧首尾拼接（追加式日志必须用流式）。"""
    try:
        import zstandard  # type: ignore
    except ImportError:
        return None
    try:
        reader = zstandard.ZstdDecompressor().stream_reader(
            io.BytesIO(data), read_across_frames=True
        )
        with reader:
            return reader.read()
    except Exception:
        return None


def _zstd_via_exe(data: bytes) -> Optional[bytes]:
    """用系统 zstd.exe 解压（走管道，不落临时文件）。"""
    exe = shutil.which("zstd") or shutil.which("zstd.exe")
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, "-d", "-c", "-q"], input=data, capture_output=True, timeout=120
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 and proc.stdout else None


def decompress_zstd(data: bytes) -> Optional[bytes]:
    """解压 zstd 数据；两条路径都不可用时返回 None 并提示一次。"""
    if not data:
        return b""
    out = _zstd_via_module(data)
    if out is None:
        out = _zstd_via_exe(data)
    if out is None:
        _warn_once(
            "zstd",
            "[zstd] 无法解压：请 `pip install zstandard` 或安装 zstd（"
            "Codex / dsh 的会话是 zstd 压缩的，缺它只能读到元数据）",
        )
    return out


def is_zstd(path: Path) -> bool:
    return path.suffix.lower() in ZSTD_SUFFIXES


def read_bytes(path: Path) -> Optional[bytes]:
    """读文件；zstd 后缀自动解压。失败返回 None。"""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if is_zstd(path):
        return decompress_zstd(raw)
    return raw


def iter_jsonl(path: Path) -> Iterator[dict]:
    """逐行解析 JSONL；坏行跳过，不抛异常。"""
    raw = read_bytes(path)
    if raw is None:
        return
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            yield item


def parse_iso(value: Any) -> float:
    """ISO8601（含毫秒 / Z / +08:00）-> epoch 秒；失败返回 0.0。"""
    if not isinstance(value, str) or not value:
        return 0.0
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    for candidate in (text, text[:26], text[:19]):
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return 0.0


def content_text(content: Any) -> str:
    """从各家格式的 content 字段抽出纯文本。

    支持：str / [{"text": ..}] / [{"type": "text", "text": ..}]，
    跳过 reasoning、tool-call、tool_result、图片等非正文块。
    """
    if not content:
        return ""
    if isinstance(content, str):
        return content.strip()
    parts: List[str] = []
    if isinstance(content, dict):
        return content_text([content])
    if isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "").lower()
            if kind in ("reasoning", "thinking", "tool-call", "tool_call",
                        "tool-result", "tool_result", "image", "image_url"):
                continue
            text = item.get("text") or item.get("content") or item.get("value")
            if isinstance(text, str) and text.strip():
                parts.append(text)
    return "\n".join(parts).strip()


#: Codex / dsh 会把运行时上下文注入成一条 "user" 消息，必须剔除
_INJECTED_PREFIXES = (
    "<environment_context>",
    "<app-context>",
    "<user_instructions>",
    "<INSTRUCTIONS",
    "<system-reminder>",
    "<permissions",
    "<skills>",
)


def is_injected_user_text(text: str) -> bool:
    """判断这条“用户消息”其实是工具注入的上下文，而非真人输入。"""
    head = (text or "").lstrip()[:64]
    return head.startswith(_INJECTED_PREFIXES)


def strip_long_path(value: str) -> str:
    """去掉 Windows 扩展长度前缀 `\\\\?\\`，便于分类与展示。"""
    text = (value or "").strip()
    for prefix in ("\\\\?\\", "\\??\\"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    if text.startswith("UNC"):
        text = text[3:]
    return text


def first_existing(*paths: Optional[Path]) -> Optional[Path]:
    """返回第一个存在的路径。"""
    for p in paths:
        if p and Path(p).exists():
            return Path(p)
    return None


def group_by_day(messages: List[tuple]) -> dict:
    """按北京时间自然日分组：[(ts, text)] -> {"YYYY-MM-DD": [(ts, text)]}。"""
    days: dict = {}
    for ts, text in messages:
        days.setdefault(to_bj(ts).date().isoformat(), []).append((ts, text))
    return days


def timeline(items: List[tuple], limit: int = 400, snippet: int = 60) -> str:
    """渲染成单行时间线：'14:41 xxx；14:43 yyy…'。

    超长时保留首尾（开头诉求与最新进展都要在），中间截断；
    完整正文留在 raw["messages"]。
    """
    parts = [
        f"{to_bj(ts).strftime('%H:%M')} {truncate(' '.join(str(text).split()), snippet)}"
        for ts, text in items
        if text
    ]
    joined = "；".join(parts)
    if len(joined) <= limit or len(parts) < 3:
        return truncate(joined, limit)
    first, last = parts[0], parts[-1]
    budget = limit - len(first) - len(last) - 1
    if budget <= 20:
        return truncate(joined, limit)
    return f"{first}；{truncate('；'.join(parts[1:-1]), budget)}；{last}"
