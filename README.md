# maibot-mimo-asr-bridge

麦麦（MaiBot）的 MiMo 语音识别桥接插件：在插件内部直接调用小米 MiMo 的 ASR API，
把聊天里的语音消息转成文字。

## 插件信息

- 插件 ID：`lgv-h.mimo-asr-bridge`
- 仓库：https://github.com/Lgv-H/lgv-h.mimo-asr-bridge
- 版本：1.0.0
- 分类：媒体处理
- 图标：🎧
- 宿主能力声明：**无**（`capabilities: []`）

## 它和主程序内置语音识别的关系

主程序的语音识别走 `model_config.toml` 里的 `voice` 任务模型，通过 SDK 能力
`ctx.llm.transcribe_audio` 走 OpenAI 风格的 `/audio/transcriptions`（multipart 文件上传）。

本插件**完全绕开那条链路**：

- 音频容器嗅探、silk 解码、必要时 ffmpeg 转码，全部由插件自己做；
- 用 **Chat Completions + base64 音频**（MiMo 官方形态）直接 POST；
- 识别结果原地替换 `raw_message` 里的 voice 段为文本段，再交给宿主重建纯文本。

因此主程序**不需要**开启 `voice.enable_asr`；识别失败时原语音段保持不动，
若你同时也开着了内置 ASR，它会按老路径兜底。

## MiMo 接口契约

请求形态对齐 [官方文档](https://mimo.mi.com/docs/zh-CN/api/audio/Speech-Recognition)：

```
POST {base_url}/chat/completions
api-key: $MIMO_API_KEY        （或 Authorization: Bearer $MIMO_API_KEY）
```

```json
{
  "model": "mimo-v2.5-asr",
  "messages": [
    {
      "role": "user",
      "content": [
        {
          "type": "input_audio",
          "input_audio": { "data": "data:audio/wav;base64,....", "format": "wav" }
        }
      ]
    }
  ],
  "asr_options": { "language": "auto" }
}
```

识别文本取 `choices[0].message.content`；`stream = true` 时累加 SSE 的
`choices[0].delta.content`。

官方限制：输入只支持 `wav` / `mp3`，base64 后字符串不超过 10 MB。

## 音频是怎么处理的

插件按 magic 头嗅探容器，再决定直传还是转码：

| 嗅探到 | 处理 |
| --- | --- |
| `wav` | 直传 `data:audio/wav;base64,...` |
| `mp3` | 直传 `data:audio/mpeg;base64,...` |
| `silk`（QQ 原始语音） | `pysilk` 解成 s16le/24000Hz 单声道 PCM，用标准库 `wave` 套 wav 头，**不需要 ffmpeg** |
| `amr` / `ogg` / `m4a` / `flac` / `aac` / 其它 | 调用 ffmpeg 转 16kHz 单声道 wav |

常见部署下这一层基本是空转：NapCat 画像的 `get_record` 直接要 `wav`，
SnowLuma 适配器已经把服务端返回的 silk 转成了 `mp3`。
只有在适配器没开转码、或碰到其它协议适配器时才需要 `pysilk` / `ffmpeg`。

## 安装

### 目录安装

把本目录放到 `<MaiBot>/plugins/` 下（宿主只扫描 `plugins/` 的**直接子目录**）。
插件 ID 取自 `_manifest.json`，建议目录名用 `ID` 里的点换成下划线（即
`lgv-h_mimo-asr-bridge`）—— WebUI 的配置页、README 查看与卸载都按这个规则定位目录。

### WebUI / 插件市场安装

在插件市场搜索本插件，或通过 Git URL 安装本仓库，不需要额外服务。

## 配置

`config.toml` 由 Runner 在首次加载时依据 `plugin.py` 的 `config_model` 生成，
属于安装实例的运行时配置（仓库里不含它），也可以在 WebUI 插件配置页直接修改。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `plugin.enabled` | `false` | 是否启用；新插件默认关闭，确认后再打开 |
| `plugin.config_version` | `"1.0.0"` | 配置版本，宿主校验要求存在，别删 |
| `asr.api_key` | `""` | 小米 MiMo 开放平台的 API Key |
| `asr.base_url` | `https://api.xiaomimimo.com/v1` | API 根地址；Token Plan 用户可改为 `https://token-plan-cn.xiaomimimo.com/v1` |
| `asr.model` | `mimo-v2.5-asr` | 当前官方只提供这一个 ASR 模型 |
| `asr.auth_mode` | `api-key` | `api-key` 或 `bearer` |
| `asr.language` | `auto` | `auto` / `zh` / `en`；明确语种识别更准，中英混说用 `auto` |
| `asr.timeout_seconds` | `45.0` | 单次请求超时 |
| `asr.retry_times` | `1` | 网络错误、限流（429）或 5xx 时的重试次数 |
| `asr.stream` | `false` | 是否用 SSE 流式返回 |
| `asr.max_base64_mb` | `10.0` | base64 字符串大小上限，官方限制为 10 |
| `audio.text_template` | `[语音: {text}]` | 写回消息的模板，必须含 `{text}` |
| `audio.remove_placeholder` | `true` | 识别成功后清掉适配器附带的 `[voice]` 占位文本段 |
| `audio.ffmpeg_path` | `""` | ffmpeg 路径；留空从 PATH 探测 |
| `audio.ffmpeg_timeout_seconds` | `30.0` | ffmpeg 转码超时 |

改完文件宿主会自动调用 `on_config_update()` 应用新配置，不需要重启。

## 使用示例

1. 在 WebUI 插件配置页填好 `[asr].api_key`，把 `[plugin].enabled` 打开（或直接在
   `config.toml` 里改）；
2. 让群友发一条语音；
3. 这条语音在麦麦的上下文里就变成了文字，日志里同时会出现：

```text
[plugin.lgv-h.mimo-asr-bridge] MiMo ASR 识别成功: format=wav raw_bytes=6444 converted=False elapsed_ms=812 chars=6
```

写回消息的默认模板是主程序内置的 `[语音: xxx]`；把 `[audio].text_template` 改成
`{text}` 就只留下纯文本。识别失败时原语音段保持不变，日志里会给出具体原因
（HTTP 状态码、base64 超限、缺 ffmpeg、silk 解码失败等）。

## 隐私与安全

- **音频会上传到云端**：语音内容会以 base64 形式发送到 `asr.base_url` 指向的
  小米 MiMo 服务，请自行确认这符合你的使用场景与合规要求。
  （这一点和本地 sidecar 类 STT 插件完全不同。）
- **识别文本不进日志**：插件只记录字符数、耗时、媒体格式和字节数，不记录识别出的文字内容。
- **不记录凭据**：日志里只出现鉴权方式（`api-key` / `bearer`），不打印 API Key。
- **临时文件**：只有 ffmpeg 转码路径会在系统临时目录里落盘，转码结束即随 `TemporaryDirectory` 删除。

## 已知限制

- 只处理入站消息的 voice 段，不做 TTS，也不改写其它类型的消息段。
- 一次请求只支持单条音频（官方限制）；一条消息里的多个语音段会并发发出多次请求。
- 同一消息里其它段的基数二进制（图片、表情）会随 Hook 载荷原样回传，这是宿主 Hook 的既有行为。
- 宿主的首页卡片目前只渲染装饰器上的静态内容，卡片里的运行时统计只在代码层面可用。
- 本机没有 ffmpeg 时，非 wav/mp3/silk 的语音会转写失败并在日志里报错，而不是静默降级。

## 许可证

MIT
