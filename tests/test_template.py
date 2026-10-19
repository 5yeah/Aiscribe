"""格式克隆测试：样例特征抽取、多份合并、规范文本、模板存取、样式注入生成器。

所有用例都把模板目录指向临时目录（AIREREPORT_TEMPLATES_DIR），不碰真实仓库。
"""

import base64
import contextlib
import io
import os
import shutil
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from ai_report import cli, docx as docx_mod, generator, template, ui
from ai_report.models import UnifiedRecord
from ai_report.utils import BJ_TZ

# 样例一：与用户历史格式一致（整行加粗项目行 + 带圈事项 + 说明 + 双版本 + 斜体收尾）
SAMPLE_A = """# AI 日报 2026-10-14

**汇报人：** 张三 ｜ **部门：** 算法组 ｜ **日期：** 2026年10月14日

**1、项目甲（09:06–11:20）：**

①、部署自检：确认 torch 版本与 CUDA 连通。

②、链路排查：前端不显示结果，定位为推送格式错误。

**2、项目乙：**

①、方案评审：输出两套备选架构。

**3、待办事项：**

①、项目甲：回归测试未完成。

---

*数据来源：本地日志；本报告由 AI 阅读原始对话后归纳生成。*

---

# AI 日报（简洁版）2026-10-14

**1、项目甲：**

①、部署自检

②、链路排查

**2、项目乙：**

①、方案评审

**3、待办事项：**

①、回归测试
"""

# 样例二：同样的排版习惯，但没有收尾斜体行（测多数票合并）
SAMPLE_B = SAMPLE_A.replace(
    "\n*数据来源：本地日志；本报告由 AI 阅读原始对话后归纳生成。*\n", "\n")

# 少量样本：项目行用 `1、名字：`（不加粗）、事项用 `1. 名字`
SAMPLE_C = """# 周报 2026-10-12 ~ 2026-10-18

1、项目甲：

1. 部署自检
2. 链路排查

2、项目乙：

1. 方案评审
"""


def _rec(project, title, summary="", sid="s1"):
    start = datetime(2026, 10, 14, 9, 0, tzinfo=BJ_TZ).timestamp()
    return UnifiedRecord(
        source="Cass", session_id=sid, title=title, project=project,
        status="已完成", cwd="", start_ts=start, end_ts=start + 600,
        summary=summary or title,
    )


class TemplateTestBase(unittest.TestCase):
    """把模板目录重定向到临时目录，隔离真实仓库。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aireport-tpl-")
        self._old_dir = os.environ.get(template.TEMPLATES_ENV)
        self._old_name = os.environ.get("AIREREPORT_TEMPLATE")
        os.environ[template.TEMPLATES_ENV] = self.tmp
        os.environ.pop("AIREREPORT_TEMPLATE", None)

    def tearDown(self):
        if self._old_dir is None:
            os.environ.pop(template.TEMPLATES_ENV, None)
        else:
            os.environ[template.TEMPLATES_ENV] = self._old_dir
        if self._old_name is not None:
            os.environ["AIREREPORT_TEMPLATE"] = self._old_name
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_sample(self, name, text):
        path = Path(self.tmp) / name
        path.write_text(text, encoding="utf-8")
        return path


class TestExtract(TemplateTestBase):
    def test_title_and_meta_normalized(self):
        feat = template.extract_features(SAMPLE_A)
        self.assertEqual(feat["title"], "AI 日报 {date}")
        self.assertIn("{汇报人}", feat["meta"])
        self.assertIn("{日期}", feat["meta"])

    def test_project_heading_style(self):
        feat = template.extract_features(SAMPLE_A)
        self.assertEqual(feat["project"]["kind"], "bold-arabic")
        self.assertEqual(feat["project"]["punct"], "、")

    def test_project_heading_with_time_range_parens(self):
        """括号里带时间冒号的项目行也必须能识别（曾因 [^：:] 漏判）。"""
        feat = template.extract_features(SAMPLE_A)
        self.assertGreaterEqual(feat["project"]["count"], 6)

    def test_item_style_circled_with_detail(self):
        feat = template.extract_features(SAMPLE_A)
        self.assertEqual(feat["item"]["style"], "circled")
        self.assertTrue(feat["item"]["has_detail"])

    def test_short_version_and_todo_and_footer(self):
        feat = template.extract_features(SAMPLE_A)
        self.assertTrue(feat["short_version"])
        self.assertEqual(feat["todo"], "{n}、待办事项：")
        self.assertEqual(feat["footer"], "*{note}*")

    def test_plain_and_numeric_style(self):
        feat = template.extract_features(SAMPLE_C)
        self.assertEqual(feat["project"]["kind"], "plain-arabic")
        self.assertEqual(feat["item"]["style"], "numeric")
        self.assertFalse(feat["short_version"])


class TestMerge(TemplateTestBase):
    def test_vote_keeps_majority_and_fills_missing(self):
        merged = template.merge_features([
            template.extract_features(SAMPLE_A),
            template.extract_features(SAMPLE_B),
        ])
        # 两份样例的共性：项目行加粗 + 带圈事项
        self.assertEqual(merged["project"]["kind"], "bold-arabic")
        self.assertEqual(merged["item"]["style"], "circled")
        # 一份没有收尾行，另一份有 → 仍保留
        self.assertEqual(merged["footer"], "*{note}*")

    def test_confidence_recorded(self):
        merged = template.merge_features([template.extract_features(SAMPLE_A)])
        self.assertEqual(merged["project"]["kind"], "bold-arabic")
        merged2 = template.merge_features([template.extract_features(SAMPLE_A),
                                           template.extract_features(SAMPLE_C)])
        # 两种项目行写法冲突时，按多数票取其一
        self.assertIn(merged2["project"]["kind"], ("bold-arabic", "plain-arabic"))


class TestSpecBlock(TemplateTestBase):
    def test_spec_contains_stepwise_rules(self):
        tpl = template.learn_from_paths([self.write_sample("a.md", SAMPLE_A)], "甲模板")
        spec = tpl["spec"]
        self.assertIn("输出格式规范", spec)
        self.assertIn("甲模板", spec)
        self.assertIn("**{n}、{项目名}：**", spec)
        self.assertIn("{①}、{事项名}：{说明}", spec)
        self.assertIn("简洁版", spec)
        self.assertIn("不要", spec)  # 明确禁止照搬样例内容


class TestStore(TemplateTestBase):
    def test_save_load_list(self):
        self.write_sample("a.md", SAMPLE_A)
        tpl = template.learn_from_paths([Path(self.tmp) / "a.md"], "我的日报")
        path = template.save_template(tpl)
        self.assertTrue(path.is_file())
        loaded = template.load_template("我的日报")
        self.assertEqual(loaded["name"], "我的日报")
        self.assertIn("输出格式规范", loaded["spec"])
        names = [t["name"] for t in template.list_templates()]
        self.assertIn("我的日报", names)

    def test_missing_template_raises(self):
        with self.assertRaises(template.TemplateError):
            template.load_template("不存在的模板")

    def test_resolve_prefers_explicit_then_env_then_latest(self):
        for name in ("旧模板", "新模板"):
            template.save_template({"name": name, "features": {
                "project": {"kind": "hash", "punct": "、"}}})
        self.assertEqual(template.resolve_template("旧模板")["name"], "旧模板")
        os.environ["AIREREPORT_TEMPLATE"] = "旧模板"
        self.assertEqual(template.resolve_template()["name"], "旧模板")
        os.environ.pop("AIREREPORT_TEMPLATE")
        self.assertEqual(template.resolve_template()["name"], "新模板")

    def test_resolve_returns_none_when_empty(self):
        self.assertIsNone(template.resolve_template())
        self.assertIsNone(template.resolve_template("缺失"))


class TestStyleForGenerator(TemplateTestBase):
    def test_style_mapping(self):
        tpl = template.learn_from_paths([self.write_sample("a.md", SAMPLE_A)], "甲")
        style = template.style_for_generator(tpl)
        self.assertEqual(style["project_kind"], "bold-arabic")
        self.assertEqual(style["item_style"], "circled")
        self.assertTrue(style["item_has_detail"])

    def test_no_template_means_none(self):
        self.assertIsNone(template.style_for_generator(None))
        self.assertIsNone(template.style_for_generator({"features": {}}))

    def test_short_version_flag_passed_through(self):
        style = template.style_for_generator(
            {"features": {"project": {"kind": "bold-arabic"}, "short_version": True}})
        self.assertTrue(style["short_version"])
        style2 = template.style_for_generator(
            {"features": {"project": {"kind": "bold-arabic"}}})
        self.assertFalse(style2["short_version"])


class TestGeneratorStyle(TemplateTestBase):
    def test_style_applied_to_headings(self):
        tpl = template.learn_from_paths([self.write_sample("a.md", SAMPLE_A)], "甲")
        style = template.style_for_generator(tpl)
        md = generator.render_daily(date(2026, 10, 14),
                                    records=[_rec("项目甲", "部署自检", "确认 CUDA 连通")],
                                    style=style)
        self.assertIn("**1、项目甲：**", md)
        self.assertIn("①、部署自检：确认 CUDA 连通", md)
        # 学到的样式里时间行不出现（样例是纯事项行）
        self.assertNotIn("时间：", md)

    def test_default_layout_unchanged(self):
        md = generator.render_daily(date(2026, 10, 14),
                                    records=[_rec("项目甲", "部署自检")])
        self.assertIn("## 一、项目甲", md)
        self.assertIn("### 1. 部署自检", md)


class TestLearnFromFiles(TemplateTestBase):
    def test_docx_sample_supported(self):
        path = Path(self.tmp) / "sample.docx"
        docx_mod.write_docx(SAMPLE_A, path)
        tpl = template.learn_from_paths([path], "docx 模板")
        self.assertEqual(tpl["samples"][0]["kind"], "docx")
        self.assertTrue(tpl["features"])

    def test_image_only_raises_template_error(self):
        path = Path(self.tmp) / "shot.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n0000")
        with self.assertRaises(template.TemplateError):
            template.learn_from_paths([path], "图片模板")

    def test_unsupported_suffix_raises(self):
        path = Path(self.tmp) / "a.pdf"
        path.write_bytes(b"%PDF-1.4")
        with self.assertRaises(template.TemplateError):
            template.learn_from_paths([path], "x")

    def test_missing_file_raises(self):
        with self.assertRaises(template.TemplateError):
            template.learn_from_paths([Path(self.tmp) / "nope.md"], "x")

    def test_image_hint_lists_paths(self):
        path = Path(self.tmp) / "shot.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n0000")
        stub = template.stub_for_vision("图片模板", path.name)
        stub["samples"] = [{"file": path.name, "kind": "image", "chars": 0,
                            "path": str(path)}]
        hint = template.image_hint(stub)
        self.assertIn("shot.png", hint)
        self.assertIsNone(template.image_hint({"samples": []}))


class TestUiLearn(TemplateTestBase):
    def _b64(self, path):
        return base64.b64encode(Path(path).read_bytes()).decode("ascii")

    def test_learn_text_returns_spec(self):
        src = self.write_sample("a.md", SAMPLE_A)
        out = ui.learn_samples([{"name": "a.md", "data": self._b64(src)}], "UI模板")
        self.assertFalse(out["pending_vision"])
        self.assertIn("输出格式规范", out["spec"])
        self.assertTrue(Path(out["saved_to"]).is_file())

    def test_learn_image_returns_vision_hint(self):
        src = Path(self.tmp) / "shot.png"
        src.write_bytes(b"\x89PNG\r\n\x1a\n0000")
        out = ui.learn_samples([{"name": "shot.png", "data": self._b64(src)}], "UI图片")
        self.assertTrue(out["pending_vision"])
        self.assertIn("shot.png", out["image_hint"])

    def test_learn_rejects_empty_files(self):
        with self.assertRaises(ValueError):
            ui.learn_samples([], "x")
        with self.assertRaises(ValueError):
            ui.learn_samples([{"name": "a.md", "data": ""}], "x")

    def test_instruction_carries_format_spec(self):
        src = self.write_sample("a.md", SAMPLE_A)
        ui.learn_samples([{"name": "a.md", "data": self._b64(src)}], "UI模板")
        text = ui.build_instruction(["Cass|a:2026-10-14"], mode="daily",
                                    date_value="2026-10-14", template_name="UI模板")
        self.assertIn("输出格式规范", text)
        self.assertIn("{①}、{事项名}", text)
        self.assertIn("--template", text)

    def test_instruction_without_template_has_no_spec(self):
        text = ui.build_instruction(["Cass|a:2026-10-14"], mode="daily",
                                    date_value="2026-10-14")
        self.assertNotIn("输出格式规范", text)

    def test_template_detail_reports_style(self):
        src = self.write_sample("a.md", SAMPLE_A)
        ui.learn_samples([{"name": "a.md", "data": self._b64(src)}], "UI模板")
        detail = ui.template_detail("UI模板")
        self.assertEqual(detail["style"]["item_style"], "circled")
        self.assertIn("输出格式规范", detail["spec"])


class TestCli(TemplateTestBase):
    def test_learn_and_templates_commands(self):
        src = self.write_sample("a.md", SAMPLE_A)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["learn", str(src), "--name", "CLI模板"])
        self.assertEqual(code, 0)
        self.assertIn("输出格式规范", buf.getvalue())

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["templates"])
        self.assertEqual(code, 0)
        self.assertIn("CLI模板", buf.getvalue())

    def test_style_from_template_helper(self):
        template.save_template({"name": "CLI模板", "features": {
            "project": {"kind": "bold-arabic", "punct": "、"},
            "item": {"style": "circled", "has_detail": True}}})
        style = cli._style_from_template("CLI模板")
        self.assertEqual(style["item_style"], "circled")
        self.assertIsNone(cli._style_from_template(None))


if __name__ == "__main__":
    unittest.main()
