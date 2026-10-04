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
        # 官方要求：不要把上界锁死在小版本（否则麦麦一发新版插件就被挡），
        # 官方内置插件的做法是放到 999.999.999，只认真约束 min_version
        self.assertEqual(MANIFEST["sdk"], {"min_version": "2.0.0", "max_version": "999.999.999"})
        self.assertEqual(
            MANIFEST["host_application"], {"min_version": "1.0.0", "max_version": "999.999.999"}
        )

    def test_all_versions_are_strict_semver(self):
        # 提交清单要求：所有版本号都是三段式
        self.assertRegex(MANIFEST["version"], r"^\d+\.\d+\.\d+$")
        for key in ("host_application", "sdk"):
            for bound in ("min_version", "max_version"):
                with self.subTest(field=f"{key}.{bound}"):
                    self.assertRegex(MANIFEST[key][bound], r"^\d+\.\d+\.\d+$")

    def test_id_matches_market_rule(self):
        # 官方 ID 规则：^[a-z0-9]+(?:[.-][a-z0-9]+)+$
        self.assertRegex(MANIFEST["id"], r"^[a-z0-9]+(?:[.-][a-z0-9]+)+$")

    def test_repository_url_has_no_git_suffix(self):
        self.assertFalse(MANIFEST["urls"]["repository"].endswith(".git"))

    def test_market_required_root_files_exist(self):
        # 插件市场要求仓库根目录必须有这套文件
        for file_name in ("_manifest.json", "plugin.py", "LICENSE", "README.md"):
            with self.subTest(file=file_name):
                self.assertTrue((PLUGIN_DIR / file_name).is_file())

    def test_instance_config_is_gitignored(self):
        # 官方目录约定：config.toml 是实例配置，仓库里不提交、.gitignore 要忽略
        gitignore_text = (PLUGIN_DIR / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("/config.toml", gitignore_text)

    def test_i18n_default_locale(self):
        self.assertEqual(MANIFEST["i18n"]["default_locale"], "zh-CN")


if __name__ == "__main__":
    unittest.main()
