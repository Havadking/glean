# 拾光笺 · Glean

> 拾取视频里的光，攒成一张能读、能搜、能追问的笺。

给一个视频链接（Bilibili / YouTube / 抖音 / 其他 yt-dlp 支持的站点），自动产出**结构化转写**、**大模型总结**和**思维导图**，处理过的视频按 UP 主攒成一个本地库。本地 Windows 单机、一张消费级显卡、一个 DeepSeek key 就能跑；命令行叫 `vsum`。

**能做什么**

- **转写**：有人工字幕就用字幕，没有就本地语音识别（FunASR Fun-ASR-Nano，兜底 faster-whisper），可选说话人分离
- **总结**：总体 / 要点 / 时间线 / 分说话人 / 思维导图五种，长视频自动 map-reduce，花钱前先报预估
- **纠专有名词**：模型只出替换表叠在原文上，逐条可否决；确认过的词按 UP 主攒成词表，下次先套词表再喂给 ASR 当热词
- **问视频 / 问 UP 主**：只依据转写回答，每条结论附可点击的时间戳；跨视频提问一位创作者的观点有没有变化
- **库**：按 UP 主分组、AI 标签、全文搜索（SQLite FTS5，零 LLM 成本）、按周回顾
- **批量**：贴合集或 UP 主空间链接，勾选排队串行跑；任务队列落盘，关页面、重启服务都接着跑
- **成本记账**：每次模型调用按真实 token 记一笔，库页 / 详情页 / 设置页都能看花了多少

设计与取舍见 [DESIGN.md](DESIGN.md)，代码结构见 [docs/ARCHITECTURE-NOTES.md](docs/ARCHITECTURE-NOTES.md)。路线图 v0.1 – v0.8 均已落地，项目现处于**功能完整、暂停开发**的状态，日常在用。

## 快速开始

```bash
git clone https://github.com/Havadking/glean.git && cd glean
uv sync --extra cuda --extra ui --extra funasr --extra cookies
cp .env.example .env              # 然后填入 DEEPSEEK_API_KEY
```

各 extra 的作用：`funasr` 是默认 ASR（要拉 torch，约 3GB）、`cuda` 是 whisper 兜底走 GPU 需要的运行库、`ui` 是 Web 界面、`cookies` 是抖音的浏览器兜底（只装 playwright 的 Python 包，用本机 Chrome/Edge，不下载浏览器内核）。只想先跑起来的话 `uv sync --extra funasr` 就够。

图形界面：

```bash
uv run vsum ui
```

Windows 日常用：双击 [`scripts/一键启动.vbs`](scripts/一键启动.vbs)，服务没起会自动起（隐藏窗口，日志写在 `logs/`），已经在跑就直接开浏览器。配套的 [`scripts/停止.vbs`](scripts/停止.vbs) 用来关掉后台服务。建个桌面快捷方式指向这两个文件最方便。

命令行：

```bash
uv run vsum inspect "https://www.bilibili.com/video/BVxxxxxxx"
uv run vsum run "https://www.bilibili.com/video/BVxxxxxxx"
```

产物落在 `output/<标题>-<视频ID>/` 下：

- `transcript.json` — 结构化转写（时间轴 + 文本，开了说话人分离还带 speaker）
- `summary.md` — 总结，开头附来源、模型、生成时间，便于追溯
- `mindmap.html` — 思维导图类型才有，自包含单文件，双击可开

## 命令

| 命令 | 作用 |
|---|---|
| `vsum ui` | 启动 Web 界面（默认 http://127.0.0.1:7860） |
| `vsum inspect <url>` | 只探测：有没有人工字幕、时长多少。不下载任何东西 |
| `vsum run <url>` | 完整流程：转写 + 总结 |
| `vsum summarize <transcript.json>` | 拿已有转写换个角度重新总结，不重跑 ASR |
| `vsum search <词>` | 全库搜转写和总结正文，不调模型 |
| `vsum cache` | 查看缓存概况；`cache list` 看明细，`cache clear` 清理，`cache refresh-meta` 给老记录补 UP 主信息，`cache reindex` 重建搜索索引 |
| `vsum config` | 打印当前生效的配置和密钥状态 |

### 界面

FastAPI 后端 + React 前端，构建产物随包分发，**用户不需要装 Node**。三个主页面（另有 UP 主页、回顾、设置，见后文）：

- **新任务** — 贴链接先「探测」：封面、UP 主、有没有字幕、是不是处理过、**预计花多少钱多久**，看清楚再点「开始」。之后是四段进度（探测 / 下载 / 识别 / 总结），识别段有进度条，日志折叠。任务在后台队列里跑，关掉页面不影响，重开能续看。
- **视频详情** — 左边是转写阅读视图（碎分句聚成段落，段首时间码，可在本条内搜索高亮），右边是总结。五种总结类型做成分段控件，类型前的小点表示生成过没有；没生成的显示预计花费和「生成」按钮，生成过的直接看。思维导图内嵌渲染，可折叠缩放、下载 SVG。
- **库** — 处理过的视频按 UP 主分组，一行一条：封面、标题、识别方式、已有哪些总结、日期；悬停出「打开目录 / 删除」。顶上四个数：视频数、转写总时长、总结数、UP 主数。

深色模式跟随系统，也可手动切。快捷键 `N` 新任务、`L` 库、`R` 回顾。

### 抖音

底层是 yt-dlp，抖音链接直接贴。分享口令（"8.52 复制打开抖音… https://v.douyin.com/xxx/ …"）整段贴进去也行，程序会把链接抠出来。抖音没有字幕，永远走语音识别。

抖音的详情接口要页面 JS 现算的风控参数（uifid、a_bogus、x-secsdk-web-signature），yt-dlp 只带 cookie 会被拦，报 "Fresh cookies (not necessarily logged in) are needed"——手动导出多新的 cookie 都没用。程序的做法是被拦时无头拉起本机已装的 Chrome / Edge（访客身份，不登录，不碰你日常的浏览器配置），打开一次 douyin.com，在页面上下文里请求详情接口让站点自己把签名补齐，拿到的结果交回 yt-dlp 解析、下载。整个过程 3~5 秒，全自动。需要 `uv sync --extra cookies`；`config.yaml` 里 `download.douyin_browser` 可以指定浏览器或关掉。

拿到的抖音 cookie 会顺手合并写回 `download.cookies_file`（只动抖音域，B 站等其他站点的原样保留）。**cookie 文件绝不能提交进仓库**（`.gitignore` 已经挡了 `*cookie*`）；用扩展手动导出时也请只导出当前站点，别把全部站点的登录态一起导出来。

### 合集 / UP 主空间批量处理

新任务页贴一个 **UP 主空间**（`space.bilibili.com/<uid>/video`）或 **合集 / 播放列表** 链接，出来的是一页视频列表：勾选、排队，串行跑。默认勾上没处理过的；已处理的标出来。UP 主空间支持**在投稿里按关键词搜**——一个 1500 条投稿的搬运号里找「护肤」，一秒出四条。

- 相邻两个视频之间隔 `download.batch_delay_sec`（默认 5 秒）再碰站点
- 排队的任务记在 `cache.sqlite` 里，服务重启会接着跑；「清空排队的」只清还没开始的
- **B 站空间接口限流很紧**：匿名状态下一两分钟内只能请求几次，超了 412。碰到了等一两分钟；配置 `download.cookies_file`（浏览器扩展导出的 Netscape 格式 cookie）会宽松很多

### 问视频

详情页右栏的「问视频」：对这一条转写提问，模型**只依据转写回答，每条结论末尾附时间戳** `[03:27]`。时间戳是可点的，点了左栏滚到那一段并高亮——读者能自己验证，这是对「模型会编」最结构性的防御；没有时间戳可引的结论，就是转写里没有的。问答记录存在 `cache.sqlite` 里，重开还在；最近四轮会作为上下文带上。每问一次是一次 DeepSeek 调用，转写整篇进上下文，面板上标着预计花费。

### 存储

识别用的 wav 是 16kHz 单声道，每分钟约 2MB，一个 22 分钟的视频 40MB。识别完就用不上了。设置页「存储」显示音频缓存占用，一键清理所有 `audio/`——转写和总结不受影响，只有 `--force` 重跑识别时才会重新下载。

### 去口水话（可选）

转写阅读视图右上角有个「去口水话」开关：去掉「呃」「嗯」、「就是就是」「那个那个」这类填充和结巴重复，时间轴不变。规则刻意保守——「嗯嗯」（表示同意）、「看看」「谢谢」「妮妮」这些不碰，单字重复也不动。开关状态记在浏览器里。想让送给模型的转写也清洗，`config.yaml` 里 `summarizer.clean_transcript: true`；落盘和缓存里的转写永远是原文。实测口语视频能省 1–4% 的 token，主要价值是好读。

### 纠专有名词（可选）

中文语音识别的专名错几乎全是同音字：「语数科技」（宇树科技）、「一焕方量化」（幻方量化）、「梁文峰」（梁文锋）。新任务面板勾上「纠专有名词」，或详情页转写栏点「纠专有名词」，模型把全文看一遍，**只输出一张替换表**（不重写全文），程序校验后叠加在原文上——原文和时间轴都不动，替换表存在 `transcript.json` 的 `meta.corrections` 里。转写栏显示「已修正 N 处」，点开能看每一条（原文 → 改后 · 命中几处 · 模型的依据），去掉勾就是否决，即时生效；「看原文」能切回识别原文。

校验比 prompt 可靠：`from` 必须在原文里原样出现、不含数字（数字错交给人）、长度差不超过 2 字、不许带句末标点、不许链式替换，不过的一律丢。否决过的条目重跑也不会再冒出来。

**词表会攒下来。** 每条视频确认过的替换按 UP 主记进 `cache.sqlite` 的 `terms` 表，同一分组的 UP 主共享。下一条视频先按词表确定性地套一遍（表里标「词表」，没花模型），再把正确写法作为热词喂给 ASR、作为已知术语喂给纠错模型，模型只负责发现表里没有的新词，新发现的再回填。否决一条也同步进词表，之后同组的视频不再自动套。纠错关着的时候词表照样套。`GET /api/terms?uploader=` 能看某个 UP 主的词表。搜索索引原文和修正文都进，记得哪个写法搜哪个都能命中；总结和问答用修正后的正文，替换表变了总结缓存自动失效。只对语音识别来源生效，官方字幕不跑；ollama 本地小模型跳过。一小时视频约 ¥0.03、十几秒。默认关：`summarizer.correct_terms: true` 打开，或每次处理时勾选。

### 问 UP 主（跨视频）

侧栏点 UP 主名字，或库里分组标题旁的「问 TA · 跨视频」，进 UP 主页面：左边是这位创作者的所有视频（按发布日期），右边提问——「她关于防晒的观点是什么，前后有没有变化」「推荐过哪些产品」。模型拿的是**每条视频的总结**（优先「总体」，没有就退而求其次，什么总结都没有的只用转写开头），不是整篇转写，所以四条视频问一次约 ¥0.01。每条结论标来自哪条视频（【2】），渲染成带标题的链接，点了打开那条视频；不同视频说法有变化时会按日期指出来。想让回答更全，先给没总结的视频生成「总体」。

### 标签

处理完一条视频，模型顺手从总结里提 3–5 个主题词当标签（`summarizer.auto_tags`，默认开；用总结做输入，一条约 1500 token）。库页面每行下面是标签，顶上一排标签云，点了就是筛选，搜索框里打 `#护肤` 也一样。详情页可以手动加、删，也能让 AI 重打一遍。

防词表漂移是重点：每次生成都把库里已有的标签喂给模型，要求优先复用、只在确实没有合适的时候才新建 —— 不然会得到「护肤 / 护肤品 / 皮肤护理」三个意思一样的标签。你删掉的 AI 标签会被记住，重新生成不会再冒出来；手动加的也不会被覆盖。老库点「给 N 条没标签的补标签」，进任务队列一条一条跑。

### 回顾

侧栏「回顾」：这一周（或这一天）收藏的视频，让模型做一次**跨视频**的综合 —— 按主题分组、指出不同视频互相补充或矛盾的地方、挑一两条值得回头看的。每条结论标来自哪条视频，点了打开。材料是每条视频的总结（同「问 UP 主」），一周十来条约 ¥0.01。

不是定时任务：本地服务晚上不一定开着，也没有推送渠道。做成按需 —— 打开页面时这段时间有视频但还没有回顾，就自动生成一次存进 `cache.sqlite`；之后又收藏了新的，只在侧栏给个小点、页面上提示「有 N 条新的」，点了才更新。默认按周（一天一两条视频没什么可综合的），可切到按天，可翻到以前的周。

### 全文搜索

库页面的搜索框搜的是**转写和总结的正文**，不只是标题。结果按视频分组，每条命中带原文摘录和时间码，点了直接打开视频、滚到那一段并高亮；命中在总结里的跳到对应类型。零 LLM 成本。

索引是 SQLite FTS5，放在 `cache.sqlite` 里，任务跑完自动更新，老库第一次启动自动补建。中文按字切分后建索引、查询时拼成短语，所以「甘油」「蜂花」这种两字词也能搜（FTS5 自带的 trigram 对少于三个字的词无能为力）；拉丁词做前缀匹配，搜 `elephant` 能命中 `elephants`。

`config.yaml` 里的 `price_input_per_m` / `price_output_per_m` 是每百万 token 的单价，填了界面才会把 token 换算成钱。

改前端：

```bash
cd frontend && npm install && npm run dev     # 开发服务器 5173，/api 代理到 7860
npm run build                                  # 产物写到 src/video_summarizer/web/dist/，提交进仓库
```

接口文档在 http://127.0.0.1:7860/api/docs。

### 思维导图

第 5 种总结类型。思维导图不是图片，是树 —— 让模型输出嵌套的 Markdown 大纲（`#` 根、`##` 主分支、缩进 `-` 列表），程序解析成树，用 [Markmap](https://markmap.js.org/) 渲染成可折叠、可缩放的图。**只用 DeepSeek 就够，不需要生图模型。**

```bash
uv run vsum run "视频链接" --summary-type mindmap
```

产出 `mindmap.html`：**自包含单文件，双击就能在浏览器里打开**，不需要联网。渲染库（d3 + markmap-view，共 322KB）打包在仓库里，国内访问 CDN 时好时坏的问题不存在。界面里也能直接看，带「居中」和「下载 SVG」。

实测 22 分钟的护肤视频出 100 多个节点、7 个主分支、4 层，模型对格式守得很好。第四层默认折叠，点节点展开。

`vsum run` 常用参数：

```bash
--summary-type overall|by_speaker|timeline|key_points|mindmap   # 总结类型
--provider claude     # 临时换总结 provider：openai | claude | ollama
--diarize             # 强制开说话人分离（默认 auto，选 by_speaker 时自动开）
--no-diarize          # 强制关
--no-summary          # 只转写，不花钱
--no-cache            # 这次不读也不写缓存
--force-asr           # 有字幕也强制走语音识别
--force               # 忽略缓存全部重跑
--asr-model medium    # 临时换小模型，快一些
-y                    # 跳过成本确认
```

换 ASR 模型、开说话人分离都会改变缓存指纹，不用再加 `--force` —— 会自动重跑。

## 工作方式

1. **字幕优先**：`yt-dlp` 探测，只认人工上传字幕；自动生成/自动翻译字幕默认忽略（准确率不够）。B 站只有弹幕的情况会被识别为"无字幕"。
2. **无字幕才跑 ASR**：下载最佳音轨 → ffmpeg 转 16k 单声道 wav → FunASR（FSMN-VAD 切段 + Fun-ASR-Nano 批量识别，词表里的正确写法作为热词一起喂）。
3. **兜底**：Fun-ASR-Nano 覆盖中英日（SenseVoice-Small 多韩粤）。配置的语言超出范围、或 FunASR 跑失败/结果为空，自动切 faster-whisper（近百种语言）。whisper 这一路 GPU 失败还会再退 CPU。
4. **长文本自动分策略**：转写 token 数在模型上下文预算内就整篇送入，超了自动走 map-reduce（切块局部摘要 → 汇总）。
5. **花钱前先问**：调用大模型前打印预估 token 量和请求次数，确认后才发。
6. **按指纹缓存**：转写和总结都进 SQLite，同样的输入不重算也不重付。

### 缓存

指纹包含视频 id、走字幕还是 ASR、ASR 的 provider / 模型 / 语言 / 是否分离。**换了其中任何一项就是另一个指纹**，不会拿到设置不符的旧结果；反过来视频改了标题、输出目录换了名字，指纹不变，照样命中。总结的指纹再叠上模型、总结类型、输出语言和附加要求。

```bash
uv run vsum cache          # 概况
uv run vsum cache list     # 明细
uv run vsum cache clear    # 清理（产物文件不受影响，只是下次要重算）
```

三个开关的区别：

| | 读缓存 | 重下音频 | 重跑 ASR |
|---|---|---|---|
| 默认 | 是 | 否 | 否（命中时） |
| `--no-cache` | 否 | 否 | 是 |
| `--force` | 否 | 是 | 是 |

缓存是索引层，不是产物本身 —— `transcript.json` 和 `summary.md` 照旧写到输出目录。删掉 `cache.sqlite` 只会让下次重算；如果输出目录里的产物指纹对得上，还会被直接认领，不用重跑。

### 说话人分离

选「分说话人摘要」时会自动开启（`config.yaml` 里 `asr.diarize: auto`），也可以用 `--diarize` 强制开、`--no-diarize` 强制关。

开启后换成 FunASR 的整合 pipeline：`paraformer-zh + FSMN-VAD + ct-punc + CAM++`，一次输出句级文本 + 起止时间 + 说话人编号。句子很碎（22 分钟能有 600 多句），同一个人连续说的会合并成"一轮发言"再交给大模型。

不默认全开的原因：这条 pipeline 只做中文，而且比 SenseVoice 慢一半。

### 实测速度（RTX 4070，22 分钟中文视频）

| ASR | 耗时 | 实时率 | 说话人 |
|---|---|---|---|
| FunASR Fun-ASR-Nano（默认） | 约 80 秒 | ~16x | 无 |
| FunASR SenseVoice-Small | 11 秒 | ~118x | 无 |
| FunASR paraformer-zh + CAM++ | 38 秒 | ~35x | 有 |
| faster-whisper large-v3 | 约 9 分钟 | ~2.5x | 无 |

Fun-ASR-Nano 是 SenseVoice 编码器接一个 Qwen3-0.6B 解码器（共 0.8B），有语言模型撑着，「英派/鹰派」「一息/议息」这类同音专名错基本不犯（同一段 5 分钟财经视频：SenseVoice 错 6 个专名 × 十几处，Nano 一处不错），还吃热词。代价是比 SenseVoice 慢 6 倍、多占 2GB 显存，权重 2.1GB 首次从 ModelScope 下载。追求速度或者内容是韩语/粤语，`asr.model` 改回 `sensevoice-small`。

## 配置

- `config.yaml` — provider 选择、模型、prompt 类型、上下文预算。可提交。
- `.env` — 密钥。已在 `.gitignore` 里，不提交。

### 换总结模型

默认走 DeepSeek。三种 provider，改 `config.yaml` 的 `summarizer` 段即可，主流程不动：

| provider | 用途 | 要点 |
|---|---|---|
| `openai` | 所有 OpenAI 兼容接口（DeepSeek / 通义 / Kimi / 智谱…） | 只换 `base_url` + `model` + `api_key_env` |
| `claude` | Anthropic 原生接口 | 上下文 1M，长转写基本不用切块 |
| `ollama` | 本地模型，零成本不出网 | 上下文小，长转写一定走 map-reduce |

`config.yaml` 里有三种写法的完整示例。临时试一下不用改文件：

```bash
uv run vsum summarize output/xxx/transcript.json --provider claude
```

`--provider` 会一并套用该 provider 的默认 key 变量和接口地址，不会拿着 DeepSeek 的 key 去调 Claude。

**两个各自的坑**（代码里都处理了，但值得知道）：

- **Claude 不接受 `temperature`**：当前一代已移除采样参数，传了直接 400。配置里的 `temperature` 只对 `openai` 和 `ollama` 生效；Claude 那边用 `effort` 控制成本。另外 Claude 默认开着思考，**思考的 token 也算进 `max_output_tokens`**，设小了正文会被截断，建议 16000 以上。
- **Ollama 默认上下文只有 4096，超出部分被静默丢弃** —— 不报错不警告，只是总结莫名其妙漏掉后半段。所以走的是原生 `/api/chat` 而不是它的 OpenAI 兼容端点：只有原生接口能传 `num_ctx`。程序会按 `max_context_tokens` 显式传下去，并在输入吃满时警告。注意 `num_ctx` 很吃显存，12GB 上跑 8B 模型 32K 差不多是上限。

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
- **`claude` 和 `ollama` 两个 provider 没做过真实调用验证**：开发机上没有 Anthropic key，也没装 Ollama。协议层（参数构造、错误分支、响应解析）用假服务器和假响应对象全测过了，但第一次真连的时候还是留个心。`openai` 那条是真实跑通的。

## 路线图

见 [DESIGN.md 第 8 节](DESIGN.md)。v0.1 – v0.5（命令行 → ASR → 分离 → provider 抽象 → 缓存）、v0.6 界面重构（FastAPI + React + 任务队列）、v0.7 库管理（全文搜索、问视频、批量）、v0.8 打磨（成本记账、去口水话、问 UP 主、纠专有名词、标签、回顾）全部落地。没做的三件事——Claude / Ollama 的真实验证、多人对话分离实测、英文 ASR——都是因为没有对应的使用需求，不是技术障碍。

## 测试

```bash
uv run pytest
```

不碰网络、不加载模型，二十秒跑完。Ollama 那部分对着进程内起的假服务器发真实 HTTP，Claude 的响应解析用假 message 对象覆盖；需要真实模型的验证标了 `slow`，默认不跑。

```bash
uv run pytest -m slow      # 需要 GPU 和已下载的模型
```
