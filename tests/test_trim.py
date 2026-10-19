"""时间剪辑测试：窗口过滤、跨夜、无时间戳保留、存取与 payload 联动。"""

import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from ai_report import index, trim
from ai_report.models import UnifiedRecord
from ai_report.utils import BJ_TZ

SUMMARY = "09:30 请覆盖部署；10:05 改完回归通过；23:40 顺手整理了脚本"


def _ts(hour, minute):
    return datetime(2026, 10, 16, hour, minute, tzinfo=BJ_TZ).timestamp()


def _rec(session_id="cid:2026-10-16", summary=SUMMARY):
    return UnifiedRecord(
        source="CodeBuddy CN", session_id=session_id, title="部署确认",
        status="completed", cwd="D:/project/sop-demo",
        start_ts=_ts(9, 0), end_ts=_ts(23, 50), summary=summary,
        raw={"messages": [{"ts": _ts(9, 30), "text": "请覆盖部署"},
                          {"ts": _ts(23, 40), "text": "顺手整理了脚本"}]},
    )


class TestClipText(unittest.TestCase):
    def test_window_keeps_only_inside(self):
        out = trim.clip_text(SUMMARY, "09:00", "12:00")
        self.assertIn("请覆盖部署", out)
        self.assertIn("改完回归通过", out)
        self.assertNotIn("整理了脚本", out)

    def test_overnight_window(self):
        out = trim.clip_text(SUMMARY, "23:00", "01:00")
        self.assertIn("整理了脚本", out)
        self.assertNotIn("请覆盖部署", out)

    def test_start_only(self):
        out = trim.clip_text(SUMMARY, "10:00", "")
        self.assertNotIn("请覆盖部署", out)
        self.assertIn("整理了脚本", out)

    def test_end_only(self):
        out = trim.clip_text(SUMMARY, "", "10:00")
        self.assertIn("请覆盖部署", out)
        self.assertNotIn("改完回归通过", out)

    def test_untimed_segment_is_kept(self):
        out = trim.clip_text("没有时间戳的一段；09:30 有时间戳", "12:00", "13:00")
        self.assertEqual(out, "没有时间戳的一段")

    def test_no_window_returns_unchanged(self):
        self.assertEqual(trim.clip_text(SUMMARY, "", ""), SUMMARY)

    def test_all_segments_clipped_returns_empty(self):
        self.assertEqual(trim.clip_text("09:30 只有这一段", "20:00", "21:00"), "")


class TestKeepTime(unittest.TestCase):
    def test_same_day(self):
        self.assertTrue(trim.keep_time("09:30", "09:00", "10:00"))
        self.assertFalse(trim.keep_time("10:30", "09:00", "10:00"))

    def test_overnight(self):
        self.assertTrue(trim.keep_time("23:30", "23:00", "01:00"))
        self.assertTrue(trim.keep_time("00:30", "23:00", "01:00"))
        self.assertFalse(trim.keep_time("12:00", "23:00", "01:00"))

    def test_unparsable_time_is_kept(self):
        self.assertTrue(trim.keep_time("", "09:00", "10:00"))
        self.assertTrue(trim.keep_time("--", "09:00", "10:00"))


class TestWindow(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(trim.normalize_time("9:05"), "09:05")
        self.assertEqual(trim.normalize_time(None), "")
        self.assertEqual(trim.window("9:00", "12:30"), {"start": "09:00", "end": "12:30"})

    def test_both_empty_means_no_trim(self):
        self.assertIsNone(trim.window("", ""))

    def test_invalid_time_raises(self):
        for bad in ("25:00", "9点", "09:70"):
            with self.assertRaises(ValueError):
                trim.normalize_time(bad)


class TestStore(unittest.TestCase):
    """存取走临时文件（AIREREPORT_TRIMS 覆盖），不碰仓库里的 trims.json。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "trims.json"
        self._env = mock.patch.dict(os.environ, {"AIREREPORT_TRIMS": str(self.path)})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_set_and_get(self):
        win = trim.set_trim("CodeBuddy CN|a", "9:00", "12:30")
        self.assertEqual(win, {"start": "09:00", "end": "12:30"})
        self.assertEqual(trim.get_trim("CodeBuddy CN|a"), win)
        self.assertEqual(trim.count(), 1)
        self.assertIsInstance(json.loads(self.path.read_text("utf-8")), dict)

    def test_empty_values_clear_the_entry(self):
        trim.set_trim("CodeBuddy CN|a", "09:00", "10:00")
        self.assertIsNone(trim.set_trim("CodeBuddy CN|a", "", ""))
        self.assertIsNone(trim.get_trim("CodeBuddy CN|a"))
        self.assertEqual(trim.count(), 0)

    def test_clear_all(self):
        trim.set_trim("a", "09:00", "10:00")
        trim.set_trim("b", "11:00", "12:00")
        self.assertEqual(trim.clear_all(), 2)
        self.assertEqual(trim.count(), 0)

    def test_clear_single(self):
        trim.set_trim("a", "09:00", "10:00")
        self.assertTrue(trim.clear_trim("a"))
        self.assertFalse(trim.clear_trim("a"))

    def test_missing_id_raises(self):
        with self.assertRaises(ValueError):
            trim.set_trim("", "09:00", "")

    def test_broken_file_is_ignored(self):
        self.path.write_text("{ not json", encoding="utf-8")
        self.assertEqual(trim.load_trims(), {})


class TestPayloadTrim(unittest.TestCase):
    """payload 里剪辑已生效：窗口外片段被丢弃，并回传 trim 状态。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._env = mock.patch.dict(
            os.environ, {"AIREREPORT_TRIMS": str(Path(self._tmp.name) / "trims.json")})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_full_detail_filters_messages(self):
        trim.set_trim(index.record_id(_rec()), "09:00", "12:00")
        entry = index.records_payload([_rec()], detail="full")["projects"][0]["records"][0]
        self.assertEqual([m["time"] for m in entry["messages"]], ["09:30"])
        self.assertEqual(entry["trim"], {"start": "09:00", "end": "12:00"})

    def test_summary_detail_clips_timeline(self):
        trim.set_trim(index.record_id(_rec()), "09:00", "12:00")
        entry = index.records_payload([_rec()], detail="summary")["projects"][0]["records"][0]
        self.assertIn("请覆盖部署", entry["summary"])
        self.assertNotIn("整理了脚本", entry["summary"])

    def test_fully_clipped_record_dropped_for_ai(self):
        trim.set_trim(index.record_id(_rec()), "13:00", "14:00")
        payload = index.records_payload([_rec()], detail="full")
        self.assertEqual(payload["total_records"], 0)

    def test_fully_clipped_record_kept_in_summary_for_undo(self):
        trim.set_trim(index.record_id(_rec()), "13:00", "14:00")
        entry = index.records_payload([_rec()], detail="summary")["projects"][0]["records"][0]
        self.assertTrue(entry["trimmed_out"])
        self.assertEqual(entry["summary"], "")

    def test_no_trim_leaves_payload_untouched(self):
        entry = index.records_payload([_rec()], detail="full")["projects"][0]["records"][0]
        self.assertNotIn("trim", entry)
        self.assertEqual(len(entry["messages"]), 2)


if __name__ == "__main__":
    unittest.main()
