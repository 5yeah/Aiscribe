"""Markdown → 自包含 HTML 报告（单文件、内嵌样式、无 JS、无外部资源）。

为什么要有它：md 便于二次编辑、docx 便于内部流转，而「可分享 HTML」要能
直接丢进任何浏览器/IM 里看，所以必须是一个不依赖网络的自包含文件。

支持报告实际用到的 Markdown 子集：
  `#` / `##` / `###` 标题、`---` 分隔线、`**加粗**`、`*斜体*`、`` `行内代码` ``、
  `- 列表`、`> 引用`、`①②③ 事项行`、`1、项目行`、普通段落。

安全：所有文本一律 HTML 转义后再内联，素材里的任何字符都不会破坏页面结构，
也不会引入脚本（本文件不生成任何 `<script>`）。

设计 token 与本地引导页（ai_report/web/index.html）保持一致：
品牌绿 --brand #05994F、扁平卡片、自动跟随系统深浅色。
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import List, Optional

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$")
_HR_RE = re.compile(r"^-{3,}\s*$")
_LIST_RE = re.compile(r"^[-*]\s+(.*)$")
_QUOTE_RE = re.compile(r"^>\s?(.*)$")
_ITEM_RE = re.compile(r"^(?P<mark>[①-⑳])\s*、?\s*(?P<rest>.*)$")
_PROJ_RE = re.compile(r"^(?:\*\*)?\d+\s*、|^\*\*[^*]+：\*\*$")

# 行内标记：先加粗，再斜体，最后行内代码（顺序保证 ** 不被单个 * 抢先匹配）
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_CODE_RE = re.compile(r"`([^`]+)`")

_STYLE = """\
:root {
  color-scheme: light;
  --bg: #F6F7F8;
  --surface: #FFFFFF;
  --surface-muted: #F1F2F4;
  --text: #17181A;
  --text-muted: #5C6167;
  --border: rgba(23, 24, 26, 0.12);
  --brand: #05994F;
  --brand-soft: #E5FFF2;
  --radius: 8px;
  --radius-card: 14px;
  --shadow: 0 1px 2px rgba(23, 24, 26, .06), 0 8px 24px rgba(23, 24, 26, .06);
}
@media (prefers-color-scheme: dark) {
  :root {
    color-scheme: dark;
    --bg: #0F1011;
    --surface: #17181A;
    --surface-muted: #202124;
    --text: #E7E8EA;
    --text-muted: #9AA0A6;
    --border: rgba(231, 232, 234, 0.14);
    --brand: #32F08C;
    --brand-soft: #04321C;
    --shadow: 0 1px 2px rgba(0, 0, 0, .5), 0 8px 24px rgba(0, 0, 0, .35);
  }
}
* { box-sizing: border-box; }
html, body { margin: 0; }
body {
  background: var(--bg);
  color: var(--text);
  font: 400 15px/24px "SF Pro Text", "PingFang SC", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
.sheet {
  max-width: 780px; margin: 32px auto;
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius-card); padding: 40px 44px;
  box-shadow: var(--shadow);
}
h1 { font-size: 24px; line-height: 34px; font-weight: 600; margin: 0 0 18px; }
h2 {
  font-size: 18px; line-height: 28px; font-weight: 600;
  margin: 26px 0 10px; padding-bottom: 6px; border-bottom: 1px solid var(--border);
}
h3 { font-size: 15px; line-height: 24px; font-weight: 600; margin: 20px 0 6px; color: var(--brand); }
p { margin: 10px 0; }
p.proj { font-weight: 600; margin: 22px 0 8px; }
p.item { margin: 12px 0; }
p.item b { font-weight: 600; }
p.item b.mk { color: var(--brand); }
blockquote {
  margin: 12px 0; padding: 8px 14px; font-size: 13px; color: var(--text-muted);
  background: var(--brand-soft); border-left: 2px solid var(--brand);
  border-radius: 0 var(--radius) var(--radius) 0;
}
ul.meta { margin: 4px 0 12px; padding-left: 20px; font-size: 13px; color: var(--text-muted); }
ul.meta li { margin: 2px 0; }
code {
  font-family: "JetBrains Mono", ui-monospace, Consolas, monospace; font-size: 13px;
  background: var(--surface-muted); border-radius: 4px; padding: 1px 5px;
}
hr.rule { border: 0; border-top: 1px dashed var(--border); margin: 28px 0; }
.made { max-width: 780px; margin: 0 auto 40px; text-align: center; color: var(--text-muted); font-size: 12px; }
@media (max-width: 640px) { .sheet { margin: 0; border: 0; border-radius: 0; padding: 24px 18px; } }
@media print {
  body { background: #fff; }
  .sheet { border: 0; border-radius: 0; box-shadow: none; margin: 0; padding: 0; }
  hr.rule { border-top-color: #ccc; }
}
"""


def _inline(text: str) -> str:
    """转义后套用行内标记（加粗 / 斜体 / 行内代码）。"""
    out = html.escape(text)
    out = _BOLD_RE.sub(r"<strong>\1</strong>", out)
    out = _ITALIC_RE.sub(r"<em>\1</em>", out)
    out = _CODE_RE.sub(r"<code>\1</code>", out)
    return out


def _item_html(mark: str, rest: str) -> str:
    """事项行 `①、事项名：说明` → 序号与事项名加粗，说明保持正文。"""
    rest = rest.strip()
    head, sep, tail = rest.partition("：")
    prefix = f'<b class="mk">{html.escape(mark)}、</b>'
    if not sep:
        return prefix + _inline(head)
    return f"{prefix}<b>{_inline(head)}</b>：{_inline(tail)}"


def _render_blocks(md: str) -> str:
    """把 Markdown 正文转成 HTML 片段。"""
    out: List[str] = []
    in_list = False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for raw in (md or "").splitlines():
        stripped = raw.strip()
        if not stripped:
            close_list()
            continue
        if _HR_RE.match(stripped):
            close_list()
            out.append('<hr class="rule">')
            continue
        m = _HEADING_RE.match(stripped)
        if m:
            close_list()
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline(m.group(2).strip())}</h{level}>")
            continue
        m = _LIST_RE.match(stripped)
        if m:
            if not in_list:
                out.append('<ul class="meta">')
                in_list = True
            out.append(f"<li>{_inline(m.group(1).strip())}</li>")
            continue
        m = _QUOTE_RE.match(stripped)
        if m:
            close_list()
            out.append(f"<blockquote>{_inline(m.group(1).strip())}</blockquote>")
            continue
        close_list()
        m = _ITEM_RE.match(stripped)
        if m:
            out.append(f'<p class="item">{_item_html(m.group("mark"), m.group("rest"))}</p>')
            continue
        cls = ' class="proj"' if _PROJ_RE.match(stripped) else ""
        out.append(f"<p{cls}>{_inline(stripped)}</p>")

    close_list()
    return "\n".join(out)


def _first_heading(md: str) -> str:
    """取文档首个标题（优先一级标题）作为网页标题。"""
    first = ""
    for line in (md or "").splitlines():
        m = _HEADING_RE.match(line.strip())
        if not m:
            continue
        text = m.group(2).strip()
        if len(m.group(1)) == 1:
            return text
        if not first:
            first = text
    return first


def markdown_to_html(md: str, title: Optional[str] = None) -> str:
    """把报告 Markdown 转成一个自包含的 HTML 单文件字符串。"""
    name = title or _first_heading(md) or "AI 工作总结"
    body = _render_blocks(md)
    return (
        '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(name)}</title>\n"
        f"<style>\n{_STYLE}</style>\n</head>\n<body>\n"
        f'<main class="sheet">\n{body}\n</main>\n'
        '<footer class="made">由 Aiscribe 生成 · 本机聚合，数据不出本机</footer>\n'
        "</body>\n</html>\n"
    )


def write_html(md: str, out_path: Path, title: Optional[str] = None) -> Path:
    """把报告 Markdown 写成 .html 文件（UTF-8、LF），返回路径。"""
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(markdown_to_html(md, title))
    return path
