"""aireport 命令行入口：日报 / 周报 / 区间报告（输出 md / Word docx / HTML）/ 本地引导页 / 源与配置查看。"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from ai_report import config, generator
from ai_report.docx import write_docx
from ai_report.html import write_html
from ai_report.llm import ReportSynthesizer, polish_outline
from ai_report.utils import log, now_bj

FORMATS = ("md", "docx", "html")

_FORMAT_HELP = "输出格式，默认 md；docx 为 Word 文件，html 为自包含可分享网页"


def _today_str() -> str:
    return now_bj().strftime("%Y-%m-%d")


def _parse_date(value: str) -> "datetime.date":
    return datetime.strptime(value, "%Y-%m-%d").date()


def _write_report(md: str, out: Optional[str], default_base: str,
                  fmt: str = "md") -> Path:
    """写报告文件，返回路径。

    - 不指定 --out：写 reports/<默认名>.<fmt>
    - 指定 --out：无扩展名补 .<fmt>；有扩展名则按扩展名决定格式（md/docx/html）
    """
    fmt = fmt if fmt in FORMATS else "md"
    if out:
        path = Path(out)
        actual = path.suffix.lstrip(".").lower() if path.suffix else fmt
        if not path.suffix:
            path = path.with_suffix(f".{fmt}")
    else:
        path = config.ensure_reports_dir() / f"{default_base}.{fmt}"
        actual = fmt
    if actual == "docx":
        return write_docx(md, path)
    if actual == "html":
        return write_html(md, path)

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(md if md.endswith("\n") else md + "\n")
    return path


def write_report_file(md: str, default_base: str, fmt: str = "md") -> Path:
    """落盘入口（默认写 reports/<default_base>.<fmt>）。

    引导页「直接生成」复用这一套命名与格式分发，保证同一天在命令行与引导页
    生成的报告文件名、内容口径一致。
    """
    return _write_report(md, None, default_base, fmt)


def _render(outline: dict, style: Optional[dict], use_llm: bool) -> str:
    """渲染骨架；--llm 时先渲染草稿再让归纳器只改写文字。

    排版与格式校验由 generator / llm 共同兜底：归纳结果结构跑偏或残留时间戳一类
    噪声时，一律退回纯模板草稿（llm.polish_outline 内部处理）。
    """
    synth = ReportSynthesizer() if use_llm else None
    md, _err = polish_outline(outline, style, synth)
    return md


def _style_from_template(name: Optional[str]) -> Optional[dict]:
    """把学到的格式模板压成 generator 需要的样式；未指定模板时返回 None（用默认排版）。"""
    if not name:
        return None
    from ai_report import template

    return template.style_for_generator(template.load_template(name))


def cmd_daily(args: argparse.Namespace) -> int:
    d = _parse_date(args.date) if args.date else None
    style = _style_from_template(args.template)
    md = _render(generator.outline_daily(d), style, args.llm)
    day = d.isoformat() if d else _today_str()
    path = _write_report(md, args.out, f"AI日报_{day}", args.format)
    print(f"日报已生成：{path}")
    return 0


def cmd_weekly(args: argparse.Namespace) -> int:
    d = _parse_date(args.date) if args.date else None
    style = _style_from_template(args.template)
    md = _render(generator.outline_weekly(d), style, args.llm)
    base = d.isoformat() if d else _today_str()
    path = _write_report(md, args.out, f"AI周报_{base}", args.format)
    print(f"周报已生成：{path}")
    return 0


def cmd_range(args: argparse.Namespace) -> int:
    start, end = _parse_date(args.start), _parse_date(args.end)
    if start > end:
        print("错误：--start 不能晚于 --end", file=sys.stderr)
        return 2
    style = _style_from_template(args.template)
    md = _render(generator.outline_range(start, end), style, args.llm)
    path = _write_report(md, args.out, f"AI报告_{args.start}_{args.end}", args.format)
    print(f"区间报告已生成：{path}")
    return 0


def cmd_learn(args: argparse.Namespace) -> int:
    """从过去的日报样例学习排版结构，固化成可复用模板。"""
    from ai_report import template

    paths = [Path(p) for p in args.files]
    name = (args.name or "").strip() or "我的模板"
    try:
        tpl = template.learn_from_paths(paths, name)
    except template.TemplateError as exc:
        # 图片样例：本机不做识图（保持零依赖），存档后由 AI 读图回填
        tpl = template.stub_for_vision(name, paths[0].name)
        tpl["samples"] = [{"file": p.name, "kind": "image", "chars": 0,
                           "path": str(p.resolve())} for p in paths]
        tpl["note"] = str(exc)

    saved = template.save_template(tpl)
    print(f"格式模板已保存：{saved}")
    if tpl.get("features"):
        print()
        print(tpl.get("spec") or template.spec_block(tpl))
    else:
        print("\n样例是图片，本机不做识图（保持零依赖）。请让 AI 读图后回填：")
        print(template.image_hint(tpl))
    return 0


def cmd_templates(_args: argparse.Namespace) -> int:
    """列出已学到的格式模板。"""
    from ai_report import template

    items = template.list_templates()
    if not items:
        print("还没有学到的格式模板。先上传以前的日报样例：")
        print("  python -m ai_report learn 日报.md --name 我的模板")
        return 0
    print("已学到的格式模板：")
    for item in items:
        flag = "  ← 待读图回填" if item.get("pending_vision") else ""
        print(f"  - {item['name']:<20} 样例 {item['sample_count']} 份"
              f"  学到于 {item.get('created', '')}{flag}")
    return 0


def cmd_sources(_args: argparse.Namespace) -> int:
    """列出已发现的数据源与可用状态（调试用）。"""
    from ai_report.connectors.registry import REGISTRY

    print("已注册数据源：")
    total = 0
    for conn in REGISTRY:
        ok = conn.available()
        state = "available" if ok else "missing"
        count = len(conn.read()) if ok else 0
        total += count
        print(f"  - {conn.name:<14} [{state}] 会话数={count}")
        print(f"      路径: {conn.location()}")
    print(f"合计：{total} 条会话（来自 {sum(1 for c in REGISTRY if c.available())} 个可用源）")
    return 0


def cmd_config(_args: argparse.Namespace) -> int:
    """显示当前配置（敏感字段已打码）。"""
    print("当前配置：")
    for key, value in config.describe().items():
        print(f"  {key:<22}: {value}")
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    """启动 MCP 服务：--stdio 走标准接入；缺省起本地 HTTP（仅回环地址）。"""
    from ai_report.mcp_server import serve, serve_stdio
    if args.stdio:
        serve_stdio()
    else:
        serve(args.port)
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    """启动本地引导页：雷达扫描 → 素材勾选 → 格式与预览（仅回环地址）。"""
    from ai_report.ui import serve_ui
    serve_ui(args.port, open_browser=not args.no_open)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aireport",
        description="AI 活动日报聚合器：本地聚合 AI 会话 → 日报/周报（md 或 Word）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--date", help="日期 YYYY-MM-DD（北京时间，默认今天）")
        p.add_argument("--llm", action="store_true", help="用 LLM 润色（失败自动退回模板）")
        p.add_argument("--out", help="输出文件路径（默认写 reports/）")
        p.add_argument("--format", choices=list(FORMATS), default="md",
                       help=_FORMAT_HELP)
        p.add_argument("--template", help="用学到的格式模板排版（见 learn / templates 子命令）")

    p_daily = sub.add_parser("daily", help="生成日报")
    add_common(p_daily)
    p_daily.set_defaults(func=cmd_daily)

    p_weekly = sub.add_parser("weekly", help="生成周报")
    add_common(p_weekly)
    p_weekly.set_defaults(func=cmd_weekly)

    p_range = sub.add_parser("range", help="生成区间报告")
    p_range.add_argument("--start", required=True, help="起始日期 YYYY-MM-DD")
    p_range.add_argument("--end", required=True, help="结束日期 YYYY-MM-DD")
    p_range.add_argument("--llm", action="store_true", help="用 LLM 润色")
    p_range.add_argument("--out", help="输出文件路径")
    p_range.add_argument("--format", choices=list(FORMATS), default="md",
                         help=_FORMAT_HELP)
    p_range.add_argument("--template", help="用学到的格式模板排版")
    p_range.set_defaults(func=cmd_range)

    p_learn = sub.add_parser(
        "learn", help="从过去的日报样例（md/txt/docx/图片）学习排版格式，固化成模板")
    p_learn.add_argument("files", nargs="+", help="样例文件路径（可多个，越多越准）")
    p_learn.add_argument("--name", help="模板名，默认「我的模板」")
    p_learn.set_defaults(func=cmd_learn)

    p_tpl = sub.add_parser("templates", help="列出已学到的格式模板")
    p_tpl.set_defaults(func=cmd_templates)

    p_src = sub.add_parser("sources", help="列出数据源")
    p_src.set_defaults(func=cmd_sources)

    p_cfg = sub.add_parser("config", help="显示配置（敏感字段打码）")
    p_cfg.set_defaults(func=cmd_config)

    p_mcp = sub.add_parser(
        "mcp", help="启动 MCP 服务（--stdio 供 dsh/Claude Code/Cursor 接入，缺省本地 HTTP）")
    p_mcp.add_argument("--stdio", action="store_true",
                       help="标准 MCP stdio 模式（JSON-RPC 走 stdin/stdout）")
    p_mcp.add_argument("--port", type=int, default=5070, help="HTTP 模式监听端口，默认 5070")
    p_mcp.set_defaults(func=cmd_mcp)

    p_ui = sub.add_parser(
        "ui", help="启动本地引导页（雷达扫描 → 素材勾选 → 格式与预览，仅监听 127.0.0.1）")
    p_ui.add_argument("--port", type=int, default=5071, help="监听端口，默认 5071")
    p_ui.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    p_ui.set_defaults(func=cmd_ui)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130
    except Exception as exc:  # 兜底：清晰报错，不静默失败
        log(f"执行失败：{type(exc).__name__}: {exc}")
        print(f"执行失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
