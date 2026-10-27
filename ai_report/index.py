"""L2 统一索引与分类：合并、去重、分类、按日期过滤、JSON 导出。

项目分类规则可外置：`projects.json`（见 projects.example.json）。
查找顺序：环境变量 AIREREPORT_PROJECTS > <数据目录>/projects.json > ~/.aireport/projects.json；
都没有时用下面内置的默认规则，因此新项目也能靠 cwd 目录名兜底归类。
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ai_report import trim as trimmer
from ai_report.connectors.registry import iter_available
from ai_report.models import UnifiedRecord
from ai_report.utils import BJ_TZ, fmt_bj, log, now_bj

#: 自定义项目规则文件的环境变量名
PROJECTS_FILE_ENV = "AIREREPORT_PROJECTS"

# ---- 项目分类规则：顺序匹配，命中即停（关键词 -> 项目名）----
# 下面只是内置的示例规则，可被 projects.json 覆盖（见 projects.example.json）。
PROJECT_MAP: List[Tuple[Tuple[str, ...], str]] = [
    (("copper-wire", "ffc"), "铜线检测项目"),
    (("hmi-console", "上位机"), "上位机项目"),
    (("terminal-detect", "端子"), "端子检测项目"),
    (("profile-flaw", "型材"), "型材检测项目"),
    (("wafer-flaw", "晶圆"), "晶圆检测项目"),
    (("pointcloud-change", "pcd_change", "点云"), "点云检测项目"),
    (("eeg-signal", "脑电"), "脑电项目"),
]

# WorkBuddy 等源 cwd 不是项目目录 → 用标题关键词补分类
TITLE_HINTS: Dict[str, str] = {
    "铜线": "铜线检测项目",
    "FFC": "铜线检测项目",
    "上位机": "上位机项目",
    "端子": "端子检测项目",
    "型材": "型材检测项目",
    "晶圆": "晶圆检测项目",
    "点云": "点云检测项目",
    "脑电": "脑电项目",
    "EEG": "脑电项目",
}

#: 规则缓存（进程内只读一次）
_RULES: Optional[Tuple[List[Tuple[Tuple[str, ...], str]], Dict[str, str]]] = None


def projects_file() -> Optional[Path]:
    """项目规则文件位置：环境变量 > 数据目录 projects.json > ~/.aireport/projects.json。"""
    env = os.environ.get(PROJECTS_FILE_ENV)
    if env:
        return Path(env)
    from ai_report import config
    for base in config.config_dirs():
        candidate = base / "projects.json"
        if candidate.is_file():
            return candidate
    return None


def _load_rules() -> Tuple[List[Tuple[Tuple[str, ...], str]], Dict[str, str]]:
    """读取用户自定义项目规则；文件缺失/损坏/为空时退回内置规则。"""
    path = projects_file()
    if not path:
        return PROJECT_MAP, TITLE_HINTS
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log(f"[分类] 读取 {path} 失败，改用内置规则：{exc}")
        return PROJECT_MAP, TITLE_HINTS

    rules: List[Tuple[Tuple[str, ...], str]] = []
    for item in (data.get("rules") or []):
        name = str(item.get("project") or "").strip()
        kws = tuple(str(k).strip() for k in (item.get("keywords") or []) if str(k).strip())
        if name and kws:
            rules.append((kws, name))
    hints = {str(k).strip(): str(v).strip()
             for k, v in (data.get("title_hints") or {}).items()
             if str(k).strip() and str(v).strip()}
    if not rules and not hints:
        return PROJECT_MAP, TITLE_HINTS
    return rules, hints


def _rules() -> Tuple[List[Tuple[Tuple[str, ...], str]], Dict[str, str]]:
    """带缓存的规则读取。"""
    global _RULES
    if _RULES is None:
        _RULES = _load_rules()
    return _RULES


# ---- 状态归一（大小写不敏感）----
STATUS_MAP: Dict[str, str] = {
    "completed": "已完成",
    "complete": "已完成",
    "done": "已完成",
    "working": "进行中",
    "in_progress": "进行中",
    "running": "进行中",
}

UNCATEGORIZED = "其他"

# ---- cwd 路径兜底分类的噪声段（系统目录/通用目录名，不算项目名）----
_SEG_NOISE = {
    "users", "administrator", "desktop", "downloads", "documents", "home",
    "project", "projects", "workspace", "work", "code", "src", "app", "appdata",
    "local", "roaming", "tmp", "temp", "opt", "usr", "bin", "etc", "data",
}
# 版本号样目录段：demo_V1.5 / app_v2.1（取上一级目录做项目名）
_SEG_VERSION = re.compile(r".*[_\-]v?\d+(\.\d+)+$", re.IGNORECASE)
_SEG_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")   # 2026-10-09 之类时间戳目录
_SEG_PURE_NUM = re.compile(r"^\d+$")            # 18 / 2026 之类数字段


def _seg_is_noise(seg: str) -> bool:
    """路径段是否不算项目名（系统目录 / 版本号 / 时间戳 / 纯数字）。"""
    s = seg.strip().lower()
    return (
        not s
        or s in _SEG_NOISE
        or bool(_SEG_VERSION.match(seg))
        or bool(_SEG_DATE.search(seg))
        or bool(_SEG_PURE_NUM.match(seg))
    )


def _cwd_fallback(cwd: str) -> str:
    """cwd 路径末段做项目名：d:/project/sop-demo -> sop-demo。

    从最后一段往前找第一个非噪声段；无路径分隔符（如裸 "random"）不兜底。
    """
    if not cwd or ("/" not in cwd and "\\" not in cwd):
        return ""
    for seg in reversed([s for s in re.split(r"[/\\]+", cwd) if s]):
        if not _seg_is_noise(seg):
            return seg
    return ""


def classify(project_hint: str = "", cwd: str = "", title: str = "") -> str:
    """把 hint/cwd/title 归类到项目名；未命中返回“其他”。

    优先级（用户 2026-10-18 修正）：cwd 是真实工作目录，最可信——
    先拿 cwd 匹配配置的 rules，匹配不到就用 cwd 路径末段兜底；
    只有 cwd 给不出项目名时（WorkBuddy 的会话目录、豆包无 cwd），
    才退回 project_hint / 标题关键词。否则会出现「本机工具的会话标题里提到别的项目，
    整条记录被归到那个项目」的错误归类。
    """
    rules, hints = _rules()

    haystack = " ".join(x for x in (cwd or "", project_hint or "") if x)
    for keywords, project in rules:
        for kw in keywords:
            if kw in haystack:
                return project
    fallback = _cwd_fallback(cwd)
    if fallback:
        return fallback

    # cwd 给不出项目名 → 再用标题关键词补分类
    for keywords, project in rules:
        for kw in keywords:
            if kw in (title or ""):
                return project
    for kw, project in hints.items():
        if kw in (title or ""):
            return project
    return UNCATEGORIZED


def normalize_status(status: str) -> str:
    """状态归一：completed->已完成，working->进行中，其余保留原文。"""
    status = (status or "").strip()
    if not status:
        return "-"
    return STATUS_MAP.get(status.lower(), status)


def _dedupe(records: Iterable[UnifiedRecord]) -> List[UnifiedRecord]:
    """按 (source, session_id) 去重（同源会话 id 唯一），保留先出现者。"""
    seen = set()
    result = []
    for rec in records:
        key = (rec.source, rec.session_id)
        if key in seen:
            continue
        seen.add(key)
        result.append(rec)
    return result


def _read_one(conn) -> List[UnifiedRecord]:
    """单个连接器的读取，异常不外抛（一个源坏了不该拖垮整份报告）。"""
    try:
        return conn.read()
    except Exception as exc:  # noqa: BLE001 - 连接器是第三方格式，容错优先
        log(f"[{conn.name}] 读取失败（已跳过）：{type(exc).__name__}: {exc}")
        return []


def collect() -> List[UnifiedRecord]:
    """遍历可用连接器，合并 → 分类 → 去重 → 按开始时间排序。

    顺手加了单一数据源的失败隔离：某个连接器解析崩了只丢它自己，不拖垮整份报告。

    注：这里**不**并发。实测（2026-10）串行 0.70s vs 线程池 0.69s——耗时集中在
    dsh / CodeBuddy CN / 豆包 的纯 Python 解析上，受 GIL 限制，线程换不来墙钟时间。
    真要提速得做「按文件指纹的持久化缓存」，见 README 的「已知限制」。
    """
    merged: List[UnifiedRecord] = []
    for conn in iter_available():
        merged.extend(_read_one(conn))

    for rec in merged:
        rec.project = classify(cwd=rec.cwd, title=rec.title)
        rec.status = normalize_status(rec.status)

    records = _dedupe(merged)
    records.sort(key=lambda r: (r.start_ts, r.end_ts))
    return records


def filter_by_date(
    records: Iterable[UnifiedRecord],
    date_: Optional[date] = None,
    start: Optional[date] = None,
    end: Optional[date] = None,
) -> List[UnifiedRecord]:
    """按北京时间筛选：start_ts 落在 [start, end]（含两端）内。

    - 只传 date_：筛选该自然日（北京时间）
    - 传 start/end：区间
    - 都不传：返回全部
    """
    if date_ is not None:
        start, end = date_, date_
    if start is None and end is None:
        return list(records)

    lo = _day_start_ts(start)
    hi = _day_end_ts(end)
    # 只收“工作发生在窗口内”的记录：
    # 1) 记录起始落在窗口内；或
    # 2) 窗口前创建、跨度 <= 1 天（跨夜短会话）且窗口内仍在活动。
    # 不收“多日前创建、仅被碰过一次”的陈年会话，防止别的时间段
    # 的内容混进当日日报（旧逻辑按区间交集，7 天前的会话也能进今天）。
    return [
        r for r in records
        if r.start_ts < hi and (
            r.start_ts >= lo
            or (r.end_ts >= lo and (r.end_ts - r.start_ts) <= 86400)
        )
    ]


def _day_start_ts(d: Optional[date]) -> float:
    if d is None:
        return 0.0
    dt = datetime(d.year, d.month, d.day, tzinfo=BJ_TZ)
    return dt.timestamp()


def _day_end_ts(d: Optional[date]) -> float:
    if d is None:
        return float("inf")
    dt = datetime(d.year, d.month, d.day, tzinfo=BJ_TZ) + timedelta(days=1)
    return dt.timestamp()


def group_by_project(records: Iterable[UnifiedRecord]) -> Dict[str, List[UnifiedRecord]]:
    """按项目分组，保留“其他”作为兜底组。"""
    groups: Dict[str, List[UnifiedRecord]] = {}
    for rec in records:
        groups.setdefault(rec.project or UNCATEGORIZED, []).append(rec)
    # 项目名按出现顺序输出，“其他”排最后
    ordered = {k: v for k, v in groups.items() if k != UNCATEGORIZED}
    if UNCATEGORIZED in groups:
        ordered[UNCATEGORIZED] = groups[UNCATEGORIZED]
    return ordered


def parse_day(value: Optional[str]) -> date:
    """YYYY-MM-DD -> date；空值取今天（北京时间）。"""
    if not value:
        return now_bj().date()
    return datetime.strptime(value, "%Y-%m-%d").date()


def select_records(mode: str = "daily", date_value: Optional[str] = None,
                   start_value: Optional[str] = None,
                   end_value: Optional[str] = None) -> List[UnifiedRecord]:
    """按模式取窗口内的记录（北京时间）：daily / weekly / range。

    MCP 工具与本地引导页共用这一份窗口逻辑，避免两处规则漂移。
    """
    if mode == "range":
        if not (start_value and end_value):
            raise ValueError("range 模式需要同时提供 start 和 end")
        return filter_by_date(collect(), start=parse_day(start_value),
                              end=parse_day(end_value))
    day = parse_day(date_value)
    if mode == "weekly":
        monday = day - timedelta(days=day.weekday())
        return filter_by_date(collect(), start=monday, end=monday + timedelta(days=6))
    if mode != "daily":
        raise ValueError(f"未知 mode：{mode}（应为 daily/weekly/range）")
    return filter_by_date(collect(), date_=day)


def record_id(rec: UnifiedRecord) -> str:
    """记录稳定 id（引导页勾选后回传用），形如 `CodeBuddy CN|9f3c…:2026-10-16`。"""
    return f"{rec.source}|{rec.session_id}"


def records_payload(records: Iterable[UnifiedRecord], detail: str = "full",
                    project: Optional[str] = None,
                    ids: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """按项目输出素材，供 AI 或引导页使用；含稳定 id 便于按勾选过滤。

    - detail="full"：返回 raw["messages"] 逐条原文（AI 归纳需要）
    - detail="summary"：只返回单行时间线（引导页卡片墙只需要这个）
    - ids：只保留这些 id 对应的记录（引导页勾选结果回传）

    已做时间剪辑（见 trim.py）的素材在这里就地生效：窗口外的片段被丢弃，
    并回传 `trim` 字段；若窗口内一条不剩，full 明细里直接不再出现（summary
    视图仍保留该条并标 `trimmed_out`，供引导页撤销剪辑）。
    """
    want_full = (detail or "full") == "full"
    only = (project or "").strip()
    keep = set(ids) if ids else None
    trims = trimmer.load_trims()

    groups = group_by_project(records)
    projects: List[Dict[str, Any]] = []
    kept = 0
    for name, items in groups.items():
        if only and name != only:
            continue
        rows: List[Dict[str, Any]] = []
        for r in sorted(items, key=lambda x: x.start_ts or 0):
            rid = record_id(r)
            if keep is not None and rid not in keep:
                continue
            win = trims.get(rid)
            entry: Dict[str, Any] = {
                "id": rid,
                "source": r.source,
                "time": f"{fmt_bj(r.start_ts, '%H:%M')}~{fmt_bj(r.end_ts, '%H:%M')}",
                "title": r.title,
                "status": r.status,
                "cwd": r.cwd,
            }
            msgs = r.raw.get("messages") if isinstance(r.raw, dict) else None
            if want_full and msgs:
                # 有逐条原文就用原文（AI 需要完整上下文才能归纳）。
                # role 一并给出去：user 是诉求，assistant 是结论/改动，归纳时要能区分。
                items_msgs = [
                    {"time": fmt_bj(m.get("ts", 0), "%H:%M"),
                     "role": str(m.get("role") or "user"),
                     "text": m.get("text", "")}
                    for m in msgs
                    if str(m.get("text") or "").strip()
                ]
                if win:
                    items_msgs = [m for m in items_msgs
                                  if trimmer.keep_time(m["time"], win["start"], win["end"])]
                entry["messages"] = items_msgs
            else:
                entry["summary"] = (trimmer.clip_text(r.summary, win["start"], win["end"])
                                    if win else r.summary)
            if win:
                entry["trim"] = dict(win)
                if not (entry.get("messages") or entry.get("summary")):
                    if want_full:
                        continue  # 窗口内没有剩余内容 → 等同未勾选，不给 AI
                    entry["trimmed_out"] = True
            rows.append(entry)
        kept += len(rows)
        projects.append({"project": name, "count": len(rows), "records": rows})
    return {"total_records": kept, "projects": projects}


def to_json(records: Iterable[UnifiedRecord], ensure_ascii: bool = False) -> str:
    """导出为 JSON 字符串（UTF-8 语义），便于缓存与调试。"""
    return json.dumps(
        [r.to_dict() for r in records], ensure_ascii=ensure_ascii, indent=2
    )


def from_json(text: str) -> List[UnifiedRecord]:
    """从 JSON 字符串还原记录列表。"""
    data = json.loads(text)
    return [UnifiedRecord.from_dict(d) for d in data]
