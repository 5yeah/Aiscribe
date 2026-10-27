"""本地引导页服务：标准库 HTTP 服务 + 单页引导（只监听 127.0.0.1，数据不出本机）。

启动：
  python -m ai_report ui                  # 默认 5071，自动打开浏览器
  python -m ai_report ui --port 5080 --no-open

路由：
  GET  /                    引导页（ai_report/web/index.html）
  GET  /browse              历史会话浏览页（ai_report/web/browse.html）
  GET  /api/sources         源清单（名称 / 内容等级 / 是否可用），不读数据，很快
  GET  /api/source?name=X   单个源的真实扫描结果（可用性 + 会话数），供逐源点亮
  GET  /api/browse          全部历史会话：筛选（source/project/q/start/end）+ 分页 + 左栏计数
  GET  /api/session?id=X    单条会话详情（含 raw["messages"] 逐条正文）
  GET  /api/conversations   按项目分组的素材（含稳定 id），供卡片墙勾选
  GET  /api/templates       已学格式模板清单（供“格式克隆”下拉）
  GET  /api/template?name=X 单个模板的规范文本 + 图片提示（供预览）
  POST /api/instruction     依据勾选结果生成“交给 AI 的指令”文本（可带模板）
  POST /api/learn           上传样例（base64）→ 抽排版结构 → 存成可复用模板
  POST /api/trim            给单条素材设时间剪辑（空值 = 清除该条；clear=true 清空全部）
  POST /api/generate        本机直接出报告：按勾选 + 剪辑过滤素材，渲染并按需 LLM 归纳后落盘
  GET  /api/report?name=X   取回 reports/ 里的成品报告（供等待页打开/下载）
  GET  /api/llm             归纳能力状态（provider / 模型 / 是否已配置 key），供等待页如实展示

设计边界：本页负责「展示 + 选择 + 预览」，并提供两条产出路径 ——
① `POST /api/generate`：本机直接出报告；默认在此基础上再调一次 LLM 归纳（等价 CLI 的
   `--llm`，只上传已汇总草稿、不上传原始对话），未配置 key 或调用失败则如实退回纯模板草稿；
② `POST /api/instruction`：拼出「交给 AI 的指令」，由客户端 AI 依据
   skills/ai-report/SKILL.md 读原文归纳（质量更高，取数走 MCP 的 get_conversations）。
"""

from __future__ import annotations

import base64
import json
import time
import webbrowser
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, quote, urlparse

from ai_report import cli, config, generator, index, llm, template
from ai_report import trim as trimmer
from ai_report.connectors.registry import REGISTRY
from ai_report.template import image_hint
from ai_report.utils import fmt_bj, log

WEB_DIR = Path(__file__).resolve().parent / "web"
INDEX_HTML = WEB_DIR / "index.html"
BROWSE_HTML = WEB_DIR / "browse.html"

DEFAULT_PORT = 5071

#: 浏览接口的进程内缓存：全量读取一次约 0.5~1 秒，翻页/搜索不该每次都重读
_BROWSE_CACHE: Dict[str, Any] = {"ts": 0.0, "records": []}
_BROWSE_TTL = 30.0
_BROWSE_PAGE = 60


def _content_levels() -> Dict[str, str]:
    """数据源名 -> 内容等级（浏览页用来标注「这条能拿到多少正文」）。"""
    return {c.name: c.content_level for c in REGISTRY}


def browse_records(refresh: bool = False) -> List[index.UnifiedRecord]:
    """全量会话（带 30 秒缓存）；refresh=True 强制重读本地文件。"""
    fresh = time.time() - _BROWSE_CACHE["ts"] < _BROWSE_TTL and _BROWSE_CACHE["records"]
    if refresh or not fresh:
        _BROWSE_CACHE["records"] = index.collect()
        _BROWSE_CACHE["ts"] = time.time()
    return _BROWSE_CACHE["records"]


def _message_list(rec: "index.UnifiedRecord") -> List[Dict[str, Any]]:
    """取原始逐条消息；兼容各家 raw 结构差异（有的只给 summary）。"""
    raw = rec.raw if isinstance(rec.raw, dict) else {}
    msgs = raw.get("messages")
    if not isinstance(msgs, list):
        return []
    out = []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        text = str(m.get("text") or "").strip()
        if not text:
            continue
        out.append({
            "ts": float(m.get("ts") or 0.0),
            "time": fmt_bj(m.get("ts") or 0, "%H:%M") if m.get("ts") else "",
            "role": str(m.get("role") or "user"),
            "text": text,
        })
    return out


def _session_row(rec: "index.UnifiedRecord") -> Dict[str, Any]:
    """浏览列表里的一行（不含正文，翻页轻量）。"""
    msgs = _message_list(rec)
    return {
        "id": index.record_id(rec),
        "source": rec.source,
        "content_level": _content_levels().get(rec.source, ""),
        "project": rec.project or index.UNCATEGORIZED,
        "title": rec.title,
        "status": rec.status,
        "cwd": rec.cwd,
        "start_ts": rec.start_ts,
        "end_ts": rec.end_ts,
        "date": fmt_bj(rec.start_ts, "%Y-%m-%d") if rec.start_ts else "",
        "time": (f"{fmt_bj(rec.start_ts, '%H:%M')}~{fmt_bj(rec.end_ts, '%H:%M')}"
                 if rec.start_ts else ""),
        "summary": rec.summary,
        "messages": len(msgs),
        "has_body": bool(msgs),
    }


def _facets(records: List["index.UnifiedRecord"]) -> Dict[str, Any]:
    """左栏筛选项：源 / 项目 / 全量时间范围。"""
    sources: Dict[str, int] = {}
    projects: Dict[str, int] = {}
    for rec in records:
        sources[rec.source] = sources.get(rec.source, 0) + 1
        name = rec.project or index.UNCATEGORIZED
        projects[name] = projects.get(name, 0) + 1
    stamps = [r.start_ts for r in records if r.start_ts]
    return {
        "sources": [{"name": k, "count": v}
                    for k, v in sorted(sources.items(), key=lambda kv: -kv[1])],
        "projects": [{"name": k, "count": v}
                     for k, v in sorted(projects.items(), key=lambda kv: -kv[1])],
        "total": len(records),
        "first": min(stamps) if stamps else 0.0,
        "last": max(stamps) if stamps else 0.0,
    }


def browse(source: Optional[str] = None, project: Optional[str] = None,
           q: Optional[str] = None, start_value: Optional[str] = None,
           end_value: Optional[str] = None, limit: int = _BROWSE_PAGE,
           offset: int = 0, refresh: bool = False) -> Dict[str, Any]:
    """浏览历史会话：筛选 + 分页 + 左栏计数，按时间倒序（最近的在前）。"""
    records = browse_records(refresh)
    facets = _facets(records)

    rows = list(records)
    if source:
        rows = [r for r in rows if r.source == source]
    if project:
        rows = [r for r in rows if (r.project or index.UNCATEGORIZED) == project]
    if start_value or end_value:
        rows = index.filter_by_date(
            rows,
            start=index.parse_day(start_value) if start_value else None,
            end=index.parse_day(end_value) if end_value else None,
        )
    needle = (q or "").strip().lower()
    if needle:
        def hit(rec: "index.UnifiedRecord") -> bool:
            hay = [rec.title, rec.summary, rec.cwd, rec.project or ""]
            hay += [m["text"] for m in _message_list(rec)]
            return any(needle in str(x).lower() for x in hay)
        rows = [r for r in rows if hit(r)]

    rows.sort(key=lambda r: (r.start_ts or 0, r.end_ts or 0), reverse=True)
    total = len(rows)
    limit = max(1, min(int(limit or _BROWSE_PAGE), 500))
    offset = max(0, int(offset or 0))
    page = rows[offset:offset + limit]

    # 状态标记：是否来自缓存，便于排查「为什么数字没变」
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "facets": facets,
        "stale_seconds": round(time.time() - _BROWSE_CACHE["ts"], 1),
        "sessions": [_session_row(r) for r in page],
    }


def session_detail(record: str) -> Dict[str, Any]:
    """单条会话详情：元数据 + 逐条正文（没有正文的源如实说明）。"""
    rid = (record or "").strip()
    if not rid:
        raise ValueError("id 不能为空")
    for rec in browse_records():
        if index.record_id(rec) != rid:
            continue
        row = _session_row(rec)
        msgs = _message_list(rec)
        row.update({
            "messages": len(msgs),
            "body": msgs,
            "partial": bool(rec.raw.get("partial")) if isinstance(rec.raw, dict) else False,
            "note": ("该数据源本地只有标题/元数据，正文在云端，读不到逐条内容"
                     if not msgs else ""),
        })
        return row
    raise KeyError(f"找不到这条会话：{rid}（可能数据已变化，刷新后重试）")



def sources_info() -> List[Dict[str, Any]]:
    """源清单：只报可用性与内容等级，不读会话（逐源扫描时再读）。"""
    return [
        {"name": c.name, "content_level": c.content_level,
         "available": c.available()}
        for c in REGISTRY
    ]


def scan_source(name: str) -> Dict[str, Any]:
    """真实扫描单个源：可用性与会话数（供引导页逐源点亮）。"""
    for c in REGISTRY:
        if c.name == name:
            count = len(c.read()) if c.available() else 0
            return {"name": name, "content_level": c.content_level,
                    "available": c.available(), "count": count}
    raise KeyError(f"未注册的数据源：{name}")


def _range_label(mode: str, date_value: Optional[str],
                 start: Optional[str], end: Optional[str]) -> Dict[str, str]:
    """把请求的窗口解析成可展示的日期（缺省即今天/本周，北京时间）。"""
    if mode == "range":
        return {"start": start or "", "end": end or ""}
    day = index.parse_day(date_value)
    if mode == "weekly":
        monday = day - timedelta(days=day.weekday())
        return {"start": monday.isoformat(),
                "end": (monday + timedelta(days=6)).isoformat()}
    return {"date": day.isoformat()}


def conversations(mode: str = "daily", date_value: Optional[str] = None,
                  start: Optional[str] = None, end: Optional[str] = None,
                  detail: str = "summary",
                  project: Optional[str] = None) -> Dict[str, Any]:
    """按项目分组的素材（含稳定 id），供引导页卡片墙展示与勾选。"""
    recs = index.select_records(mode, date_value, start, end)
    payload = index.records_payload(recs, detail=detail, project=project)
    payload["range"] = _range_label(mode, date_value, start, end)
    return payload


#: 成品报告的下载响应类型（按扩展名给，md/html 直接在浏览器里看）
_REPORT_CTYPES = {
    "md": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def generate_report(ids: List[str], mode: str = "daily",
                    date_value: Optional[str] = None,
                    start: Optional[str] = None, end: Optional[str] = None,
                    fmt: str = "md",
                    template_name: Optional[str] = None,
                    polish: bool = True) -> Dict[str, Any]:
    """本机直接出报告：按勾选 + 剪辑过滤素材，纯模板渲染后落盘。

    与 CLI 的 daily / weekly / range 走同一条渲染与落盘链路，所以同一天在引导页
    与命令行生成的报告内容、文件名一致，差别只在素材按引导页的勾选做了裁剪。

    时间剪辑在这里真实生效：窗口外的片段已从 summary 和 raw["messages"] 里丢弃，
    窗口内一条不剩的记录直接不出现在报告里（与 records_payload 的 `trimmed_out` 语义一致）。

    polish=True 时再把骨架交给 LLM 归纳一次（同一套「骨架 + 槽位」链路，等价于
    CLI 的 `--llm`）：**排版仍由本机 generator 负责**，模型只被允许改写条目的
    标题与正文，结果还要过结构/格式校验；未配置 key、调用失败或校验不过都会退回
    草稿，并在返回值里如实说明原因。
    """
    keep = set(ids or [])
    if not keep:
        raise ValueError("ids 不能为空：至少要勾选一条记录")

    picked: List[Any] = []
    trims = trimmer.load_trims()
    for rec in index.select_records(mode, date_value, start, end):
        rid = index.record_id(rec)
        if rid not in keep:
            continue
        win = trims.get(rid)
        if win:
            rec.summary = trimmer.clip_text(rec.summary or "", win["start"], win["end"])
            raw = rec.raw if isinstance(rec.raw, dict) else {}
            msgs = raw.get("messages")
            if isinstance(msgs, list):
                # generator 的 assistant 提示直接读 raw["messages"]，这里必须同步裁剪，
                # 否则窗口外的 AI 正文仍会进入报告。
                kept_msgs = trimmer.clip_messages(msgs, win["start"], win["end"])
                raw["messages"] = kept_msgs
            else:
                kept_msgs = []
            has_message = any(
                str(m.get("text") or "").strip()
                for m in kept_msgs if isinstance(m, dict)
            )
            if not rec.summary.strip() and not has_message:
                continue  # 窗口内一条不剩 → 等同未勾选
        picked.append(rec)

    style = (template.style_for_generator(template.load_template(template_name))
             if template_name else None)

    day = index.parse_day(date_value)
    if mode == "range":
        if not (start and end):
            raise ValueError("range 模式需要同时提供 start 和 end")
        first, last = index.parse_day(start), index.parse_day(end)
        if first > last:
            raise ValueError("start 不能晚于 end")
        outline = generator.outline_range(first, last, records=picked)
        base = f"AI报告_{start}_{end}"
    elif mode == "weekly":
        outline = generator.outline_weekly(day, records=picked)
        base = f"AI周报_{day.isoformat()}"
    else:
        outline = generator.outline_daily(day, records=picked)
        base = f"AI日报_{day.isoformat()}"

    synth = llm.ReportSynthesizer()
    md, _err = llm.polish_outline(outline, style, synth if polish else None)

    path = cli.write_report_file(md, base, fmt)
    return {
        "name": path.name,
        "path": str(path),
        "url": "/api/report?name=" + quote(path.name),
        "format": fmt if fmt in cli.FORMATS else "md",
        "total": len(picked),
        "projects": len(index.group_by_project(picked)),
        "polished": bool(polish and synth.available and not synth.last_error),
        "llm": {"provider": synth.provider, "model": synth.model},
        "llm_error": synth.last_error,
        "text": md,
    }


def template_list() -> List[Dict[str, Any]]:
    """已学格式模板清单（供引导页「格式克隆」下拉，带 generator 用的样式）。"""
    out = []
    for item in template.list_templates():
        try:
            style = template.style_for_generator(template.load_template(item["name"]))
        except template.TemplateError:
            style = None
        item["style"] = style
        out.append(item)
    return out


def template_detail(name: str) -> Dict[str, Any]:
    """单个模板的规范文本 + 图片提示（供引导页预览与校验）。"""
    tpl = template.load_template(name)
    return {
        "name": tpl.get("name", name),
        "spec": tpl.get("spec") or template.spec_block(tpl),
        "image_hint": image_hint(tpl),
        "sample_count": len(tpl.get("samples") or []),
        "pending_vision": not tpl.get("features"),
        "style": template.style_for_generator(tpl),
    }


def learn_samples(files: List[Dict[str, Any]], name: str = "") -> Dict[str, Any]:
    """接收 base64 样例：存档 → 抽取排版结构 → 落盘成模板，返回模板数据。

    文本 / docx 样例在本机直接推断；图片样例只存档，返回读图提示交给宿主 AI 回填。
    """
    if not files:
        raise ValueError("没有收到样例文件")
    name = (name or "").strip() or "我的模板"
    saved: List[Path] = []
    for i, item in enumerate(files, 1):
        item = item or {}
        fname = str(item.get("name") or f"sample-{i}")
        raw = item.get("data") or ""
        try:
            blob = base64.b64decode(raw, validate=False)
        except Exception as exc:  # 前端已做校验，这里兜底报清楚
            raise ValueError(f"样例 {fname} 的 base64 无法解码：{exc}")
        if not blob:
            raise ValueError(f"样例 {fname} 是空文件")
        target = template.samples_dir() / (
            f"{template.slug(name)}-{i}-{template.slug(Path(fname).name)}")
        target.write_bytes(blob)
        saved.append(target)
        log(f"[模板] 样例已存档：{target}")

    try:
        tpl = template.learn_from_paths(saved, name)
    except template.TemplateError as exc:
        # 图片样例（或全部不可解析）：留待宿主 AI 读图后经 MCP save_template 回填
        tpl = template.stub_for_vision(name, saved[0].name)
        tpl["samples"] = [
            {"file": p.name, "kind": "image", "chars": 0, "path": str(p.resolve())}
            for p in saved
        ]
        tpl["note"] = str(exc)

    tpl["pending_vision"] = not tpl.get("features")
    tpl["saved_to"] = str(template.save_template(tpl))
    tpl["image_hint"] = image_hint(tpl)
    log(f"[模板] 已学成「{name}」：样例 {len(tpl.get('samples') or [])} 份，"
        f"待读图={tpl['pending_vision']}")
    return tpl


def build_instruction(ids: List[str], mode: str = "daily",
                      date_value: Optional[str] = None, fmt: str = "md",
                      template_name: Optional[str] = None,
                      start: Optional[str] = None,
                      end: Optional[str] = None) -> str:
    """依据勾选结果拼出「交给 AI 的指令」：格式 → 取数 → 归纳 → 落盘。

    - daily / weekly：由 `date` 推出窗口（当天 / 该日期所在自然周）
    - range（引导页的「自定义时间」）：直接用 `start` / `end` 两个日期
    """
    ids_json = json.dumps(ids, ensure_ascii=False)
    if mode == "range":
        s, e = start or "", end or ""
        label = f"{s} ~ {e}"
        fname = f"AI报告_{s}_{e}"
        get_args = (f'get_conversations(mode="range", start="{s}", end="{e}", '
                    f'detail="full", ids={ids_json})')
        cli = f" python -m ai_report range --start {s} --end {e} --format {fmt}"
    else:
        win = _range_label(mode, date_value, None, None)
        date_str = date_value or win.get("date") or win.get("start", "")
        label = date_str if mode == "daily" else f"{win.get('start')} ~ {win.get('end')}"
        fname = ("日报_" if mode == "daily" else "周报_") + date_str
        get_args = (f'get_conversations(mode="{mode}", date="{date_str}", '
                    f'detail="full", ids={ids_json})')
        cli = f" python -m ai_report {mode} --date {date_str} --format {fmt}"
    save_args = '{markdown: <你归纳的正文>, filename: "' + fname + '", format: "' + fmt + '"}'

    lines = [
        f"请生成本机 AI 工作总结（{label}，北京时间），"
        "严格按 skills/ai-report/SKILL.md 的格式规范归纳。",
    ]

    # 学到了你的排版：把格式规范块放到最前面，先定格式再填内容
    if template_name:
        tpl = template.load_template(template_name)  # 不存在会抛 TemplateError → 400
        lines += ["", tpl.get("spec") or template.spec_block(tpl)]
        hint = image_hint(tpl)
        if hint:
            lines += ["", hint]
        lines += ["", "（格式只为复现你的排版，**不要照搬样例里的具体内容**。）"]

    lines += [
        "",
        f"第1步 取素材：调用 aireport MCP 的 {get_args}",
        f"只取我在引导页勾选的 {len(ids)} 条记录；未勾选的内容不要写进报告。",
    ]
    trimmed = [rid for rid in ids if rid in trimmer.load_trims()]
    if trimmed:
        lines.append(
            f"其中 {len(trimmed)} 条素材已在引导页做过时间剪辑：窗口外的片段已被工具丢弃，"
            "请勿自行恢复，也不要改用全窗口的数据。")
    lines += [
        f"第2步 落盘：调用 save_report({save_args})",
        "第3步 回复我文件路径与各项目条数概览即可，不要复述正文",
    ]

    if template_name:
        cli += f' --template "{template_name}"'
    lines += [
        "",
        "（如果这个 AI 工具还没接 aireport MCP，可改用命令行：",
        cli + "）",
    ]
    return "\n".join(lines)


class _Handler(BaseHTTPRequestHandler):
    server_version = "aireport-ui"

    #: 只接受本机回环地址访问（DNS rebinding 会让 Host 变成攻击者域名）
    _LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")

    # ---- 基础响应 ----

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, code: int = 200) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _fail(self, code: int, message: str) -> None:
        self._json({"error": message}, code)

    # ---- 来源校验 ----

    def _reject_foreign(self) -> bool:
        """拒绝非本机来源；是外来请求时返回 True（已回 403）。

        只监听 127.0.0.1 并不等于安全：DNS rebinding 能让任意网页把请求打到
        本地端口。两道校验：
        1. Host 必须是回环地址（rebinding 时 Host 是攻击者域名）
        2. 带 Origin 的写请求，Origin 必须与 Host 同源
        """
        host = (self.headers.get("Host") or "").strip().lower()
        hostname = host
        if hostname.startswith("["):                    # [::1]:5071
            hostname = hostname.split("]")[0] + "]"
        elif ":" in hostname:
            hostname = hostname.rsplit(":", 1)[0]
        if hostname not in self._LOCAL_HOSTS:
            self._fail(403, "只接受本机回环地址访问")
            return True

        origin = (self.headers.get("Origin") or "").strip().lower()
        if origin:
            allowed = {f"http://{host}", f"https://{host}"}
            if origin not in allowed:
                self._fail(403, "跨源请求被拒绝")
                return True
        return False

    # ---- 路由 ----

    def do_GET(self):  # noqa: N802 - 标准库回调命名
        if self._reject_foreign():
            return
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path in ("/", "/index.html"):
                if not INDEX_HTML.is_file():
                    self._fail(500, f"引导页文件缺失：{INDEX_HTML}")
                    return
                self._send(200, INDEX_HTML.read_bytes(),
                           "text/html; charset=utf-8")
            elif url.path in ("/browse", "/browse.html"):
                if not BROWSE_HTML.is_file():
                    self._fail(500, f"浏览页文件缺失：{BROWSE_HTML}")
                    return
                self._send(200, BROWSE_HTML.read_bytes(),
                           "text/html; charset=utf-8")
            elif url.path == "/api/browse":
                self._json(browse(
                    source=query.get("source"),
                    project=query.get("project"),
                    q=query.get("q"),
                    start_value=query.get("start"),
                    end_value=query.get("end"),
                    limit=int(query.get("limit") or _BROWSE_PAGE),
                    offset=int(query.get("offset") or 0),
                    refresh=query.get("refresh") in ("1", "true", "yes"),
                ))
            elif url.path == "/api/session":
                self._json(session_detail(query.get("id") or ""))
            elif url.path == "/api/sources":
                self._json(sources_info())
            elif url.path == "/api/source":
                self._json(scan_source(query.get("name", "")))
            elif url.path == "/api/conversations":
                self._json(conversations(
                    mode=query.get("mode") or "daily",
                    date_value=query.get("date"),
                    start=query.get("start"),
                    end=query.get("end"),
                    detail=query.get("detail") or "summary",
                    project=query.get("project"),
                ))
            elif url.path == "/api/templates":
                self._json(template_list())
            elif url.path == "/api/template":
                name = query.get("name") or ""
                if not name:
                    self._fail(400, "name 不能为空")
                    return
                self._json(template_detail(name))
            elif url.path == "/api/llm":
                self._json(llm.status())
            elif url.path == "/api/report":
                self._serve_report(query.get("name") or "")
            else:
                self._fail(404, "not found")
        except (ValueError, KeyError) as exc:
            self._fail(400, str(exc))
        except Exception as exc:  # 兜底：报清楚原因，不静默
            log(f"[UI] {url.path} 处理失败：{type(exc).__name__}: {exc}")
            self._fail(500, f"{type(exc).__name__}: {exc}")

    def do_POST(self):  # noqa: N802 - 标准库回调命名
        if self._reject_foreign():
            return
        url = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, json.JSONDecodeError) as exc:
            self._fail(400, f"请求体不是合法 JSON：{exc}")
            return
        try:
            if url.path == "/api/instruction":
                ids = body.get("ids") or []
                if not ids:
                    self._fail(400, "ids 不能为空：至少要勾选一条记录")
                    return
                self._json({"text": build_instruction(
                    ids,
                    mode=body.get("mode") or "daily",
                    date_value=body.get("date"),
                    fmt=body.get("format") or "md",
                    template_name=body.get("template") or None,
                    start=body.get("start"),
                    end=body.get("end"),
                )})
            elif url.path == "/api/learn":
                tpl = learn_samples(body.get("files") or [],
                                    body.get("name") or "")
                self._json({
                    "name": tpl.get("name"),
                    "sample_count": len(tpl.get("samples") or []),
                    "pending_vision": tpl.get("pending_vision", False),
                    "image_hint": tpl.get("image_hint"),
                    "spec": tpl.get("spec") or "",
                    "saved_to": tpl.get("saved_to", ""),
                })
            elif url.path == "/api/trim":
                self._json(self._apply_trim(body))
            elif url.path == "/api/generate":
                self._json(generate_report(
                    body.get("ids") or [],
                    mode=body.get("mode") or "daily",
                    date_value=body.get("date"),
                    start=body.get("start"),
                    end=body.get("end"),
                    fmt=body.get("format") or "md",
                    template_name=body.get("template") or None,
                    polish=body.get("polish", True) is not False,
                ))
            else:
                self._fail(404, "not found")
        except (ValueError, KeyError) as exc:
            self._fail(400, str(exc))
        except Exception as exc:
            log(f"[UI] {url.path} 处理失败：{type(exc).__name__}: {exc}")
            self._fail(500, f"{type(exc).__name__}: {exc}")

    def _serve_report(self, name: str) -> None:
        """把 reports/ 里的成品报告发给浏览器（仅供单层文件名，防目录穿越）。"""
        if not name:
            self._fail(400, "name 不能为空")
            return
        if Path(name).name != name:
            self._fail(400, "非法文件名：只允许 reports 目录内的文件名")
            return
        path = config.ensure_reports_dir() / name
        if not path.is_file():
            self._fail(404, f"报告不存在：{name}（先在等待页点「生成报告」）")
            return
        ctype = _REPORT_CTYPES.get(path.suffix.lstrip(".").lower(),
                                   "application/octet-stream")
        self._send(200, path.read_bytes(), ctype)

    def _apply_trim(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """设置/清除素材的时间剪辑。

        body：`{id, start, end}`（两端都空 = 清除该条），或 `{clear: true}` 清空全部。
        时间用 HH:MM（24 小时制）；`end < start` 视为跨夜窗口。
        """
        if body.get("clear"):
            return {"cleared": trimmer.clear_all(), "count": trimmer.count()}
        rid = str(body.get("id") or "").strip()
        if not rid:
            raise ValueError("id 不能为空：请指定要剪辑的素材")
        win = trimmer.set_trim(rid, body.get("start"), body.get("end"))
        return {"id": rid, "trim": win, "count": trimmer.count()}

    def log_message(self, fmt, *args):
        pass  # 访问日志静音，避免污染 stderr


def serve_ui(port: int = DEFAULT_PORT, open_browser: bool = True) -> None:
    """启动引导页服务（仅回环地址，数据不出本机）。"""
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    url = f"http://127.0.0.1:{port}/"
    log(f"[UI] 引导页已启动：{url}")
    print(f"引导页已启动：{url}（只监听本机，Ctrl+C 停止）")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception as exc:  # 打不开浏览器不影响服务
            log(f"[UI] 自动打开浏览器失败：{exc}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n引导页已停止。")
    finally:
        server.server_close()
