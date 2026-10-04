"""测试用的音频样本构造器（纯标准库 + 可选 pysilk）。"""

from __future__ import annotations

import base64
import struct
import wave
from io import BytesIO


def build_pcm(seconds: float = 0.2, sample_rate: int = 24000) -> bytes:
    """生成一段 s16le 单声道 PCM（方波，便于编解码往返比对）。"""

    return b"".join(
        struct.pack("<h", 4000 if (index // 40) % 2 else -4000)
        for index in range(int(seconds * sample_rate))
    )


def build_wav(seconds: float = 0.2, sample_rate: int = 16000) -> bytes:
    """生成一段 16bit 单声道 wav。"""

    buffer = BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(build_pcm(seconds, sample_rate))
    return buffer.getvalue()


def build_silk(seconds: float = 0.2, sample_rate: int = 24000) -> bytes:
    """用 pysilk 把 PCM 编成 silk（QQ 语音的真实容器）。"""

    import pysilk

    output = BytesIO()
    pysilk.encode(BytesIO(build_pcm(seconds, sample_rate)), output, sample_rate, sample_rate)
    return output.getvalue()


def b64(data: bytes) -> str:
    """转成标准 base64 文本。"""

    return base64.b64encode(data).decode("ascii")


def build_voice_segment(audio: bytes, **extra) -> dict:
    """构造一个带二进制的 voice 消息段（与宿主序列化结果同构）。"""

    segment = {
        "type": "voice",
        "data": "",
        "hash": "test-hash",
        "binary_data_base64": b64(audio),
    }
    segment.update(extra)
    return segment
