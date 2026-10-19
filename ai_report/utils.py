"""时间 / 路径 / 日志工具（全模块共用，仅标准库）。

约定：
- 时间戳可能是毫秒或秒混用：epoch > 1e11 视为毫秒，统一除以 1000 转秒
- 展示统一北京时间（UTC+8），格式 YYYY-MM-DD HH:MM
- 文件 IO 与日志统一 UTF-8
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

# 北京时间（UTC+8）
BJ_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")

# 毫秒/秒分界：1e11 秒约为公元 5138 年，1e11 毫秒约为 1973 年，用 1e11 区分安全
_MS_THRESHOLD = 1e11


def parse_ts(value: Union[int, float, str, None]) -> float:
    """把毫秒/秒混用的时间戳统一解析为 epoch 秒（float）。非法值返回 0.0。"""
    if value is None:
        return 0.0
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return 0.0
    if ts <= 0:
        return 0.0
    if ts > _MS_THRESHOLD:
        ts /= 1000.0
    return ts


def to_bj(ts: float) -> datetime:
    """epoch 秒 -> 北京时间 datetime。"""
    return datetime.fromtimestamp(ts, tz=BJ_TZ)


def fmt_bj(ts: float, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """epoch 秒 -> 北京时间格式化字符串。"""
    if not ts:
        return "--"
    return to_bj(ts).strftime(fmt)


def now_bj() -> datetime:
    """当前北京时间。"""
    return datetime.now(tz=BJ_TZ)


def date_range_bj(start_ts: float, end_ts: float) -> str:
    """渲染记录的起止时间段，如 '2026-10-09 14:30 ~ 15:02'。"""
    if not start_ts and not end_ts:
        return "--"
    if not end_ts or end_ts == start_ts:
        return fmt_bj(start_ts)
    return f"{fmt_bj(start_ts)} ~ {fmt_bj(end_ts)}"


def log(msg: str) -> None:
    """统一日志输出（stderr，UTF-8），避免污染 stdout 的报告内容。"""
    try:
        sys.stderr.write(msg.rstrip("\n") + "\n")
        sys.stderr.flush()
    except Exception:
        pass  # 日志失败不应影响主流程


def safe_str(value: Optional[str], default: str = "") -> str:
    """把可能为 None 的字段转成安全字符串。"""
    if value is None:
        return default
    return str(value)


def truncate(text: str, limit: int) -> str:
    """按字符数截断（保留完整语义的省略号），用于钉钉字段上限。"""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"
