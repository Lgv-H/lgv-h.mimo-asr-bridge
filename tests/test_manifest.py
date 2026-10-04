"""测试 manifest — 插件市场安装所需的元信息与依赖声明。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]

with (PLUGIN_DIR / "_manifest.json").open(encoding="utf-8") as manifest_file:
    MANIFEST = json.load(manifest_file)


class TestManifest(unittest.TestCase):
    """Manifest v2 的关键字段：宿主 ``extra="forbid"``，写错就加载失败。"""

    def test_identity(self):
        self.assertEqual(MANIFEST["manifest_version"], 2)
        self.assertEqual(MANIFEST["id"], "lgv-h.mimo-asr-bridge")
        self.assertEqual(MANIFEST["version"], "1.0.0")
        self.assertEqual(MANIFEST["plugin_type"], "media")
        self.assertEqual(MANIFEST["license"], "MIT")
        self.assertTrue(MANIFEST["name"])
        self.assertTrue(MANIFEST["description"])

    def test_plugin_dir_is_resolvable_by_host_lookup(self):
        """装进宿主后，目录名必须是宿主能解析的形式之一。

        宿主 ``get_plugin_candidate_paths()`` 会尝试
        ``plugin_id.replace(".", "_")``（只换点、连字符保留）和 ``plugin_id`` 本身；
        两者都对不上时，WebUI 的配置页 / README / 卸载都找不到插件。
        这条只在 ``plugins/`` 安装布局下检查 —— git 检出目录名是任意的。
        """

        if PLUGIN_DIR.parent.name != "plugins":
            self.skipTest(f"非 plugins/ 安装布局（父目录为 {PLUGIN_DIR.parent.name}），跳过目录名检查")

        accepted_names = {MANIFEST["id"], MANIFEST["id"].replace(".", "_")}
        self.assertIn(PLUGIN_DIR.name, accepted_names)

    def test_changelog_declaration_points_to_existing_file(self):
        self.assertEqual(MANIFEST["changelog"], "CHANGELOG.md")
        self.assertTrue((PLUGIN_DIR / MANIFEST["changelog"]).is_file())

    def test_license_file_matches_declaration(self):
        license_text = (PLUGIN_DIR / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("MIT License", license_text)
        self.assertIn("Lgv-H", license_text)
        self.assertEqual(MANIFEST["license"], "MIT")

    def test_author_matches_repository_owner(self):
        self.assertEqual(MANIFEST["author"]["url"], "https://github.com/Lgv-H")
        self.assertTrue(MANIFEST["urls"]["repository"].startswith("https://github.com/Lgv-H/"))

    def test_urls_are_http(self):
        for key in ("repository", "homepage", "documentation", "issues"):
            with self.subTest(field=key):
                self.assertTrue(MANIFEST["urls"][key].startswith("http"))
        self.assertTrue(MANIFEST["author"]["url"].startswith("http"))

    def test_declares_no_host_capability(self):
        # 插件自己完成音频编码和 HTTP 请求，不需要宿主任何能力（也不走 SDK 的 STT）
        self.assertEqual(MANIFEST["capabilities"], [])

    def test_only_aiohttp_dependency(self):
        self.assertEqual(
            MANIFEST["dependencies"],
            [
                {
                    "type": "python_package",
                    "name": "aiohttp",
                    "version_spec": ">=3.9.0",
                }
            ],
        )

    def test_version_ranges(self):
        self.assertEqual(MANIFEST["sdk"], {"min_version": "2.0.0", "max_version": "2.99.99"})
        self.assertEqual(
            MANIFEST["host_application"], {"min_version": "1.0.0", "max_version": "1.99.99"}
        )

    def test_i18n_default_locale(self):
        self.assertEqual(MANIFEST["i18n"]["default_locale"], "zh-CN")


if __name__ == "__main__":
    unittest.main()
