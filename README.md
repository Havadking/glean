# video-summarizer

给一个视频链接（YouTube / Bilibili / 其他 yt-dlp 支持的站点），自动产出**结构化转写**和**大模型总结**。

设计细节见 [DESIGN.md](DESIGN.md)。当前进度：**v0.3**（说话人分离，支持多人对话/播客的分角色总结）+ Web 界面。

## 快速开始

```bash
uv sync --extra cuda --extra ui --extra funasr
cp .env.example .env              # 然后填入 DEEPSEEK_API_KEY
```

各 extra 的作用：`funasr` 是默认 ASR（要拉 torch，约 3GB）、`cuda` 是 whisper 兜底走 GPU 需要的运行库、`ui` 是 Web 界面。只想先跑起来的话 `uv sync --extra funasr` 就够。

图形界面：

```bash
uv run vsum ui
```

命令行：

```bash
uv run vsum inspect "https://www.bilibili.com/video/BVxxxxxxx"
uv run vsum run "https://www.bilibili.com/video/BVxxxxxxx"
```

产物落在 `output/<标题>-<视频ID>/` 下：

- `transcript.json` — 结构化转写（时间轴 + 文本，ASR 路径下未来会带 speaker）
- `summary.md` — 总结，开头附来源、模型、生成时间，便于追溯

## 命令

| 命令 | 作用 |
|---|---|
| `vsum ui` | 启动 Web 界面（默认 http://127.0.0.1:7860） |
| `vsum inspect <url>` | 只探测：有没有人工字幕、时长多少。不下载任何东西 |
| `vsum run <url>` | 完整流程：转写 + 总结 |
| `vsum summarize <transcript.json>` | 拿已有转写换个角度重新总结，不重跑 ASR |
| `vsum config` | 打印当前生效的配置和密钥状态 |

界面分两步：先「获取转写」出转写和预计消耗，确认后再点「生成总结」才会真正调大模型 —— 和命令行的成本确认是同一个道理。「历史」标签页可以拿已有转写换个总结类型重跑，不用再走一遍 ASR。

`vsum run` 常用参数：

```bash
--summary-type overall|by_speaker|timeline|key_points   # 总结类型
--diarize             # 强制开说话人分离（默认 auto，选 by_speaker 时自动开）
--no-diarize          # 强制关
--no-summary          # 只转写，不花钱
--force-asr           # 有字幕也强制走语音识别
--force               # 忽略缓存全部重跑
--asr-model medium    # 临时换小模型，快一些
-y                    # 跳过成本确认
```

注意 `--force` 之外的重跑都会复用已有的 `transcript.json`。已经转写过的视频要换成带说话人的版本，得 `--diarize --force` 一起加。

## 工作方式

1. **字幕优先**：`yt-dlp` 探测，只认人工上传字幕；自动生成/自动翻译字幕默认忽略（准确率不够）。B 站只有弹幕的情况会被识别为"无字幕"。
2. **无字幕才跑 ASR**：下载最佳音轨 → ffmpeg 转 16k 单声道 wav → FunASR（FSMN-VAD 切段 + SenseVoice-Small 批量识别）。
3. **兜底**：FunASR 只覆盖中英日韩粤。配置的语言超出范围、或 FunASR 跑失败/结果为空，自动切 faster-whisper（近百种语言）。whisper 这一路 GPU 失败还会再退 CPU。
4. **长文本自动分策略**：转写 token 数在模型上下文预算内就整篇送入，超了自动走 map-reduce（切块局部摘要 → 汇总）。
5. **花钱前先问**：调用大模型前打印预估 token 量和请求次数，确认后才发。

### 说话人分离

选「分说话人摘要」时会自动开启（`config.yaml` 里 `asr.diarize: auto`），也可以用 `--diarize` 强制开、`--no-diarize` 强制关。

开启后换成 FunASR 的整合 pipeline：`paraformer-zh + FSMN-VAD + ct-punc + CAM++`，一次输出句级文本 + 起止时间 + 说话人编号。句子很碎（22 分钟能有 600 多句），同一个人连续说的会合并成"一轮发言"再交给大模型。

不默认全开的原因：这条 pipeline 只做中文，而且比 SenseVoice 慢一半。

### 实测速度（RTX 4070，22 分钟中文视频）

| ASR | 耗时 | 实时率 | 说话人 |
|---|---|---|---|
| FunASR SenseVoice-Small | 11 秒 | ~118x | 无 |
| FunASR paraformer-zh + CAM++ | 38 秒 | ~35x | 有 |
| faster-whisper large-v3 | 约 9 分钟 | ~2.5x | 无 |

SenseVoice 快是因为只有 234M 参数（large-v3 是 1.55B），而且 VAD 切出的段可以批量推理。中文准确率也是 FunASR 更好。

## 配置

- `config.yaml` — provider 选择、模型、prompt 类型、上下文预算。可提交。
- `.env` — 密钥。已在 `.gitignore` 里，不提交。

默认总结走 DeepSeek（OpenAI 兼容接口）。换厂商只改 `config.yaml`：

```yaml
summarizer:
  provider: openai
  model: qwen-plus
  base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1"
  api_key_env: DASHSCOPE_API_KEY
```

本地 Ollama 同理，`base_url: http://localhost:11434/v1`。

## 环境依赖

- Python 3.10+（本仓库用 uv 管理，托管的是 3.12）
- `ffmpeg` 在 PATH 里
- GPU：FunASR 走 torch（`--extra funasr` 会从 PyTorch 官方源装 cu130 版，PyPI 上的 Windows 轮子是纯 CPU 的）；whisper 走 ctranslate2，需要 `--extra cuda` 的运行库

模型权重和缓存的位置由环境变量控制，本机已指向 `E:\personal\.cache`，不占 C 盘：

| 变量 | 管什么 |
|---|---|
| `HF_HOME` | faster-whisper 的模型权重 |
| `MODELSCOPE_CACHE` | FunASR 的模型权重 |
| `UV_CACHE_DIR` | uv 的包缓存 |
| `UV_PYTHON_INSTALL_DIR` | uv 托管的 Python |

**这些是用户级环境变量，只有新开的终端才会读到。** 如果发现模型往 C 盘下，多半是终端开得比设置早，重开一个即可。`vsum config` 会打印当前生效的路径。

## 已知局限

- **模型会编**：即使 system prompt 里写死了「只依据转写内容作答」，模型仍可能从标题认出视频，然后掺进转写里没有的背景知识（上传时间、播放量之类），语气还很笃定。约束能压住大部分，但不能根除 —— 拿总结当索引，别当事实来源。
- **说话人分离只做中文**：走的是 paraformer-zh 那条 pipeline。如果配置的语言不是中文，会退到 whisper 兜底，而 whisper 不输出说话人标签，这时「分说话人摘要」只能靠模型从语气和称呼推断。
- **分离的准确率不追求 100%**：背景音乐、多人抢话、音色接近的场景会打折扣。目前在合成的双人音频（一男一女、语言不同）上验证过能正确分开，在真实独白上验证过不会误分成多人；**真实多人对话/播客场景还没实测**，拿你自己的播客试一下更有参考价值。
- **时间轴的粒度取决于 VAD**：不分离时时间戳来自 FSMN-VAD 的语音段边界（单段上限 30 秒），不是逐词对齐，长段落起止会偏粗；分离时是句级的，细得多。
- B 站对同一 IP 的请求频率敏感，超了返回 412。已经做了复用探测结果少发请求 + 退避重试，还是撞上的话等几分钟，或者在 `config.yaml` 里配 `download.cookies_from_browser` 用登录态。
- whisper 那一路的 VAD 对纯音乐、强背景音会整段误判成非人声，遇到这种情况会自动关掉 VAD 重跑一次。

## 路线图

见 [DESIGN.md 第 8 节](DESIGN.md)。v0.1 - v0.3 已完成，Gradio 界面（原排在 v0.5）也提前做完了。

剩下：**v0.4** 补齐 LLM provider 抽象层（Anthropic 原生接口、Ollama 本地模型），**v0.5** 加 SQLite 缓存。目前复用是靠输出目录里已有的 `transcript.json` 和音频文件，效果类似但没有跨目录的索引能力。
