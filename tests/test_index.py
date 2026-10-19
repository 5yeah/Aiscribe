"""L2 索引与分类的单元测试：PROJECT_MAP 各分支、状态归一、去重、日期过滤。"""

import unittest
from datetime import date

from ai_report import index
from ai_report.models import UnifiedRecord


def make_rec(**kw):
    base = dict(
        source="Test",
        session_id="s1",
        title="标题",
        cwd="",
        start_ts=0.0,
        end_ts=0.0,
    )
    base.update(kw)
    return UnifiedRecord(**base)


class TestClassify(unittest.TestCase):
    """PROJECT_MAP 各分支逐一验证。"""

    CASES = [
        ("copper-wire", "铜线检测项目"),
        ("f:/work/demo-vision/ffc", "铜线检测项目"),
        ("hmi-console", "上位机项目"),
        ("上位机联调", "上位机项目"),
        ("terminal-detect", "端子检测项目"),
        ("端子检测", "端子检测项目"),
        ("profile-flaw", "型材检测项目"),
        ("型材表面", "型材检测项目"),
        ("wafer-flaw", "晶圆检测项目"),
        ("晶圆缺陷分析", "晶圆检测项目"),
        ("pointcloud-change", "点云检测项目"),
        ("点云变化检测", "点云检测项目"),
        ("pcd_change", "点云检测项目"),
        ("eeg-signal", "脑电项目"),
    ]

    def test_project_map_branches(self):
        for text, expected in self.CASES:
            with self.subTest(text=text):
                self.assertEqual(index.classify(cwd=text), expected)

    def test_order_mismatch_falls_to_other(self):
        self.assertEqual(index.classify(cwd="random", title="nothing"), "其他")

    def test_empty_falls_to_other(self):
        self.assertEqual(index.classify(), "其他")

    def test_title_hints_supplement(self):
        """WorkBuddy 类记录：cwd 非项目目录，靠标题关键词补分类。"""
        self.assertEqual(
            index.classify(cwd="2026-10-09-09-02-18", title="铜线检测周报"),
            "铜线检测项目",
        )
        self.assertEqual(
            index.classify(cwd="", title="上位机界面优化"),
            "上位机项目",
        )


class TestStatus(unittest.TestCase):
    def test_status_map(self):
        self.assertEqual(index.normalize_status("completed"), "已完成")
        self.assertEqual(index.normalize_status("COMPLETED"), "已完成")
        self.assertEqual(index.normalize_status("working"), "进行中")
        self.assertEqual(index.normalize_status("Working"), "进行中")

    def test_unknown_status_kept(self):
        self.assertEqual(index.normalize_status("paused"), "paused")

    def test_empty_status(self):
        self.assertEqual(index.normalize_status(""), "-")


class TestFilter(unittest.TestCase):
    # 2026-10-09 10:00 北京时间 = 该时刻 epoch
    from datetime import datetime, timedelta
    from ai_report.utils import BJ_TZ

    TS = datetime(2026, 10, 9, 10, 0, tzinfo=BJ_TZ).timestamp()
    OUT_TS = datetime(2026, 10, 8, 10, 0, tzinfo=BJ_TZ).timestamp()

    def test_filter_single_day(self):
        recs = [make_rec(session_id="a", start_ts=self.TS),
                make_rec(session_id="b", start_ts=self.OUT_TS)]
        got = index.filter_by_date(recs, date_=date(2026, 10, 9))
        self.assertEqual([r.session_id for r in got], ["a"])

    def test_filter_range(self):
        recs = [make_rec(session_id="a", start_ts=self.TS),
                make_rec(session_id="b", start_ts=self.OUT_TS)]
        got = index.filter_by_date(recs, start=date(2026, 10, 9), end=date(2026, 10, 10))
        self.assertEqual([r.session_id for r in got], ["a"])

    def test_filter_none_returns_all(self):
        recs = [make_rec(session_id="a", start_ts=self.TS)]
        self.assertEqual(len(index.filter_by_date(recs)), 1)

    def test_filter_includes_earlier_session_active_today(self):
        """昨天创建、今天仍有活动的会话应计入今天的日报（回归）。"""
        recs = [
            make_rec(session_id="old-updated", start_ts=self.OUT_TS, end_ts=self.TS),
            make_rec(session_id="old-dead", start_ts=self.OUT_TS, end_ts=self.OUT_TS),
        ]
        got = index.filter_by_date(recs, date_=date(2026, 10, 9))
        self.assertEqual([r.session_id for r in got], ["old-updated"])


class TestGroupAndJson(unittest.TestCase):
    def test_group_by_project_order(self):
        recs = [
            make_rec(session_id="1", project="铜线检测项目"),
            make_rec(session_id="2", project="其他"),
            make_rec(session_id="3", project="铜线检测项目"),
        ]
        groups = index.group_by_project(recs)
        self.assertEqual(list(groups.keys()), ["铜线检测项目", "其他"])
        self.assertEqual(len(groups["铜线检测项目"]), 2)

    def test_json_roundtrip(self):
        recs = [make_rec(session_id="x", project="脑电项目", start_ts=1728000000.0)]
        text = index.to_json(recs)
        back = index.from_json(text)
        self.assertEqual(len(back), 1)
        self.assertEqual(back[0].project, "脑电项目")
        self.assertEqual(back[0].start_ts, 1728000000.0)


class TestCollectDedupe(unittest.TestCase):
    def test_dedupe_by_source_and_session(self):
        a = make_rec(session_id="same", source="A")
        b = make_rec(session_id="same", source="A")
        c = make_rec(session_id="same", source="B")
        got = index._dedupe([a, b, c])
        self.assertEqual(len(got), 2)  # A 去重一条，B 不同源保留


if __name__ == "__main__":
    unittest.main()
