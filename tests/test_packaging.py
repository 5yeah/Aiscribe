"""DSH 插件包清单测试：市场扫描要求根 package.json 与 npm 包元数据一致。"""

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "dsh-plugin"


def _load(path: Path) -> dict:
    return json.loads(path.read_text("utf-8"))


class TestDshPluginManifest(unittest.TestCase):
    def test_root_and_publish_manifest_agree(self):
        root = _load(ROOT / "package.json")
        pkg = _load(PLUGIN / "package.json")
        self.assertEqual(root["name"], pkg["name"])
        self.assertEqual(root["version"], pkg["version"])
        self.assertTrue(root.get("dsh", {}).get("bundle", {}).get("patch"))
        self.assertTrue(pkg.get("dsh", {}).get("bundle", {}).get("patch"))

    def test_patch_files_exist_and_reference_the_package(self):
        for manifest in (ROOT / "package.json", PLUGIN / "package.json"):
            spec = _load(manifest)["dsh"]["bundle"]["patch"]
            patch = (manifest.parent / spec).resolve()
            self.assertTrue(patch.is_file(), f"缺少 bundle patch：{patch}")
            text = patch.read_text("utf-8")
            self.assertIn("name: dsh-aireport", text)

    def test_entry_icon_and_locale_are_present(self):
        pkg = _load(PLUGIN / "package.json")
        for spec in ("main",):
            self.assertTrue((PLUGIN / pkg[spec]).is_file())
        self.assertTrue((PLUGIN / pkg["exports"]["./icon"]).is_file())
        for lang in ("zh", "en"):
            data = _load(PLUGIN / "locale" / f"{lang}.json")
            self.assertTrue(data["meta"]["title"])
            self.assertTrue(data["meta"]["description"])

    def test_market_requirements_are_declared(self):
        pkg = _load(PLUGIN / "package.json")
        self.assertTrue(pkg["engines"]["dsh"])
        self.assertIn("@deepseek-ai/dsh-mcp-client", pkg["peerDependencies"])
        self.assertIn("THIRD_PARTY_NOTICES.md", pkg["files"])
        self.assertIn("runtime/**", pkg["files"])
        self.assertTrue((PLUGIN / "THIRD_PARTY_NOTICES.md").is_file())

    def test_readme_has_install_command(self):
        text = (PLUGIN / "README.md").read_text("utf-8")
        self.assertIn("dsh plugin --profile", text)
        self.assertIn("dsh-aireport", text)
        self.assertIn("隐私", text)


if __name__ == "__main__":
    unittest.main()
