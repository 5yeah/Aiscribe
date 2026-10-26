"""配置测试：默认只读当前用户目录，扫同机其他用户需要显式开关。"""

import os
import unittest
from pathlib import Path
from unittest import mock

from ai_report import config


class TestOtherUsersOptIn(unittest.TestCase):
    """多人共用一台机器时，别人的会话属于别人的隐私，不能被默认读进来。"""

    def _run(self, env_value=None):
        calls = []
        env = {k: v for k, v in os.environ.items() if k != config.ENV_SCAN_ALL_USERS}
        if env_value is not None:
            env[config.ENV_SCAN_ALL_USERS] = env_value
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(config, "_home", return_value=Path(r"C:\Users\me")), \
                mock.patch.object(config, "_other_homes",
                                  side_effect=lambda: calls.append(1) or []):
            config.default_paths()
        return calls

    def test_default_does_not_list_other_users(self):
        self.assertEqual(self._run(), [])

    def test_off_value_does_not_list_other_users(self):
        self.assertEqual(self._run("0"), [])

    def test_explicit_switch_enables_it(self):
        for value in ("1", "true", "YES", "on"):
            with self.subTest(value=value):
                self.assertEqual(self._run(value), [1])

    def test_current_user_paths_still_derived(self):
        with mock.patch.dict(os.environ, {k: v for k, v in os.environ.items()
                                          if k != config.ENV_SCAN_ALL_USERS}, clear=True), \
                mock.patch.object(config, "_home", return_value=Path(r"C:\Users\me")), \
                mock.patch.object(config, "_other_homes", return_value=[]):
            paths = config.default_paths()
        self.assertTrue(paths["WORKBUDDY_DB"].lower().startswith(r"c:\users\me"))
        self.assertTrue(paths["CODEX_DIR"].lower().startswith(r"c:\users\me"))


if __name__ == "__main__":
    unittest.main()
