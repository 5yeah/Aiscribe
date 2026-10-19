"""L3 报告生成器测试：项目分组、状态映射、空数据处理、日期区间，
以及「骨架 + 槽位」（build_outline / render_outline）与归纳结果的格式校验（check_outline）。"""

import json
import unittest
from datetime import date, datetime

from ai_report import generator
from ai_report.models import UnifiedRecord
from ai_report.utils import BJ_TZ


def ts(y, m, d, hh=9, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=BJ_TZ).timestamp()


def rec(sid, project, title, status, start, source="Cass", cwd=""):
    return UnifiedRecord(
        source=source, session_id=sid, title=title, project=project,
        status=status, cwd=cwd, start_ts=start, end_ts=start + 1800,
        summary=title,
    )


def raw(sid, project, title, summary, start, status="-", source="Cass"):
    """标题与素材刻意不同的记录（用于去重 / 清洗类用例）。"""
    return UnifiedRecord(
        source=source, session_id=sid, title=title, project=project,
        status=status, cwd="", start_ts=start, end_ts=start + 1800,
        summary=summary,
    )


def clone(outline):
    """深拷贝骨架，便于在用例里逐项改坏。"""
    return json.loads(json.dumps(outline, ensure_ascii=False))


SAMPLE = [
    rec("1", "铜线检测项目", "铜线视觉调试", "已完成", ts(2026, 10, 9, 9)),
    rec("2", "铜线检测项目", "铜线缺陷复测", "进行中", ts(2026, 10, 9, 14)),
    rec("3", "上位机项目", "上位机联调", "已完成", ts(2026, 10, 9, 16)),
    rec("4", "其他", "随便聊聊", "-", ts(2026, 10, 9, 18)),
    rec("5", "铜线检测项目", "上周旧任务", "已完成", ts(2026, 10, 1, 9)),
]


class TestDaily(unittest.TestCase):
    def test_project_grouping(self):
        md = generator.render_daily(date(2026, 10, 9), records=SAMPLE)
        self.assertIn("## 一、铜线检测项目", md)
        self.assertIn("## 二、上位机项目", md)
        # 只含当天记录，10-01 的旧任务不能出现
        self.assertNotIn("上周旧任务", md)

    def test_item_layout_and_no_meta_lines(self):
        """条目下只留标题（+ 具体动作）；不再写「时间/来源/状态」元信息行。"""
        md = generator.render_daily(date(2026, 10, 9), records=SAMPLE)
        self.assertIn("### 1. 铜线视觉调试", md)
        self.assertIn("具体动作", md)
        self.assertNotIn("状态：", md)
        self.assertNotIn("- 时间：", md)
        self.assertNotIn("- 来源：", md)

    def test_uncategorized_section(self):
        md = generator.render_daily(date(2026, 10, 9), records=SAMPLE)
        self.assertIn("## 未分类", md)
        self.assertIn("随便聊聊", md)

    def test_source_kept_once_in_header_only(self):
        md = generator.render_daily(date(2026, 10, 9), records=SAMPLE)
        self.assertIn("> 数据来源：Cass", md)
        # 来源只在头部出现一次
        self.assertEqual(md.count("Cass"), 1)

    def test_no_meta_lines_for_template_styles(self):
        """学了模板的排版（circled / dash / numeric）同样不许有元信息行。"""
        for style in ({"item_style": "circled", "item_has_detail": True},
                      {"item_style": "dash"},
                      {"item_style": "numeric", "item_has_detail": True},
                      {"project_kind": "bold-arabic"}):
            md = generator.render_daily(date(2026, 10, 9), records=SAMPLE, style=style)
            self.assertNotIn("状态：", md, style)
            self.assertNotIn("时间：", md, style)

    def test_header_and_footer(self):
        md = generator.render_daily(date(2026, 10, 9), records=SAMPLE)
        self.assertIn("# AI 日报 2026-10-09", md)
        self.assertIn("本地聚合，隐私优先", md)
        self.assertIn("具体动作", md)


class TestEmpty(unittest.TestCase):
    def test_empty_daily_friendly(self):
        md = generator.render_daily(date(2026, 10, 10), records=[])
        self.assertIn("本期没有符合条件的 AI 会话记录", md)

    def test_empty_range_friendly(self):
        md = generator.render_range(date(2026, 1, 1), date(2026, 1, 7), records=[])
        self.assertIn("本期没有符合条件的 AI 会话记录", md)


class TestWeekly(unittest.TestCase):
    def test_week_range(self):
        # 2026-10-09 是周五，本周一=10-05，周日=10-11 → 包含 10-01 之外全部
        md = generator.render_weekly(date(2026, 10, 9), records=SAMPLE)
        self.assertIn("AI 周报 2026-10-05 ~ 2026-10-11", md)
        self.assertIn("铜线视觉调试", md)
        self.assertNotIn("上周旧任务", md)

    def test_week_covers_whole_week(self):
        # 周日传入也应得到同一周
        md = generator.render_weekly(date(2026, 10, 11), records=SAMPLE)
        self.assertIn("AI 周报 2026-10-05 ~ 2026-10-11", md)


class TestRange(unittest.TestCase):
    def test_range_inclusive(self):
        md = generator.render_range(date(2026, 10, 1), date(2026, 10, 9), records=SAMPLE)
        self.assertIn("上周旧任务", md)
        self.assertIn("铜线视觉调试", md)


class TestOutline(unittest.TestCase):
    """骨架由程序生成：项目顺序、条目位置在这里定死，AI 只能改文字。"""

    def test_outline_is_plain_structured_data(self):
        ol = generator.outline_daily(date(2026, 10, 9), records=SAMPLE)
        self.assertEqual([s["name"] for s in ol["sections"]],
                         ["铜线检测项目", "上位机项目", "其他"])
        first = ol["sections"][0]["items"][0]
        self.assertEqual(first["title"], "铜线视觉调试")
        # 骨架里不含任何排版标记（编号/标题符号由 render_outline 加）
        self.assertNotIn("#", first["title"])
        self.assertNotIn("一、", json.dumps(ol, ensure_ascii=False))

    def test_item_count_follows_records(self):
        ol = generator.outline_daily(date(2026, 10, 9), records=SAMPLE)
        self.assertEqual(len(ol["sections"][0]["items"]), 2)
        self.assertEqual(len(ol["sections"][1]["items"]), 1)

    def test_same_material_in_one_project_collapses_to_one_item(self):
        """同一项目内当日素材实质相同的两条（同会话跨天拆分/同一问题问两遍）只留一条。"""
        recs = [
            raw("a", "流程SOP项目", "部署问题", "新版部署怎么确认", ts(2026, 10, 9, 9)),
            raw("b", "流程SOP项目", "部署问题 [图片]", "新版部署怎么确认 [图片]", ts(2026, 10, 9, 10)),
        ]
        ol = generator.outline_daily(date(2026, 10, 9), records=recs)
        self.assertEqual(len(ol["sections"]), 1)
        self.assertEqual(len(ol["sections"][0]["items"]), 1)

    def test_same_question_in_two_sessions_collapses_even_with_different_times(self):
        """用户当天在两条会话里问了同一个问题 → 只留一条（时间戳不参与去重）。"""
        recs = [
            raw("a", "点云检测项目", "类别咨询",
                "13:30 我现在这个算法 检测的结果到底有哪些类别？ [图片]",
                ts(2026, 10, 9, 13)),
            raw("b", "点云检测项目", "类别咨询 [图片]",
                "13:37 我现在这个算法 检测的结果到底有哪些类别？ [图片]",
                ts(2026, 10, 9, 13)),
        ]
        ol = generator.outline_daily(date(2026, 10, 9), records=recs)
        self.assertEqual(len(ol["sections"][0]["items"]), 1)

    def test_body_drops_inline_timestamps(self):
        """条目正文不再带 13:30 这类时间戳（简洁口径；AI 路径与模板路径一致）。"""
        recs = [raw("a", "项目甲", "部署自检", "09:14 确认 CUDA 连通；09:20 继续",
                    ts(2026, 10, 9, 9))]
        ol = generator.outline_daily(date(2026, 10, 9), records=recs)
        self.assertEqual(ol["sections"][0]["items"][0]["body"], "确认 CUDA 连通")

    def test_boilerplate_only_day_is_dropped_entirely(self):
        """当日只发了「继续」这类口头指令的记录没有实质内容，不出现在报告里。"""
        recs = [
            raw("x", "流程SOP项目", "继续", "16:43 继续", ts(2026, 10, 9, 16)),
            raw("y", "端子检测项目", "结论行修正", "11:05 删除结果图里多余的结论行",
                ts(2026, 10, 9, 11)),
        ]
        ol = generator.outline_daily(date(2026, 10, 9), records=recs)
        self.assertEqual([s["name"] for s in ol["sections"]], ["端子检测项目"])
        md = generator.render_daily(date(2026, 10, 9), records=recs)
        self.assertNotIn("流程SOP项目", md)
        self.assertNotIn("继续", md)

    def test_uncategorized_section_is_unnumbered(self):
        md = generator.render_daily(date(2026, 10, 9), records=SAMPLE)
        self.assertIn("## 未分类", md)
        self.assertNotIn("## 三、其他", md)

    def test_render_outline_accepts_uncategorized_only(self):
        md = generator.render_outline({
            "title": "AI 日报 2026-10-09",
            "sources": "> 数据来源：Cass",
            "sections": [{"name": "其他", "items": [{"title": "零散讨论", "body": ""}]}],
        })
        self.assertIn("## 未分类", md)
        self.assertIn("零散讨论", md)

    def test_empty_outline_renders_friendly_notice(self):
        md = generator.render_outline({"title": "AI 日报 2026-10-09", "sources": "",
                                       "sections": []})
        self.assertIn("本期没有符合条件的 AI 会话记录", md)

    def test_short_version_appended_when_template_asks(self):
        """模板要求双版本时：详细版之后用 --- 分隔追加简洁版，简洁版不带说明。"""
        style = {"project_kind": "bold-arabic", "project_punct": "、",
                 "item_style": "circled", "item_has_detail": True, "short_version": True}
        recs = [raw("a", "项目甲", "部署自检", "确认 CUDA 连通", ts(2026, 10, 9, 9))]
        md = generator.render_daily(date(2026, 10, 9), records=recs, style=style)
        self.assertIn("①、部署自检：确认 CUDA 连通", md)
        self.assertIn("# AI 日报 2026-10-09（简洁版）", md)
        self.assertIn("---", md)
        tail = md[md.index("（简洁版）"):]
        self.assertIn("①、部署自检", tail)
        self.assertNotIn("确认 CUDA 连通", tail)     # 简洁版删掉标记后的说明

    def test_no_short_version_without_template_flag(self):
        md = generator.render_daily(date(2026, 10, 9), records=SAMPLE)
        self.assertNotIn("（简洁版）", md)


class TestCheckOutline(unittest.TestCase):
    """格式校验：归纳结果的结构必须与骨架一致，文字不许残留噪声。"""

    def setUp(self):
        self.ref = generator.outline_daily(date(2026, 10, 9), records=SAMPLE)

    def test_identical_structure_passes(self):
        self.assertEqual(generator.check_outline(clone(self.ref), self.ref), [])

    def test_non_dict_is_rejected(self):
        self.assertTrue(generator.check_outline("不是结构", self.ref))

    def test_section_count_mismatch(self):
        cand = clone(self.ref)
        cand["sections"].pop()
        self.assertTrue(any("项目分节数不一致" in p
                            for p in generator.check_outline(cand, self.ref)))

    def test_added_item_is_rejected(self):
        cand = clone(self.ref)
        cand["sections"][0]["items"].append({"title": "编的条目", "body": "编的正文"})
        self.assertTrue(any("条目数不一致" in p
                            for p in generator.check_outline(cand, self.ref)))

    def test_renamed_project_is_rejected(self):
        cand = clone(self.ref)
        cand["sections"][0]["name"] = "铜线"
        self.assertTrue(any("项目名被改动" in p
                            for p in generator.check_outline(cand, self.ref)))

    def test_empty_title_is_rejected(self):
        cand = clone(self.ref)
        cand["sections"][0]["items"][0]["title"] = "  "
        self.assertTrue(any("标题为空" in p
                            for p in generator.check_outline(cand, self.ref)))

    def test_emptied_body_is_rejected(self):
        cand = clone(self.ref)
        cand["sections"][0]["items"][0]["body"] = ""
        self.assertTrue(any("正文被清空" in p
                            for p in generator.check_outline(cand, self.ref)))

    def test_empty_body_is_fine_when_template_had_none(self):
        ref = {"title": "t", "sources": "",
               "sections": [{"name": "P", "items": [{"title": "只有标题", "body": ""}]}]}
        self.assertEqual(generator.check_outline(clone(ref), ref), [])

    def test_noise_in_text_is_rejected(self):
        for text, expect in (("09:14 重新编译", "时间戳"),
                             ("[图片] 结论是错的", "[图片]"),
                             ("参考 https://example.com/x", "URL"),
                             ("来源：本机日志", "元信息行"),
                             ("Traceback (most recent call last)", "日志"),
                             ("```python\nprint(1)\n```", "代码块")):
            cand = clone(self.ref)
            cand["sections"][0]["items"][0]["body"] = text
            problems = generator.check_outline(cand, self.ref)
            self.assertTrue(any(expect in p for p in problems), f"{text!r} → {problems}")

    def test_normal_time_words_are_not_flagged(self):
        """正文里排除时间戳的正则不能误伤「2 小时」「3 个项目」这类正常表述。"""
        cand = clone(self.ref)
        cand["sections"][0]["items"][0]["body"] = "耗时 2 小时完成，覆盖 3 个检测项"
        self.assertEqual(generator.check_outline(cand, self.ref), [])


if __name__ == "__main__":
    unittest.main()
