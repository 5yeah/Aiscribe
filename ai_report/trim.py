"""素材级时间剪辑：按起止时间丢掉窗口外的对话片段。

为什么在工具侧做：取数与一切确定性数据处理是本工具的职责，宿主 AI 只负责阅读归纳。
剪辑生效后，窗口外的片段根本不会出现在 get_conversations 的返回里，
所以 AI 写出来的报告不可能带进被剪掉的内容——不需要靠“提醒 AI 别用”来兜底。

时段口径（北京时间）：
- 素材摘要按「；」分段，段首可能带 `HH:MM(:SS)`；**无时间戳的段一律保留**
- 窗口 `[start, end]`：`end < start` 视为跨夜（如 22:30 ~ 02:00）
- 只给一端时另一端按 `00:00` / `23:59` 补全
- 逐条消息（`messages[].time`）同样按窗口过滤

存储：`<数据目录>/trims.json`（环境变量 `AIREREPORT_TRIMS` 可覆盖；已 gitignore）。
源码运行时数据目录是仓库根；安装态/sidecar 是 `~/.aireport`。
键是记录稳定 id（`f"{source}|{session_id}"`），值是 `{"start": "HH:MM", "end": "HH:MM"}`。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ai_report import config
from ai_report.utils import fmt_bj, log

#: 剪辑文件位置的环境变量名
TRIMS_FILE_ENV = "AIREREPORT_TRIMS"

_DAY = 24 * 60

#: 用户输入必须是 HH:MM（24 小时制）
_INPUT_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
#: 素材里的段首时间戳可能是 HH:MM 或 HH:MM:SS
_CLOCK_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?")


def trims_file() -> Path:
    """剪辑文件位置：环境变量 > 数据目录 trims.json。"""
    env = os.environ.get(TRIMS_FILE_ENV)
    return Path(env) if env else config.data_dir() / "trims.json"


def normalize_time(value: Any) -> str:
    """校验并归一 `HH:MM`（`9:05` -> `09:05`）；空值返回空串；非法抛 ValueError。"""
    text = str(value or "").strip()
    if not text:
        return ""
    m = _INPUT_RE.match(text)
    if not m:
        raise ValueError(f"时间格式应为 HH:MM（24 小时制）：{value!r}")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _minutes(text: Any) -> Optional[int]:
    """`HH:MM(:SS)` -> 当日分钟数；解析不出返回 None。"""
    m = _CLOCK_RE.match(str(text or ""))
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def _bounds(start: str, end: str) -> tuple:
    """窗口上下界（分钟）：只给一端时另一端补 00:00 / 23:59。"""
    lo = _minutes(start) if start else 0
    hi = _minutes(end) if end else _DAY - 1
    return lo, hi


def _in_window(minutes: int, lo: int, hi: int) -> bool:
    """是否落在窗口内；hi < lo 视为跨夜窗口。"""
    if hi < lo:
        return minutes >= lo or minutes <= hi
    return lo <= minutes <= hi


def window(start: Any, end: Any) -> Optional[Dict[str, str]]:
    """归一化的剪辑窗口；两端都空返回 None（表示不剪辑）。"""
    s = normalize_time(start)
    e = normalize_time(end)
    return {"start": s, "end": e} if (s or e) else None


def clip_text(text: str, start: str, end: str) -> str:
    """按窗口丢弃“带时间戳且落在窗口外”的片段；无时间戳的段保留。"""
    if not (start or end):
        return text or ""
    lo, hi = _bounds(start, end)
    kept: List[str] = []
    for seg in (text or "").split("；"):
        seg = seg.strip()
        if not seg:
            continue
        minutes = _minutes(seg)
        if minutes is None or _in_window(minutes, lo, hi):
            kept.append(seg)
    return "；".join(kept)


def keep_time(hhmm: Any, start: str, end: str) -> bool:
    """某个时刻是否落在窗口内（供逐条消息过滤）；解析不出时刻的视为保留。"""
    if not (start or end):
        return True
    minutes = _minutes(hhmm)
    if minutes is None:
        return True
    lo, hi = _bounds(start, end)
    return _in_window(minutes, lo, hi)


def clip_messages(messages: List[Any], start: str, end: str) -> List[Any]:
    """按窗口过滤逐条消息；时间戳解析不出的消息保留。

    UI 的「时间剪辑」原先只裁 summary，generator 仍能从 raw["messages"] 取到
    窗口外的 assistant 正文。这里对原始消息做同一套过滤，避免剪辑泄漏。
    """
    if not (start or end) or not isinstance(messages, list):
        return messages
    kept: List[Any] = []
    for msg in messages:
        if not isinstance(msg, dict):
            kept.append(msg)
            continue
        clock = None
        ts = msg.get("ts")
        if isinstance(ts, (int, float)) and ts > 0:
            clock = fmt_bj(ts, "%H:%M")
        elif msg.get("time"):
            clock = str(msg.get("time"))
        if clock and not keep_time(clock, start, end):
            continue
        kept.append(msg)
    return kept


# ---- 存取（trims.json）----

def load_trims() -> Dict[str, Dict[str, str]]:
    """读取全部剪辑；文件缺失/损坏时返回空表（不中断主流程）。"""
    path = trims_file()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log(f"[剪辑] 读取 {path} 失败，按无剪辑处理：{exc}")
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, Dict[str, str]] = {}
    for rid, win in data.items():
        if not isinstance(win, dict):
            continue
        start = str(win.get("start") or "").strip()
        end = str(win.get("end") or "").strip()
        if start or end:
            out[str(rid)] = {"start": start, "end": end}
    return out


def save_trims(data: Dict[str, Dict[str, str]]) -> Path:
    """整体写回剪辑表。"""
    path = trims_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def get_trim(rid: str) -> Optional[Dict[str, str]]:
    """单条素材的剪辑窗口；没有则返回 None。"""
    return load_trims().get((rid or "").strip())


def set_trim(rid: str, start: Any = "", end: Any = "") -> Optional[Dict[str, str]]:
    """写入/更新一条素材的剪辑；两端都空 = 清除该条剪辑。返回新窗口（清除时 None）。"""
    rid = (rid or "").strip()
    if not rid:
        raise ValueError("id 不能为空")
    win = window(start, end)
    data = load_trims()
    if win is None:
        data.pop(rid, None)
    else:
        data[rid] = win
    save_trims(data)
    log(f"[剪辑] {rid} -> {win or '已清除'}")
    return win


def clear_trim(rid: str) -> bool:
    """清除一条素材的剪辑；返回是否确实清掉了一条。"""
    data = load_trims()
    if (rid or "").strip() not in data:
        return False
    data.pop((rid or "").strip(), None)
    save_trims(data)
    return True


def clear_all() -> int:
    """清空全部剪辑；返回被清掉的条数。"""
    data = load_trims()
    if data:
        save_trims({})
    return len(data)


def count() -> int:
    """当前剪辑条数（供引导页展示）。"""
    return len(load_trims())
