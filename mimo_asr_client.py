"""MiMo 语音识别客户端：自己拼 Chat Completions 请求，不走 SDK 的 STT 能力。

请求形态对齐 MiMo 官方文档（OpenAI Chat Completions 兼容）：

.. code-block:: text

    POST {base_url}/chat/completions
    api-key: $MIMO_API_KEY
    {
      "model": "mimo-v2.5-asr",
      "messages": [{"role": "user", "content": [
        {"type": "input_audio",
         "input_audio": {"data": "data:audio/wav;base64,....", "format": "wav"}}
      ]}],
      "asr_options": {"language": "auto"}
    }

识别文本取 ``choices[0].message.content``；流式模式下累加 ``choices[0].delta.content``。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import asyncio
import json
import logging

import aiohttp

from .audio_codec import PreparedAudio

# 这些状态码代表服务端瞬时问题，值得重试；其余 4xx 属于请求本身写错了
_RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})

_AUTH_MODE_API_KEY = "api-key"
_AUTH_MODE_BEARER = "bearer"
SUPPORTED_AUTH_MODES = (_AUTH_MODE_API_KEY, _AUTH_MODE_BEARER)

_MAX_BACKOFF_SECONDS = 4.0


class MimoAsrError(RuntimeError):
    """MiMo ASR 调用失败。"""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        """记录错误信息以及是否值得重试。

        Args:
            message: 面向日志的错误描述。
            retryable: 网络抖动、限流等瞬时错误为 ``True``。
        """

        super().__init__(message)
        self.retryable = retryable


def _content_text(content: Any) -> str:
    """把 ``message.content`` 归一化成字符串（保留首尾空白，流式分片需要原始内容）。

    Args:
        content: 响应里的 content 字段，可能是字符串或内容分片数组。

    Returns:
        str: 归一化后的文本；无法识别时返回空串。
    """

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return ""


class MimoAsrClient:
    """MiMo ASR 的极简 HTTP 客户端。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        auth_mode: str,
        language: str,
        timeout_seconds: float,
        retry_times: int,
        stream: bool,
        logger: logging.Logger,
    ) -> None:
        """保存本次请求所需的所有参数。

        Args:
            api_key: MiMo API Key。
            base_url: API 根地址，需包含 ``/v1``。
            model: ASR 模型 ID。
            auth_mode: 鉴权请求头，``api-key`` 或 ``bearer``。
            language: ``asr_options.language``，取值 auto/zh/en。
            timeout_seconds: 单次请求超时。
            retry_times: 瞬时错误的重试次数。
            stream: 是否使用 SSE 流式返回。
            logger: 插件日志器。

        Raises:
            MimoAsrError: 关键参数缺失时抛出。
        """

        self._api_key = str(api_key or "").strip()
        self._base_url = str(base_url or "").strip().rstrip("/")
        self._model = str(model or "").strip()
        self._auth_mode = str(auth_mode or "").strip().lower()
        self._language = str(language or "auto").strip().lower() or "auto"
        self._timeout_seconds = max(float(timeout_seconds), 1.0)
        self._retry_times = max(int(retry_times), 0)
        self._stream = bool(stream)
        self._logger = logger
        self._session: Optional[aiohttp.ClientSession] = None

        if not self._api_key:
            raise MimoAsrError("未配置 MiMo api_key")
        if not self._base_url:
            raise MimoAsrError("未配置 MiMo base_url")
        if not self._model:
            raise MimoAsrError("未配置 MiMo model")
        if self._auth_mode not in SUPPORTED_AUTH_MODES:
            raise MimoAsrError(
                f"不支持的 asr.auth_mode: {auth_mode!r}，只能是 {' 或 '.join(SUPPORTED_AUTH_MODES)}"
            )

    @property
    def endpoint(self) -> str:
        """返回实际请求的完整地址。"""

        return f"{self._base_url}/chat/completions"

    def describe_target(self) -> str:
        """返回用于日志/首页卡片的接口摘要。"""

        return (
            f"{self.endpoint} (model={self._model}, auth={self._auth_mode}, "
            f"language={self._language}, stream={self._stream}, timeout={self._timeout_seconds:g}s)"
        )

    async def close(self) -> None:
        """关闭复用的 HTTP 会话。"""

        if self._session is None or self._session.closed:
            self._session = None
            return
        await self._session.close()
        self._session = None

    async def transcribe(self, audio: PreparedAudio) -> str:
        """把一段音频交给 MiMo 识别，返回文本。

        Args:
            audio: 已整理好的音频（wav/mp3 的 data URL）。

        Returns:
            str: 识别出的文本。

        Raises:
            MimoAsrError: 请求失败或响应里没有文本时抛出。
        """

        payload = self._build_payload(audio)
        self._logger.debug(
            "MiMo ASR 请求: endpoint=%s format=%s raw_bytes=%d encoded_bytes=%d converted=%s",
            self.endpoint,
            audio.format,
            audio.raw_bytes,
            audio.encoded_bytes,
            audio.converted,
        )

        for attempt in range(self._retry_times + 1):
            try:
                return await self._request(payload)
            except MimoAsrError as exc:
                if not exc.retryable or attempt >= self._retry_times:
                    raise
                backoff_seconds = min(0.5 * (2**attempt), _MAX_BACKOFF_SECONDS)
                self._logger.warning(
                    "MiMo ASR 第 %d 次请求失败（%s），%.1fs 后重试",
                    attempt + 1,
                    exc,
                    backoff_seconds,
                )
                await asyncio.sleep(backoff_seconds)

        # 循环体要么 return 要么 raise，这里只是让类型检查器确信函数不会隐式返回 None
        raise MimoAsrError("MiMo ASR 重试流程异常结束")

    def _build_payload(self, audio: PreparedAudio) -> Dict[str, Any]:
        """按 MiMo 文档拼请求体。"""

        # data URL 里已经带了 MIME_TYPE，format 本可省略；显式带上让服务端少一次猜测
        # （文档要求：同时给出 MIME_TYPE 与 format 时两者取值必须匹配）
        input_audio: Dict[str, Any] = {"data": audio.data_url, "format": audio.format}
        payload: Dict[str, Any] = {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "input_audio", "input_audio": input_audio}],
                }
            ],
            "asr_options": {"language": self._language},
        }
        if self._stream:
            payload["stream"] = True
        return payload

    def _build_headers(self) -> Dict[str, str]:
        """按配置的鉴权方式构造请求头。"""

        headers = {"Content-Type": "application/json"}
        if self._auth_mode == _AUTH_MODE_BEARER:
            headers["Authorization"] = f"Bearer {self._api_key}"
        else:
            headers["api-key"] = self._api_key
        return headers

    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建一个复用的 aiohttp 会话。"""

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
            )
        return self._session

    async def _request(self, payload: Dict[str, Any]) -> str:
        """执行一次请求并解析识别文本。"""

        session = await self._get_session()
        try:
            async with session.post(self.endpoint, json=payload, headers=self._build_headers()) as response:
                if response.status != 200:
                    body = (await response.text())[:500]
                    raise MimoAsrError(
                        f"MiMo ASR 返回 HTTP {response.status}: {body}",
                        retryable=response.status in _RETRYABLE_STATUS,
                    )
                if self._stream:
                    return await self._consume_stream(response)
                try:
                    decoded = await response.json(content_type=None)
                except ValueError as exc:
                    body = (await response.text())[:300]
                    raise MimoAsrError(f"MiMo ASR 返回的不是合法 JSON: {exc}；原始响应: {body}") from exc
        except aiohttp.ClientError as exc:
            raise MimoAsrError(f"MiMo ASR 网络错误: {exc}", retryable=True) from exc
        except asyncio.TimeoutError as exc:
            raise MimoAsrError(f"MiMo ASR 请求超时（{self._timeout_seconds:g}s）", retryable=True) from exc

        text = _content_text(self._pick_message(decoded).get("content")).strip()
        if not text:
            raise MimoAsrError(f"MiMo ASR 响应里没有识别文本: {str(decoded)[:300]}")
        return text

    async def _consume_stream(self, response: aiohttp.ClientResponse) -> str:
        """读取 SSE 流式响应并拼接识别文本。"""

        parts: List[str] = []
        async for raw_line in response.content:
            line = raw_line.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            body = line[len("data:") :].strip()
            if body == "[DONE]":
                break
            try:
                chunk = json.loads(body)
            except ValueError:
                self._logger.warning("MiMo ASR 流式分片无法解析: %s", body[:120])
                continue
            parts.append(self._pick_delta_text(chunk))

        text = "".join(parts).strip()
        if not text:
            raise MimoAsrError("MiMo ASR 流式响应里没有识别文本")
        return text

    @staticmethod
    def _pick_message(payload: Any) -> Dict[str, Any]:
        """取出非流式响应里的第一条 message。"""

        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not isinstance(choices, list) or not choices:
            raise MimoAsrError(f"MiMo ASR 响应缺少 choices: {str(payload)[:300]}")
        first_choice = choices[0]
        message = first_choice.get("message") if isinstance(first_choice, dict) else None
        if not isinstance(message, dict):
            raise MimoAsrError(f"MiMo ASR 响应缺少 message: {str(first_choice)[:300]}")
        return message

    @staticmethod
    def _pick_delta_text(chunk: Any) -> str:
        """从流式分片里取出增量文本。"""

        choices = chunk.get("choices") if isinstance(chunk, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return ""
        first_choice = choices[0]
        delta = first_choice.get("delta")
        if isinstance(delta, dict):
            return _content_text(delta.get("content"))
        message = first_choice.get("message")
        if isinstance(message, dict):
            return _content_text(message.get("content"))
        return ""
