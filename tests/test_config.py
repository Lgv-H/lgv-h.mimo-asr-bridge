"""测试 config — config.toml 与配置模型的一致性，以及 WebUI Schema 元数据。"""

from __future__ import annotations

import sys
import tomllib
import unittest
from pathlib import Path

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _loader  # noqa: E402

module = _loader.load_plugin_module()

PLUGIN_DIR = Path(__file__).resolve().parents[1]

with (PLUGIN_DIR / "config.toml").open("rb") as config_file:
    CONFIG_DATA = tomllib.load(config_file)


class TestConfigTomlMatchesModel(unittest.TestCase):
    """config.toml 的段名/字段名必须和配置模型一一对应，避免宿主读不到值。

    注意：``config.toml`` 是**运行中的用户配置**（宿主与 WebUI 都会改写它，
    可能已经填了真实 api_key、打开了 enabled），所以这里只校验"结构对不对、
    模型能不能读"，不断言具体取值；出厂默认值由 ``TestModelDefaults`` 通过
    模型默认值来锁。
    """

    def test_config_toml_is_valid_for_model(self):
        config = module.BridgeConfig.model_validate(CONFIG_DATA)
        self.assertIsInstance(config.plugin.enabled, bool)
        self.assertTrue(config.asr.base_url.startswith("http"))
        self.assertIn("{text}", config.audio.text_template)

    def test_sections_match_model_fields(self):
        self.assertEqual(
            set(CONFIG_DATA),
            set(module.BridgeConfig.model_fields),
        )

    def test_every_section_field_is_declared(self):
        for section_name, section_values in CONFIG_DATA.items():
            with self.subTest(section=section_name):
                section_model = module.BridgeConfig.model_fields[section_name].annotation
                self.assertEqual(set(section_values), set(section_model.model_fields))

    def test_auth_mode_is_validated_by_model(self):
        invalid = {
            **CONFIG_DATA,
            "asr": {**CONFIG_DATA["asr"], "auth_mode": "cookie"},
        }
        with self.assertRaises(ValidationError):
            module.BridgeConfig.model_validate(invalid)

    def test_language_is_validated_by_model(self):
        invalid = {
            **CONFIG_DATA,
            "asr": {**CONFIG_DATA["asr"], "language": "ja"},
        }
        with self.assertRaises(ValidationError):
            module.BridgeConfig.model_validate(invalid)


class TestModelDefaults(unittest.TestCase):
    """出厂默认值：新插件必须默认关闭，接口默认指向官方地址。"""

    def setUp(self):
        self.defaults = module.BridgeConfig()

    def test_disabled_by_default(self):
        self.assertFalse(self.defaults.plugin.enabled)

    def test_default_endpoint(self):
        self.assertEqual(self.defaults.asr.base_url, "https://api.xiaomimimo.com/v1")
        self.assertEqual(self.defaults.asr.model, "mimo-v2.5-asr")
        self.assertEqual(self.defaults.asr.auth_mode, "api-key")
        self.assertEqual(self.defaults.asr.language, "auto")

    def test_default_audio_handling(self):
        self.assertEqual(self.defaults.audio.text_template, module.DEFAULT_TEXT_TEMPLATE)
        self.assertTrue(self.defaults.audio.remove_placeholder)
        self.assertEqual(self.defaults.audio.ffmpeg_path, "")


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
