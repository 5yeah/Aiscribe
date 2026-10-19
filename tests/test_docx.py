"""docx.py 测试：结构合法、XML 良构、内容完整回读。"""

import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from ai_report.docx import markdown_to_body_xml, read_docx_text, write_docx

MD = """# AI 日报 2026-10-09
> 数据来源：WorkBuddy / CodeBuddy CN（本地聚合，隐私优先）

## 按项目汇总

### 铜线检测项目

- [已完成] 铜线检测不准根因（CodeBuddy CN，2026-10-09 16:27 ~ 17:06）
- [进行中] 铜线缺陷复测（Cass，2026-10-09 14:00 ~ 14:30）

## 未分类

- [-] 整理WPS云文档中的简历（WorkBuddy，2026-10-09 09:02 ~ 09:15）

---
说明：本地仅存会话元数据，正文在云端，本报告为“任务列表级”汇总。
"""


class TestBodyXml(unittest.TestCase):
    def test_headings_and_list(self):
        xml = markdown_to_body_xml(MD)
        self.assertIn('w:val="Title"', xml)
        self.assertIn('w:val="Heading1"', xml)
        self.assertIn("铜线检测项目", xml)
        self.assertIn("• ", xml)

    def test_special_chars_escaped(self):
        xml = markdown_to_body_xml('a < b & c "d"')
        self.assertIn("&lt;", xml)
        self.assertIn("&amp;", xml)
        self.assertNotIn("a < b", xml)

    def test_empty_input(self):
        self.assertEqual(markdown_to_body_xml(""), "")


class TestWriteDocx(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "报告.docx"

    def tearDown(self):
        self.tmp.cleanup()

    def test_package_structure(self):
        write_docx(MD, self.path)
        with zipfile.ZipFile(self.path) as zf:
            names = set(zf.namelist())
            for required in ["[Content_Types].xml", "_rels/.rels",
                             "word/document.xml", "word/_rels/document.xml.rels",
                             "word/styles.xml"]:
                self.assertIn(required, names)
            # 所有 XML 必须良构
            for n in names:
                if n.endswith(".xml") or n.endswith(".rels"):
                    ET.fromstring(zf.read(n))

    def test_roundtrip_content(self):
        write_docx(MD, self.path)
        text = read_docx_text(self.path)
        self.assertIn("AI 日报 2026-10-09", text)      # 标题文字（不含 #）
        self.assertIn("铜线检测项目", text)
        self.assertIn("[已完成] 铜线检测不准根因", text)
        self.assertIn("任务列表级", text)

    def test_chinese_filename(self):
        write_docx(MD, self.path)
        self.assertTrue(self.path.is_file())
        self.assertGreater(self.path.stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
