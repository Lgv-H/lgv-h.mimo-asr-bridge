"""测试 plugin — Hook 改写、失败语义、配置热重载与组件契约。

HTTP 层全程打桩，不联网。
"""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _fixtures  # noqa: E402
import _loader  # noqa: E402

module = _loader.load_plugin_module()
client_module = _loader.load_submodule("mimo_asr_client")

# SDK 把组件元数据挂在函数上的属性名（maibot_sdk.components._COMPONENT_INFO_ATTR）
COMPONENT_INFO_ATTR = "__maibot_component_info__"

VOICE_AUDIO = _fixtures.build_wav()
OCR_TEXT = "今天天气不错"


def build_message(segments, session_id: str = "stream-1") -> dict:
    """构造 Hook 会收到的入站消息字典（只保留本插件用到的键）。"""

    return {
        "message_id": "msg-1",
        "session_id": session_id,
        "processed_plain_text": "",
        "raw_message": list(segments),
    }


class PluginTestCase(unittest.IsolatedAsyncioTestCase):
    """公共装配：启用插件 + 打桩 HTTP + 静音插件日志。"""

    async def asyncSetUp(self) -> None:
        self.plugin = _loader.build_configured_plugin()
        await self.plugin.on_load()
        self.addAsyncCleanup(self.plugin.on_unload)

        self.logger = self.plugin.ctx.logger
        self.logger.setLevel(logging.CRITICAL)
        self.addCleanup(self.logger.setLevel, logging.NOTSET)

        self.request_mock = AsyncMock(return_value=OCR_TEXT)
        patcher = patch.object(client_module.MimoAsrClient, "_request", new=self.request_mock)
        patcher.start()
        self.addCleanup(patcher.stop)


class TestPluginContract(PluginTestCase):
    """插件入口与组件装饰器契约。"""

    def test_create_plugin_returns_sdk_plugin(self):
        plugin = module.create_plugin()
        self.assertIsInstance(plugin, module.MimoAsrBridgePlugin)
        self.assertIs(plugin.config_model, module.BridgeConfig)

    def test_hook_component_metadata(self):
        info = getattr(module.MimoAsrBridgePlugin.transcribe_voice_segments, COMPONENT_INFO_ATTR)
        self.assertEqual(info.hook, "chat.receive.before_process")
        self.assertEqual(info.mode.value, "blocking")
        self.assertEqual(info.error_policy.value, "skip")
        self.assertGreater(info.timeout_ms, 0)

    def test_drop_voice_placeholder_only_drops_exact_placeholder(self):
        segments = [
            {"type": "text", "data": "[voice]"},
            {"type": "text", "data": "[VOICE]"},
            {"type": "text", "data": "[语音: 你好]"},
            {"type": "voice", "data": ""},
            {"type": "image", "data": ""},
        ]
        module.MimoAsrBridgePlugin._drop_voice_placeholder(segments)
        self.assertEqual(
            [segment["data"] for segment in segments if segment["type"] == "text"],
            ["[语音: 你好]"],
        )
        self.assertEqual(len(segments), 3)


class TestHookRewrite(PluginTestCase):
    """语音段改写与占位清理。"""

    async def test_voice_segment_is_replaced_by_text(self):
        message = build_message(
            [
                {"type": "text", "data": "[voice]"},
                _fixtures.build_voice_segment(VOICE_AUDIO),
            ]
        )

        result = await self.plugin.transcribe_voice_segments(message=message)

        self.assertEqual(result["action"], "continue")
        self.assertIs(result["modified_kwargs"]["message"], message)
        self.assertEqual(message["raw_message"], [{"type": "text", "data": f"[语音: {OCR_TEXT}]"}])
        self.request_mock.assert_awaited_once()
        self.assertEqual(self.plugin._stats["success"], 1)
        self.assertEqual(self.plugin._stats["requests"], 1)
        self.assertEqual(self.plugin._stats["segments"], 1)
        self.assertEqual(self.plugin._last_error, "")

    async def test_other_segments_are_untouched(self):
        message = build_message(
            [
                {"type": "text", "data": "前面"},
                _fixtures.build_voice_segment(VOICE_AUDIO),
                {"type": "image", "data": "", "hash": "img-hash"},
            ]
        )

        await self.plugin.transcribe_voice_segments(message=message)

        segments = message["raw_message"]
        self.assertEqual([segment["type"] for segment in segments], ["text", "text", "image"])
        self.assertEqual(segments[0]["data"], "前面")
        self.assertEqual(segments[1]["data"], f"[语音: {OCR_TEXT}]")
        self.assertEqual(segments[2]["hash"], "img-hash")

    async def test_multiple_voice_segments_are_all_transcribed(self):
        self.request_mock.side_effect = ["第一句", "第二句"]
        message = build_message(
            [
                _fixtures.build_voice_segment(VOICE_AUDIO),
                _fixtures.build_voice_segment(VOICE_AUDIO),
            ]
        )

        await self.plugin.transcribe_voice_segments(message=message)

        self.assertEqual(
            {segment["data"] for segment in message["raw_message"]},
            {"[语音: 第一句]", "[语音: 第二句]"},
        )
        self.assertEqual(self.request_mock.await_count, 2)

    async def test_custom_template_is_applied(self):
        plugin = _loader.build_configured_plugin({"audio": {"text_template": "{text}"}})
        await plugin.on_load()
        self.addAsyncCleanup(plugin.on_unload)

        message = build_message([_fixtures.build_voice_segment(VOICE_AUDIO)])
        await plugin.transcribe_voice_segments(message=message)

        self.assertEqual(message["raw_message"][0]["data"], OCR_TEXT)

    async def test_placeholder_kept_when_placeholder_removal_disabled(self):
        plugin = _loader.build_configured_plugin({"audio": {"remove_placeholder": False}})
        await plugin.on_load()
        self.addAsyncCleanup(plugin.on_unload)

        message = build_message(
            [
                {"type": "text", "data": "[voice]"},
                _fixtures.build_voice_segment(VOICE_AUDIO),
            ]
        )
        await plugin.transcribe_voice_segments(message=message)

        self.assertEqual(len(message["raw_message"]), 2)


class TestHookSkipConditions(PluginTestCase):
    """不该介入的情况必须原样放行（返回 None）。"""

    async def test_text_only_message(self):
        message = build_message([{"type": "text", "data": "你好"}])
        self.assertIsNone(await self.plugin.transcribe_voice_segments(message=message))
        self.request_mock.assert_not_awaited()

    async def test_missing_message(self):
        self.assertIsNone(await self.plugin.transcribe_voice_segments())

    async def test_raw_message_not_a_list(self):
        self.assertIsNone(
            await self.plugin.transcribe_voice_segments(message={"raw_message": "not-a-list"})
        )


class TestHookFailure(PluginTestCase):
    """失败时保留原语音段，并把原因暴露出来。"""

    async def test_api_failure_keeps_segment(self):
        self.request_mock.side_effect = client_module.MimoAsrError("HTTP 401: invalid api key")
        message = build_message([_fixtures.build_voice_segment(VOICE_AUDIO)])

        result = await self.plugin.transcribe_voice_segments(message=message)

        self.assertIsNone(result)
        self.assertEqual(message["raw_message"][0]["type"], "voice")
        self.assertIn("401", self.plugin._last_error)
        self.assertEqual(self.plugin._stats["failure"], 1)
        self.assertEqual(self.plugin._stats["success"], 0)

    async def test_audio_encoding_failure_does_not_call_api(self):
        # 显式清空 ffmpeg 路径，让"非 wav/mp3 且无 ffmpeg"分支与宿主环境无关
        self.plugin._ffmpeg_path = ""
        ogg_segment = _fixtures.build_voice_segment(b"OggS" + b"\x00" * 32)
        message = build_message([ogg_segment])

        result = await self.plugin.transcribe_voice_segments(message=message)

        self.assertIsNone(result)
        self.assertIn("ffmpeg", self.plugin._last_error)
        self.request_mock.assert_not_awaited()

    async def test_missing_binary_data_records_failure(self):
        message = build_message([{"type": "voice", "data": "", "hash": "no-binary"}])

        result = await self.plugin.transcribe_voice_segments(message=message)

        self.assertIsNone(result)
        self.assertIn("binary_data_base64", self.plugin._last_error)
        self.request_mock.assert_not_awaited()

    async def test_failed_transcription_keeps_placeholder(self):
        self.request_mock.side_effect = client_module.MimoAsrError("boom")
        message = build_message(
            [
                {"type": "text", "data": "[voice]"},
                _fixtures.build_voice_segment(VOICE_AUDIO),
            ]
        )

        self.assertIsNone(await self.plugin.transcribe_voice_segments(message=message))
        # 占位文本没被清掉，主程序内置 ASR（若开启）仍能按老路径兜底
        self.assertEqual([segment["data"] for segment in message["raw_message"]], ["[voice]", ""])


class TestConfiguration(PluginTestCase):
    """配置生效、热重载与模板退化。"""

    async def test_home_card_reports_status(self):
        blocks = (await self.plugin.mimo_asr_home_card())["blocks"]
        self.assertEqual(blocks[0]["type"], "markdown")
        entries = blocks[1]["entries"]
        self.assertIn("成功率", entries)
        self.assertIn("chat/completions", entries["接口"])
        self.assertEqual(entries["插件状态"], "启用")

    async def test_config_update_rebuilds_client(self):
        self.assertIsNotNone(self.plugin._client)

        self.plugin.set_plugin_config(
            {"plugin": {"enabled": True, "config_version": "1.0.0"}, "asr": {"api_key": ""}}
        )
        await self.plugin.on_config_update("self", {}, "1.0.1")
        self.assertIsNone(self.plugin._client)

        self.plugin.set_plugin_config(
            {"plugin": {"enabled": True, "config_version": "1.0.0"}, "asr": {"api_key": "sk-again"}}
        )
        await self.plugin.on_config_update("self", {}, "1.0.2")
        self.assertIsNotNone(self.plugin._client)

    async def test_config_update_ignores_other_scopes(self):
        client_before = self.plugin._client
        await self.plugin.on_config_update("other", {}, "whatever")
        self.assertIs(self.plugin._client, client_before)

    async def test_render_text_falls_back_to_builtin_template(self):
        plugin = _loader.build_configured_plugin({"audio": {"text_template": "语音"}})
        self.assertEqual(plugin._render_text("你好"), "[语音: 你好]")

    async def test_bad_ffmpeg_path_is_rejected_at_load(self):
        bad_path = str(_loader.PLUGIN_DIR / "not-here" / "ffmpeg.exe")
        plugin = _loader.build_configured_plugin({"audio": {"ffmpeg_path": bad_path}})
        await plugin.on_load()
        self.addAsyncCleanup(plugin.on_unload)
        self.assertEqual(plugin._ffmpeg_path, "")

    async def test_on_unload_clears_client(self):
        plugin = _loader.build_configured_plugin()
        await plugin.on_load()
        await plugin.on_unload()
        self.assertIsNone(plugin._client)


class TestUnconfiguredPlugin(unittest.IsolatedAsyncioTestCase):
    """没配置好就不介入，但要在日志里吵一声。"""

    async def test_missing_api_key_disables_bridge(self):
        plugin = _loader.build_configured_plugin({"asr": {"api_key": ""}})

        with self.assertLogs(plugin.ctx.logger.name, level="ERROR") as captured:
            await plugin.on_load()
        self.addAsyncCleanup(plugin.on_unload)

        self.assertIsNone(plugin._client)
        self.assertTrue(any("api_key" in line for line in captured.output))
        message = build_message([_fixtures.build_voice_segment(VOICE_AUDIO)])
        self.assertIsNone(await plugin.transcribe_voice_segments(message=message))
        self.assertEqual(message["raw_message"][0]["type"], "voice")

    async def test_disabled_plugin_disables_bridge(self):
        plugin = _loader.build_configured_plugin({"plugin": {"enabled": False}})
        await plugin.on_load()
        self.addAsyncCleanup(plugin.on_unload)

        self.assertIsNone(plugin._client)
        self.assertIsNone(
            await plugin.transcribe_voice_segments(
                message=build_message([_fixtures.build_voice_segment(VOICE_AUDIO)])
            )
        )


if __name__ == "__main__":
    unittest.main()
