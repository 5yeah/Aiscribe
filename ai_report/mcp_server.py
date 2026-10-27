"""MCP server：标准 stdio transport（供 dsh / Claude Code / Cursor 等接入）
+ 可选本地 HTTP transport（供网页/脚本查询）。

- stdio：逐行 JSON-RPC 2.0（MCP 标准 newline-delimited），只走 stdin/stdout，
  所有日志强制走 stderr，避免污染协议流
- HTTP：POST /mcp + GET /health，只监听 127.0.0.1，数据不出本机

启动：
  python -m ai_report mcp --stdio            # MCP 客户端接入用这个
  python -m ai_report mcp --port 5070        # 本地 HTTP 查询用这个
  python -m ai_report.mcp_server --stdio     # 独立等价入口
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, TextIO

from ai_report import generator
from ai_report.connectors.registry import REGISTRY
from ai_report.utils import log, now_bj

# 客户端可能请求的协议版本，我们按已知版本 echo（MCP 握手约定）
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

SERVER_INFO = {"name": "aireport", "version": "0.1.0"}

#: initialize 时交给模型的使用约束：原文会进入模型上下文，必须让用户知情
INSTRUCTIONS = (
    "Aiscribe 只读取本机 AI 会话文件，不联网、不上传完整会话。"
    "但工具返回的内容会进入当前模型的上下文：如果使用的是云端模型，"
    "detail='full' 的对话原文等同于发送给该模型提供商。"
    "默认先用 detail='summary' 摸底；只有用户明确要求生成报告并知晓这一点时，"
    "才对相关项目使用 detail='full'。不要读取与当前请求无关的日期或项目。"
)

_READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}
_WRITE_LOCAL = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "list_sources",
        "title": "列出本机会话数据源",
        "description": "列出本机已发现的 AI 会话数据源（Codex/Copilot/CodeBuddy/WorkBuddy 等）及可用状态与会话数",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "annotations": _READ_ONLY,
    },
    {
        "name": "generate_report",
        "title": "生成本地报告草稿",
        "description": (
            "用内置模板直接生成日报/周报（不含理解归纳，仅按项目罗列当日记录）。"
            "需要归纳总结时请改用 get_conversations 取原始素材、由你（AI）撰写后 save_report 落盘"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["daily", "weekly", "range"],
                         "description": "报告类型，默认 daily"},
                "date": {"type": "string",
                         "description": "YYYY-MM-DD；daily 为报告日，weekly 为周内任意一天，默认今天"},
                "start": {"type": "string", "description": "range 模式起始日期 YYYY-MM-DD"},
                "end": {"type": "string", "description": "range 模式结束日期 YYYY-MM-DD"},
                "format": {"type": "string", "enum": ["md", "docx", "html"], "default": "md",
                           "description": "输出格式：md / docx / html"},
                "polish": {"type": "boolean", "default": False,
                           "description": "是否用 LLM 润色（需要 DEEPSEEK_API_KEY，失败自动退回模板）"},
                "save": {"type": "boolean", "default": False,
                         "description": "是否把报告写入 reports/ 目录（返回文件路径）"},
            },
            "required": [],
        },
        "annotations": _WRITE_LOCAL,
    },
    {
        "name": "get_conversations",
        "title": "读取会话素材",
        "description": (
            "取指定日期/区间的原始对话素材（按项目分组，含用户逐条真实输入全文），"
            "供 AI 自行阅读、理解并归纳成工作报告。这是“会理解”的推荐入口。"
            "注意：返回内容会进入模型上下文；默认先 summary，确认需要后再 full"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["daily", "weekly", "range"],
                         "description": "取数范围，默认 daily"},
                "date": {"type": "string",
                         "description": "YYYY-MM-DD；daily 为当天，weekly 为周内任意一天，默认今天"},
                "start": {"type": "string", "description": "range 模式起始日期 YYYY-MM-DD"},
                "end": {"type": "string", "description": "range 模式结束日期 YYYY-MM-DD"},
                "project": {"type": "string",
                            "description": "只取某个项目（可选，缺省返回全部项目）"},
                "detail": {"type": "string", "enum": ["full", "summary"], "default": "summary",
                           "description": "summary 只返回单行时间线（默认，先摸底）；full 返回逐条消息全文"},
                "ids": {"type": "array", "items": {"type": "string"},
                        "description": "只取这些记录 id（本地引导页勾选结果）；缺省返回窗口内全部记录"},
            },
            "required": [],
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "save_report",
        "title": "保存报告",
        "description": (
            "把 AI 撰写好的报告正文落盘到 reports/ 目录"
            "（支持 md / docx / html；html 为自包含单文件，可直接分享）"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "markdown": {"type": "string", "description": "报告正文（Markdown）"},
                "filename": {"type": "string",
                             "description": "文件名（不含扩展名），默认 AI日报_<今天>"},
                "format": {"type": "string", "enum": ["md", "docx", "html"], "default": "md",
                           "description": "输出格式：md 可二次编辑 / docx 便于流转 / html 便于分享"},
            },
            "required": ["markdown"],
        },
        "annotations": _WRITE_LOCAL,
    },
    {
        "name": "list_templates",
        "title": "列出格式模板",
        "description": (
            "列出用户已学到的报告格式模板（由引导页「格式克隆」上传其过去的日报/周报样例学到）。"
            "pending_vision=true 表示样例是图片、本机未识图，需要你读图后调用 save_template 回填"
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
        "annotations": _READ_ONLY,
    },
    {
        "name": "get_format_spec",
        "title": "读取格式规范",
        "description": (
            "取模板的「输出格式规范」文本：写报告前先调用它，然后严格照此排版。"
            "缺省取用户当前生效的模板（AIREREPORT_TEMPLATE 或最新一份）"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "模板名（可选；缺省取最新/环境变量指定的模板）"},
            },
            "required": [],
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "save_template",
        "title": "保存格式模板",
        "description": (
            "保存/回填一个格式模板（读图后把看到的排版结构写回）。"
            "features 只描述结构，不要写样例里的具体工作内容。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "模板名（必填）"},
                "features": {
                    "type": "object",
                    "description": "排版结构特征；可只给已看清的字段",
                    "properties": {
                        "title": {"type": "string",
                                  "description": "首行标题写法，日期用 {date} 占位，如 'AI 日报 {date}'"},
                        "meta": {"type": "string", "description": "头部信息行写法（含汇报人/部门/日期）"},
                        "project": {
                            "type": "object",
                            "description": "项目行样式",
                            "properties": {
                                "kind": {"type": "string",
                                         "enum": ["bold-arabic", "hash", "plain-arabic",
                                                  "bold-name-only"]},
                                "punct": {"type": "string", "description": "序号后的标点，如 、 或 ."},
                            },
                        },
                        "item": {
                            "type": "object",
                            "description": "事项行样式",
                            "properties": {
                                "style": {"type": "string",
                                          "enum": ["circled", "numeric", "hash3", "dash"]},
                                "has_detail": {"type": "boolean",
                                               "description": "事项名后是否用「：」接一句说明"},
                            },
                        },
                        "todo": {"type": "string", "description": "待办段标题，如 '{n}、下一步计划'"},
                        "footer": {"type": "string", "description": "收尾说明行，内容用 {note} 占位"},
                        "short_version": {"type": "boolean",
                                          "description": "是否「详细版 + 简洁版」双版本（--- 分隔）"},
                    },
                },
                "samples": {"type": "array", "items": {"type": "object"},
                            "description": "样例记录（可原样沿用 list_templates 给出的 samples）"},
                "note": {"type": "string", "description": "备注（可选）"},
            },
            "required": ["name", "features"],
        },
        "annotations": _WRITE_LOCAL,
    },
]

def _tool_list_sources(_args: Dict[str, Any]) -> str:
    data = [
        {
            "name": c.name,
            "available": c.available(),
            "count": len(c.read()) if c.available() else 0,
        }
        for c in REGISTRY
    ]
    return json.dumps(data, ensure_ascii=False, indent=2)


def _tool_generate_report(args: Dict[str, Any]) -> str:
    from ai_report import index

    mode = args.get("mode") or "daily"
    if mode not in ("daily", "weekly", "range"):
        raise ValueError(f"未知 mode：{mode}（应为 daily/weekly/range）")

    if mode == "range":
        if not (args.get("start") and args.get("end")):
            raise ValueError("range 模式需要同时提供 start 和 end")
        outline = generator.outline_range(
            index.parse_day(args["start"]), index.parse_day(args["end"]))
    elif mode == "weekly":
        outline = generator.outline_weekly(index.parse_day(args.get("date")))
    else:
        outline = generator.outline_daily(index.parse_day(args.get("date")))

    from ai_report.llm import ReportSynthesizer, polish_outline
    synth = ReportSynthesizer() if args.get("polish") else None
    # 排版由 generator 负责；polish 时模型只改写文字，校验不过自动退回草稿
    md, _err = polish_outline(outline, None, synth)
    fmt = args.get("format") or "md"

    if args.get("save"):
        path = _save(md, fmt, mode, args)
        if fmt == "md":
            return f"报告已保存：{path}\n\n{md}"
        return f"报告已保存：{path}（{fmt.upper()}）"
    return md


def _save(md: str, fmt: str, mode: str, args: Dict[str, Any]) -> Path:
    from ai_report import config
    fmt = fmt if fmt in ("md", "docx", "html") else "md"
    day = args.get("date") or now_bj().strftime("%Y-%m-%d")
    if mode == "weekly":
        name = f"AI周报_{day}"
    elif mode == "range":
        name = f"AI报告_{args.get('start')}_{args.get('end')}"
    else:
        name = f"AI日报_{day}"
    path = config.ensure_reports_dir() / f"{name}.{fmt}"
    if fmt == "docx":
        from ai_report.docx import write_docx
        write_docx(md, path)
    elif fmt == "html":
        from ai_report.html import write_html
        write_html(md, path)
    else:
        path.write_text(md if md.endswith("\n") else md + "\n", encoding="utf-8")
    return path


def _tool_get_conversations(args: Dict[str, Any]) -> str:
    """按项目输出原始对话素材，供 AI 阅读归纳（本项目“会理解”的关键入口）。"""
    from ai_report import index

    recs = index.select_records(args.get("mode") or "daily", args.get("date"),
                                args.get("start"), args.get("end"))
    payload = index.records_payload(recs, detail=args.get("detail") or "summary",
                                    project=args.get("project"),
                                    ids=args.get("ids"))
    payload["range"] = {k: args.get(k) for k in ("mode", "date", "start", "end")}
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _tool_save_report(args: Dict[str, Any]) -> str:
    """把 AI 写好的报告落盘到 reports/（md / docx / html）。"""
    from ai_report import config

    md = args.get("markdown") or ""
    if not md.strip():
        raise ValueError("markdown 不能为空")
    fmt = args.get("format") or "md"
    if fmt not in ("md", "docx", "html"):
        raise ValueError(f"未知 format：{fmt}（应为 md/docx/html）")
    name = Path(args.get("filename") or
                f"AI日报_{now_bj().strftime('%Y-%m-%d')}").name
    path = config.ensure_reports_dir() / f"{Path(name).stem}.{fmt}"
    if fmt == "docx":
        from ai_report.docx import write_docx
        write_docx(md, path)
    elif fmt == "html":
        from ai_report.html import write_html
        write_html(md, path)
    else:
        path.write_text(md if md.endswith("\n") else md + "\n", encoding="utf-8")
    return f"报告已保存：{path}"


def _tool_list_templates(_args: Dict[str, Any]) -> str:
    """列出已学格式模板（含待读图的骨架）。"""
    from ai_report import template

    items = template.list_templates()
    if not items:
        return ("（本机还没有学到的格式模板。用户可在引导页「格式克隆」上传过去的日报样例，"
                "或用 save_template 直接写入。）")
    return json.dumps(items, ensure_ascii=False, indent=2)


def _tool_get_format_spec(args: Dict[str, Any]) -> str:
    """取模板的格式规范文本：写报告前先看它，再照此排版。"""
    from ai_report import template

    tpl = template.resolve_template(args.get("name"))
    if not tpl:
        return ("（本机还没有学到的格式模板：请按 skills/ai-report/SKILL.md 的默认格式规范归纳；"
                "用户也可以先上传样例做「格式克隆」。）")
    out = tpl.get("spec") or template.spec_block(tpl)
    hint = template.image_hint(tpl)
    if hint:
        out += "\n\n" + hint
    return out


def _tool_save_template(args: Dict[str, Any]) -> str:
    """保存/回填格式模板（读图后把结构写回）。"""
    from ai_report import template

    name = (args.get("name") or "").strip()
    if not name:
        raise ValueError("name 不能为空")
    features = args.get("features") or {}
    if not isinstance(features, dict) or not features:
        raise ValueError("features 不能为空：至少给出 project 或 item 的排版结构")
    tpl = {
        "name": name,
        "created": now_bj().strftime("%Y-%m-%d %H:%M"),
        "samples": args.get("samples") or [],
        "features": features,
    }
    if args.get("note"):
        tpl["note"] = args["note"]
    path = template.save_template(tpl)
    return f"格式模板已保存：{path}\n\n{tpl.get('spec') or template.spec_block(tpl)}"


def _call_tool(fn, args: Dict[str, Any]) -> Dict[str, Any]:
    """统一执行工具：异常转 isError 结果（不走 JSON-RPC error）。"""
    try:
        return {"result": {"content": [{"type": "text", "text": fn(args)}],
                           "isError": False}}
    except Exception as exc:
        return {"result": {"content": [{"type": "text",
                                        "text": f"工具执行失败：{exc}"}],
                           "isError": True}}


def dispatch(method: str, params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """处理单条 JSON-RPC 消息，返回 {"result": ...} 或 {"error": ...}。"""
    params = params or {}
    if method == "initialize":
        requested = params.get("protocolVersion")
        # 始终 echo 客户端版本：客户端必然支持自己声明的版本，避免因
        # 版本表过时被严格校验的客户端判定为不兼容而断开（Connection closed）
        return {"result": {
            "protocolVersion": requested or SUPPORTED_PROTOCOLS[0],
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        }}
    if method == "ping":
        return {"result": {}}
    if method == "tools/list":
        return {"result": {"tools": TOOLS}}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "list_sources":
            return _call_tool(_tool_list_sources, args)
        if name == "generate_report":
            return _call_tool(_tool_generate_report, args)
        if name == "get_conversations":
            return _call_tool(_tool_get_conversations, args)
        if name == "save_report":
            return _call_tool(_tool_save_report, args)
        if name == "list_templates":
            return _call_tool(_tool_list_templates, args)
        if name == "get_format_spec":
            return _call_tool(_tool_get_format_spec, args)
        if name == "save_template":
            return _call_tool(_tool_save_template, args)
        return {"result": {"content": [{"type": "text",
                                        "text": f"未知工具：{name}"}],
                           "isError": True}}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return {"result": None}  # 通知类：调用方自行决定不回包
    return {"error": {"code": -32601, "message": f"未知方法：{method}"}}


def _make_response(req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """构造一条响应；通知（无 id）不回包；解析失败回 parse error。"""
    if "method" not in req:
        return {"jsonrpc": "2.0", "id": req.get("id"),
                "error": {"code": -32600, "message": "无效请求"}}
    is_notification = "id" not in req
    try:
        out = dispatch(req.get("method", ""), req.get("params"))
    except Exception as exc:  # noqa: BLE001 - 单条请求失败不能打断 stdio 循环
        log(f"[MCP] {req.get('method')} 处理失败：{type(exc).__name__}: {exc}")
        out = {"error": {"code": -32603,
                         "message": f"内部错误：{type(exc).__name__}: {exc}"}}
    if is_notification:
        return None
    resp = {"jsonrpc": "2.0", "id": req["id"]}
    if "error" in out:
        resp["error"] = out["error"]
    else:
        resp["result"] = out["result"]
    return resp


def _force_utf8(stream: TextIO) -> None:
    """把真实控制台流切到 UTF-8；注入的测试流没有 reconfigure 就跳过。"""
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass


def serve_stdio(in_stream: Optional[TextIO] = None,
                out_stream: Optional[TextIO] = None) -> None:
    """标准 MCP stdio 循环：逐行读 JSON-RPC，逐行写响应（可注入流便于测试）。"""
    stdin = in_stream or sys.stdin
    stdout = out_stream or sys.stdout
    if in_stream is None:
        _force_utf8(stdin)
        _force_utf8(stdout)
        _force_utf8(sys.stderr)
    log("[MCP] stdio 模式已启动（日志走 stderr，协议走 stdout）")
    for line in stdin:
        # 容错：部分客户端会写入 UTF-8 BOM / CRLF 行尾
        line = line.lstrip("﻿").strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            resp = {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "Parse error"}}
            _emit(stdout, resp)
            continue
        # 支持批量请求（JSON 数组）
        if isinstance(req, list):
            for item in req:
                if not isinstance(item, dict):
                    continue
                try:
                    r = _make_response(item)
                except Exception as exc:  # noqa: BLE001
                    r = {"jsonrpc": "2.0", "id": item.get("id"),
                         "error": {"code": -32603,
                                   "message": f"内部错误：{type(exc).__name__}: {exc}"}}
                if r is not None:
                    _emit(stdout, r)
            continue
        r = _make_response(req) if isinstance(req, dict) else None
        if r is not None:
            _emit(stdout, r)


def _emit(out: TextIO, resp: Dict[str, Any]) -> None:
    out.write(json.dumps(resp, ensure_ascii=False) + "\n")
    out.flush()


# --------------------------------------------------------------------------
# 可选 HTTP transport（原样保留，供本地网页/脚本查询）
# --------------------------------------------------------------------------

def _http_dispatch(method: str, params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return dispatch(method, params)


def serve(port: int = 5070) -> None:
    """启动本地 HTTP 服务（只监听 127.0.0.1，数据不出本机）。"""
    import os
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse

    token = (os.environ.get("AIREREPORT_HTTP_TOKEN") or "").strip()
    loopback = ("127.0.0.1", "localhost", "::1")

    class _Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _trusted(self) -> bool:
            """挡 DNS rebinding / 跨站请求：Host 与 Origin 必须是回环地址。"""
            host = (self.headers.get("Host") or "").strip()
            if host.startswith("["):
                host_name = host[1:host.find("]")] if "]" in host else host
            else:
                host_name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
            if host_name.lower() not in loopback:
                return False
            origin = self.headers.get("Origin")
            if origin:
                parsed = urlparse(origin)
                if (parsed.hostname or "").lower() not in loopback:
                    return False
            return True

        def _authorized(self) -> bool:
            if not token:
                return True
            return self.headers.get("Authorization", "") == f"Bearer {token}"

        def do_GET(self):  # noqa: N802
            if not self._trusted():
                self._send(403, {"error": "forbidden host/origin"})
                return
            if self.path == "/health":
                self._send(200, {"status": "ok", "server": SERVER_INFO["name"]})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if not self._trusted():
                self._send(403, {"error": "forbidden host/origin"})
                return
            if self.path != "/mcp":
                self._send(404, {"error": "not found"})
                return
            if not self._authorized():
                self._send(401, {"error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                req = json.loads(self.rfile.read(length).decode("utf-8"))
                if isinstance(req, list):
                    responses = [r for r in (_make_response(x) if isinstance(x, dict)
                                             else None for x in req) if r is not None]
                    if not responses:
                        self.send_response(204)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    self._send(200, responses)
                    return
                if not isinstance(req, dict):
                    self._send(400, {"jsonrpc": "2.0", "id": None, "error": {
                        "code": -32600, "message": "无效请求"}})
                    return
                resp = _make_response(req)
                if resp is None:
                    self.send_response(204)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self._send(200, resp)
            except (json.JSONDecodeError, ValueError) as exc:
                self._send(400, {"jsonrpc": "2.0", "error": {
                    "code": -32700, "message": f"解析失败：{exc}"}})
            except Exception as exc:  # noqa: BLE001 - 单请求失败不拖垮 HTTP 服务
                self._send(500, {"jsonrpc": "2.0", "error": {
                    "code": -32603, "message": f"内部错误：{type(exc).__name__}: {exc}"}})

        def log_message(self, fmt, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    log(f"[MCP] HTTP 已启动：http://127.0.0.1:{port}/mcp（健康检查 /health）")
    print(f"MCP HTTP 服务运行中：http://127.0.0.1:{port}/mcp（Ctrl+C 停止）")
    if token:
        log("[MCP] HTTP 已启用 Bearer Token 校验（AIREREPORT_HTTP_TOKEN）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nMCP 服务已停止。")
    finally:
        server.server_close()


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="aireport MCP server")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--stdio", action="store_true",
                       help="标准 MCP stdio 模式（供 dsh/Claude Code/Cursor 接入）")
    group.add_argument("--port", type=int, nargs="?", const=5070, default=None,
                       help="本地 HTTP 模式端口（默认 5070）")
    args = parser.parse_args(argv)
    if args.stdio:
        serve_stdio()
    else:
        serve(args.port or 5070)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
