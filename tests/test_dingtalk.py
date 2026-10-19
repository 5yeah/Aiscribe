"""dingtalk.py 测试：字段映射、截断、拒绝逻辑、mock 验证网络调用。"""

import json
import os
import unittest
from unittest import mock

from ai_report import dingtalk
from ai_report.dingtalk import (
    CONTENT_LIMIT,
    DingTalkError,
    create_report,
    get_token,
    load_config,
    require_credentials,
    resolve_source,
    submit,
)

REPORT = "## 按项目汇总\n\n### 铜线检测项目\n- [已完成] 任务A\n\n## 未分类\n\n- [-] 杂项\n"


class TestResolveSource(unittest.TestCase):
    def test_default_mapping_all(self):
        contents = resolve_source(REPORT)
        self.assertEqual(len(contents), 1)
        self.assertEqual(contents[0]["key"], "今日完成工作")
        self.assertEqual(contents[0]["value"], REPORT)

    def test_mapping_projects_section(self):
        contents = resolve_source(REPORT, {"明日工作计划": "PROJECTS"})
        value = contents[0]["value"]
        self.assertIn("按项目汇总", value)
        self.assertIn("任务A", value)
        self.assertNotIn("杂项", value)

    def test_mapping_named_section(self):
        contents = resolve_source(REPORT, {"需要协调/帮助": "section:未分类"})
        self.assertIn("杂项", contents[0]["value"])

    def test_truncation_at_limit(self):
        big = "长" * 5000
        contents = resolve_source(big, {"今日完成工作": "ALL"})
        self.assertEqual(len(contents[0]["value"]), CONTENT_LIMIT)
        self.assertTrue(contents[0]["value"].endswith("…"))


class TestCredentials(unittest.TestCase):
    def test_missing_credentials_rejected(self):
        env = {"DINGTALK_APPKEY": "", "DINGTALK_APPSECRET": ""}
        with mock.patch.dict(os.environ, env):
            os.environ.pop("DINGTALK_APPKEY", None)
            os.environ.pop("DINGTALK_APPSECRET", None)
            with self.assertRaises(DingTalkError) as ctx:
                require_credentials()
            self.assertIn("DINGTALK_APPKEY", str(ctx.exception))

    def test_credentials_ok(self):
        with mock.patch.dict(os.environ, {
            "DINGTALK_APPKEY": "k", "DINGTALK_APPSECRET": "s"
        }):
            self.assertEqual(require_credentials(), ("k", "s"))


class TestConfig(unittest.TestCase):
    def test_missing_config_rejected(self):
        from pathlib import Path
        with self.assertRaises(DingTalkError) as ctx:
            load_config(Path("f:/nonexistent/dingtalk_config.json"))
        self.assertIn("不存在", str(ctx.exception))

    def test_missing_required_field(self):
        import tempfile
        from pathlib import Path
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as f:
            json.dump({"template_id": "t"}, f)
            path = f.name
        try:
            with self.assertRaises(DingTalkError) as ctx:
                load_config(Path(path))
            self.assertIn("userid", str(ctx.exception))
        finally:
            os.unlink(path)


class TestGate(unittest.TestCase):
    """人工审核闸门：默认绝不发送。"""

    def test_no_confirm_never_sends(self):
        with mock.patch.dict(os.environ, {
            "DINGTALK_APPKEY": "k", "DINGTALK_APPSECRET": "s"
        }):
            import tempfile
            from pathlib import Path
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                             encoding="utf-8") as f:
                json.dump({"template_id": "t", "userid": "u"}, f)
                cfg_path = f.name
            try:
                opener = mock.Mock()
                with mock.patch("builtins.print"):
                    sent = submit(REPORT, config_path=Path(cfg_path),
                                  confirm=False, opener=opener)
                self.assertFalse(sent)
                opener.assert_not_called()  # 连网络都没碰
            finally:
                os.unlink(cfg_path)

    def test_confirm_but_user_says_no(self):
        with mock.patch.dict(os.environ, {
            "DINGTALK_APPKEY": "k", "DINGTALK_APPSECRET": "s"
        }):
            import tempfile
            from pathlib import Path
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                             encoding="utf-8") as f:
                json.dump({"template_id": "t", "userid": "u"}, f)
                cfg_path = f.name
            try:
                opener = mock.Mock()
                with mock.patch("builtins.print"):
                    sent = submit(REPORT, config_path=Path(cfg_path),
                                  confirm=True, opener=opener,
                                  input_fn=lambda _p: "no")
                self.assertFalse(sent)
                opener.assert_not_called()
            finally:
                os.unlink(cfg_path)


class TestApi(unittest.TestCase):
    def test_get_token_ok(self):
        def opener(url, payload, headers):
            return {"access_token": "tok123", "errcode": 0}
        self.assertEqual(get_token("k", "s", opener=opener), "tok123")

    def test_get_token_errcode(self):
        def opener(url, payload, headers):
            return {"errcode": 40014, "errmsg": "invalid token"}
        with self.assertRaises(DingTalkError):
            get_token("k", "s", opener=opener)

    def test_create_report_payload_and_error(self):
        cfg = {"template_id": "tpl1", "userid": "u1"}
        contents = [{"key": "今日完成工作", "value": "内容"}]
        seen = {}

        def opener(url, payload, headers):
            seen["url"] = url
            seen["payload"] = payload
            return {"errcode": 0}

        create_report("tok", cfg, contents, opener=opener)
        self.assertIn("access_token=tok", seen["url"])
        body = seen["payload"]
        self.assertEqual(body["template_id"], "tpl1")
        self.assertEqual(body["userid"], "u1")
        self.assertEqual(body["contents"], contents)
        self.assertEqual(body["dd_from"], "ai-report")

        def bad(url, payload, headers):
            return {"errcode": 60020, "errmsg": "not allow to access"}
        with self.assertRaises(DingTalkError):
            create_report("tok", cfg, contents, opener=bad)


if __name__ == "__main__":
    unittest.main()
