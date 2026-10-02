# 拾光笺 · Glean — 设计文档

> 拾光笺 / Glean（命令行仍叫 `vsum`）：把视频变成一张能读的笺。
> 给一个链接，拿到结构化转写、可切换类型的 AI 总结、思维导图，并把处理过的视频攒成一个能搜的库。
>
> 文档分两部分：**已落地的现状**（第 1–7 节，按实际代码写，和最初的设想不一致的地方单独标出）
> 和 **路线图**（第 8–10 节）。路线图 v0.1 – v0.8 已全部落地；v0.9「伴读」一期已落地（见 8 节），视觉稿 `docs/mockup/v0.9-study.html`。

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

**2026-10 新增画像：专业长视频。** 本地导入的创新药、资本博弈之类的视频，几十分钟到几小时，概念密、论证链长。
这类视频要的不是「压缩」而是「看懂」，现有功能有三处不合用：

- 「只依据转写」的铁律挡住了最需要的东西——视频没解释的背景知识
- 总结把论证过程压没了，只剩结论
- 问答不知道你看到了哪里，几小时的转写还可能被截断

对策是 v0.9 的**伴读模式**，和原来的「速读」并存，不替换。

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

### 3.6 界面 `web/` + `frontend/`

- `api.py`：FastAPI，REST + SSE；`jobs.py`：单 worker 后台队列，事件带自增序号，断线续传
- `reading.py`（分句聚段）、`library.py`（缓存 + 产物目录合并、按 UP 主分组）、`mindmap.py`（大纲解析、CLI 导出）是框架无关的纯逻辑
- 前端 React + TypeScript + Vite，构建产物提交在 `web/dist/`，用户不需要 Node；设计见第 9 节
- 曾经的 Gradio 版已删除（原因见第 7 节第 8 条）

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
| 界面 | FastAPI + SSE 后端，React + Vite + TypeScript 前端 | 构建产物随包分发；见第 9 节 |
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
    "uploader": "…", "upload_date": "20241024", "thumbnail": "http://…",
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

`transcripts(key, video_id, source_url, title, source_type, language, duration_sec, segment_count, has_speakers, payload, created_at, uploader, upload_date, thumbnail)`（后三列 v2 加，老库 ALTER TABLE 迁移）
`summaries(key, transcript_key, video_id, title, provider, summary_type, language, content, created_at)`
`questions(id, video_id, question, answer, provider, citations, created_at)`（v0.7 问视频）
`pending_jobs(id, kind, title, params, created_at)`（v0.7 批量：排队中的任务，重启续跑）

`search_fts(tok, video_id, kind, ref, start, text)`：FTS5 虚表（v0.7）。`tok` 是按字切开的索引列，`kind` 是 transcript / summary，`ref` 是段落序号 / 总结类型，`start` 是段落起始秒 / 行号。任务完成时增量更新，启动时补缺。

---

## 6. 配置（现状）

```yaml
subtitle: {languages: [zh-Hans, zh, en], allow_auto: false}
download: {user_agent: null, cookies_from_browser: null, cookies_file: null, batch_delay_sec: 5}
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
  price_input_per_m: 2.0   # 每百万 token 单价，界面换算金额用；留空只显示 token
  price_output_per_m: 3.0
output_dir: ./output
cache_db: ./cache.sqlite
```

密钥只在 `.env`（gitignore）。模型缓存目录由环境变量指到 E 盘：`HF_HOME` `MODELSCOPE_CACHE` `UV_CACHE_DIR` `UV_PYTHON_INSTALL_DIR` 等，`vsum config` 会打印。

---

## 7. 踩过的坑（实打实的）

1. **B 站 412 是按 IP 限流，和 UA 无关**。裸 yt-dlp 同样会中。对策：探测的 info 直接复用给下载，少发请求；失败退避重试。**空间列表接口比视频页紧得多**：匿名一两分钟只能请求几次，8 秒重试基本白等，一两分钟才放开；列表页结果在服务里缓存 10 分钟，翻回上一页不再打站点
2. **Windows 上 ctranslate2 找不到 `cublas64_12.dll`**：pip 装的 nvidia 库不在 PATH。要 `os.add_dll_directory` **并且** 前置 PATH，两个都做
3. **FunASR `max_single_segment_time` 传给 `generate()` 会被静默忽略**，必须在 `AutoModel(...)` 构造时传；`AutoModel(vad_model=...)` 一体化模式不返回时间戳，VAD 得单独跑
4. **whisper 的 VAD 会把纯音乐视频切成空**，要检测 100% 被过滤后关 VAD 重跑
5. **模型会用标题编内容**。SYSTEM prompt 里明令只依据转写，效果立竿见影
6. **Claude 接口不收 `temperature`**（新模型），走 `effort`；**Ollama 不显式传 `num_ctx` 默认只有 2k 上下文**，长转写会被静默截断
7. **测试 fixture 把真密钥发到了别家接口**：`base_url` 默认必须是 `None`，provider 要校验密钥前缀；读 `.env` 的测试要 monkeypatch 掉 `load_dotenv`
8. **Gradio 6**：主题和 css 只能从 `launch()` 传；`js=` 和 `fn` 不能放同一个事件；`.prose` 样式要 `!important` 才压得住；组件更新只换 innerHTML 不跑脚本（Markmap 靠 MutationObserver 重渲染）。这些是换框架的直接原因
9. **说话人分离 pipeline 仅中文**，且对背景音乐、抢话效果打折；本项目主要内容是独白，未在真实多人素材上验证
10. **模型权重和 uv 的 Python 默认落 C 盘**，环境变量要在装任何东西之前设好；删缓存目录前先看里面是什么
11. **抖音必须带浏览器 cookie**（不必登录），否则 yt-dlp 报 "Fresh cookies are needed"；`uploader` 字段是账号 handle，昵称在 `channel` 里——各站字段不一致，`VideoInfo` 里 channel 优先。分享口令里的链接用正则抠出来
12. 合规：定位个人学习工具，不做公开分发服务

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

### v0.6 — 界面重构（已完成）

目标：从「模型 demo」变成「产品」。**FastAPI + React**，视觉稿见 `docs/mockup/v0.6-ui.html`，设计决策见第 9 节。

- 后端 `web/api.py`：REST + SSE
  - `GET /api/library`（按 UP 主分组、统计）、`GET /api/videos/{id}`（段落化转写 + 各类型总结状态）
  - `POST /api/jobs`（处理一个 URL）→ `GET /api/jobs/{id}/events`（阶段、进度、日志，SSE）
  - `POST /api/videos/{id}/summaries`（生成某类型，先返回估算，确认后执行，流式）
  - `POST /api/probe`（贴链接即出探测卡：标题、UP 主、时长、有无字幕、是否命中缓存、预计花费）
- **任务队列**：后台线程单 worker，任务表落 SQLite，界面关了任务继续跑、重开能续看。这是 v0.7 批量的底座
- 前端 `frontend/`：Vite + React + TypeScript + Tailwind；构建产物提交到 `web/dist/` 随包分发，`vsum ui` 直接服务，用户不需要 Node
- 数据层补 **UP 主字段**：`VideoInfo` / `Transcript.meta` 记 `uploader`（yt-dlp 的 `uploader` / `channel`），库按它分组
- 保留：`reading.py` `library.py` `mindmap.py`；Gradio 版已删除，`ui` extra 只剩 `fastapi + uvicorn`（SSE 用 StreamingResponse 手写，不引第三方）
- 老记录没有 UP 主信息：`vsum cache refresh-meta` 逐条重新探测补齐

### v0.7 — 从「处理一个视频」到「管理一个库」（已完成）

按优先级：

1. ✅ **全库搜索**：SQLite FTS5，中文按字切分，搜转写正文与总结；结果带原文片段 + 时间戳，点开定位到段落。零 LLM 成本
2. ✅ **问视频**（`qa.py`）：对一条转写提问，模型只依据转写回答，**每条结论附时间戳**，点了左栏跳到那一段。转写带 `[mm:ss]` 分句整篇进上下文，超预算截断并提示；不做 map-reduce（切块汇总丢时间戳）。同步调用不进队列（不该排在 ASR 后面）。问答记录存 `questions` 表；详情页两栏改成各自滚动，跳转写时回答不跟着滚走
3. ✅ **批量**（`listing.py`）：合集 / UP 主空间链接 → 一页 30 条（标题、时长、封面、是否已处理）→ 勾选入队。B 站空间直接调 yt-dlp 内部用的 `x/space/wbi/arc/search`（yt-dlp 自己 flat 模式只吐 BV 号），顺带支持关键词过滤；其他列表走 flat 提取分页。串行跑，相邻隔 `batch_delay_sec`；排队任务记 `pending_jobs` 表，重启续跑。空间接口限流极紧（匿名几次/分钟），退避一次不行就明确提示等待或配 `cookies_file`

### v0.8 — 打磨（已完成）

- ✅ 成本记账（`usage` 表）：provider 从响应读真实用量，接口层每件事记一笔并按单价换算；库页、详情页、设置页展示
- ✅ 转写清洗（`cleaning.py`）**可选**：阅读视图开关 + `summarizer.clean_transcript`。规则保守：只删边界上的"呃""嗯"、只折叠多字重复；原文永远落盘
- ✅ 库管理：删除、打开产物目录（v0.6 已有）；设置页「存储」显示音频缓存占用，一键清理 `audio/`（转写总结不受影响）
- ✅ 问 UP 主（跨视频，`qa.ask_uploader` + `/uploader/:name` 页）：材料是每条视频的总结（overall > key_points > timeline > by_speaker > mindmap > 转写开头 1500 token），按发布日期排序，模型按【n】引用、指出前后变化。四条视频一问约 ¥0.01，比整篇转写便宜一个量级。记录存 `questions` 表（video_id = `uploader:<名>`）

### v0.9 — 伴读：和 AI 一起看专业长视频（一期已完成）

视觉稿 `docs/mockup/v0.9-study.html`。定位：**AI 是陪你看的人，目标是让你看懂，不是替你省时间。**
详情页顶部加「速读 / 伴读」切换；速读就是 v0.6 以来的详情页，原样保留。

#### 原则的修订：从「只依据转写」到「来源分区」

v0.7 问视频的铁律是「转写里没有的不许说」，防的是模型编造。伴读要放开背景知识，但不放弃可验证：

- 回答固定分两块：**「视频里说」**每句带 `[mm:ss]`，可点跳转；**「背景补充」**是视频之外的知识，视觉上明确区分（虚线框 + 标签）
- 背景补充用了联网结果的，标来源链接和检索日期；没联网、凭模型自身知识的，标「模型知识，未核实」
- 视频里的说法和背景补充矛盾时，必须明说，不许悄悄用一个覆盖另一个
- 速读模式和问 UP 主不变，仍是只依据转写

#### 1. 播放：在工具里看

- 详情页伴读模式左栏是视频播放器。本地导入的视频直接用原文件；链接任务默认只下音频，伴读需要时补下视频（720p 够用，落 `output/<目录>/video.*`，设置页可清理）
- `GET /api/videos/{id}/media` 用 `FileResponse` 出流，和音频一样支持 Range
- 进度条上画章节分隔；转写跟随播放滚动、高亮当前句；点转写任一句跳到那里
- 快捷键：空格 播放/暂停，`←/→` 5 秒，`Q` 在当前时间点提问，`M` 记一条笔记

#### 2. 学习底稿（看之前生成一次，之后免费复用）

一个新的产物类型 `study`，结构化 JSON（`study.json` + `summaries` 表，type = `study`），五部分：

| 部分 | 内容 | 来源约束 |
|---|---|---|
| 前置知识 | 看这期前要懂的 5–10 个概念，每个两三句 | 背景知识，标「模型知识」 |
| 章节与论证线 | 每章：标题、起止时间、**论点 → 证据 → 结论** | 只依据转写，带时间戳 |
| 术语表 | 词条：视频里怎么说（时间戳）+ 背景解释（分区） | 两栏分开 |
| 论断与数字 | 关键数据和判断，标「有时效性 / 可核实」 | 只依据转写；核实按需联网 |
| 玩家 | 公司 / 人 / 机构及其关系（合作、竞争、收购、授权） | 只依据转写 |

- 生成：按章节切块 map（每块抽本章论证线、术语、论断，时间戳天然保留），reduce 只合并术语表、归并玩家、写前置知识。和问视频不同，这里切块不丢时间戳
- 花钱前照例报预估；两小时视频约 4 万 token 输入，DeepSeek 下预计几毛钱
- 术语表进专名词表：确认过的术语顺手进 UP 主词表，下次 ASR 当热词

#### 3. 定位提问（看的时候）

- 提问自动带「📍 当前时间 · 第 n 章」，上下文构造：
  1. **固定前缀**：system + 全片转写（带 `[mm:ss]`）+ 学习底稿——同一视频每次一样，吃 DeepSeek 上下文硬盘缓存，追问的边际成本很低
  2. **可变尾部**：当前时间点前 5 分钟、后 1 分钟的原文（重复强调一遍「我在这」）+ 最近几轮对话 + 问题
- 超过上下文窗口的超长视频：全片转写退化为章节论证线，只保留当前章 ± 1 章的原文
- **防剧透开关**（默认关）：开了以后，后文的内容只说「[52:10] 会讲到」不展开
- 联网：DeepSeek function calling 给模型一个 `web_search` 工具，模型觉得需要时自己调；界面上显示「检索了 n 次」。每次提问最多 3 次检索
- 快捷问法按钮：「这里在说什么」「为什么」「和前面矛盾吗」「举个例子」
- 记录进 `questions` 表，加 `position_sec` 列

#### 4. 联网检索 `websearch.py`

- provider 抽象，先接一个：**Tavily**（API 对 LLM 友好，返回正文摘要，免费额度够个人用）；留 SearXNG（自建、免费）的口子
- 只在两处用：问答里模型主动调用；论断清单里点「核实」
- 检索结果缓存进 SQLite（查询词 + 日期为键），同一个问题不重复花钱；计入成本记账
- 没配 key 时联网能力整体隐藏，背景补充一律标「模型知识，未核实」

#### 5. 看完以后（第二期）

- 划线 / 笔记绑时间戳，存 `notes` 表，「存到 Obsidian」时一起导出（复用 v1.0 的通道）
- 理解自测：按章节出 3–5 道题，或「用你的话复述这一章」由模型指出遗漏和误解
- 跨视频：术语表按词聚合，同一个概念在哪些视频里出现过

#### 一期落地记录（和上面设想不一致的地方）

- 代码：`study.py`（底稿两遍生成、提问的消息构造与检索工具循环）、`websearch.py`、`components/Study.tsx`；接口 `/api/videos/{id}/study`、`/study/ask`、`/study/known`、`/media`、`/media/remux`
- 底稿存 `study_sheets` 表（一条视频一份，不进 `summaries`，免得混进总结列表和 Obsidian 导出）+ 产物目录 `study.json`；输入指纹对不上时提示「转写改过，可以重新生成」，旧的照样能看
- 伴读提问存 `questions` 表 `mode = 'study'`，带 `position_sec` 和检索来源；和速读的问视频互不混。历史由服务端从库里取最近 4 轮，不靠前端回传
- 消息顺序：system → 全片转写 + 底稿大纲（user）→ 固定的确认（assistant）→ 历史 → 当前位置附近原文 + 问题。实测 46 分钟视频第二问输入 11.7k、缓存命中 9.7k
- 播放：本地导入的原文件直接出流；flv / ts 这类浏览器放不了的，一键 `ffmpeg -c copy` 转封装成产物目录里的 `video.mp4`；链接视频走「存视频」下一份
- 背景表的键常被模型带上括号里的英文全称（「PCB（Printed Circuit Board）」），对键时去掉括号再比
- 联网只实现了 OpenAI 兼容接口的 function calling；Claude / Ollama 走不带工具的回退，界面上联网开关是灰的

#### 分期

| 期 | 范围 |
|---|---|
| 一期 ✅ | 视频播放 + 转写联动；学习底稿（前置知识、章节论证线、术语表）；定位提问（来源分区、缓存友好的上下文）；联网检索 |
| 二期 | 论断核实、玩家关系；笔记 → Obsidian；自测 |
| 三期 | 跨视频术语；关键帧识图（图表少，优先级最低） |

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

## 10. 目录结构

```
glean/
├── DESIGN.md  README.md  config.yaml  .env.example  pyproject.toml
├── docs/mockup/v0.6-ui.html        界面视觉稿
├── docs/mockup/v0.9-study.html     伴读模式视觉稿
├── frontend/                       React 源码（v0.6，Node 只在开发时需要）
├── src/video_summarizer/
│   ├── cli.py  pipeline.py  config.py  models.py  cache.py  ytdlp_base.py  errors.py
│   ├── subtitle/   audio/   asr/   summarizer/
│   └── web/
│       ├── api.py  jobs.py         FastAPI 路由与任务队列（v0.6）
│       ├── reading.py  library.py  mindmap.py   框架无关的逻辑
│       ├── static/                 d3 + markmap-view（CLI 导出 mindmap.html 用）
│       └── dist/                   前端构建产物（提交进仓库）
├── tests/
├── output/         产物（gitignore）
└── cache.sqlite    缓存（gitignore）
```
