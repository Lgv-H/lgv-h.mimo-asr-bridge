# Changelog

本插件的所有值得注意的改动都会记在这里。版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.0.0] — 2026-10-04

首个版本。插件在自身进程内完成音频编码与 HTTP 请求，直接调用小米 MiMo 的语音识别接口，
把聊天里的语音消息转成文字交给 MaiBot，全程不使用 SDK 的 STT 能力。

### 桥接实现

- `@HookHandler("chat.receive.before_process")` 阻塞拦截入站消息，把 `raw_message` 里的
  voice 段原地替换成文本段（默认 `[语音: {text}]`，与主程序内置格式一致），并清理适配器
  附带的 `[voice]` 占位文本段
- `mimo_asr_client.py`：按 MiMo 官方契约构造 `POST {base_url}/chat/completions` 请求体
  （`input_audio` + `data:` URL + `asr_options.language`），支持 `api-key` 与
  `Authorization: Bearer` 两种鉴权、非流式与 SSE 流式两种返回
- 重试策略按错误类型区分：网络错误、超时、限流（429）与 5xx 才重试，其余 4xx 立即失败并
  把 HTTP 状态码与响应体摘要打进日志
- 单条消息里的多个语音段并发转写；任一段失败只记日志并保留原语音段，不阻断消息流
- `capabilities` 为空 —— 不申请任何宿主能力

### 音频处理

- 按 magic 头嗅探容器：`wav` / `mp3` 直传为 data URL；`silk` 用 `pysilk` 解成
  s16le/24000Hz 单声道 PCM 后再用标准库 `wave` 套 wav 头（**不需要 ffmpeg**）；
  `amr` / `ogg` / `m4a` / `flac` / `aac` 等容器交给 ffmpeg 转 16kHz 单声道 wav
- 兼容三种 base64 入参：纯 base64、`data:...;base64,` 前缀、缺失 `=` 补位的 base64
- 按 MiMo 官方限制校验 base64 字符串大小（默认 10MB），超限直接报错而不是截断上传

### WebUI 配置

- 配置分 `[plugin]` / `[asr]` / `[audio]` 三段，带分组标题、图标、排序和输入框 placeholder
- `asr.auth_mode` / `asr.language` 用 `Literal` 声明，WebUI 渲染为下拉框
- 超时、重试次数、体积上限带 `ge`/`le` 边界，越界值在配置解析阶段就被拒绝
- `config.toml` 缺失 `{text}` 占位符、`ffmpeg_path` 指向不存在的文件、未填 `api_key`
  等情况都会在加载时打 ERROR/WARNING 日志，不静默兜底
- `@HomeCard` 展示接口摘要（endpoint / 模型 / 鉴权方式 / 语种）与调参说明

### 测试与工程细节

- 新增 6 个测试文件、79 条离线用例：容器嗅探、base64 解码、silk 编解码往返、体积上限、
  请求体与请求头、响应与 SSE 解析、重试策略、Hook 改写与失败语义、配置热重载、WebUI
  Schema 形状、manifest 字段
- 测试加载器复刻宿主的合成模块名规则（`_maibot_plugin_<id>`）—— 用带点的模块名会让
  `plugin.py` 的相对导入落到另一个模块对象，导致打桩失效、测试真的发网络请求；同时断言
  插件实际导入的子模块与测试打桩的是同一个对象
- `.gitignore` 忽略 `config_back/` 与 `data/`：前者是宿主保存插件配置时生成的历史备份，
  内含 `api_key`，不能在提交/打包时带出去
- `config.toml`、`.gitattributes` 与提交信息均以无 BOM 的 UTF-8 写入（BOM 会让
  `tomllib` / `tomlkit` 解析配置失败）

### 已知限制

- MiMo 的 ASR 接口一次只接受单条音频，且只支持 `wav` / `mp3` 容器
- 音频会上传到 `asr.base_url` 指向的小米云端服务，与本地 sidecar 类 STT 插件不同
- 账号余额不足时接口返回 `402 insufficient_balance`，插件只记失败日志、不改写消息
- 未安装 ffmpeg 时，非 `wav` / `mp3` / `silk` 的语音会转写失败并在日志中报错
- 宿主的首页卡片目前只渲染装饰器上的静态内容，卡片里的运行时统计仅在代码层面可用
