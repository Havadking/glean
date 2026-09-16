# 拾光笺 · Glean深度理解文档

> 本文是一次完整的代码通读结果，不是 README 的复述。目标是回答三个问题：
> **它是怎么搭起来的**（结构与数据流）、**为什么这么搭**（每条设计决策背后的约束和踩过的坑）、
> **改哪一层要注意什么**（file:line 级的技术债与扩展点）。
>
> 阅读前提：`README.md` 讲"怎么用"，`DESIGN.md` 讲"当初怎么想"。本文讲"代码实际长什么样"。
>
> 通读范围：`src/video_summarizer/` 全部模块、`frontend/src/` 全部页面与组件、
> `tests/`、`config.yaml`、`pyproject.toml`、`scripts/`。快速测试实跑通过（272 passed）。

---

## 0. 一句话与三层价值

**拾光笺把"一个视频链接"变成"一份可读、可检索、可追问的结构化知识"**，命令行叫 `vsum`。
它不是"ASR 调用示例"，而是一个**带缓存层、带成本账本、带后台任务队列的个人知识库应用**。

它真正解决的问题按代码里的痕迹排序（这是理解整个项目的关键）：

| 优先级 | 约束（来自实际使用画像） | 代码里的体现 |
|---|---|---|
| 1 | **只有 DeepSeek 额度，每次花钱都要先看得见** | `plan() → CostEstimate → confirm()` 三段式；`/api/probe`、`/videos/{id}/estimate`；`usage` 表按真实 token 记账 |
| 2 | **追同一个 UP 主，中文独白，3–22 分钟** | 库按 UP 主分组；FunASR SenseVoice 默认；「问 UP 主」跨视频；`by_speaker` 默认不开（独白用不上） |
| 3 | **要结构多过要段落** | 5 种总结类型；思维导图输出 Markdown 大纲而非生图；时间戳可点击验证 |
| 4 | **本地 Windows 单机、12G 显存、不许占 C 盘** | 单 worker 串行队列；模型权重目录靠环境变量指到 E 盘；`vsum config` 打印生效路径 |

一个能概括全部设计的判断：**这个项目把"诚实"当成功能来设计**。
模型会编（`prompts.py:16-17` 有实证记录），于是有了：转写之外的背景知识被 SYSTEM 明令禁止、
每条问答结论必须附时间戳且可回查、专有名词纠正只出替换表且逐条可否决、
钱按真实用量记账而不是估算、缓存按输入指纹而不是路径。

---

## 1. 数据流总览

```
                        URL
                         │
        ┌────────────────▼────────────────┐
        │ [0] probe()  yt-dlp 只探测不下载  │  ← 一次拿到 info，之后复用（省请求 = 省 412）
        └────────────────┬────────────────┘
                         │  VideoInfo{video_id, title, duration, manual_subs, auto_subs, uploader, raw}
                         │
        ┌────────────────▼─────────────────┐
        │ plan_transcript_key()            │  ← 探测阶段就能算出"会产出什么样的转写"
        │  命中字幕? → subtitle 指纹        │     据此得缓存指纹，不必真跑一遍
        │  否则      → asr 指纹             │
        └────────────────┬─────────────────┘
                         │
        ┌────────────────▼─────────────────┐
        │ _get_transcript()  三级取用       │
        │  1. SQLite 命中 → 直接用          │
        │  2. 产物目录 fingerprint 相符 → 认领│
        │  3. 真跑：字幕 → vtt 清洗          │
        │          无字幕 → 音频 → ASR       │
        └────────────────┬─────────────────┘
                         │  Transcript{segments[{start,end,text,speaker}], title, video_id, meta}
                         │
        ┌────────────────▼─────────────────┐
        │ correction.polish()  可选          │  只出替换表，写进 meta.corrections，原文不动
        └────────────────┬─────────────────┘
                         │  落盘 transcript.json（永远是原文）+ 写缓存
                         │
        ┌────────────────▼─────────────────┐
        │ correction.apply() + cleaning()   │  ← 只影响"送给模型的那一份"
        └────────────────┬─────────────────┘
                         │
        ┌────────────────▼─────────────────┐
        │ provider.plan() → CostEstimate    │  ← 花钱前先报数
        │   confirm() 通过 → summarize()    │
        │   map-reduce 在基类里统一实现      │
        └────────────────┬─────────────────┘
                         │  summary markdown
                         ▼
   transcript.json · summary.md · mindmap.html · cache.sqlite（索引+账本+标签+问答+回顾）
                         │
                         ▼
        FastAPI(web/api.py) ── SSE ──► React(frontend/)  三个主页面 + UP 主页 + 回顾页
```

**模块间的唯一契约是 `models.py` 里的 `Transcript` / `Segment` / `SummaryOptions`。**
四个子系统（字幕、音频、ASR、总结）彼此不 import，全部通过这个中间结构对接，
任何一个实现都能单独替换 —— 这是这个项目分层上最干净的一点。

模块依赖是单向的（`pipeline.py` 是唯一的编排者）：

```
cli.py ──┐
         ├──► pipeline.py ──► subtitle/ audio/ asr/ summarizer/ cache
web/api.py ┘        │
                    └──► correction / cleaning / tagging / qa / digest / search / listing
                                              （经 web/library.py 取材料）
```

`web/` 里有一个刻意的设计：**框架无关的纯逻辑单独成文件**（`reading.py` 段落聚合、
`library.py` 库合并、`mindmap.py` 大纲解析），CLI 和 API 都能用，也都单独可测。
`cli.py search` 就直接 import 了 `web.library.load_library`。

---

## 2. 五个"支点"设计决策

理解了这个项目里最有分量的五个决定，剩下的都是细节。

### 2.1 缓存按**输入指纹**索引，不按路径（`cache.py:150-213`）

```python
_fingerprint(parts) = sha256(json.dumps(parts, sort_keys=True))[:32]
```

- 转写指纹 `transcript_key()`：`kind(subtitle|asr)` + `video_id` + `extractor` +（字幕：语言 + 是否自动）
  或（ASR：provider + model + language + diarize）。
- 总结指纹 `summary_key()`：转写指纹 + `provider.describe()` + 总结类型 + 输出语言 + 附加要求
  + `corrections` 指纹。

这一条修掉了初版"output 目录里有 transcript.json 就复用"的致命毛病：
**换了 ASR 模型或开了说话人分离，指纹就变了，绝不会静默返回设置不符的旧结果**；
反过来视频改了标题、输出目录改了名字，指纹不变，照样命中（`cache.py:6-8`）。

配套的三级取用（`pipeline.py:254-269`）值得单独记住：

| 顺序 | 条件 | 动作 |
|---|---|---|
| 1 | 缓存指纹命中 | 直接用 |
| 2 | 缓存没有，但 `output/*/transcript.json` 的 `meta.cache_key` == 当前指纹 | **认领**（`_adopt_existing`，`pipeline.py:305`）—— 给"删了 cache.sqlite"和"从旧版本升上来"兜底 |
| 3 | 都没有 | 真跑 |

第 2 步是巧妙的一环：产出转写时把 `cache_key` 写进 `meta`（`pipeline.py:129`），
于是**缓存是索引层、产物才是本体**，删掉 `cache.sqlite` 只意味着下次慢一点，不丢东西。
第 2 步同时被 `--no-cache` 刻意绕过（`pipeline.py:252-254`），否则"不走缓存"会变成一句空话。

`--no-cache` / `--force` 的语义差别是**故意**的（README 有表）：

| | 读缓存 | 重下音频 | 重跑 ASR |
|---|---|---|---|
| 默认 | 是 | 否 | 否 |
| `--no-cache` | 否 | 否 | 是 |
| `--force` | 否 | 是 | 是 |

### 2.2 ASR 双路 + 语言门控的兜底（`asr/`）

默认 **FunASR SenseVoice-Small**，走"FSMN-VAD 切段 → SenseVoice 批量识别"两条独立模型
（`funasr_provider.py:136-143`）。这不是优化，是**必需**：SenseVoice 在长音频上质量会崩，
而且 VAD 独立跑才拿得到时间戳（一体化的 `AutoModel(vad_model=...)` 只返回 `key`/`text`）。

开说话人分离则换成**一体化 pipeline**：`paraformer-zh + fsmn-vad + ct-punc + cam++`
（`funasr_provider.py:127-133`），因为只有 paraformer 会输出 `sentence_info`（文本+起止+说话人）。

**兜底包装 `FallbackASRProvider`（`asr/fallback.py:46-66`）的触发条件有三条**，值得记住：

1. **语言门控**：`primary.supports_language()` 为假时**根本不试主路**，直接走备路。
   FunASR 不分离时覆盖 `{zh,en,yue,ja,ko}`，分离时只覆盖 `{zh}`（`funasr_provider.py:79-82`）。
2. 主路抛 `ASRError`。
3. 主路返回空 segments。

兜底会在 `meta` 里留痕：`fallback_from` / `fallback_reason`。

**没默认全开说话人分离的原因是成本不是效果**：paraformer pipeline 只做中文，且慢一半
（配置注释里给了实测：22 分钟视频 31s vs 11s）。`diarize: auto`（`config.py:61-70`）
只在总结类型是 `by_speaker` 时才开 —— 这个"按需开启"的语义由 `wants_diarization(needed=...)` 实现。

### 2.3 总结层用模板方法把"估算"和"执行"分开（`summarizer/base.py`）

```
plan(input, options) → CostEstimate{transcript_tokens, chunks, strategy, estimated_input_tokens}
summarize(input, options) → str        # 内部再调一次 plan()，然后 map or map-reduce
_complete(system, user) → str           # provider 只实现这一个
```

- `plan()` **无副作用、不发请求**，因此可以被流水线、CLI、`/estimate` 各调一次而免费。
- 分块决策（`base.py:106-116`）：`budget = max_context_tokens - max_output_tokens - 1500`；
  `chunk_strategy`：`never` / `always` / `auto`（`body_tokens > budget`）。
- map-reduce（`base.py:139-186`）：逐块 `_complete` **串行**、按序累积要点，
  最后一次 `_complete` 做 reduce。**没有并发、没有逐块重试、reduce 输入不做大小检查**。
- `provider.complete()` 是"裸调用"的公开口，问答 / 打标签 / 纠错都走它，绕开总结模板。

用量记账也是一条独立通路：`_record_usage` 只在**成功拿到真实 token 数**时累加
（`base.py:67-74`，`"?"` 之类解析不出来的值会被静默丢），`take_usage()` 取走并清零，
再由 `web/api.py:_record_usage` 按 `price_*_per_m` 换算成钱写进 `usage` 表。

三种 provider 的差异全在 `_complete` 的"协议怪癖"上，而且都是踩过坑才写下的：

| provider | 关键处理 | 不这样会怎样 |
|---|---|---|
| `openai` | 手写重试（`max_retries=0`，4s/8s/12s），只重试限流/超时/连接/5xx | SDK 重试无法区分认证错误，会浪费时间 |
| `claude` | **不发 `temperature`**，改用 `output_config={"effort": ...}`；**强制流式**；`<8192` 的 `max_output_tokens` 给警告 | 当前一代 Claude 收到采样参数直接 400；100k+ token 非流式会超时；思考 token 也算进 max_tokens，设小了正文被截断 |
| `claude` | 密钥不以 `sk-ant-` 开头且没配 `base_url` → **拒绝发送** | 测试 fixture 曾经把真密钥发到了别家接口（`DESIGN.md` 第 7 节第 7 条） |
| `ollama` | 走**原生** `/api/chat` 只为传 `options.num_ctx`；输入吃满时警告 | 官方兼容端点传不了 `num_ctx`，默认 4096 会把长转写**静默截断**，总结莫名少一半 |

`temperature` 在配置注释里明确标注"只对 openai/ollama 生效"（`config.py:84-88`）——
配置项有 provider 适用范围这件事被写进了注释、文档和代码三处。

### 2.4 抗幻觉是**结构**，不是 prompt 措辞

这是整个项目最有想法的一块。SYSTEM prompt 的约束（`prompts.py:12-23`）：
> 只依据转写内容作答…… 元信息里给出的标题和链接只是让你知道在处理哪个视频，不是可以引用的内容来源……
> 转写内容少，总结就短；宁可写"视频内容有限，没有更多可总结的"。

但作者显然不信 prompt 能兜住，于是每一处都用结构再兜一次：

| 幻觉风险 | 结构性防御 |
|---|---|
| 模型自由发挥背景知识 | 时间线模板硬性要求 `- [mm:ss] 标题 — 说明` 且"时间点必须来自转写中给出的时间戳"（`prompts.py:85`） |
| 问答结论无法验证 | SYSTEM 要求每条结论末尾附 `[03:27]`；`TIMESTAMP_RE` 抽出来存进 `questions.citations`；前端点击 → 跳到那一段并高亮（`AskPanel.tsx:10-18,120-131`）。**没有时间戳可引的结论，就等于转写里没有** |
| 跨视频问答张冠李戴 | `【n】` 编号引用 → `citeHtml()` 渲染成指向那条视频的链接（`lib/cite.ts:10-19`）；材料按**发布日期升序**排，模型才能说"先说 X 后来改口 Y"（`api.py:1152`） |
| 专名同音字错 | 纠错**只输出替换表**，不重写全文；`validate()` 六道程序化校验（见 §5.1） |
| 标签词表漂移 | 每次生成都把库里已有标签喂回去，要求优先复用（见 §5.3） |

"模型会编"这一条被写进了 README 的**已知局限第一条**：
"约束能压住大部分，但不能根除 —— 拿总结当索引，别当事实来源。" 这种自我认知在个人项目里很少见。

### 2.5 Web 前端**构建产物入库**，用户不装 Node

`vite.config.ts:9` 把 `outDir` 直接指向 `../src/video_summarizer/web/dist`，构建产物提交进仓库，
`vsum ui` 用 FastAPI 静态服务它（`api.py:1490-1499`）。

代价是**改前端必须 `npm run build` 并提交新的 hash 资源**，只改 TSX 在 7860 上毫无变化。
`index.html` 刻意加了 `Cache-Control: no-cache`（`api.py:1499`），否则前端更新后浏览器还会引用旧 assets。

收益是一个真实的用户体验：`uv run vsum ui` 一条命令跑起来，不需要 Node、不需要 pnpm、
不需要 nginx。对于一个"给自己用"的工具，这个取舍是对的。

---

## 3. 采集层：探测 / 字幕 / 音频 / ASR

### 3.1 探测与信息复用（`ytdlp_base.py`）

- `probe()`（`:120-150`）：`sanitize_info(extract_info(download=False))`，**只探测不下载**。
- 重试：3 次，线性退避 8s / 16s，且**只在错误形似 `HTTP Error (412|429|403)` 时重试**
  （`_RATE_LIMIT_RE`，`:243`）。非限流错误立刻抛出 —— 别把"视频不存在"重试三遍。
- **info 复用是这个项目对抗 B 站 412 最有效的一招**：`download(info, opts)` 把
  `info.raw` 序列化成临时 `info.json`，用 `download_with_info_file` 下载，
  等价于 `--load-info-json`，**少发一次页面请求**（`:203-214`）。序列化失败则降级为按 URL 下载。
- `pick_language()`（`:222-240`）：按配置顺序精确匹配 → **双向前缀匹配**
  （`lang.startswith(w+"-") or w.startswith(lang+"-")`，所以 `zh` 能匹配 `zh-Hans`/`zh-CN`）
  → 最后兜底取 `next(iter(available))`（**依赖 yt-dlp dict 的插入顺序，各站不一致**）。
- 伪字幕轨剔除：`PSEUDO_SUBTITLE_LANGS = {danmaku, live_chat, rechat}`（`:33`），
  `subtitle` 和 `automatic_captions` 都过滤。B 站只有弹幕 → 正确识别为"无字幕"。
- `video_info_from_dict`（`:153-176`）：单个视频走 flat 提取时直接得到完整 info，
  **不用再探测一次**（`listing.py:237-250` 的注释明说：对 B 站来说少一个请求就少一分 412 风险）。
- `uploader` 取 `channel or uploader or uploader_id`：B 站 `uploader` 是昵称，
  抖音 `uploader` 是 handle 而昵称在 `channel` —— 各站字段不一致，这里统一成"channel 优先"。

### 3.2 字幕：宁可没有，不要不准

`accept_auto_captions: false` 是默认值（`config.py:21`）。只有自动字幕时抛
`SubtitleNotFoundError`，消息里带上"自动字幕 N 种，已按配置忽略"（`fetcher.py:42-45`），
流水线捕获它之后自然转入 ASR（`pipeline.py:271-276`）—— **用异常做控制流**，但语义清晰。

`vtt.py` 的清洗顺序有个细节：**先剥 `<...>` 标签再 `html.unescape`**（`:39-44`），
反了的话 `&lt;b&gt;` 会被误剥。另外去 U+200B 零宽空格、`\xa0` 转空格、`\s+` 折叠。

去重（`_dedupe`，`:94-111`）处理两类真实字幕形态：
- 完全相同的连续文本 → 延长上一段（保留更早的 start）
- **滚动字幕**（后一段以「已显示的上一段」为前缀且时间重叠）→ 采用更长的那份

时间戳全程**不做四舍五入**，唯一的调整是 `end = max(end, start)`。

### 3.3 音频：16k 单声道是硬要求

`ffmpeg -y -loglevel error -i SRC -vn -ac 1 -ar 16000 -c:a pcm_s16le TARGET`（`extractor.py:106-125`）。
复用条件是 `is_file() and size > 0 and not force` —— **零字节残留文件永远不信任**（`:37-39`）。

UA 只在**最后一次**重试时才覆盖（`_download_audio`，`:48-75`），
配置注释解释了原因："B 站 412 是按 IP 的频率风控，换 UA 治不好"。

### 3.4 FunASR 的两个坑（`funasr_provider.py`）

1. **`max_single_segment_time` 必须在 `AutoModel(...)` 构造时传**（`:138-139`）。
   传给 `generate()` 不报错，只是**静默忽略**，于是用默认的 60000ms —— 远超 SenseVoice 的
   30 秒训练窗口，质量直接掉。
2. **`AutoModel(vad_model=...)` 一体化模式不返回时间戳**（模块 docstring `:7-14`），
   所以 VAD 必须单独跑，这也顺带让"VAD 切段 → 批量识别"成为可能（`batch_size=16`）。

时间戳的语义值得注意：**不分离时，分句的 start/end 来自 VAD 语音段边界，不是逐词对齐**
（`_transcribe_fast`，`:225-235`）。所以长段落起止会偏粗 —— 这是 README 里承认的局限的代码根源。

`_transcribe_fast` 里有一处硬校验（`:297-300`）：识别结果条数必须等于切片条数，
否则抛 `ASRError("时间轴无法对齐")`。**正是这个校验让 VAD span 和文本的 zip 是安全的**。

分离模式（`:155-204`）用 `batch_size_s=300`（300 秒动态批）而非固定批，
然后把句子级的碎分句合并成"一轮发言"：同一说话人 + 间隔 ≤ 1.5s + 总长 ≤ 45s
（`_merge_turns`，`:308-327`）。

### 3.5 ASR 跑在子进程里（`asr/worker.py`）

`pipeline.py` 不直接构造 provider，而是调 `asr_worker.transcribe()`：起一个
`python -m video_summarizer.asr.worker` 子进程，stdin 送一行 JSON 请求，stdout 逐行收
日志和最终结果，子进程送完结果就 `os._exit`。原因是内存：`import torch` 就是 1.4GB 提交内存，
加 funasr 和 CUDA context 到 2.5GB，一条任务跑完后 torch 的缓存分配器、cuDNN/cuBLAS 工作区
留在进程里收不回（实测 `vsum ui` 跑完一条 5 分钟视频常驻 5.6GB）。子进程退出，系统一次收走，
主进程稳定在几十 MB。代价是每条任务多付一次 import（磁盘缓存热着约 10 秒）。

几个实现细节：
- **子进程把 fd 1 让给协议流，fd 2 覆盖到 fd 1**，funasr/modelscope 的 `print` 和进度条全进
  stderr（也就是 `ui.err.log`），不会污染 JSON 协议。
- **日志转发在调用方线程上重放**（`_pump`），因为 `jobs.py` 的任务日志按线程号过滤；
  "转写进度 xx%" 这行也因此照常驱动界面进度条。
- **父进程死了子进程跟着退**，Windows 上是等父进程句柄（`WaitForSingleObject`）。
  不能用"阻塞读 stdin 等 EOF"：有线程卡在 stdin 的 `ReadFile` 时 numpy 的扩展模块一加载就死锁
  （实测复现）。
- `VSUM_ASR_INPROCESS=1` 退回主进程内跑，调试用。
- 服务启动时的预热（`api.py:_warm_up_asr`）也改成起一个只 import 就退的子进程，
  目的只是把 3GB DLL 读进操作系统文件缓存。

### 3.6 whisper 兜底（`whisper_provider.py`）

- **Windows cublas 修复**（`:30-57`）：`add_dll_directory` **和** 前置 `PATH` **两个都做**。
  注释解释了为什么：ctranslate2 第一次矩阵乘时用裸 `LoadLibrary` 解析 cublas，
  **只认 PATH**；只做前者会"模型加载成功，第一次计算才炸"。
  同时保留 DLL 目录句柄（`_dll_handles`），因为句柄释放会把目录从搜索路径里移除。
- **两级降级**：加载时按 `[("cuda",...), ("cpu",...)]` 顺序试（`:108-121`），
  运行时第一次 CUDA 报错（消息里必须含 `cublas/cudnn/cuda/gpu/device` 之一）再降一次 CPU。
  **用户显式写了 `device: cuda` 时不静默降级**，直接把错抛出去（`:131`）。
- **VAD 清空重试**：`vad_filter=true` 得到空结果时，警告（纯音乐/强背景音）后**关掉 VAD 重跑一次**；
  再空才报错（`:139-153`）。

---

## 4. 转写语义层：段落、纠错、清洗

### 4.1 `reading.py`：只影响显示的段落聚合

这是被低估的一个文件。它开头那张表说明了必要性 —— 各来源的分句粒度完全不可读：

| 来源 | 分句数 | 平均时长 | 平均字数 |
|---|---|---|---|
| 字幕 | — | 2.4 秒 | 35 字 |
| whisper | 49 | 3.0 秒 | 36 字 |
| FunASR（不分离） | 175 | 4.6 秒 | 25 字 |
| FunASR + 说话人分离 | 31 | 42.9 秒 | 222 字 |

聚段规则（`_should_break`，`:126-135`），按优先级：
1. 换说话人 → 永远断开
2. 超过硬上限 `MAX_SEC = 150s` → 必须断开
3. 停顿 ≥ `LONG_PAUSE_SEC = 3s` → 话题多半断了
4. 已够长（≥ 60s）**且**上一句以句末标点收尾 → 在自然处断

`_join` 的空格逻辑（`:71-81`）必须认全角标点，否则 `"你好， 世界"` 这种拼接会出现
（注释里明确点了这个 bug）。

关键约束写在文件头：**只影响显示，`transcript.json` 的精确时间轴不动。**
搜索索引也是按**段落**建的（`search.py:159`），所以"搜索命中的段落"和"阅读视图看到的段落"
是同一个编号体系 —— 这是命中能直接定位的前提。

### 4.2 `correction.py`：把"改专名"做成可撤销的叠加层

设计上最巧的一处：**模型只输出替换表，不重写全文**，程序校验后叠加在原文上。

prompt（`:35-44`）有五条规则，其中第 2 条"只在**读音相同或相近**时才改"和第 4 条
"拿不准就不改，宁可漏改"是针对中文 ASR 错字特性的。注意它和总结层的规则是**反的**：
纠错**允许**使用世界知识（`":8-10"` 明确说明），因为"语数科技 → 宇树科技"必须靠外部知识。

**校验比 prompt 可靠**（`validate()`，`:130-164`），六道检查缺一不可：

| # | 规则 | 为什么 |
|---|---|---|
| V5 | 同一个 `from` 有多个 `to` → 取多数票 | 模型自相矛盾时选最可能 |
| V4 | `dst == src`／`len(src) < 2`／`len > 30` → 丢 | 单字替换风险太高；长串多半是重写句子 |
| V2 | 任一侧含数字 → 丢 | **数字错交给人**，模型改数字最危险 |
| V3 | `abs(len(dst) - len(src)) > 2` → 丢 | 同音替换长度必然接近；差太多就是重写 |
| 标点 | 含 `[\n\r。！？…；;.!?]` → 丢 | 段落是按标点切分的，改标点会改变段落编号 |
| V1 | `full_text.count(src) == 0` → 丢 | **必须在原文里原样出现**；这一条是"只出替换表"这份保证的主要落点 |
| V6 | `src` 出现在另一条的 `dst` 里 → 丢 | 禁止链式替换 |

通过后按 `(-hits, src)` 排序，最多 60 条。

运行时是个**读取时叠加**（`apply()`，`:199-205`）：没有生效条目就返回**同一个对象**（零开销），
否则 `dataclasses.replace(segments=..., meta={..., "corrected": True})`。
所以：落盘和缓存里永远是原文，阅读视图 / 搜索 / 总结 / 问答看到的是修正后的。

`fingerprint()`（`:208-213`）= `"|".join(f"{src}>{dst}")`，**掺进总结缓存 key**
（`cache.py:190-213`）—— 替换表变了，总结缓存自动失效。空指纹被刻意省略，
好让老缓存条目继续命中。

否决的状态记在 `transcript.json` 的 `meta.corrections` 里（`set_items`，`:216-226`）：
重跑时把上次被否决的 `(src, dst)` 对重新标成 `rejected`，**"不然重跑一次又冒出来"**。
界面每条一个勾选框，`PATCH /videos/{id}/corrections/{index}` 即时生效。

### 4.3 `cleaning.py`：刻意保守的去口水话

默认关（`config.py:96`）。规则（`:22-36`）：
- 单字填充 `呃`/`嗯` **只在左边界**删，且带 `(?!\1)` 负前视 ——
  **`嗯嗯` 表示同意，不删**。
- 词组填充 `呃呃/额呃/那个那个/这个这个/对吧对吧` 只在**两侧都是边界**时删。
- `([㐀-鿿]{2,4})(?:\1)+` 折叠 2–4 字词的相邻重复（`就是就是` → `就是`），
  **单字重叠一律不碰**：`看看`、`谢谢`、`慢慢`、`妮妮` 都保留。

实测口语视频省 1–4% token，"主要价值是好读"（README）。**时间轴永远不动**。
空掉的 segment 会被丢弃（`:51-58`）—— 这会让段落编号变化，但因为清洗只作用于"送给模型的那份"，
阅读视图的编号不受影响。

---

## 5. 总结之后的四个衍生能力

它们共享同一条材料选取策略（`web/library.py:169-189`），这点很关键：

```python
MATERIAL_ORDER = ("overall", "key_points", "timeline", "by_speaker", "mindmap")
TRANSCRIPT_FALLBACK_TOKENS = 1500
```

即：**优先「总体」总结 → 退而求其次 → 什么总结都没有就只取转写开头 1500 token（并标注"以下省略"）**。
「问 UP 主」、打标签、回顾三处**全部复用**这一个函数，所以给没总结的视频先补一份「总体」，
三个功能一起变好（README 明确提示了这一点）。四条视频问一次约 ¥0.01，
比整篇转写便宜一个量级 —— 这个成本优势完全来自这条策略。

### 5.1 问视频（`qa.py:ask_video`）

- 转写**带 `[mm:ss]` 整篇**进上下文，超预算就**只保留第一块**并标记 `truncated=True`，
  在 prompt 里明说"（转写太长，后半部分没有包含）"。
- **不做 map-reduce**，注释解释了原因：切块汇总会丢掉时间戳（`qa.py:8`）。
- 最近 **4 轮**问答作为上下文；前端把历史传回服务端，**服务端不维护会话**（`api.py:1115`）。
- **同步调用，不进任务队列** —— "不该排在一个 20 分钟的 ASR 后面"（`api.py:1120`）。
  这是一个很实用的判断：队列应该只放"独占 GPU / 耗时长"的活。

### 5.2 问 UP 主（跨视频，`qa.py:ask_uploader`）

- 材料是该 UP 主**所有视频**的总结，按 `(upload_date, created_at)` **升序**排，
  材料块编号成 `=== 【2】标题（日期）===`。
- SYSTEM 要求每条结论标 `【n】`，并**按日期指出前后变化**。
- **没有 token 预算、不截断**，`truncated` 硬编码为 `False`（`qa.py:202`）。
  这是当前实现里最明显的粗糙点：视频一多，输入会线性膨胀到超出上下文。
- 问答记录存在 `questions` 表，`video_id` 用 `uploader:<名>` 这个合成 key（`api.py:1262`）。

### 5.3 标签（`tagging.py`）

防词表漂移是重点。做法是**每次生成都把库里已有标签喂回去**（`tagging.py:74-90`）：

```
已有标签（优先复用）：护肤、成分、防晒……
```

SYSTEM 里写死"**优先从「已有标签」里选**……只有内容确实不属于任何已有标签时才新建"，
不这么做就会得到"护肤 / 护肤品 / 皮肤护理"三个同义标签。

**三态 source 是这套机制的关键**（`cache.py:100-105`）：

| source | 含义 | 删除时的行为 |
|---|---|---|
| `ai` | 模型打的 | 标成 `rejected`（**墓碑，不真删**） |
| `user` | 手动加的 | 真删 |
| `rejected` | 用户否决过的 AI 标签 | — |

于是：`set_ai_tags` 只重写 `source='ai'` 的行，**用户加的和墓碑全部保留**（`:630-652`）；
`videos_with_ai_tags()` 把 `rejected` 也算进去（`:654-664`），
所以"AI 标签全被删光"的视频不会被补标签任务反复骚扰；
`add_tag` 允许手动加一个标签来**复活**墓碑（`:597-599`）。

解析容错（`:37-71`）：先试 JSON 数组正则 `\[[^\[\]]*\]`，失败则按 `[,，、\n;；|]` 硬切并剥前缀符号。
`normalize()` 剥 `#`/引号、拒绝超 20 字、ASCII 转小写、按序去重、最多 5 个。

### 5.4 回顾（`digest.py`）

- 按周（默认）或按天，**按本地日期**归堆（`local_date` 把 UTC `created_at` 转本地，
  否则晚上 11 点收藏的视频会被算到第二天）。
- **不是定时任务**：打开页面时有视频但没回顾就自动生成一次，之后有新视频只在侧栏点个红点、
  提示"有 N 条新的"，点了才更新（`Review.tsx:28,53-60`）。
  决策依据很诚实：本地服务晚上不一定开着，也没有推送渠道。
- 缓存按 `(period, key, video_id 集合)`；集合不变就直接返回旧的，除非 `force`。
  `stale` / `new_count` 把"漂移"暴露给界面（`api.py:1379-1382`）。
- `generate()` 里有一行 `text.replace("\\n", "\n")`（`digest.py`）——
  修的是一些模型会吐出**字面量反斜杠 n**。

### 5.5 全文搜索（`search.py`）

**中文不用 FTS5 自带的 trigram**，因为它对**少于三个字的词无能为力**，而"甘油""蜂花"
这种两字产品名恰恰最常搜。做法是：

- 索引时把 CJK **按字切开**：`防晒棒` → `防 晒 棒`（`tokenize()`，`:52-66`）
- 查询时把词**拼成短语**：`"防 晒"`，于是相邻匹配、命中精确
- 拉丁词做**前缀匹配**（末尾加 `*`）：搜 `elephant` 命中 `elephants`

索引粒度：转写按**阅读视图的段落**（命中直接定位到用户看到的那一段），
总结按**行**（命中跳到对应类型）。

有一处细节做得很好（`:156-164`）：索引列**原文和修正文都进**（`tokenize(shown) + " " + tokenize(p.text)`），
"用户记得哪个写法就搜哪个"，而 `ref` 段落号用与阅读视图一致的编号 —— 因为纠错不动标点和时间轴。

索引和缓存在同一个 sqlite 文件，**任何失败都降级成"没结果"**，不影响主流程。
老库靠启动时 `sync_index` 补建（`api.py:104-109`），平时靠任务完成时 `index_one` 增量更新。

---

## 6. Web 层

### 6.1 后端结构（`web/api.py`，1522 行，49 个路由）

`create_app(cfg)` 是应用工厂，请求体模型定义在函数**内部**，
所以文件顶部有一条重要警告（`api.py:9-10`）：

> 这个文件不能用 `from __future__ import annotations` —— 请求体模型定义在 create_app 里面，
> 字符串注解会让 FastAPI 找不到类，把 body 当成 query 参数。

`State`（`:95-154`）是进程级单例，缓了四样东西：
1. `_probed`: `url -> VideoInfo`，探测结果**复用给任务**（少发一次请求）
2. `_listings`: `(url, page, keyword) -> (时间, 页)`，**TTL 600s** ——
   B 站空间接口限流极紧，翻回上一页不该再打一次站点
3. `_last_site_touch` + `throttle()`：批量时相邻视频之间隔 `batch_delay_sec`
4. `fresh_config()`：**每个任务重新读 `config.yaml`**，所以改了配置不用重启服务

路由分组：

| 组 | 端点 |
|---|---|
| 元信息/配置 | `/meta` `/usage` `/storage` `/config/pricing` `/config/llm` `/config/asr` `/config/test-llm` |
| 库 | `/library` `/videos/{id}` `/videos/{id}/estimate` `DELETE /videos/{id}` `/videos/{id}/open` |
| 探测 | `POST /probe`（单视频出探测卡，合集/空间出一页列表） |
| 任务 | `POST /jobs` `POST /jobs/batch` `GET /jobs` `GET /jobs/{id}` `DELETE /jobs` `DELETE /jobs/{id}` |
| **SSE** | `GET /jobs/{id}/events` |
| 总结 | `POST /videos/{id}/summaries`（已有且非 force 就直接给，**不进队列**） |
| 音频 | `GET/POST /videos/{id}/audio`（本地播放/下载） |
| 纠错 | `POST /videos/{id}/corrections` `PATCH /videos/{id}/corrections/{index}` |
| 问答 | `/videos/{id}/questions` `POST /videos/{id}/ask` `DELETE /questions/{id}` |
| UP 主 | `/uploaders/{name}` `POST /uploaders/{name}/ask` `/uploaders/{name}/avatar` `/uploaders/groups` |
| 标签 | `/tags` `POST /tags/backfill` `POST|DELETE /videos/{id}/tags...` |
| 回顾 | `GET /review` `POST /review/generate` |
| 搜索 | `GET /search` `POST /search/reindex` |
| SPA | `/{path:path}` 兜底返回 index.html（`:1493-1499`，`".."` 做了路径穿越防护） |

### 6.2 任务队列与 SSE（`web/jobs.py`）

`JobManager` 是**单 worker 线程**（`:164`）："ASR 独占 GPU，LLM 调用也没必要并发"。

事件的续传机制是这个文件里最讲究的部分：

- 每个 job 积累 `events`，**`seq = len(events) + 1`**（`:87`）。
- `subscribe(after_seq)` 先**补发**历史事件再挂上队列（`:94-102`）。
- SSE 端把 `seq` 同时写进 `id:` 字段（`api.py:1024`），
  浏览器重连自带 `Last-Event-ID`，服务端取 `max(after, last_event_id)`（`:1005-1007`）——
  **不会漏也不会重**。
- 客户端先用 `GET /jobs/{id}` 拿全部历史事件回放一遍，再带着 `after=<最后一个 seq>` 订阅
  （`useJob.ts:66-86`）。所以页面刷新/重开都能续看。

几个细节：
- 任务结束且队列已空时主动 `return`，不让客户端干等（`:1016-1017`）；否则 15 秒发一次 keepalive 注释行。
- **日志自动变成进度**：`_JobLogHandler` 只在 worker 线程上生效（`:142`），
  并用正则 `转写进度\s+(\d+)%` 从日志里抠出百分比（`:30`、`:152-154`）。
  这解释了 ASR provider 为什么要打那种格式的日志。
- `cancel()` **只能取消还没开始的**（`:193-201`）："正在跑的 ASR 没法安全打断"。
  但 `backfill_tags` 的 work 函数自己检查 `rep.job.status == "cancelled"` 来中途停下（`api.py:1344`）——
  两种取消语义并存。
- `find_active()`（`:203-210`）去重：同样的活已在排队/在跑就直接返回它，不重复提交。
- `MAX_KEPT_JOBS = 200`，超出就丢最老的**已结束**任务（`:247-255`）。

**持久化的边界很值得注意**：任务本身只在内存（进程重启就没了，`jobs.py:4-7` 坦白说明），
但**排队中的任务**记进了 `pending_jobs` 表（`api.py:836`），启动时 `_resume_pending()` 重新入队（`:841-859`）。
所以"服务重启会接着跑"，但"正在跑的那个"会从头再来（好在有缓存兜底）。

### 6.3 库的合成（`web/library.py`）

库有两个来源，必须合并（`:1-5`）：
1. `_from_cache`：SQLite `transcripts` / `summaries`
2. `_from_output_dir`：扫 `output/*/transcript.json` —— **v0.5 之前跑的视频只有这里有**

按 `video_id` 聚合，同一视频多份转写（先用字幕后又强制跑了 ASR）取最新；
`_merge()` 是"缺什么补什么"（`:314-330`），其中一条判断有讲究：
缓存里已有结构化总结记录时，不要再把目录里那份 `unknown` 的塞进来。

`_summary_from_file`（`:298-311`）从 `summary.md` 开头的元信息块（`- 总结类型：xxx`）
反解类型 —— **这就是 `render_summary_markdown` 那段 front matter 存在的理由**
（`pipeline.py:325-347`）：产物要能自解释，脱离缓存也能被识别。

`load_transcript(raw=False)` 默认**应用纠错表**（`:100-109`），
注释明确列举了消费者：阅读、搜索、总结、问答都该看修正后的。

`speaker_count` 在缓存来源里是硬编码的 `2`（`:240`）——
因为 `transcripts` 表只记了 `has_speakers` 布尔值，"具体几个要读产物才知道"。

### 6.4 前端结构（React 19 + Vite 8 + 手写 CSS）

- **Tailwind v4 被引进了（`@import "tailwindcss"` + vite 插件），但 TSX 里一个 utility class 都没用**。
  它实际只贡献了 preflight 重置，真正样式是 733 行手写 CSS，按屏幕分段 organizers。
  改前端时**应该延续手写 CSS + 设计 token 的约定**，加 Tailwind 类会是全仓首例。
- 设计 token 三套并存（`index.css:4-29`）：`:root` 亮色、`[data-theme="dark"]` 暗色、
  以及 `@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) }` 跟随系统。
  强调色是 B 站玫红 `--accent`，**只用在"当前项、主按钮、搜索命中"三处**；
  状态色 `--ok/--warn/--info/--bad` 与强调色体系分开。深色模式有个小瑕疵：
  `index.html` 没有预渲染的内联脚本，硬刷新时暗色用户会看到一次白闪。
- 状态管理是**一个 context，不用状态库**（`store.tsx:1` 明确说明）。
  队列轮询：有活时 3 秒，空闲时 12 秒（`:70-74`）。
  `refreshLibrary()` 是**唯一的失效原语** —— 变更接口只返回一个 ack，界面统一重新拉取。
- 路由：`/` 新任务、`/library`、`/review`、`/video/:id`、`/settings`、`/uploader/:name`。
  快捷键 `N`/`L`/`R`，在表单控件内不触发。

### 6.5 三种"引用"机制（前端最容易看错的地方）

| 标记 | 出现在 | 渲染 | 点击行为 |
|---|---|---|---|
| `[03:27]` | 问视频的回答 | `AskPanel.tsx:10-18` 正则替换成 `<a class="cite" data-t="秒">`，再经 marked + DOMPurify | 委托事件读 `dataset.t` → 可选地让本地播放器 seek → `paragraphAt()` 找"最后一个 start ≤ 秒"的段落 → 滚动 + 高亮闪一下（`AskPanel.tsx:120-131`） |
| `【2】` | 问 UP 主 / 回顾 | `lib/cite.ts` 替换成 `<a href="/video/{id}">` | **整页跳转**（不是 SPA 导航），打开那条视频 |
| `?t=秒` | 库搜索命中 | 库列表链接自带参数 | `Video.tsx:96-111` 加载时定位到 `p-{floor(t)}` 并高亮 2.7 秒 |

`[03:27]` 这条链路值得单独强调：它把"模型说的每句话"和"转写里的具体位置"绑在一起，
并且**没有引用时间戳时界面会明确提示"回答可能不在转写里"**（`AskPanel.tsx:88-92`）。
这是 §2.4 说的结构性抗幻觉在 UI 上的落点。

### 6.6 Windows 启动脚本

`scripts/一键启动.vbs` / `停止.vbs` 是 **UTF-16LE with BOM**（文本工具会当二进制，
这是修乱码的结果，git 历史里有对应 commit）。它们解析自身目录后隐藏窗口调用
`start-vsum.ps1` / `stop-vsum.ps1`：

- 启动：检查 7860 是否已响应 → 已响应就直接开浏览器（**不重复起服务**）；
  否则 `uv run vsum ui --no-browser` 隐藏窗口启动，日志写 `logs/ui.log` 和 `logs/ui.err.log`，
  然后每 500ms 轮询最多 30 秒，成功才开浏览器，超时弹 MessageBox 指向 err 日志。
- 停止：`Get-NetTCPConnection -LocalPort 7860 -State Listen` 找占用进程，
  连它的 `uv` 父进程一起 `Stop-Process -Force`。
  **这是非优雅终止，正在跑的任务会死**（好在有缓存和 `pending_jobs` 兜底）。

---

## 7. 存储与记账

### 7.1 SQLite 九张表（`cache.py:30-129`，`SCHEMA_VERSION = 4`）

| 表 | 作用 | 主键 |
|---|---|---|
| `transcripts` | 转写 + 元信息（含 `uploader`/`upload_date`/`thumbnail`） | 指纹 `key` |
| `summaries` | 总结正文 | 指纹 `key` |
| `questions` | 问视频/问 UP 主的问答记录（`citations` 存 JSON） | 自增 |
| `usage` | 记账：真实 token、次数、金额 | 自增 |
| `tags` | 标签（`source` 三态） | `(video_id, tag)` |
| `digests` | 回顾 | `(period, key)` |
| `uploader_groups` | UP 主分组 | `uploader` |
| `pending_jobs` | 排队中的任务（重启续跑） | job id |
| `search_fts` | FTS5 虚表（`search.py:31-41`） | — |

**统一的一致性策略：任何 SQLite 错误都降级成"未命中/无结果"，并 `log.warning`，绝不阻塞主流程。**
`_connect()` 里有一处防抖：打开失败后把 `self.path` 设为 `None`，
"别每次调用都重试同一个坏文件"（`:304`）。

迁移策略很轻：`_MIGRATE_COLUMNS` 声明式地给老库 `ALTER TABLE ADD COLUMN`（`:133-143`），
FTS5 索引靠启动时 `sync_index` 补建。

### 7.2 记账链路

```
provider._complete() → _record_usage(i, o)   # 只在成功时累加，解析不出的值静默丢
      ↓
provider.take_usage() → (input, output, calls)   # 取走并清零
      ↓
web/api.py:_record_usage / _record_usage_raw
      ↓
_money(cfg, i, o) = i*price_in/1e6 + o*price_out/1e6
      ↓
Cache.add_usage(video_id, kind, detail, provider, i, o, calls, cost)
```

`kind` 有六种：`summary` / `qa` / `uploader_qa` / `tags` / `polish` / `review`。
单价留空时 `cost` 为 `None`，界面只显示 token 数（`_money`，`:224-228`）。

`_auto_tag` 那一步会把 `result.summary_provider` 传进去复用**同一个 provider 实例**
（`api.py:821`），所以打标签的用量能被 `_record_usage` 正确读到 ——
如果重新实例化一个 provider，这一步的账就丢了。

界面上的估算和实际记账是两条路：估算用 `TOKENS_PER_SEC = 3.2`（中文口语约 3.2 token/秒）
和 `OUTPUT_TOKENS_PER_CALL = 1200` 这类常量（`api.py:77-79`），
问答/UP 主/回顾的估算则固定假设 600/800 输出 token。实际金额以 `usage` 表为准。

### 7.3 产物目录

```
output/<标题前60字>-<video_id>/
├── audio/<video_id>.wav     16k 单声道，约 2MB/分钟；识别完就没用了
├── subs/                    下载的字幕（workdir 传了就不清理）
├── transcript.json          含 meta.cache_key（用于认领）
├── summary.md               开头有 front matter（供脱离缓存时反解类型）
└── mindmap.html             仅思维导图类型，JS 全内联，离线双击可开
```

思维导图**不是图片而是树**：模型输出嵌套 Markdown 大纲，程序解析成树，
用打包在 `web/static/` 里的 Markmap（d3 + markmap-view，322KB）渲染。
刻意**不用 `markmap-lib`，解析器自己写**（`DESIGN.md` 技术栈表），
换来的是"产出物自包含、离线可用、不需要生图模型"。

---

## 8. 配置系统

三层（`config.py`）：

1. `config.yaml` —— 可提交。`_build()` 会**拒绝未知字段**（`:127-134`），
   写错 key 立刻报错而不是静默用默认值。这是个好决定。
2. `.env` —— 密钥，gitignore。就近查找：配置文件同级目录优先，其次当前目录往上。
3. 环境变量 —— 决定模型权重落哪个盘（`HF_HOME` / `MODELSCOPE_CACHE` / `UV_*`）。

有一处设计注释值得引用（`config.py:78-80`）：
> 默认留空，具体地址由 config.yaml 给。这里要是写死某一家的地址，
> 换 provider 时忘了改就会把请求（连同密钥）发到错误的厂商去。

这是 `base_url` 默认 `None` 而**不是** `https://api.deepseek.com/v1` 的原因，
对应 `DESIGN.md` 第 7 节第 7 条那个"测试 fixture 把真密钥发到别家接口"的事故。

**在线改配置**（设置页用）：`update_summarizer_config` / `update_asr_config` / `update_env_key` /
`update_pricing` 都是**文本级改写** `config.yaml`，用正则保留注释和排版（`:186-211`），
不重新 dump YAML。`_UNSET` 哨兵用来区分"没传"和"传了 None"（`:214`）。

CLI 的 `--provider` 会**一并套用该 provider 的默认 key 变量和接口地址**（`cli.py:50-64`），
否则会"拿着 DeepSeek 的 key 去调 Claude，报错还很莫名其妙"。

---

## 9. 已知技术债与瑕疵（通读发现，按重要性排）

这一节是本文相对 README 的增量最大的部分。以下都是**代码事实**，不是猜测。

### 9.1 结构性

1. **`ask_uploader` 和 `digest` 没有 token 预算，不截断**（`qa.py:186-203`、`digest.py` 的
   `build_prompt`）。视频一多输入线性膨胀，迟早超上下文。`qa.py` 里 `truncated` 硬编码 `False`。
   对比：`ask_video` 有 `_fit_transcript` 做预算和截断提示。
2. **`FallbackASRProvider` 只捕获 `ASRError`**（`asr/fallback.py:56`），
   而分离路径里 `int(s.get("spk", 0))` 在受保护的 `try` **之外**
   （`funasr_provider.py:167-176`）—— 非数字 `spk` 会抛裸 `TypeError` 穿透兜底。
3. **字幕下载路径丢掉了 `cfg`**（`fetcher.py:95` 调 `download(info, opts)` 不传 cfg），
   导致抖音浏览器兜底在字幕路径上是**死代码**（`ytdlp_base.py:190-197` 那个分支进不去）。
4. **字幕临时文件在有 `workdir` 时不清理**（`fetcher.py:48`）：
   只有 `workdir is None` 才用 `TemporaryDirectory`；流水线传的是 `work_dir/"subs"`，
   所以 `output/*/subs/` 会一直留着。
5. **`_from_cache` 里 `speaker_count` 硬编码为 2**（`library.py:240`）——
   只有"有没有"的信息，界面上显示的说话人数在缓存来源下永远是 2。
6. **`pick_language` 的兜底取 `next(iter(available))`**（`ytdlp_base.py:238`），
   依赖 yt-dlp 字典的插入顺序，各站不一致（不是稳定排序）。

### 9.2 前端

7. **`AudioBar` 的全局 keydown effect 没有依赖数组**（`AudioBar.tsx:97-111`），
   每次渲染都注册/注销一次；而且它全局吃掉空格/方向键/M，
   与 `App.tsx` 的 `r`/`l`/`n` 快捷键（只排除表单控件）可能打架。
8. **设置页把 `api_key` 明文 POST 到 `/config/llm`，并同时缓存进 localStorage**
   （`Settings.tsx:113-165,237-292`）。本地单机工具下可以接受，但值得知道。
9. **`tsconfig` 没有开 `strict`**，但有 `noUnusedLocals`/`noUnusedParameters` ——
   所以 `npm run build`（先跑 `tsc -b`）会因未使用的 import 硬失败，
   而类型假设（`Record<string, unknown>`、`as string`）完全不检查。
10. **没有 ESLint**，只有 oxlint；`Video.tsx:99` 那条 `eslint-disable` 是失效的，
    oxlint 也没开 `exhaustive-deps`。
11. **`【n】` 引用是纯 `<a href>`，会整页刷新**（`lib/cite.ts`），不是 SPA 导航。
12. 深色模式硬刷新有一次白闪（`index.html` 没有预渲染脚本）。

### 9.3 当前工作区状态（重要！）

**工作区是脏的，而且正在做 UP 主分组功能：**

```
M  frontend/src/components/Sidebar.tsx
M  frontend/src/index.css
M  frontend/src/pages/Library.tsx
M  frontend/src/pages/Uploader.tsx
D  src/video_summarizer/web/dist/assets/index-7hOx_t08.js
D  src/video_summarizer/web/dist/assets/index-C3s5j8Q6.css
M  src/video_summarizer/web/dist/index.html
?? frontend/src/components/GroupMenu.tsx
?? frontend/src/components/UploaderTree.tsx
?? src/video_summarizer/web/dist/assets/index-CW0fsuB_.js
?? src/video_summarizer/web/dist/assets/index-CBkaan3F.css
```

`dist/index.html` 已经指向新的（未跟踪的）资源，且资源存在，所以**当前服务的界面是自洽的**。
但提交时必须同时包含：两个删除、两个新资源、两个新组件
（`Sidebar`/`Library`/`Uploader` 都 import 了它们，漏掉就构建失败）。

另外仓库里有一个 Claude Code worktree
（`.claude/worktrees/project-startup-optimization-1bb842`，停在 `444d0a3`），
里面有一整套 `.venv` 副本 —— 这是 `glob`/`grep` 会重复命中同一个文件的原因。

### 9.4 未真实验证的部分

- `claude` 和 `ollama` 两个 provider **没做过真实调用**：开发机上没有 Anthropic key、没装 Ollama。
  协议层（参数构造、错误分支、响应解析）用假服务器和假响应对象全测过
  （`tests/test_summarizer_providers.py`），第一次真连时留个心。
- **多人对话的说话人分离没在真实素材上验证**：只在合成双人音频上验证过能分开，
  在真实独白上验证过不会误分。README 建议"拿你自己的播客试一下"。
- 时间轴粒度取决于 VAD（见 §3.4），不是逐词对齐。

---

## 10. 测试策略

```bash
uv run pytest            # 272 个快测试，约 20 秒，不碰网络、不加载模型
uv run pytest -m slow    # 需要 GPU 和已下载的模型
```

`pyproject.toml:65` 里 `addopts = "-q -m 'not slow'"` 是默认排除机制。
测试文件按模块对齐，其中两个最能说明测试哲学：

- `test_summarizer_providers.py`：Ollama 那部分**对着进程内起的假 HTTP 服务器发真实请求**，
  Claude 用**假 message 对象**覆盖响应解析。不装 Ollama、不花 API 钱，把协议层和错误分支全测到。
- `test_web_api.py`（40KB，最大一个）：用 FastAPI `TestClient` 覆盖全部路由。

`conftest.py` 里有针对"读 `.env` 的测试要 monkeypatch 掉 `load_dotenv`"的处理 ——
`DESIGN.md` 第 7 节第 7 条那个密钥泄漏事故留下的防线。

---

## 11. 修改指南（改哪一层要注意什么）

| 想做的事 | 改哪里 | 必须同时做/注意 |
|---|---|---|
| 换总结模型 | `config.yaml` 的 `summarizer` | 换 provider 要一起换 `api_key_env` + `base_url` + `model`，否则报错莫名其妙 |
| 加一种总结类型 | `summarizer/prompts.py` 的 `TEMPLATES` + 可能的 `map_instruction` | 类型名会被写进 `summary_key` 指纹和 summarize 的 URL/DB，属于**破坏性变更**；`api.py:TYPE_HINTS` 和前端 `Seg` 也要加 |
| 加一个 LLM provider | `summarizer/` 新建文件实现 `_complete` + 注册进 `AVAILABLE_PROVIDERS` 和 `_ALIASES` | 别忘 `describe()`（它进缓存指纹）和 `take_usage()`（否则不记账） |
| 换 ASR 模型 | `config.yaml` 的 `asr` | 指纹随之变化，会自动重跑，不需要 `--force` |
| 改前端 | `frontend/src/` | **必须 `npm run build` 并提交 `web/dist/` 的新 hash 资源**，否则 7860 上毫无变化 |
| 加一个 API 端点 | `web/api.py` 的 `create_app` 内 | **不能加 `from __future__ import annotations`**；请求体模型必须定义在函数内部 |
| 加一张表 | `cache.py` 的 `_SCHEMA` + 提升 `SCHEMA_VERSION` | 新列走 `_MIGRATE_COLUMNS` 的 `ALTER TABLE`；接口失败要降级不要抛 |
| 改分段/段落逻辑 | `web/reading.py` | 搜索索引的 `ref` 就是段落号，**改了编号会让已有索引错位**，要提醒 `vsum cache reindex` |
| 改纠错规则 | `correction.py` 的 `validate()` + 提升 `PROMPT_VERSION` | `PROMPT_VERSION` 存在 `meta` 里就是为了识别过期替换表 |
| 加后台任务 | `web/jobs.py` + `_submit_process` 的模式 | 要让 `pending_jobs` 支持续跑就得写那两行；进度可以从日志里用正则抠 |

---

## 12. 总结：这个项目的三个特征

**一、它把"成本"和"可信"当一等公民，而不是附属功能。**
`plan() → CostEstimate → confirm()` 贯穿 CLI 和界面；`usage` 表记的是**真实 token** 而不是估算；
任何要花钱的按钮点之前都看得到金额。可信方面：时间戳可点击回查、
专名替换逐条可否决、缓存按指纹而非路径、README 主动写"模型会编，别当事实来源"。

**二、它把"踩过的坑"全部固化成了代码和注释。**
B 站 412 是 IP 频控（于是复用探测结果、批量间隔、列表页 10 分钟缓存）；
Ollama 默认 4096 上下文静默截断（于是走原生接口传 `num_ctx`）；
Claude 不收 `temperature`（于是专门不发这个字段）；
`max_single_segment_time` 只能构造时传（于是代码里有一行注释专门说这件事）；
Windows ctranslate2 只认 PATH（于是 DLL 目录和 PATH 两个都做）。
`DESIGN.md` 第 7 节那 12 条和代码里的注释是对得上的 —— 这种"文档就是事故报告"的风格，
让这个项目比同等规模的个人项目好读得多。

**三、它的分层是实用的，不是教条的。**
`Transcript` 是唯一的模块契约，四个子系统可以独立替换；
但项目并没有为了"架构干净"而拒绝实用主义 —— 单 worker 串行队列（ASR 独占 GPU，
并发没意义）、同步问答不进队列（不该排在 20 分钟的 ASR 后面）、
前端产物入库（用户不装 Node）、回顾做成按需而非定时（本地服务晚上不一定开着）。
每一条取舍都能在代码或注释里找到**具体的理由**，而不是"最佳实践"。

规模上，约 7,500 行 Python（`web/api.py` 1,522 行占大头）+ 约 4,000 行 TS/TSX
和 755 行手写 CSS，对应 272 个快测试。路线图的 v0.1–v0.8 基本落地，v0.6 的界面重构和 v0.7/v0.8 的
搜索、问视频、批量、标签、回顾、问 UP 主都已经在代码里了。

---

*本文基于当前工作区（HEAD `e149696` + 未提交的 UP 主分组改动）通读生成。
行号引用以该状态为准；改动后可能有偏移。*
