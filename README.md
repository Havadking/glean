# video-summarizer

给一个视频链接（YouTube / Bilibili / 其他 yt-dlp 支持的站点），自动产出**结构化转写**和**大模型总结**。

设计细节见 [DESIGN.md](DESIGN.md)。当前进度：**v0.1**（字幕/ASR → 转写 → LLM 总结）+ Web 界面。

## 快速开始

```bash
uv sync --extra cuda --extra ui   # 只用 CPU 去掉 --extra cuda；只用命令行去掉 --extra ui
cp .env.example .env              # 然后填入 DEEPSEEK_API_KEY
```

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
--no-summary          # 只转写，不花钱
--force-asr           # 有字幕也强制走语音识别
--force               # 忽略缓存全部重跑
--asr-model medium    # 临时换小模型，快一些
-y                    # 跳过成本确认
```

## 工作方式

1. **字幕优先**：`yt-dlp` 探测，只认人工上传字幕；自动生成/自动翻译字幕默认忽略（准确率不够）。B 站只有弹幕的情况会被识别为"无字幕"。
2. **无字幕才跑 ASR**：下载最佳音轨 → ffmpeg 转 16k 单声道 wav → faster-whisper 转写。GPU 加载失败会自动退回 CPU。
3. **长文本自动分策略**：转写 token 数在模型上下文预算内就整篇送入，超了自动走 map-reduce（切块局部摘要 → 汇总）。
4. **花钱前先问**：调用大模型前打印预估 token 量和请求次数，确认后才发。

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
- GPU 转写需要 CUDA 运行库，由 `--extra cuda` 装进 venv

模型权重和 uv 缓存的位置由环境变量控制（`HF_HOME` / `UV_CACHE_DIR` / `UV_PYTHON_INSTALL_DIR`），本机已指向 `E:\personal\.cache`，不占 C 盘。

## 已知局限

- **模型会编**：即使 system prompt 里写死了「只依据转写内容作答」，模型仍可能从标题认出视频，然后掺进转写里没有的背景知识（上传时间、播放量之类），语气还很笃定。约束能压住大部分，但不能根除 —— 拿总结当索引，别当事实来源。
- **说话人分离还没做**（路线图 v0.3），所以「分说话人摘要」目前只能靠模型从语气和称呼推断。
- VAD 对纯音乐、强背景音会整段误判成非人声，遇到这种情况会自动关掉 VAD 重跑一次。

## 路线图

见 [DESIGN.md 第 8 节](DESIGN.md)。Gradio 界面原本排在 v0.5，已提前做完。下一步 v0.2：接入 FunASR 作为默认 ASR，whisper 降级为兜底。
