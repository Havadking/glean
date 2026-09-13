# 拾光笺 — 设计文档

> 拾光笺（命令行仍叫 `vsum`）：把视频变成一张能读的笺。
> 给一个链接，拿到结构化转写、可切换类型的 AI 总结、思维导图，并把处理过的视频攒成一个能搜的库。
>
> 文档分两部分：**已落地的现状**（第 1–7 节，按实际代码写，和最初的设想不一致的地方单独标出）
> 和 **接下来的路线**（第 8–10 节）。

---

## 1. 目标

给定一个视频链接（YouTube / Bilibili / 其他 yt-dlp 支持的站点），自动完成：

1. 优先取官方/人工字幕；没有就下载音频做本地语音识别
2. 需要时给识别结果标说话人（对话、播客）
3. 用可切换的大模型做结构化总结（总体 / 分角色 / 时间线 / 要点 / 思维导图）
4. 落盘：`transcript.json` + `summary.md`（+ 思维导图 `mindmap.html`），同时写入 SQLite 缓存
5. 一个能直接用的本地界面，处理过的视频按 UP 主归堆，可搜、可回看

运行环境：本地 Windows，RTX 4070 Super（12GB）。**所有依赖、模型权重、工具缓存都放 E 盘**（`E:\personal\.cache`），不进 C 盘。

### 1.1 实际使用画像（决定后续优先级的依据）

- 主要用法是**追某个 B 站 UP 主**：处理过的视频大半来自同一位创作者，3–22 分钟中文独白
- 验收标准是**能不能直接用、好不好看**，不是功能多少
- 只有 DeepSeek 额度，每个动作要先知道花多少钱；能不调模型就不调
- 要**结构**（思维导图、要点）多过要段落

---

## 2. 整体架构（现状）

```
URL
 │
 ▼
[0] 探测 yt-dlp（一次，info 复用给下载）─────── 缓存指纹 = f(video_id, 走字幕/ASR, 模型, 分离开关)
 │                                                     │ 命中 → 直接取转写
 ├─ 有人工字幕 ──► 下载 vtt → 清洗 ──────────────────┐  │
 │                                                    │  │
 └─ 无字幕 ──► [2] 提取音频 ──► [3] ASR ─────────────┤◄─┘
                                 FunASR SenseVoice     │
                                 （分离时 Paraformer   │
                                   +CAM++；whisper兜底）│
                                                       ▼
                                    Transcript（segments + title + meta.cache_key）
                                                       │
                                                       ▼
                                   [4] 总结 provider（OpenAI 兼容 / Claude / Ollama）
                                       plan() 先估 token 与成本，确认后再 summarize()
                                       同样输入的总结命中缓存则不调模型
                                                       │
                                                       ▼
                       transcript.json · summary.md · mindmap.html · cache.sqlite
                                                       │
                                                       ▼
                                   [5] 界面：库（按 UP 主）/ 详情（转写 + 总结）/ 新任务
```

模块之间只靠 `Transcript` 这个中间结构传递，各层可单独替换。

---

## 3. 模块设计（现状）

### 3.1 字幕获取 `subtitle/`

- `ytdlp_base.probe()` 一次拿全信息；只认 **人工字幕**，`danmaku` / `live_chat` 这类伪字幕轨剔除
- 语言按 `config.subtitle.languages` 顺序挑；自动字幕默认不要（可配）
- vtt 清洗：去头、去时间戳、去 HTML 实体、合并重复行

### 3.2 音频提取 `audio/`

- 探测阶段的 info 直接喂给下载（等价 `--load-info-json`），少发一次请求——这是对 B 站 412 最有效的对策
- 被限流时退避重试；自定义 UA 只作最后一根稻草，不是默认

### 3.3 ASR `asr/`

| Provider | 定位 | 实测 |
|---|---|---|
| FunASR **SenseVoice-Small** | 默认 | 独立 FSMN-VAD 切片后批量识别，约 118× 实时；中/英/日/韩/粤 |
| FunASR **Paraformer-zh + CAM++** | 说话人分离 | 约 35× 实时，仅中文；`diarize: auto` 时只在需要分角色时开 |
| faster-whisper large-v3 | 兜底 | FunASR 不支持的语言；VAD 把纯音乐视频切空时自动关 VAD 重跑 |

`FallbackASRProvider` 把主/备包成一个；`asr/__init__.get_provider()` 按配置实例化。

### 3.4 总结 `summarizer/`

- `BaseSummarizer` 模板方法：`plan()` → `CostEstimate`（token 数、要不要切块、几次请求）→ `summarize()`；map-reduce 在基类里，provider 只实现 `_complete()`
- 三个实现：`openai_provider`（OpenAI 兼容，DeepSeek 在用）、`claude_provider`（anthropic SDK，流式，不传 temperature，支持 effort，非 `sk-ant-` 密钥且无 base_url 时拒绝发送）、`ollama_provider`（原生 `/api/chat`，显式 `num_ctx`）
- 五个模板 `prompts.TEMPLATES`：overall / by_speaker / timeline / key_points / **mindmap**（嵌套 Markdown 大纲，程序解析成树）
- SYSTEM prompt 明确禁止用转写之外的知识——模型曾经根据标题编造内容

### 3.5 缓存 `cache.py`

- SQLite 两张表 `transcripts` / `summaries`，主键是**输入指纹**（不是 URL）：换模型、开分离、换总结类型都是不同的 key
- `cache_key` 同时写进 `transcript.meta`，缓存丢了还能靠它认领输出目录里的产物
- 任何 SQLite 错误降级为未命中，不阻塞主流程

### 3.6 界面 `web/`

- 当前是 Gradio 6 多页（处理 / 历史），阅读视图按停顿和时长把分句聚成段落，Markmap 本地渲染思维导图
- **已决定用 FastAPI + React 重写**，见第 9 节；`reading.py` / `library.py` / `mindmap.py` 是框架无关的，保留

---

## 4. 技术栈（现状）

| 层 | 选型 | 备注 |
|---|---|---|
| 抓取 | yt-dlp（Python API） | 探测一次，info 复用 |
| ASR | FunASR SenseVoice-Small（默认）/ Paraformer-zh+CAM++（分离）/ faster-whisper（兜底） | torch cu130 从 pytorch 源装（win32） |
| LLM | 自建 provider 抽象：OpenAI 兼容（DeepSeek 在用）/ Claude / Ollama | 只有 DeepSeek 真实验证过 |
| 缓存 | SQLite | 指纹索引 |
| 思维导图 | Markmap（markmap-view + d3，打包进 `web/static/` 离线可用） | 不用 markmap-lib，解析器自己写 |
| CLI | click | `vsum run / inspect / summarize / ui / cache / config` |
| 界面 | Gradio 6 → **FastAPI + React（v0.6）** | 见第 9 节 |
| 包管理 | uv，hatchling | extras：`cuda` `ui` `funasr` `dev` |
| 配置 | `config.yaml` + `.env` | `load_config` 拒绝未知字段 |
| 测试 | pytest | 160+ 快测试，`slow` 标记的要真模型/网络 |

---

## 5. 数据结构（现状）

### 5.1 `Transcript`

```json
{
  "source_url": "https://www.bilibili.com/video/BV...",
  "source_type": "subtitle | asr",
  "language": "zh",
  "duration_sec": 1340.0,
  "title": "…",
  "video_id": "BV…",
  "segments": [{"start": 12.4, "end": 18.1, "speaker": "Speaker_1", "text": "…"}],
  "meta": {
    "extractor": "BiliBili",
    "asr_provider": "funasr", "asr_model": "sensevoice-small", "device": "cuda:0",
    "diarization": false, "elapsed_sec": 3.8,
    "cache_key": "0452b0959f87…"
  }
}
```

相比最初设想多了 `title` / `video_id` / `meta`。`meta` 记录这份转写是怎么来的，`cache_key` 用来认领。

### 5.2 产物目录

```
output/<标题-视频id>/
├── audio/            提取的音频（复用，--force 才重下）
├── subs/             下载的字幕
├── transcript.json
├── summary.md        正文前有来源、时长、转写方式、模型、类型、时间
└── mindmap.html      思维导图类型才有，JS 全内联，离线双击可开
```

### 5.3 SQLite

`transcripts(key, video_id, source_url, title, source_type, language, duration_sec, segment_count, has_speakers, payload, created_at)`
`summaries(key, transcript_key, video_id, title, provider, summary_type, language, content, created_at)`

v0.7 加 FTS5 虚表做全文搜索（第 8 节）。

---

## 6. 配置（现状）

```yaml
subtitle: {languages: [zh-Hans, zh, en], allow_auto: false}
download: {user_agent: null, cookies_from_browser: null}
asr:
  provider: funasr
  model: sensevoice-small
  fallback: whisper
  fallback_model: large-v3
  device: cuda
  diarize: auto            # auto | true | false
  diarize_model: paraformer-zh
summarizer:
  provider: openai         # openai | claude | ollama
  model: deepseek-chat
  base_url: https://api.deepseek.com/v1
  api_key_env: DEEPSEEK_API_KEY
  max_context_tokens: 120000
  max_output_tokens: 8000
  temperature: 0.3
  chunk_strategy: auto
  chunk_tokens: 20000
  summary_type: overall
output_dir: ./output
cache_db: ./cache.sqlite
```

密钥只在 `.env`（gitignore）。模型缓存目录由环境变量指到 E 盘：`HF_HOME` `MODELSCOPE_CACHE` `UV_CACHE_DIR` `UV_PYTHON_INSTALL_DIR` 等，`vsum config` 会打印。

---

## 7. 踩过的坑（实打实的）

1. **B 站 412 是按 IP 限流，和 UA 无关**。裸 yt-dlp 同样会中。对策：探测的 info 直接复用给下载，少发请求；失败退避重试。批量处理时这是第一大风险
2. **Windows 上 ctranslate2 找不到 `cublas64_12.dll`**：pip 装的 nvidia 库不在 PATH。要 `os.add_dll_directory` **并且** 前置 PATH，两个都做
3. **FunASR `max_single_segment_time` 传给 `generate()` 会被静默忽略**，必须在 `AutoModel(...)` 构造时传；`AutoModel(vad_model=...)` 一体化模式不返回时间戳，VAD 得单独跑
4. **whisper 的 VAD 会把纯音乐视频切成空**，要检测 100% 被过滤后关 VAD 重跑
5. **模型会用标题编内容**。SYSTEM prompt 里明令只依据转写，效果立竿见影
6. **Claude 接口不收 `temperature`**（新模型），走 `effort`；**Ollama 不显式传 `num_ctx` 默认只有 2k 上下文**，长转写会被静默截断
7. **测试 fixture 把真密钥发到了别家接口**：`base_url` 默认必须是 `None`，provider 要校验密钥前缀；读 `.env` 的测试要 monkeypatch 掉 `load_dotenv`
8. **Gradio 6**：主题和 css 只能从 `launch()` 传；`js=` 和 `fn` 不能放同一个事件；`.prose` 样式要 `!important` 才压得住；组件更新只换 innerHTML 不跑脚本（Markmap 靠 MutationObserver 重渲染）。这些是换框架的直接原因
9. **说话人分离 pipeline 仅中文**，且对背景音乐、抢话效果打折；本项目主要内容是独白，未在真实多人素材上验证
10. **模型权重和 uv 的 Python 默认落 C 盘**，环境变量要在装任何东西之前设好；删缓存目录前先看里面是什么
11. 合规：定位个人学习工具，不做公开分发服务

---

## 8. 路线图

### 已完成

| 版本 | 内容 | 状态 |
|---|---|---|
| v0.1 | CLI 全流程：字幕/音频 → whisper → LLM 总结 | ✅ |
| v0.2 | FunASR SenseVoice 默认，whisper 兜底 | ✅ |
| v0.3 | 说话人分离 + 分角色总结 | ✅ |
| v0.4 | provider 抽象：Claude 原生、Ollama | ✅（仅 DeepSeek 真实验证） |
| v0.5 | Gradio 界面 + SQLite 缓存 | ✅ |
| — | 阅读视图、历史页、思维导图、成本确认（原计划外） | ✅ |

### v0.6 — 界面重构（进行中）

目标：从「模型 demo」变成「产品」。**FastAPI + React**，视觉稿见 `docs/mockup/v0.6-ui.html`，设计决策见第 9 节。

- 后端 `web/api.py`：REST + SSE
  - `GET /api/library`（按 UP 主分组、统计）、`GET /api/videos/{id}`（段落化转写 + 各类型总结状态）
  - `POST /api/jobs`（处理一个 URL）→ `GET /api/jobs/{id}/events`（阶段、进度、日志，SSE）
  - `POST /api/videos/{id}/summaries`（生成某类型，先返回估算，确认后执行，流式）
  - `POST /api/probe`（贴链接即出探测卡：标题、UP 主、时长、有无字幕、是否命中缓存、预计花费）
- **任务队列**：后台线程单 worker，任务表落 SQLite，界面关了任务继续跑、重开能续看。这是 v0.7 批量的底座
- 前端 `frontend/`：Vite + React + TypeScript + Tailwind；构建产物提交到 `web/dist/` 随包分发，`vsum ui` 直接服务，用户不需要 Node
- 数据层补 **UP 主字段**：`VideoInfo` / `Transcript.meta` 记 `uploader`（yt-dlp 的 `uploader` / `channel`），库按它分组
- 保留：`reading.py` `library.py` `mindmap.py`；Gradio 版在 React 版跑通后删除，`ui` extra 改为 `fastapi + uvicorn + sse-starlette`

### v0.7 — 从「处理一个视频」到「管理一个库」

按优先级：

1. **全库搜索**：SQLite FTS5，中文按字切分，搜转写正文与总结；结果带原文片段 + 时间戳，点开定位到段落。零 LLM 成本
2. **问视频**：对一条转写提问，模型只依据转写回答，**每条结论附时间戳**，点了跳段落。既是最高频用法，也是结构性防编造
3. **批量**：合集 / UP 主空间链接 → 列出条目 → 勾选入队。必须做节流（B 站 412）和 cookie 登录态；支持中断续跑

### v0.8 — 打磨

- 转写清洗（口水话、重复），**可选**
- 成本统计：每条视频、累计 token 与金额（数据已在日志里）
- 库管理：删除、打开产物目录、批量清理
- 跨视频总结（"这位 UP 主关于 X 的全部观点"），依赖搜索 + 问视频，成本明显上升，做之前先估

### 明确降级

- Claude / Ollama 真实验证——只用 DeepSeek
- 多人对话分离实测——内容全是独白
- Parakeet——没有英文内容需求

---

## 9. 界面设计（v0.6）

视觉稿 `docs/mockup/v0.6-ui.html`（用真实数据画的，浏览器直接打开）。

**结构**：左侧固定导航（新任务 / 库 / 设置），其下列「关注的 UP 主」和「最近」；主区三个页面。

**视觉**：冷灰底 + 一个偏 B 站的玫红做强调色，只用在「当前项、主按钮、搜索命中」三处；绿 = 已生成、橙 = 需注意、蓝 = 提示，和强调色分开。字体 Noto Sans SC，等宽字体只给时间码、token、金额。深色模式完整设计，跟随系统可手动切。

**新任务**：贴链接 → 探测卡（封面、标题、UP 主、有无字幕、是否处理过、**预计花费与时长**）→ 四段进度（探测 / 下载 / 识别 / 总结，各有耗时，识别段有进度条）→ 日志折叠一行。底部是队列。

**详情**：左右分栏。左：阅读视图（段落 + 时间码）、本条内搜索高亮；底部悬浮「问这条视频」。右：总结类型分段控件（每个类型前的小点表示已生成/未生成），未生成的显示预计花费和「生成」按钮；思维导图内嵌渲染。

**库**：顶部四个数（视频数、转写总时长、总结数、累计花费）；搜索框搜正文；列表按 UP 主分组，一行一条（缩略图、标题、识别方式、已有总结、日期，悬停出「打开目录 / 删除」）。

**原则**：任何要花钱的动作，点之前看到金额；任何能靠缓存解决的，不问用户；日志默认折叠。

---

## 10. 目录结构（现状 + v0.6）

```
video-summarizer/
├── DESIGN.md  README.md  config.yaml  .env.example  pyproject.toml
├── docs/mockup/v0.6-ui.html        界面视觉稿
├── frontend/                       React 源码（v0.6，Node 只在开发时需要）
├── src/video_summarizer/
│   ├── cli.py  pipeline.py  config.py  models.py  cache.py  ytdlp_base.py  errors.py
│   ├── subtitle/   audio/   asr/   summarizer/
│   └── web/
│       ├── api.py  jobs.py         FastAPI 路由与任务队列（v0.6）
│       ├── reading.py  library.py  mindmap.py   框架无关的逻辑
│       ├── static/                 d3 + markmap-view
│       ├── dist/                   前端构建产物（提交进仓库）
│       └── app.py                  Gradio 版（v0.6 完成后删除）
├── tests/
├── output/         产物（gitignore）
└── cache.sqlite    缓存（gitignore）
```
