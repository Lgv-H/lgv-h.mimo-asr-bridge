"""MiMo ASR 桥接服务：把语音消息转成文字交给 MaiBot。

整条链路都在插件内部完成，不使用 SDK 的 STT 能力（``ctx.llm.transcribe_audio``）：

1. ``chat.receive.before_process`` 在入站消息进入 ``SessionMessage.process()`` 之前触发；
2. 从 ``raw_message`` 的 voice 段取出适配器下发的 base64 音频；
3. :mod:`audio_codec` 嗅探容器、按需解码/转码，拼成 MiMo 要的 ``data:`` URL；
4. :mod:`mimo_asr_client` 直接 POST ``{base_url}/chat/completions``；
5. voice 段原地换成 text 段（默认 ``[语音: xxx]``），宿主随后重建纯文本。
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

import asyncio
import time

from maibot_sdk import Field, HomeCard, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from .audio_codec import AudioBridgeError, prepare_audio, resolve_ffmpeg_path
from .mimo_asr_client import MimoAsrClient, MimoAsrError

# 与主程序内置语音转写保持一致的写回格式
DEFAULT_TEXT_TEMPLATE = "[语音: {text}]"


class PluginSectionConfig(PluginConfigBase):
    """[plugin] 段：插件总开关与配置版本。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(
        default=False,
        description="是否启用插件",
        json_schema_extra={"group": "plugin"},
    )
    config_version: str = Field(
        default="1.0.0",
        description="配置版本，由插件自己维护（宿主校验要求存在）",
        json_schema_extra={"group": "plugin"},
    )


class AsrConfig(PluginConfigBase):
    """[asr] 段：MiMo 语音识别接口参数。"""

    __ui_label__ = "MiMo ASR"
    __ui_icon__ = "mic"
    __ui_order__ = 1

    api_key: str = Field(
        default="",
        description="小米 MiMo 开放平台（mimo.mi.com）的 API Key",
        json_schema_extra={"group": "asr", "placeholder": "在此填写 MiMo API Key"},
    )
    base_url: str = Field(
        default="https://api.xiaomimimo.com/v1",
        description="API 根地址（要带 /v1）；Token Plan 用户可改为 https://token-plan-cn.xiaomimimo.com/v1",
        json_schema_extra={"group": "asr", "placeholder": "https://api.xiaomimimo.com/v1"},
    )
    model: str = Field(
        default="mimo-v2.5-asr",
        description="ASR 模型 ID，当前官方只提供 mimo-v2.5-asr",
        json_schema_extra={"group": "asr", "placeholder": "mimo-v2.5-asr"},
    )
    auth_mode: Literal["api-key", "bearer"] = Field(
        default="api-key",
        description="鉴权请求头；api-key 走 `api-key: xxx`，bearer 走 `Authorization: Bearer xxx`",
        json_schema_extra={"group": "asr"},
    )
    language: Literal["auto", "zh", "en"] = Field(
        default="auto",
        description="识别语种；明确语种时识别效果更好，中英混说建议用 auto",
        json_schema_extra={"group": "asr"},
    )
    timeout_seconds: float = Field(
        default=45.0,
        description="单次识别请求超时（秒）",
        ge=1.0,
        le=300.0,
        json_schema_extra={"group": "asr", "placeholder": "45", "step": 1},
    )
    retry_times: int = Field(
        default=1,
        description="网络错误、限流或 5xx 时的重试次数",
        ge=0,
        le=5,
        json_schema_extra={"group": "asr", "placeholder": "1", "step": 1},
    )
    stream: bool = Field(
        default=False,
        description="是否使用 SSE 流式返回；非流式更省事，流式首字更快",
        json_schema_extra={"group": "asr"},
    )
    max_base64_mb: float = Field(
        default=10.0,
        description="Base64 音频字符串大小上限（MB），MiMo 官方限制为 10",
        ge=0.1,
        le=10.0,
        json_schema_extra={"group": "asr", "placeholder": "10", "step": 0.5},
    )


class AudioConfig(PluginConfigBase):
    """[audio] 段：音频编码与写回文本的规则。"""

    __ui_label__ = "音频处理"
    __ui_icon__ = "music"
    __ui_order__ = 2

    text_template: str = Field(
        default=DEFAULT_TEXT_TEMPLATE,
        description="识别结果写回消息时使用的模板，{text} 会被替换为识别文本（与主程序内置格式保持一致）",
        json_schema_extra={"group": "audio", "placeholder": DEFAULT_TEXT_TEMPLATE},
    )
    remove_placeholder: bool = Field(
        default=True,
        description="识别成功后移除适配器附带的 [voice] 占位文本段，避免和识别结果重复",
        json_schema_extra={"group": "audio"},
    )
    ffmpeg_path: str = Field(
        default="",
        description="ffmpeg 可执行文件路径；留空时从 PATH 自动探测。仅在音频既不是 wav/mp3 也不是 silk 时才会用到",
        json_schema_extra={"group": "audio", "placeholder": "留空则自动探测"},
    )
    ffmpeg_timeout_seconds: float = Field(
        default=30.0,
        description="ffmpeg 转码超时（秒）",
        ge=1.0,
        le=300.0,
        json_schema_extra={"group": "audio", "placeholder": "30", "step": 1},
    )


class BridgeConfig(PluginConfigBase):
    """顶层配置：聚合所有子配置段。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    asr: AsrConfig = Field(default_factory=AsrConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)

HOME_CARD_MARKDOWN = """**MiMo ASR 桥接服务**

语音消息进入主链前，本插件自行完成音频编码并调用 MiMo 语音识别
（Chat Completions + base64 音频），把 voice 段替换成 `[语音: 识别文本]`。
主程序不需要开启 `voice.enable_asr`。

配置要点：
- `[asr].api_key`：小米 MiMo 开放平台的 API Key
- `[asr].base_url`：默认 `https://api.xiaomimimo.com/v1`
- `[asr].language`：`auto` / `zh` / `en`
- `[audio].text_template`：写回消息的模板，必须包含 `{text}`

音频支持 wav / mp3 直传；silk 用 pysilk 解码后套 wav 头；其它容器交给 ffmpeg。
识别失败时原语音段保持不变，日志里会有 `MiMo ASR` 前缀的失败原因。
"""


class MimoAsrBridgePlugin(MaiBotPlugin):
    """插件主体。"""

    config_model = BridgeConfig

    async def on_load(self) -> None:
        """插件加载时初始化统计与客户端。"""

        self._client: Optional[MimoAsrClient] = None
        self._ffmpeg_path: str = ""
        self._last_error: str = ""
        self._stats: Dict[str, int] = {
            "segments": 0,
            "requests": 0,
            "success": 0,
            "failure": 0,
            "converted": 0,
            "latency_ms": 0,
            "chars": 0,
        }
        self._rebuild_client()
        if self._client is not None:
            self.ctx.logger.info("MiMo ASR 桥接服务已加载: %s", self._client.describe_target())
        else:
            self.ctx.logger.info("MiMo ASR 桥接服务已加载，但当前未生效（插件未启用或缺少 api_key）")

    async def on_unload(self) -> None:
        """插件卸载时释放 HTTP 会话。"""

        await self._close_client()
        self.ctx.logger.info("MiMo ASR 桥接服务已卸载")

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """配置热重载回调。"""

        del config_data
        if scope != "self":
            return
        await self._close_client()
        self._rebuild_client()
        self.ctx.logger.info("MiMo ASR 配置已更新 (version=%s)", version)

    # ===== 生命周期内部 =====

    async def _close_client(self) -> None:
        """关掉并丢弃当前客户端。"""

        if self._client is not None:
            await self._client.close()
            self._client = None

    def _rebuild_client(self) -> None:
        """按当前配置重建客户端，并探测一次 ffmpeg。

        Raises:
            MimoAsrError: 接口参数（api_key / base_url / model / auth_mode）不合法时抛出。
        """

        self._client = None
        if not self.config.plugin.enabled:
            return
        if not self.config.asr.api_key.strip():
            self.ctx.logger.error("未配置 [asr].api_key，MiMo ASR 不会转写任何语音")
            return

        self._client = MimoAsrClient(
            api_key=self.config.asr.api_key,
            base_url=self.config.asr.base_url,
            model=self.config.asr.model,
            auth_mode=self.config.asr.auth_mode,
            language=self.config.asr.language,
            timeout_seconds=self.config.asr.timeout_seconds,
            retry_times=self.config.asr.retry_times,
            stream=self.config.asr.stream,
            logger=self.ctx.logger,
        )
        if "{text}" not in self.config.audio.text_template:
            self.ctx.logger.error(
                "[audio].text_template 里没有 {text} 占位符，识别结果会被丢掉；请改成形如 %s",
                DEFAULT_TEXT_TEMPLATE,
            )
        self._ffmpeg_path = self._probe_ffmpeg()

    def _probe_ffmpeg(self) -> str:
        """探测 ffmpeg；它只有遇到非 wav/mp3/silk 的音频时才必需。"""

        try:
            ffmpeg_path = resolve_ffmpeg_path(self.config.audio.ffmpeg_path)
        except AudioBridgeError as exc:
            self.ctx.logger.error("[audio].ffmpeg_path 配置有误: %s", exc)
            return ""
        if ffmpeg_path:
            self.ctx.logger.info("ffmpeg 可用: %s", ffmpeg_path)
        else:
            self.ctx.logger.info("未找到 ffmpeg；非 wav/mp3/silk 的语音无法转码（silk 仍可用 pysilk 处理）")
        return ffmpeg_path

    # ===== HookHandler =====

    @HookHandler(
        "chat.receive.before_process",
        name="mimo_asr_transcribe",
        description="用 MiMo ASR 把入站消息里的语音段转成文字",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=90_000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def transcribe_voice_segments(
        self,
        message: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Optional[Dict[str, Any]]:
        """拦截入站消息，把 voice 段换成转写文本段。"""

        del kwargs
        if self._client is None or not isinstance(message, dict):
            return None

        raw_segments = message.get("raw_message")
        if not isinstance(raw_segments, list):
            return None

        voice_indexes = [
            index
            for index, segment in enumerate(raw_segments)
            if isinstance(segment, dict) and str(segment.get("type") or "").strip().lower() == "voice"
        ]
        if not voice_indexes:
            return None

        outcomes = await asyncio.gather(
            *(self._transcribe_segment(raw_segments[index]) for index in voice_indexes)
        )

        transcribed = 0
        for index, text in zip(voice_indexes, outcomes):
            if not text:
                continue
            segment = raw_segments[index]
            segment.clear()
            segment["type"] = "text"
            segment["data"] = self._render_text(text)
            transcribed += 1

        if not transcribed:
            return None

        if self.config.audio.remove_placeholder:
            self._drop_voice_placeholder(raw_segments)

        message["raw_message"] = raw_segments
        self.ctx.logger.info(
            "MiMo ASR 消息转写完成: session=%s voice_segments=%d transcribed=%d",
            message.get("session_id") or "",
            len(voice_indexes),
            transcribed,
        )
        return {"action": "continue", "modified_kwargs": {"message": message}}

    async def _transcribe_segment(self, segment: Dict[str, Any]) -> Optional[str]:
        """转写单个语音段；失败时记录原因并返回 ``None``（原段保留不动）。"""

        self._stats["segments"] += 1
        raw_base64 = segment.get("binary_data_base64")
        if not isinstance(raw_base64, str) or not raw_base64:
            self._record_failure("消息段里没有 binary_data_base64，取不到音频二进制")
            return None

        client = self._client
        if client is None:
            return None

        try:
            prepared = await prepare_audio(
                raw_base64,
                ffmpeg_path=self._ffmpeg_path,
                ffmpeg_timeout_seconds=self.config.audio.ffmpeg_timeout_seconds,
                max_base64_mb=self.config.asr.max_base64_mb,
            )
        except AudioBridgeError as exc:
            self._record_failure(f"音频编码失败: {exc}")
            return None

        if prepared.converted:
            self._stats["converted"] += 1

        self._stats["requests"] += 1
        started_at = time.monotonic()
        try:
            text = await client.transcribe(prepared)
        except MimoAsrError as exc:
            self._record_failure(f"MiMo ASR 调用失败: {exc}")
            return None

        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        self._stats["success"] += 1
        self._stats["latency_ms"] += elapsed_ms
        self._stats["chars"] += len(text)
        self.ctx.logger.info(
            "MiMo ASR 识别成功: format=%s raw_bytes=%d converted=%s elapsed_ms=%d chars=%d",
            prepared.format,
            prepared.raw_bytes,
            prepared.converted,
            elapsed_ms,
            len(text),
        )
        return text

    def _record_failure(self, detail: str) -> None:
        """统一记录失败：统计、最近错误、日志。"""

        self._stats["failure"] += 1
        self._last_error = detail
        self.ctx.logger.warning("MiMo ASR 转写失败: %s", detail)

    def _render_text(self, text: str) -> str:
        """按模板生成写回消息的文本。

        模板缺少 ``{text}`` 时用内置模板兜底，否则识别结果会被整段吞掉
        （这种配置错误在 ``on_load`` / ``on_config_update`` 时已经记过 ERROR 日志）。
        """

        template = self.config.audio.text_template
        if "{text}" not in template:
            template = DEFAULT_TEXT_TEMPLATE
        return template.replace("{text}", text)

    @staticmethod
    def _drop_voice_placeholder(segments: List[Dict[str, Any]]) -> None:
        """移除适配器随语音附带的 ``[voice]`` 占位文本段。

        宿主自己的 ``_remove_voice_placeholder_text_components`` 只在消息里还存在
        voice 段时才清理，而此时 voice 段已被替换成文本，所以这里自己处理。
        """

        segments[:] = [
            segment
            for segment in segments
            if not (
                isinstance(segment, dict)
                and str(segment.get("type") or "").strip().lower() == "text"
                and str(segment.get("data") or "").strip().lower() == "[voice]"
            )
        ]

    # ===== HomeCard =====

    @HomeCard(
        name="mimo_asr_status",
        title="MiMo ASR 桥接服务",
        content=HOME_CARD_MARKDOWN,
        description="插件内部直连 MiMo 语音识别接口，把语音消息转成文字",
        icon="\U0001F3A7",
        link_url="/plugin-config?plugin=lgv-h.mimo-asr-bridge",
        link_label="插件配置",
        width="medium",
        order=120,
    )
    async def mimo_asr_home_card(self) -> Dict[str, Any]:
        """返回运行状态与统计（宿主当前只渲染装饰器上的静态 content，这里保留动态数据）。"""

        stats = self._stats
        success_rate = (
            f"{stats['success'] / stats['requests'] * 100:.1f}%" if stats["requests"] else "N/A"
        )
        average_latency = f"{stats['latency_ms'] / stats['success']}ms" if stats["success"] else "N/A"
        return {
            "blocks": [
                {"type": "markdown", "text": HOME_CARD_MARKDOWN},
                {
                    "type": "key_value",
                    "entries": {
                        "插件状态": "启用" if self._client is not None else "未生效",
                        "接口": self._client.describe_target() if self._client is not None else "未配置",
                        "ffmpeg": self._ffmpeg_path or "未找到",
                        "语音段总数": str(stats["segments"]),
                        "成功 / 失败": f"{stats['success']} / {stats['failure']}",
                        "成功率": success_rate,
                        "平均耗时": average_latency,
                        "转码次数": str(stats["converted"]),
                        "累计识别字数": str(stats["chars"]),
                        "最近错误": self._last_error or "无",
                    },
                },
            ]
        }


def create_plugin() -> MimoAsrBridgePlugin:
    """宿主加载入口。"""

    return MimoAsrBridgePlugin()


# 目录名必须与 _manifest.json 的 id 保持一致：宿主按 plugin_id.replace(".", "_")
# 定位插件目录（连字符保留），不一致会让 WebUI 的配置页 / README / 卸载找不到插件。
