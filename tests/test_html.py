"""HTML 导出测试：Markdown 子集 → 自包含 HTML（转义、内联样式、落盘）。"""

import tempfile
import unittest
from pathlib import Path

from ai_report import cli
from ai_report.html import markdown_to_html, write_html

SAMPLE = """\
# AI 日报 2026-10-16

**汇报人：** 张三 ｜ **日期：** 2026年10月16日

> 数据来源：CodeBuddy CN / TRAE（本地聚合，隐私优先）

**1、Aiscribe（报告工具，09:06–22:08）：**

①、时间过滤修复：改成只收当日起始或跨夜不超过 1 天的短会话。

②、项目分类兜底：新增工作目录末段目录名。

### 1. 事项标题

- 具体动作：改完回归通过
- 时间：2026-10-16 09:30 ～ 10:05 ｜ 来源：CodeBuddy CN

---

*本报告由 AI 阅读本机原始对话后归纳生成。*
"""


class TestMarkdownToHtml(unittest.TestCase):
    def test_heading_and_title(self):
        page = markdown_to_html("# AI 日报\n\n正文")
        self.assertIn("<title>AI 日报</title>", page)
        self.assertIn("<h1>AI 日报</h1>", page)

    def test_self_contained(self):
        page = markdown_to_html(SAMPLE)
        self.assertIn("<style>", page)          # 样式内嵌
        self.assertNotIn("<script", page)       # 不带 JS
        self.assertNotIn("http://", page)       # 不引外部资源
        self.assertNotIn("https://", page)

    def test_escapes_html_from_material(self):
        page = markdown_to_html("# 标题\n\n正常 <script>alert(1)</script> 文本")
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<script>", page)

    def test_inline_markers(self):
        page = markdown_to_html("**加粗** 与 *斜体* 与 `code`")
        self.assertIn("<strong>加粗</strong>", page)
        self.assertIn("<em>斜体</em>", page)
        self.assertIn("<code>code</code>", page)

    def test_list_quote_and_rule(self):
        page = markdown_to_html("- 具体动作：改完回归通过\n\n> 数据来源：本机\n\n---")
        self.assertIn("<li>具体动作：改完回归通过</li>", page)
        self.assertIn("<blockquote>数据来源：本机</blockquote>", page)
        self.assertIn('<hr class="rule">', page)

    def test_third_level_heading(self):
        self.assertIn("<h3>1. 事项标题</h3>", markdown_to_html("### 1. 事项标题"))

    def test_project_line_gets_class(self):
        page = markdown_to_html("**1、Aiscribe：**")
        self.assertIn('class="proj"', page)
        self.assertIn("<strong>1、Aiscribe：</strong>", page)

    def test_circled_item_bolds_marker_and_label(self):
        page = markdown_to_html("①、时间过滤修复：只收当日会话。")
        self.assertIn('class="item"', page)
        self.assertIn('<b class="mk">①、</b>', page)
        self.assertIn("<b>时间过滤修复</b>：只收当日会话。", page)

    def test_title_falls_back_to_first_heading(self):
        page = markdown_to_html("## 只有二级标题")
        self.assertIn("<title>只有二级标题</title>", page)


class TestWriteHtml(unittest.TestCase):
    def test_write_creates_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_html(SAMPLE, Path(d) / "sub" / "日报.html")
            self.assertTrue(path.is_file())
            text = path.read_text("utf-8")
            self.assertIn("<!DOCTYPE html>", text)
            self.assertIn("<style>", text)

    def test_write_honours_custom_title(self):
        with tempfile.TemporaryDirectory() as d:
            path = write_html("# 原标题", Path(d) / "a.html", title="自定义标题")
            self.assertIn("<title>自定义标题</title>", path.read_text("utf-8"))


class TestCliHtml(unittest.TestCase):
    def test_cli_format_html(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "日报.html"
            code = cli.main(["daily", "--date", "2026-10-16",
                             "--format", "html", "--out", str(out)])
            self.assertEqual(code, 0)
            text = out.read_text("utf-8")
            self.assertIn("<!DOCTYPE html>", text)
            self.assertIn("本机聚合，数据不出本机", text)


if __name__ == "__main__":
    unittest.main()
