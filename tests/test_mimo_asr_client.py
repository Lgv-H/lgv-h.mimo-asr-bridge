"""测试 mimo_asr_client — 请求体、请求头、响应解析与重试策略。

全部离线：``_request`` 被打桩，不产生任何真实网络请求。
"""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _loader  # noqa: E402

audio_codec = _loader.load_submodule("audio_codec")
client_module = _loader.load_submodule("mimo_asr_client")
MimoAsrClient = client_module.MimoAsrClient
MimoAsrError = client_module.MimoAsrError

LOGGER = logging.getLogger("test.mimo_asr_client")
# 重试路径会打 WARNING，这里只关心返回值，别把日志刷进测试输出
LOGGER.setLevel(logging.CRITICAL)

PREPARED = audio_codec.PreparedAudio(
    format="wav",
    data_url="data:audio/wav;base64,UklGRg==",
    encoded_bytes=12,
    raw_bytes=8,
    converted=False,
)


def build_client(**overrides):
    """构造一个参数合法的客户端。"""

    params = {
        "api_key": "sk-test",
        "base_url": "https://api.xiaomimimo.com/v1",
        "model": "mimo-v2.5-asr",
        "auth_mode": "api-key",
        "language": "auto",
        "timeout_seconds": 30.0,
        "retry_times": 0,
        "stream": False,
        "logger": LOGGER,
    }
    params.update(overrides)
    return MimoAsrClient(**params)


class TestConstructor(unittest.TestCase):
    """构造期就该把写错的配置打回去。"""

    def test_endpoint_strips_trailing_slash(self):
        client = build_client(base_url="https://api.xiaomimimo.com/v1/")
        self.assertEqual(client.endpoint, "https://api.xiaomimimo.com/v1/chat/completions")

    def test_missing_api_key_raises(self):
        with self.assertRaises(MimoAsrError):
            build_client(api_key="")

    def test_missing_base_url_raises(self):
        with self.assertRaises(MimoAsrError):
            build_client(base_url="   ")

    def test_missing_model_raises(self):
        with self.assertRaises(MimoAsrError):
            build_client(model="")

    def test_invalid_auth_mode_raises(self):
        with self.assertRaises(MimoAsrError) as ctx:
            build_client(auth_mode="cookie")
        self.assertIn("auth_mode", str(ctx.exception))

    def test_language_defaults_to_auto(self):
        self.assertIn("language=auto", build_client(language="").describe_target())


class TestBuildPayload(unittest.TestCase):
    """请求体必须对齐 MiMo 官方的 Chat Completions 语音识别契约。"""

    def test_non_stream_payload(self):
        payload = build_client(language="zh")._build_payload(PREPARED)
        self.assertEqual(payload["model"], "mimo-v2.5-asr")
        self.assertEqual(payload["asr_options"], {"language": "zh"})
        self.assertNotIn("stream", payload)

        content = payload["messages"][0]["content"]
        self.assertEqual(len(content), 1)
        self.assertEqual(payload["messages"][0]["role"], "user")
        self.assertEqual(content[0]["type"], "input_audio")
        self.assertEqual(content[0]["input_audio"]["data"], PREPARED.data_url)
        self.assertEqual(content[0]["input_audio"]["format"], "wav")

    def test_stream_payload(self):
        payload = build_client(stream=True)._build_payload(PREPARED)
        self.assertTrue(payload["stream"])

    def test_mp3_format_is_forwarded(self):
        prepared = audio_codec.PreparedAudio(
            format="mp3",
            data_url="data:audio/mpeg;base64,SUQz",
            encoded_bytes=4,
            raw_bytes=3,
            converted=False,
        )
        input_audio = build_client()._build_payload(prepared)["messages"][0]["content"][0]["input_audio"]
        self.assertEqual(input_audio["format"], "mp3")
        self.assertTrue(input_audio["data"].startswith("data:audio/mpeg;base64,"))


class TestBuildHeaders(unittest.TestCase):
    """两种鉴权方式都要支持（官方文档说二选一）。"""

    def test_api_key_header(self):
        headers = build_client(auth_mode="api-key")._build_headers()
        self.assertEqual(headers["api-key"], "sk-test")
        self.assertNotIn("Authorization", headers)
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_bearer_header(self):
        headers = build_client(auth_mode="bearer")._build_headers()
        self.assertEqual(headers["Authorization"], "Bearer sk-test")
        self.assertNotIn("api-key", headers)

    def test_auth_mode_is_case_insensitive(self):
        headers = build_client(auth_mode="BEARER")._build_headers()
        self.assertIn("Authorization", headers)


class TestResponseParsing(unittest.TestCase):
    """响应解析：非流式取 message.content，流式累加 delta.content。"""

    def test_pick_message(self):
        payload = {"choices": [{"message": {"content": "你好"}}]}
        self.assertEqual(MimoAsrClient._pick_message(payload)["content"], "你好")

    def test_pick_message_without_choices(self):
        with self.assertRaises(MimoAsrError) as ctx:
            MimoAsrClient._pick_message({"choices": []})
        self.assertIn("choices", str(ctx.exception))

    def test_pick_message_without_message(self):
        with self.assertRaises(MimoAsrError):
            MimoAsrClient._pick_message({"choices": [{"finish_reason": "stop"}]})

    def test_content_text_shapes(self):
        self.assertEqual(client_module._content_text("abc"), "abc")
        self.assertEqual(
            client_module._content_text([{"type": "text", "text": "你"}, "好"]),
            "你好",
        )
        self.assertEqual(client_module._content_text(None), "")
        self.assertEqual(client_module._content_text(123), "")

    def test_delta_keeps_leading_space(self):
        chunk = {"choices": [{"delta": {"content": " 世界"}}]}
        self.assertEqual(MimoAsrClient._pick_delta_text(chunk), " 世界")

    def test_delta_without_choices(self):
        self.assertEqual(MimoAsrClient._pick_delta_text({"usage": {}}), "")


class TestTranscribeRetry(unittest.IsolatedAsyncioTestCase):
    """重试策略：只重试瞬时错误，且次数受 ``retry_times`` 约束。"""

    async def test_retry_then_success(self):
        client = build_client(retry_times=2)
        attempts = []

        async def flaky_request(self, payload):
            del self, payload
            attempts.append(1)
            if len(attempts) < 3:
                raise MimoAsrError("临时故障", retryable=True)
            return "识别成功"

        with patch.object(MimoAsrClient, "_request", new=flaky_request), patch(
            "asyncio.sleep", new=AsyncMock()
        ):
            text = await client.transcribe(PREPARED)

        self.assertEqual(text, "识别成功")
        self.assertEqual(len(attempts), 3)

    async def test_exhausted_retries_raise(self):
        client = build_client(retry_times=1)
        attempts = []

        async def always_fail(self, payload):
            del self, payload
            attempts.append(1)
            raise MimoAsrError("服务端 503", retryable=True)

        with patch.object(MimoAsrClient, "_request", new=always_fail), patch(
            "asyncio.sleep", new=AsyncMock()
        ):
            with self.assertRaises(MimoAsrError):
                await client.transcribe(PREPARED)

        self.assertEqual(len(attempts), 2)  # 首次 + 1 次重试

    async def test_non_retryable_error_does_not_retry(self):
        client = build_client(retry_times=3)
        attempts = []

        async def unauthorized(self, payload):
            del self, payload
            attempts.append(1)
            raise MimoAsrError("HTTP 401: invalid api key", retryable=False)

        with patch.object(MimoAsrClient, "_request", new=unauthorized), patch(
            "asyncio.sleep", new=AsyncMock()
        ):
            with self.assertRaises(MimoAsrError):
                await client.transcribe(PREPARED)

        self.assertEqual(len(attempts), 1)

    async def test_close_is_idempotent_without_session(self):
        client = build_client()
        await client.close()
        await client.close()


if __name__ == "__main__":
    unittest.main()
