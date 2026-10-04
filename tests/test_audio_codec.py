"""测试 audio_codec — 容器嗅探、base64 解码、silk 转码与体积上限。"""

from __future__ import annotations

import base64
import sys
import unittest
import wave
from importlib.util import find_spec
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _fixtures  # noqa: E402
import _loader  # noqa: E402

PLUGIN_DIR = _loader.PLUGIN_DIR
audio_codec = _loader.load_submodule("audio_codec")
AudioBridgeError = audio_codec.AudioBridgeError

build_pcm = _fixtures.build_pcm
build_wav = _fixtures.build_wav
build_silk = _fixtures.build_silk
b64 = _fixtures.b64

# 各类容器的最小可识别头部
MAGIC_SAMPLES = {
    "wav": b"RIFF\x00\x00\x00\x00WAVEfmt ",
    "mp3_id3": b"ID3\x03\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00",
    "mp3_frame": b"\xff\xfb\x90\x00\x00\x00\x00\x00\x00\x00\x00\x00",
    "aac_adts": b"\xff\xf1\x50\x80\x00\x00\x00\x00\x00\x00\x00\x00",
    "amr": b"#!AMR\n\x00\x00\x00\x00\x00\x00\x00\x00",
    "amr_wb": b"#!AMR-WB\n\x00\x00\x00\x00",
    "ogg": b"OggS\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00",
    "flac": b"fLaC\x00\x00\x00\x22\x00\x00\x00\x00",
    "m4a": b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00",
    "silk_v3": b"#!SILK_V3\x00\x00\x00\x00",
    "silk_tencent": b"\x02#!SILK_V3\x00\x00\x00",
    "unknown": b"\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c",
    "too_short": b"RI",
}

MAGIC_EXPECTED = {
    "wav": "wav",
    "mp3_id3": "mp3",
    "mp3_frame": "mp3",
    "aac_adts": "aac",
    "amr": "amr",
    "amr_wb": "amr",
    "ogg": "ogg",
    "flac": "flac",
    "m4a": "m4a",
    "silk_v3": "silk",
    "silk_tencent": "silk",
    "unknown": "unknown",
    "too_short": "unknown",
}


class TestSniffAudioFormat(unittest.TestCase):
    """容器嗅探：magic 头判断必须覆盖适配器可能下发的所有格式。"""

    def test_magic_samples(self):
        for label, payload in MAGIC_SAMPLES.items():
            with self.subTest(label=label):
                self.assertEqual(audio_codec.sniff_audio_format(payload), MAGIC_EXPECTED[label])

    def test_empty_payload(self):
        self.assertEqual(audio_codec.sniff_audio_format(b""), "unknown")

    def test_mimo_native_formats(self):
        self.assertEqual(set(audio_codec.MIMO_MIME_TYPES), {"wav", "mp3"})


class TestDecodeBase64Audio(unittest.TestCase):
    """base64 解码：适配器给的形式不止一种。"""

    def test_plain_base64(self):
        payload = build_wav()
        self.assertEqual(audio_codec.decode_base64_audio(b64(payload)), payload)

    def test_data_url(self):
        payload = build_wav()
        self.assertEqual(
            audio_codec.decode_base64_audio(f"data:audio/wav;base64,{b64(payload)}"),
            payload,
        )

    def test_whitespace_and_missing_padding(self):
        payload = build_wav()
        encoded = b64(payload).rstrip("=")
        wrapped = "\n".join(encoded[index : index + 40] for index in range(0, len(encoded), 40))
        self.assertEqual(audio_codec.decode_base64_audio(wrapped), payload)

    def test_empty_payload_raises(self):
        with self.assertRaises(AudioBridgeError):
            audio_codec.decode_base64_audio("   ")

    def test_invalid_base64_raises(self):
        with self.assertRaises(AudioBridgeError):
            audio_codec.decode_base64_audio("!!!not-base64!!!")


class TestResolveFfmpegPath(unittest.TestCase):
    """ffmpeg 路径解析。"""

    def test_blank_path_falls_back_to_path_lookup(self):
        resolved = audio_codec.resolve_ffmpeg_path("")
        self.assertIsInstance(resolved, str)
        if resolved:
            self.assertTrue(Path(resolved).is_file())

    def test_missing_explicit_path_raises(self):
        with self.assertRaises(AudioBridgeError):
            audio_codec.resolve_ffmpeg_path(str(PLUGIN_DIR / "not-here" / "ffmpeg.exe"))


class TestPrepareAudio(unittest.IsolatedAsyncioTestCase):
    """prepare_audio：把语音整理成 MiMo 能吃的 data URL。"""

    async def test_wav_passthrough(self):
        payload = build_wav()
        prepared = await audio_codec.prepare_audio(
            b64(payload), ffmpeg_path="", ffmpeg_timeout_seconds=5, max_base64_mb=10
        )
        self.assertEqual(prepared.format, "wav")
        self.assertTrue(prepared.data_url.startswith("data:audio/wav;base64,"))
        self.assertFalse(prepared.converted)
        self.assertEqual(prepared.raw_bytes, len(payload))
        self.assertEqual(
            base64.b64decode(prepared.data_url.split(",", 1)[1]),
            payload,
        )

    async def test_mp3_data_url_input(self):
        payload = MAGIC_SAMPLES["mp3_id3"]
        prepared = await audio_codec.prepare_audio(
            f"data:audio/mpeg;base64,{b64(payload)}",
            ffmpeg_path="",
            ffmpeg_timeout_seconds=5,
            max_base64_mb=10,
        )
        self.assertEqual(prepared.format, "mp3")
        self.assertTrue(prepared.data_url.startswith("data:audio/mpeg;base64,"))
        self.assertFalse(prepared.converted)

    async def test_base64_limit(self):
        with self.assertRaises(AudioBridgeError) as ctx:
            await audio_codec.prepare_audio(
                b64(build_wav()), ffmpeg_path="", ffmpeg_timeout_seconds=5, max_base64_mb=0.0005
            )
        self.assertIn("上限", str(ctx.exception))

    async def test_non_native_without_ffmpeg_raises(self):
        # 显式传空 ffmpeg 路径，必然走到"没有 ffmpeg"的分支，与宿主机是否装了 ffmpeg 无关
        with self.assertRaises(AudioBridgeError) as ctx:
            await audio_codec.prepare_audio(
                b64(MAGIC_SAMPLES["ogg"]), ffmpeg_path="", ffmpeg_timeout_seconds=5, max_base64_mb=10
            )
        self.assertIn("ffmpeg", str(ctx.exception))

    async def test_unknown_format_without_ffmpeg_raises(self):
        with self.assertRaises(AudioBridgeError):
            await audio_codec.prepare_audio(
                b64(MAGIC_SAMPLES["unknown"]),
                ffmpeg_path="",
                ffmpeg_timeout_seconds=5,
                max_base64_mb=10,
            )

    @unittest.skipUnless(find_spec("pysilk"), "需要 pysilk 才能构造 silk 样本")
    async def test_silk_is_decoded_without_ffmpeg(self):
        silk_payload = build_silk()
        self.assertEqual(audio_codec.sniff_audio_format(silk_payload), "silk")

        # 关键点：silk 走 pysilk + 标准库 wave，ffmpeg 路径为空也必须成功
        prepared = await audio_codec.prepare_audio(
            b64(silk_payload), ffmpeg_path="", ffmpeg_timeout_seconds=5, max_base64_mb=10
        )
        self.assertEqual(prepared.format, "wav")
        self.assertTrue(prepared.converted)
        self.assertTrue(prepared.data_url.startswith("data:audio/wav;base64,"))

        decoded = base64.b64decode(prepared.data_url.split(",", 1)[1])
        with wave.open(BytesIO(decoded), "rb") as wav_file:
            self.assertEqual(wav_file.getnchannels(), 1)
            self.assertEqual(wav_file.getsampwidth(), 2)
            self.assertEqual(wav_file.getframerate(), audio_codec.SILK_SAMPLE_RATE)
            self.assertGreater(wav_file.getnframes(), 0)


class TestPcmToWav(unittest.TestCase):
    """PCM 套 wav 头。"""

    def test_header_and_frames(self):
        pcm = build_pcm(0.1, 24000)
        wav_bytes = audio_codec._pcm_to_wav(pcm, 24000)
        with wave.open(BytesIO(wav_bytes), "rb") as wav_file:
            self.assertEqual(wav_file.getnchannels(), 1)
            self.assertEqual(wav_file.getsampwidth(), 2)
            self.assertEqual(wav_file.getframerate(), 24000)
            self.assertEqual(wav_file.readframes(wav_file.getnframes()), pcm)


if __name__ == "__main__":
    unittest.main()
