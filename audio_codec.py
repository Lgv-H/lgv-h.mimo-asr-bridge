"""音频编码桥接：把适配器送来的语音二进制整理成 MiMo ASR 能吃的形式。

MiMo 的 ASR 接口（Chat Completions 兼容）只接受 **wav / mp3** 两种容器的
base64 data URL，而适配器交给宿主的语音可能是：

* ``wav`` —— NapCat 画像的 ``get_record(out_format="wav")``；
* ``mp3`` —— SnowLuma 适配器把服务端返回的 silk 转码后的结果；
* ``silk`` —— 适配器没开转码、或没装 ``pysilk`` / ``ffmpeg`` 时残留的原始格式；
* 其它（``amr`` / ``ogg`` / ``m4a`` / ``flac`` …）—— 其它协议适配器可能产出的容器。

本模块负责嗅探容器、按需解码/转码，并拼出 ``data:{MIME_TYPE};base64,...``。
silk 走 ``pysilk`` + 标准库 ``wave`` 封装（不需要 ffmpeg），其余容器才走 ffmpeg。
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from shutil import which
from typing import Dict

import asyncio
import base64
import subprocess
import tempfile
import wave

FORMAT_WAV = "wav"
FORMAT_MP3 = "mp3"
FORMAT_SILK = "silk"
FORMAT_AMR = "amr"
FORMAT_OGG = "ogg"
FORMAT_M4A = "m4a"
FORMAT_FLAC = "flac"
FORMAT_AAC = "aac"
FORMAT_UNKNOWN = "unknown"

# MiMo 官方支持的容器 → 请求体里要带的 MIME_TYPE
MIMO_MIME_TYPES: Dict[str, str] = {
    FORMAT_WAV: "audio/wav",
    FORMAT_MP3: "audio/mpeg",
}

# QQ 语音（silk）按 24000Hz 单声道解码，与 SnowLuma 适配器的转码链保持一致
SILK_SAMPLE_RATE = 24000

# ffmpeg 需要借助扩展名判定输入容器（尤其是 m4a 这类需要 seek 的）
_FFMPEG_INPUT_SUFFIX: Dict[str, str] = {
    FORMAT_AMR: ".amr",
    FORMAT_OGG: ".ogg",
    FORMAT_M4A: ".m4a",
    FORMAT_FLAC: ".flac",
    FORMAT_AAC: ".aac",
    FORMAT_UNKNOWN: ".dat",
}

_SILK_MAGIC_PREFIXES = (b"#!SILK_V3", b"\x02#!SILK_V3")


class AudioBridgeError(RuntimeError):
    """音频无法被整理成 MiMo 可接受的形式。"""


@dataclass(frozen=True)
class PreparedAudio:
    """可直接塞进 MiMo 请求体的音频。"""

    format: str
    data_url: str
    encoded_bytes: int
    raw_bytes: int
    converted: bool


def sniff_audio_format(data: bytes) -> str:
    """按 magic 头判断音频容器格式。

    Args:
        data: 音频二进制内容。

    Returns:
        str: 容器格式名；无法识别时返回 ``unknown``。
    """

    if len(data) < 12:
        return FORMAT_UNKNOWN
    if data.startswith(_SILK_MAGIC_PREFIXES):
        return FORMAT_SILK
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return FORMAT_WAV
    if data.startswith(b"ID3"):
        return FORMAT_MP3
    if data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        # MP3 帧同步；带 ADTS 头的 AAC 同样是 0xFFFx，靠第二字节的层位区分
        return FORMAT_AAC if (data[1] & 0x06) == 0 else FORMAT_MP3
    if data.startswith(b"#!AMR"):
        return FORMAT_AMR
    if data.startswith(b"OggS"):
        return FORMAT_OGG
    if data.startswith(b"fLaC"):
        return FORMAT_FLAC
    if data[4:8] == b"ftyp":
        return FORMAT_M4A
    return FORMAT_UNKNOWN


def decode_base64_audio(raw_base64: str) -> bytes:
    """把适配器给的 base64 文本解成二进制。

    兼容三种写法：纯 base64、``data:audio/wav;base64,xxx``、以及缺失 ``=`` 补位的 base64。

    Args:
        raw_base64: 原始 base64 文本。

    Returns:
        bytes: 解码后的音频二进制。

    Raises:
        AudioBridgeError: 内容为空或不是合法 base64 时抛出。
    """

    value = str(raw_base64 or "").strip()
    if not value:
        raise AudioBridgeError("语音段里没有音频数据")
    if value.startswith("data:") and "," in value:
        value = value.split(",", maxsplit=1)[1].strip()
    value = "".join(value.split())
    value += "=" * (-len(value) % 4)
    try:
        return base64.b64decode(value)
    except Exception as exc:
        raise AudioBridgeError(f"语音 base64 解码失败: {exc}") from exc


def resolve_ffmpeg_path(configured_path: str) -> str:
    """解析要使用的 ffmpeg 路径。

    Args:
        configured_path: 配置里显式指定的路径，可为空。

    Returns:
        str: ffmpeg 可执行文件路径；未配置且 PATH 里没有时返回空串。

    Raises:
        AudioBridgeError: 显式配置了路径但该文件不存在时抛出。
    """

    candidate = str(configured_path or "").strip()
    if candidate:
        if not Path(candidate).is_file():
            raise AudioBridgeError(f"配置的 ffmpeg 路径不存在: {candidate}")
        return candidate
    return which("ffmpeg") or ""


async def prepare_audio(
    raw_base64: str,
    *,
    ffmpeg_path: str,
    ffmpeg_timeout_seconds: float,
    max_base64_mb: float,
) -> PreparedAudio:
    """把语音二进制整理成 MiMo 可用的 data URL。

    Args:
        raw_base64: 适配器提供的 base64 语音数据。
        ffmpeg_path: 已解析的 ffmpeg 路径，可为空。
        ffmpeg_timeout_seconds: ffmpeg 转码超时。
        max_base64_mb: base64 字符串大小上限（MiMo 官方为 10MB）。

    Returns:
        PreparedAudio: 可直接用于请求体的音频。

    Raises:
        AudioBridgeError: 格式不受支持、转码失败或体积超限时抛出。
    """

    data = decode_base64_audio(raw_base64)
    detected_format = sniff_audio_format(data)
    converted = False

    if detected_format not in MIMO_MIME_TYPES:
        data = await _convert_to_wav(
            data,
            detected_format,
            ffmpeg_path=ffmpeg_path,
            ffmpeg_timeout_seconds=ffmpeg_timeout_seconds,
        )
        detected_format = FORMAT_WAV
        converted = True

    mime_type = MIMO_MIME_TYPES[detected_format]
    encoded = base64.b64encode(data).decode("ascii")
    max_bytes = int(max(0.0, max_base64_mb) * 1024 * 1024)
    if max_bytes and len(encoded) > max_bytes:
        raise AudioBridgeError(
            f"音频 base64 大小 {len(encoded) / 1024 / 1024:.2f}MB 超过配置上限 {max_base64_mb}MB，"
            "MiMo 官方限制为 10MB（约 7.5MB 原始音频）"
        )

    return PreparedAudio(
        format=detected_format,
        data_url=f"data:{mime_type};base64,{encoded}",
        encoded_bytes=len(encoded),
        raw_bytes=len(data),
        converted=converted,
    )


async def _convert_to_wav(
    data: bytes,
    detected_format: str,
    *,
    ffmpeg_path: str,
    ffmpeg_timeout_seconds: float,
) -> bytes:
    """把非 wav/mp3 的音频转成 wav。"""

    if detected_format == FORMAT_SILK:
        pcm_data = await asyncio.to_thread(_decode_silk_to_pcm, data)
        return _pcm_to_wav(pcm_data, SILK_SAMPLE_RATE)

    if not ffmpeg_path:
        raise AudioBridgeError(
            f"音频格式 {detected_format} 不是 MiMo 支持的 wav/mp3，且没有可用的 ffmpeg，无法转码"
            "（可在 [audio].ffmpeg_path 指定 ffmpeg 路径）"
        )
    return await asyncio.to_thread(
        _run_ffmpeg_to_wav,
        ffmpeg_path,
        data,
        detected_format,
        ffmpeg_timeout_seconds,
    )


def _decode_silk_to_pcm(data: bytes) -> bytes:
    """用 pysilk 把 silk 解成 s16le 单声道 PCM。

    Args:
        data: silk 二进制内容。

    Returns:
        bytes: PCM 二进制。

    Raises:
        AudioBridgeError: pysilk 未安装或解码失败时抛出。
    """

    try:
        from importlib import import_module

        pysilk = import_module("pysilk")
    except ImportError as exc:
        raise AudioBridgeError(
            "语音是 silk 编码，需要 pysilk 才能解码；请安装 silk-python（或用适配器的 silk 转码链）"
        ) from exc

    output_buffer = BytesIO()
    try:
        pysilk.decode(BytesIO(data), output_buffer, SILK_SAMPLE_RATE)
    except Exception as exc:
        raise AudioBridgeError(f"silk 解码失败: {exc}") from exc

    pcm_data = output_buffer.getvalue()
    if not pcm_data:
        raise AudioBridgeError("silk 解码结果为空")
    return pcm_data


def _pcm_to_wav(pcm_data: bytes, sample_rate: int) -> bytes:
    """给裸 PCM 套上 wav 头（16bit / 单声道）。"""

    buffer = BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm_data)
    return buffer.getvalue()


def _run_ffmpeg_to_wav(ffmpeg_path: str, data: bytes, source_format: str, timeout_seconds: float) -> bytes:
    """调用 ffmpeg 把任意容器转成 16kHz 单声道 wav。

    Args:
        ffmpeg_path: ffmpeg 可执行文件路径。
        data: 原始音频二进制。
        source_format: 已嗅探到的容器格式，用于选择输入扩展名。
        timeout_seconds: 转码超时。

    Returns:
        bytes: wav 二进制。

    Raises:
        AudioBridgeError: ffmpeg 执行失败或输出为空时抛出。
    """

    suffix = _FFMPEG_INPUT_SUFFIX.get(source_format, ".dat")
    with tempfile.TemporaryDirectory(prefix="mimo-asr-bridge-") as temp_dir:
        source_path = Path(temp_dir) / f"input{suffix}"
        output_path = Path(temp_dir) / "output.wav"
        source_path.write_bytes(data)
        try:
            completed = subprocess.run(
                [
                    ffmpeg_path,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(source_path),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-sample_fmt",
                    "s16",
                    "-f",
                    "wav",
                    str(output_path),
                ],
                capture_output=True,
                timeout=max(float(timeout_seconds), 1.0),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AudioBridgeError(f"ffmpeg 转码 {source_format} 超时（{timeout_seconds}s）") from exc
        except OSError as exc:
            raise AudioBridgeError(f"无法执行 ffmpeg（{ffmpeg_path}）: {exc}") from exc

        if completed.returncode != 0:
            stderr_text = completed.stderr.decode("utf-8", "ignore").strip()[:300]
            raise AudioBridgeError(f"ffmpeg 转码 {source_format} 失败（退出码 {completed.returncode}）: {stderr_text}")
        if not output_path.is_file():
            raise AudioBridgeError(f"ffmpeg 转码 {source_format} 未产出文件")

        wav_bytes = output_path.read_bytes()

    if not wav_bytes:
        raise AudioBridgeError(f"ffmpeg 转码 {source_format} 输出为空")
    return wav_bytes
