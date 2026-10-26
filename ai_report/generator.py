"""L3 报告生成器：把记录整理成「骨架 + 槽位」，再由程序统一排版。

结构由谁定（用户 2026-10-18 要求「程序强制排版 + 格式校验」）：
- 报告标题、项目分节的**数量与顺序**、条目位置，**只由本程序决定**（build_outline）；
- 归纳器（llm.py）只被允许改写条目的「标题 / 正文」两段文字，改写结果还要过
  `check_outline` 的结构与格式校验，不过就退回本程序渲染的草稿——所以排版不会漂移，
  条目也不可能被 AI 增删或编造。

排版口径（对齐工作汇报习惯，简洁优先）：
  项目（## 一、二、…）→ 工作事项（### 序号. 标题）→ 具体动作
条目下**不写**「时间 / 来源 / 状态」等元信息行（用户 2026-10-18 明确要求去掉），
来源只在报告头部统一标注一行。只渲染当天真实有记录的项目，没有记录的项目不出空节。
"""

from __future__ import annotations

import argparse
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ai_report import index
from ai_report.models import UnifiedRecord
from ai_report.utils import now_bj, truncate

FOOTER = (
    "---\n"
    "说明：数据来自本地会话文件；“具体动作”为用户当时的诉求，"
    "“→”后为 AI 当时给出的结论或改动（有 assistant 正文的源才有）。"
)

_CN_NUM = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十"]

# 带圈序号（模板学到 circled 事项编号时使用，与 template.py 的 CIRCLED 保持一致）
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"

# 事项标题清洗：图片/附件引用与终端日志噪声（这些正文留在“具体动作”里）
_IMG_REF = re.compile(
    r"@(?:image|d|[a-z]):\S+|@\S+\.(?:png|jpg|jpeg|bmp)\b|\[image\]",
    re.IGNORECASE,
)
_LOG_NOISE = re.compile(r"={3,}|\[start\]|Traceback|\[INFO\]|\[WARNING\]|~~~")
_TITLE_LIMIT = 60

# 无信息量的口头指令（从时间线里挑标题时跳过）
_TIME_PREFIX = re.compile(r"^\d{2}:\d{2}\s+")
_BOILER = re.compile(r"^(请继续执行未完成的任务|继续|好的|嗯|现在)[。！!？?…\s]*$")

# 归纳结果里绝不允许出现的残留（格式校验用；命中即判定跑偏）
_FORBIDDEN: Tuple[Tuple[re.Pattern, str], ...] = (
    (re.compile(r"(?<!\d)\d{1,2}:\d{2}(?!\d)"), "还留着时间戳"),
    (re.compile(r"\[图片\]|\[image\]", re.IGNORECASE), "还留着 [图片] 占位"),
    (re.compile(r"https?://", re.IGNORECASE), "还留着 URL"),
    (re.compile(r"(?:^|[；;\n])\s*(?:时间|来源|状态)\s*[：:]"), "还写着元信息行"),
    (re.compile(r"={3,}|Traceback|\[ERROR\]|\[WARN"), "还留着日志/堆栈噪声"),
    (re.compile(r"```"), "带了代码块围栏"),
)


def _clean_text(text: str) -> str:
    """去掉图片/附件引用与多余空白，压缩为单行。"""
    return " ".join(_IMG_REF.sub("[图片]", text or "").split())


def _clean_timeline(text: str) -> str:
    """清洗多段式时间线：剔除"请继续执行未完成的任务"等无信息量口头指令。

    同时去掉每段开头的 `13:30` 这类时间戳——条目正文要简洁（用户 2026-10-18 要求
    删掉时间信息），而且保留时间戳会让「同一件事、两条会话」因为时间不同而无法合并。
    """
    kept = []
    for seg in _clean_text(text).split("；"):
        s = _TIME_PREFIX.sub("", seg).strip()
        if not s or s == "[图片]" or _BOILER.match(s):
            continue
        kept.append(s)
    return "；".join(kept)


def _first_meaningful(summary: str) -> str:
    """从时间线摘要里取第一条有信息量的用户输入（跳过"继续"类口头指令）。"""
    for seg in (summary or "").split("；"):
        s = _TIME_PREFIX.sub("", _clean_text(seg)).strip()
        if not s or s == "[图片]" or _BOILER.match(s):
            continue
        return s
    return ""


def _display_title(rec: UnifiedRecord) -> str:
    """事项标题：清洗图片引用；标题是日志噪声/空时退回摘要首条实质输入。"""
    title = re.sub(r"^(?:\[图片\]\s*)+", "", _clean_text(rec.title)).strip()
    if not title or _LOG_NOISE.search(title):
        title = _first_meaningful(rec.summary) or title
    return truncate(title, _TITLE_LIMIT)


def _parse_day(value: Optional[str]) -> date:
    """解析 YYYY-MM-DD，默认今天（北京时间）。"""
    if not value:
        return now_bj().date()
    return datetime.strptime(value, "%Y-%m-%d").date()


def _load(records: Optional[Iterable[UnifiedRecord]]) -> List[UnifiedRecord]:
    """测试可注入记录；生产路径走 index.collect()（读本地库）。"""
    return list(records) if records is not None else index.collect()


def _sources_line(records: List[UnifiedRecord]) -> str:
    """报告头部的数据来源说明。"""
    seen = []
    for rec in records:
        if rec.source not in seen:
            seen.append(rec.source)
    names = " / ".join(seen) if seen else "（本期无数据）"
    return f"> 数据来源：{names}（本地聚合，隐私优先）"


# ---- 骨架与槽位 ----


def _norm_key(text: str) -> str:
    """条目去重用的归一化文本：去掉时间戳、[图片] 占位、重复片段与全部空白。

    时间戳必须去掉——同一件事在两条会话里被问过，时间往往不同（13:30 / 13:37），
    带上时间就没法判定为重复（用户 2026-10-18 反馈的「重复条目」正是这么来的）。
    """
    parts = [p.strip() for p in re.split(r"[；;]", (text or "").replace("[图片]", " "))
             if p.strip()]
    uniq: List[str] = []
    for part in parts:
        part = _TIME_PREFIX.sub("", part).strip()
        if part and part not in uniq:
            uniq.append(part)
    return " ".join(" ".join(uniq).split()).lower()


def _slot_key(item: Dict[str, str]) -> str:
    """条目去重键：优先看当日素材，素材过短时退回标题；都太短则不去重。"""
    body = _norm_key(item.get("body") or "")
    if len(body) >= 6:
        return "b:" + body
    title = _norm_key(item.get("title") or "")
    return ("t:" + title) if len(title) >= 6 else ""


def _assistant_hint(rec: UnifiedRecord, limit: int = 120) -> str:
    """从 raw["messages"] 里取 AI 侧的要点，附在「具体动作」后面。

    模板直出（不配归纳引擎）时，只写用户输入会看不出结果；有 assistant 正文就说明
    「做了什么」。没有正文的数据源（WorkBuddy / Kimi / 豆包）自然返回空串，行为不变。
    """
    raw = rec.raw if isinstance(rec.raw, dict) else {}
    msgs = raw.get("messages")
    if not isinstance(msgs, list):
        return ""
    texts = [str(m.get("text") or "") for m in msgs
             if isinstance(m, dict) and str(m.get("role") or "") == "assistant"]
    texts = [t for t in texts if t.strip()]
    if not texts:
        return ""
    # 取最后一条：通常是最直接的结论/改动说明
    return _clean_timeline(truncate(" ".join(texts[-1].split()), limit))


def _slots(recs: List[UnifiedRecord]) -> List[Dict[str, str]]:
    """记录 → 条目槽位（title/body 两段文字）。

    两类整理在这里做掉，避免它们变成 AI 的「加戏」空间：
    - 该日素材只剩「继续」这类口头指令（清洗后为空）→ 这条当日无实质内容，不出现；
    - 同一项目内当日素材实质相同的条目（同会话跨天拆分、同一问题问两遍）→ 只留第一条。
    """
    out: List[Dict[str, str]] = []
    seen = set()
    for rec in recs:
        body = _clean_timeline(rec.summary)
        hint = _assistant_hint(rec)
        if hint and hint not in body:
            body = f"{body}；→ {hint}".strip("；")
        if (rec.summary or "").strip() and not body:
            continue
        item = {"title": _display_title(rec) or "(无标题)", "body": body}
        key = _slot_key(item)
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(item)
    return out


def build_outline(title: str, records: Iterable[UnifiedRecord]) -> Dict[str, Any]:
    """把记录整理成结构化骨架：报告标题 + 按项目分节的条目槽位（顺序即报告顺序）。"""
    recs = list(records)
    outline: Dict[str, Any] = {
        "title": title,
        "sources": _sources_line(recs),
        "sections": [],
    }
    if not recs:
        return outline
    for project, items in index.group_by_project(recs).items():
        slots = _slots(items)
        if slots:
            outline["sections"].append({"name": project, "items": slots})
    return outline


# ---- 排版（只由本程序执行） ----


def _render_record(no: int, item: Dict[str, str], style: Optional[Dict] = None,
                   short: bool = False) -> List[str]:
    """渲染一条工作事项：事项标题（+ 具体动作）。

    short=True 是简洁版：只保留标记与事项名，删掉说明（对齐模板里
    「简洁版只保留标记与事项名」的口径）。
    条目下不写「时间 / 来源 / 状态」等元信息行，保持报告简洁；
    style 来自「格式克隆」学到的模板（见 template.py），缺省时用本生成器的默认排版。
    """
    item_title = item.get("title") or "(无标题)"
    body = item.get("body") or ""
    kind = (style or {}).get("item_style") or "hash3"
    has_detail = bool((style or {}).get("item_has_detail"))
    detail = "" if short else (body if (body and body != item_title) else "")

    if kind == "circled":
        head = f"{CIRCLED[no - 1] if no <= len(CIRCLED) else no}、{item_title}"
        lines = [head + (f"：{detail}" if detail and has_detail else "")]
        if detail and not has_detail:
            lines.append(f"  {detail}")
    elif kind == "dash":
        lines = [f"- {item_title}" + (f"：{detail}" if detail else "")]
    elif kind == "numeric":
        lines = [f"{no}. {item_title}" + (f"：{detail}" if detail and has_detail else "")]
        if detail and not has_detail:
            lines.append(f"   {detail}")
    else:  # hash3：默认排版
        lines = [f"### {no}. {item_title}"]
        if detail:
            lines.append(f"- 具体动作：{detail}")

    lines.append("")
    return lines


def _render_section(heading: str, items: List[Dict[str, str]],
                    style: Optional[Dict] = None, short: bool = False) -> List[str]:
    """渲染一个项目小节：标题 + 逐条工作事项。"""
    lines = [heading, ""]
    for i, item in enumerate(items, 1):
        lines += _render_record(i, item, style, short)
    return lines


def _project_heading(n: int, project: str, style: Optional[Dict]) -> str:
    """项目行：按学到的模板排版；没有模板时沿用 `## 一、项目` 的默认写法。"""
    kind = (style or {}).get("project_kind")
    punct = (style or {}).get("project_punct") or "、"
    if kind == "bold-arabic":
        return f"**{n}{punct}{project}：**"
    if kind == "hash":
        return f"## {n}{punct}{project}"
    if kind == "plain-arabic":
        return f"{n}{punct}{project}："
    if kind == "bold-name-only":
        return f"**{project}：**"
    label = _CN_NUM[n - 1] if n <= len(_CN_NUM) else str(n)
    return f"## {label}、{project}"


def _render_body(outline: Dict[str, Any], style: Optional[Dict],
                 short: bool = False) -> str:
    """渲染报告主体（不含简洁版追加）：按项目分节 + 未分类。"""
    title = outline.get("title", "")
    if short:
        title += "（简洁版）"
    lines: List[str] = [f"# {title}", outline.get("sources", ""), ""]
    sections = outline.get("sections") or []

    n = 0
    total = 0
    for sec in sections:
        name = sec.get("name") or index.UNCATEGORIZED
        items = sec.get("items") or []
        if not items:
            continue
        total += len(items)
        if name == index.UNCATEGORIZED:
            lines += _render_section("## 未分类", items, style, short)
        else:
            n += 1
            lines += _render_section(_project_heading(n, name, style), items, style, short)

    if not total:
        lines += ["> 本期没有符合条件的 AI 会话记录。", ""]
    return "\n".join(lines)


def render_outline(outline: Dict[str, Any], style: Optional[Dict] = None) -> str:
    """按模板样式渲染骨架 → Markdown。排版口径只由本函数决定。

    模板要求双版本（`short_version=True`）时，在详细版之后用 `---` 分隔追加简洁版：
    简洁版只保留项目行与事项名（`①` 后面不带说明）。
    """
    md = _render_body(outline, style) + "\n" + FOOTER
    if (style or {}).get("short_version"):
        md += "\n\n---\n\n" + _render_body(outline, style, short=True)
    return md


# ---- 格式校验（归纳结果必须过这一关，否则退回草稿） ----


def check_outline(candidate: Any, reference: Dict[str, Any]) -> List[str]:
    """校验归纳结果：结构与格式必须与骨架一致。返回问题清单（空 = 通过）。

    结构校验是「程序强制排版」的关键——AI 只能改文字，不能增删项目/条目、
    不能改项目名、不能调换顺序；文字里也不许残留时间戳、[图片]、URL、日志、元信息行。
    """
    problems: List[str] = []
    if not isinstance(candidate, dict):
        return ["归纳结果不是结构化的“项目 / 条目”结构"]
    ref_sections = reference.get("sections") or []
    cand_sections = candidate.get("sections")
    if not isinstance(cand_sections, list):
        return ["归纳结果缺少 sections"]

    if len(cand_sections) != len(ref_sections):
        return [f"项目分节数不一致（应交 {len(ref_sections)} 个，实为 {len(cand_sections)} 个）"]

    for i, (ref_sec, cand_sec) in enumerate(zip(ref_sections, cand_sections), 1):
        name = ref_sec.get("name") or ""
        if not isinstance(cand_sec, dict):
            problems.append(f"第 {i} 个项目不是结构化条目")
            continue
        if str(cand_sec.get("name") or "") != name:
            problems.append(f"第 {i} 个项目名被改动（应保持「{name}」）")
        ref_items = ref_sec.get("items") or []
        cand_items = cand_sec.get("items")
        if not isinstance(cand_items, list) or len(cand_items) != len(ref_items):
            got = len(cand_items) if isinstance(cand_items, list) else 0
            problems.append(f"「{name}」条目数不一致（应交 {len(ref_items)} 条，实为 {got} 条）")
            continue
        for j, (ref_item, cand_item) in enumerate(zip(ref_items, cand_items), 1):
            where = f"「{name}」第 {j} 条"
            if not isinstance(cand_item, dict):
                problems.append(f"{where}不是结构化条目")
                continue
            title = str(cand_item.get("title") or "").strip()
            body = str(cand_item.get("body") or "").strip()
            if not title:
                problems.append(f"{where}标题为空")
            if (ref_item.get("body") or "").strip() and not body:
                problems.append(f"{where}正文被清空")
            for text in (title, body):
                for pattern, why in _FORBIDDEN:
                    if pattern.search(text):
                        problems.append(f"{where}{why}")
                        break
    return problems


# ---- 对外入口 ----


def outline_daily(date_: Optional[date] = None,
                  records: Optional[Iterable[UnifiedRecord]] = None) -> Dict[str, Any]:
    """指定日期（北京时间）日报的骨架。"""
    d = date_ or _parse_day(None)
    return build_outline(f"AI 日报 {d.isoformat()}",
                         index.filter_by_date(_load(records), date_=d))


def outline_weekly(week_start: Optional[date] = None,
                   records: Optional[Iterable[UnifiedRecord]] = None) -> Dict[str, Any]:
    """周报骨架：week_start 所在周的周一 ~ 周日（北京时间）。"""
    d = week_start or _parse_day(None)
    monday = d - timedelta(days=d.weekday())
    sunday = monday + timedelta(days=6)
    return build_outline(f"AI 周报 {monday.isoformat()} ~ {sunday.isoformat()}",
                         index.filter_by_date(_load(records), start=monday, end=sunday))


def outline_range(start: date, end: date,
                  records: Optional[Iterable[UnifiedRecord]] = None) -> Dict[str, Any]:
    """自定义区间报告骨架。"""
    return build_outline(f"AI 报告 {start.isoformat()} ~ {end.isoformat()}",
                         index.filter_by_date(_load(records), start=start, end=end))


def render_daily(date_: Optional[date] = None,
                 records: Optional[Iterable[UnifiedRecord]] = None,
                 style: Optional[Dict] = None) -> str:
    """渲染指定日期（北京时间）的日报（纯模板）。"""
    return render_outline(outline_daily(date_, records), style)


def render_weekly(week_start: Optional[date] = None,
                  records: Optional[Iterable[UnifiedRecord]] = None,
                  style: Optional[Dict] = None) -> str:
    """渲染周报（纯模板）。"""
    return render_outline(outline_weekly(week_start, records), style)


def render_range(start: date, end: date,
                 records: Optional[Iterable[UnifiedRecord]] = None,
                 style: Optional[Dict] = None) -> str:
    """渲染自定义区间报告（纯模板）。"""
    return render_outline(outline_range(start, end, records), style)


def main(argv: Optional[List[str]] = None) -> int:
    """独立运行入口：python generator.py --mode daily --date 2026-10-10"""
    parser = argparse.ArgumentParser(description="AI 日报/周报生成器（纯模板）")
    parser.add_argument("--mode", choices=["daily", "weekly", "range"], default="daily")
    parser.add_argument("--date", help="YYYY-MM-DD（daily 为报告日，weekly 为周内任意日）")
    parser.add_argument("--start", help="区间起始 YYYY-MM-DD（--mode range）")
    parser.add_argument("--end", help="区间结束 YYYY-MM-DD（--mode range）")
    parser.add_argument("--out", help="输出文件路径（UTF-8），缺省打印到 stdout")
    args = parser.parse_args(argv)

    if args.mode == "daily":
        md = render_daily(_parse_day(args.date))
    elif args.mode == "weekly":
        base = _parse_day(args.date)
        md = render_weekly(base)
    else:
        if not (args.start and args.end):
            parser.error("--mode range 需要同时提供 --start 和 --end")
        md = render_range(
            datetime.strptime(args.start, "%Y-%m-%d").date(),
            datetime.strptime(args.end, "%Y-%m-%d").date(),
        )

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(md)
    else:
        import sys
        sys.stdout.reconfigure(encoding="utf-8") if hasattr(sys.stdout, "reconfigure") else None
        sys.stdout.write(md + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
