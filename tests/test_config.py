"""测试 config — 配置模型的默认值与 WebUI Schema 元数据。

``config.toml`` 是**安装实例的运行时配置**：由 Runner 依据 ``plugin.py`` 里的
``config_model`` 生成，仓库里不提交（官方目录约定，已写进 ``.gitignore``）。
所以本文件以配置模型为唯一事实来源；只有当目录里确实存在 ``config.toml``
（即插件已经装进宿主）时，才额外校验实例配置与模型对得上。
"""

from __future__ import annotations

import sys
import tomllib
import unittest
from pathlib import Path
from typing import Any, Dict

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _loader  # noqa: E402

module = _loader.load_plugin_module()

PLUGIN_DIR = Path(__file__).resolve().parents[1]
CONFIG_TOML = PLUGIN_DIR / "config.toml"


def load_instance_config() -> Dict[str, Any]:
    """读取安装实例的 ``config.toml``。

    Returns:
        Dict[str, Any]: 解析后的实例配置。

    Raises:
        unittest.SkipTest: 当前目录不是已安装实例（没有 Runner 生成的 config.toml）。
    """

    if not CONFIG_TOML.is_file():
        raise unittest.SkipTest("当前不是已安装实例（没有 Runner 生成的 config.toml），跳过实例配置检查")
    with CONFIG_TOML.open("rb") as config_file:
        return tomllib.load(config_file)


class TestDefaultConfigFromModel(unittest.TestCase):
    """Runner 用配置模型生成 config.toml，模型默认值就是出厂配置。"""

    def setUp(self):
        self.defaults = module.BridgeConfig()

    def test_dumped_defaults_cover_all_sections(self):
        dumped = self.defaults.model_dump(mode="python")
        self.assertEqual(set(dumped), {"plugin", "asr", "audio"})
        self.assertEqual(set(dumped["asr"]), set(module.AsrConfig.model_fields))
        self.assertEqual(set(dumped["audio"]), set(module.AudioConfig.model_fields))

    def test_defaults_round_trip_through_model(self):
        # 宿主把默认配置写进 config.toml 后，必须还能原样读回来
        dumped = self.defaults.model_dump(mode="python")
        self.assertEqual(module.BridgeConfig.model_validate(dumped).model_dump(mode="python"), dumped)

    def test_disabled_by_default(self):
        self.assertFalse(self.defaults.plugin.enabled)

    def test_config_version_is_semver(self):
        self.assertRegex(self.defaults.plugin.config_version, r"^\d+\.\d+\.\d+$")

    def test_default_endpoint(self):
        self.assertEqual(self.defaults.asr.base_url, "https://api.xiaomimimo.com/v1")
        self.assertEqual(self.defaults.asr.model, "mimo-v2.5-asr")
        self.assertEqual(self.defaults.asr.auth_mode, "api-key")
        self.assertEqual(self.defaults.asr.language, "auto")

    def test_default_audio_handling(self):
        self.assertEqual(self.defaults.audio.text_template, module.DEFAULT_TEXT_TEMPLATE)
        self.assertIn("{text}", self.defaults.audio.text_template)
        self.assertTrue(self.defaults.audio.remove_placeholder)
        self.assertEqual(self.defaults.audio.ffmpeg_path, "")


class TestInstanceConfigMatchesModel(unittest.TestCase):
    """已安装实例：config.toml 的段名/字段名必须和配置模型一一对应。

    ``config.toml`` 由宿主与 WebUI 改写（可能已经填了真实 api_key、打开了
    enabled），所以这里只校验"结构对不对、模型能不能读"，不断言具体取值。
    """

    def setUp(self):
        self.config_data = load_instance_config()

    def test_instance_config_is_valid_for_model(self):
        config = module.BridgeConfig.model_validate(self.config_data)
        self.assertIsInstance(config.plugin.enabled, bool)
        self.assertTrue(config.asr.base_url.startswith("http"))
        self.assertIn("{text}", config.audio.text_template)

    def test_sections_match_model_fields(self):
        self.assertEqual(set(self.config_data), set(module.BridgeConfig.model_fields))

    def test_every_section_field_is_declared(self):
        for section_name, section_values in self.config_data.items():
            with self.subTest(section=section_name):
                section_model = module.BridgeConfig.model_fields[section_name].annotation
                self.assertEqual(set(section_values), set(section_model.model_fields))

    def test_auth_mode_is_validated_by_model(self):
        invalid = {**self.config_data, "asr": {**self.config_data["asr"], "auth_mode": "cookie"}}
        with self.assertRaises(ValidationError):
            module.BridgeConfig.model_validate(invalid)

    def test_language_is_validated_by_model(self):
        invalid = {**self.config_data, "asr": {**self.config_data["asr"], "language": "ja"}}
        with self.assertRaises(ValidationError):
            module.BridgeConfig.model_validate(invalid)


class TestWebUiMetadata(unittest.TestCase):
    """配置页面元数据（分组标题、图标、下拉选项、数值边界）。"""

    def test_section_metadata(self):
        self.assertEqual(module.PluginSectionConfig.__ui_label__, "插件")
        self.assertEqual(module.AsrConfig.__ui_label__, "MiMo ASR")
        self.assertEqual(module.AsrConfig.__ui_icon__, "mic")
        self.assertEqual(module.AudioConfig.__ui_label__, "音频处理")
        self.assertLess(
            module.PluginSectionConfig.__ui_order__,
            module.AsrConfig.__ui_order__,
        )
        self.assertLess(module.AsrConfig.__ui_order__, module.AudioConfig.__ui_order__)

    def test_placeholder_metadata(self):
        api_key_field = module.AsrConfig.model_fields["api_key"]
        self.assertTrue(api_key_field.json_schema_extra["placeholder"])
        self.assertEqual(module.AsrConfig.model_fields["base_url"].json_schema_extra["group"], "asr")
        self.assertEqual(
            module.AudioConfig.model_fields["remove_placeholder"].json_schema_extra["group"],
            "audio",
        )

    def test_generated_schema_shapes(self):
        from maibot_sdk.config import generate_plugin_config_schema

        schema = generate_plugin_config_schema(module.BridgeConfig)
        sections = schema["sections"]
        self.assertEqual(set(sections), {"plugin", "asr", "audio"})
        self.assertEqual(sections["asr"]["title"], "MiMo ASR")
        self.assertEqual(sections["audio"]["title"], "音频处理")

        asr_fields = sections["asr"]["fields"]
        self.assertEqual(asr_fields["auth_mode"]["choices"], ["api-key", "bearer"])
        self.assertEqual(asr_fields["auth_mode"]["ui_type"], "select")
        self.assertEqual(asr_fields["language"]["choices"], ["auto", "zh", "en"])
        self.assertEqual(asr_fields["timeout_seconds"]["min"], 1.0)
        self.assertEqual(asr_fields["timeout_seconds"]["max"], 300.0)
        self.assertEqual(asr_fields["retry_times"]["max"], 5)
        self.assertEqual(asr_fields["max_base64_mb"]["max"], 10.0)
        self.assertEqual(asr_fields["api_key"]["group"], "asr")
        self.assertIn("MiMo", asr_fields["api_key"]["placeholder"])

        audio_fields = sections["audio"]["fields"]
        self.assertEqual(audio_fields["text_template"]["default"], module.DEFAULT_TEXT_TEMPLATE)


if __name__ == "__main__":
    unittest.main()
