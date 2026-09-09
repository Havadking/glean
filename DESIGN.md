# 视频转写总结工具 — 设计文档

## 1. 目标

给定一个视频链接(YouTube / Bilibili / 其他 yt-dlp 支持的站点),自动完成:

1. 优先获取官方/人工字幕;没有的话下载音频做本地语音识别(ASR)
2. 识别结果标注说话人(多人对话/播客场景)
3. 用可自由切换的大模型对转写文本做结构化总结
4. 输出:结构化转写 JSON + 总结 Markdown/文本

运行环境:本地 Windows,GPU 为 RTX 4070 Super(12GB 显存)。

---

## 2. 整体架构

```
URL
 │
 ▼
[1] 字幕探测层 (yt-dlp --list-subs)
 │
 ├─ 有人工字幕 ──────────────► 下载字幕(vtt/srt)→ 清洗成纯文本+时间轴 ──┐
 │                                                                    │
 └─ 无字幕 / 只有弹幕                                                  │
      │                                                               │
      ▼                                                               │
    [2] 音频提取 (yt-dlp -x --audio-format wav)                        │
      │                                                               │
      ▼                                                               │
    [3] ASR + 说话人分离 (FunASR / whisper 兜底)                       │
      │                                                               │
      ▼                                                               │
    结构化转写 (带 speaker 标签 + 时间轴) ──────────────────────────────┘
                                                                       │
                                                                       ▼
                                                          [4] LLM 总结层(可切换 provider)
                                                                       │
                                                                       ▼
                                                          输出:transcript.json + summary.md
```

四个模块彼此解耦,通过标准化的中间数据结构(见第 5 节)传递,任何一个模块的实现都可以单独替换。

---

## 3. 模块设计

### 3.1 模块一:字幕获取

- 用 `yt-dlp` 做统一的站点适配层(支持 YouTube、Bilibili 等上千站点,不用自己写爬虫)
- 流程:
  1. `yt-dlp --list-subs <url>` 探测
  2. 只认 **"Available subtitles"**(人工上传)部分,忽略 "Available automatic captions" 里的自动翻译字幕(准确率差)
  3. 命中则 `--write-sub --skip-download` 下载 vtt,解析成纯文本 + 时间轴(去掉 WEBVTT 头、时间戳、`&nbsp;` 等 HTML 实体)
  4. Bilibili 一类站点常见情况:只有 `danmaku`(弹幕)没有正式字幕 → 判定为"无字幕",走模块二

### 3.2 模块二:音频提取

- `yt-dlp -f "ba" -x --audio-format wav`(用 wav 无损格式给 ASR,避免二次有损压缩)
- 部分站点(如 Bilibili)对匿名请求有风控,需要带浏览器 UA + Referer 重试

### 3.3 模块三:ASR + 说话人分离

**Provider 选型结论**(详见调研):

| Provider | 定位 | 何时用 |
|---|---|---|
| **FunASR(SenseVoice-Small / Fun-ASR-Nano)** | 默认首选 | 中文/日韩/英文为主的内容,追求速度和中文准确率 |
| **faster-whisper(large-v3)** | 兜底 | FunASR 不支持的小语种(whisper 覆盖近 100 种语言) |
| **NVIDIA Parakeet-TDT v2/v3** | 可选加速 | 纯英文/欧洲语言内容,追求极致速度和最优 WER |

- 说话人分离用 FunASR 官方 pipeline:FSMN-VAD + CAM++ 说话人嵌入 + 标点恢复模型,一步输出带 speaker 标签的分句结果,不用像 whisper 那样额外接 pyannote 再手动按时间轴对齐
- 4070 Super 12GB 显存对上述所有模型都绰绰有余(FunASR 系列模型本身只有几百 M 到 800M 参数)

### 3.4 模块四:LLM 总结层(可切换大模型)

**抽象接口设计**:

```python
class BaseSummarizer(ABC):
    @abstractmethod
    def summarize(self, transcript: Transcript, options: SummaryOptions) -> str:
        ...

class ClaudeSummarizer(BaseSummarizer): ...
class OpenAISummarizer(BaseSummarizer): ...   # 也兼容国内厂商的 OpenAI 兼容接口
class OllamaSummarizer(BaseSummarizer): ...   # 本地模型,零成本
```

- 运行时根据配置文件(见第 6 节)动态实例化对应 provider,新增模型只需新增一个实现类,不改主流程
- **长文本处理策略**:
  - 若模型 context 窗口足够大(如 Claude 系列 200K),整篇转写直接塞入,总结更连贯
  - 否则走 map-reduce:按篇幅切块 → 局部摘要 → 汇总成最终摘要
  - 该策略应做成可配置项,根据所选模型的 context 大小自动判断,不强制用户手动选
- **Prompt 模板**(可配置,预置几种常用总结类型):
  - 整体摘要
  - 分说话人摘要(多人对话/播客场景)
  - 时间轴大纲(按话题分章节,类似 YouTube chapter)
  - 关键信息提取(数字、结论、行动项)

---

## 4. 技术栈

| 层 | 选型 |
|---|---|
| 视频/字幕抓取 | yt-dlp |
| ASR | FunASR(SenseVoice-Small / Fun-ASR-Nano)+ faster-whisper 兜底 |
| 说话人分离 | FunASR 内置 pipeline(FSMN-VAD + CAM++） |
| LLM 总结 | 自建 provider 抽象层(Anthropic / OpenAI 兼容 / Ollama) |
| 语言 | Python 3.10+ |
| CLI | click |
| 界面(后期) | Gradio(跑通 CLI 流程后再加) |
| 本地缓存 | SQLite(避免同一视频重复跑 ASR,这一步最耗时) |
| 配置 | `.env`(密钥,不入库)+ `config.yaml`(provider 选择、prompt 模板) |

---

## 5. 数据结构

### 5.1 中间数据:结构化转写(`Transcript`)

```json
{
  "source_url": "https://...",
  "source_type": "subtitle | asr",
  "language": "zh",
  "duration_sec": 1234.5,
  "segments": [
    {
      "start": 12.4,
      "end": 18.1,
      "speaker": "Speaker_1",
      "text": "..."
    }
  ]
}
```

- `source_type=subtitle` 时 `speaker` 字段一般为空(官方字幕通常不区分说话人)
- `source_type=asr` 时由说话人分离模块填充

### 5.2 输出:总结结果

- `transcript.json`:上述结构化转写,持久化存档
- `summary.md`:LLM 生成的总结,包含所用的 provider/model 名称、生成时间,便于追溯

---

## 6. 配置文件设计(`config.yaml` 示例)

```yaml
asr:
  provider: funasr        # funasr | whisper | parakeet
  model: sensevoice-small
  fallback: whisper        # 主 provider 不支持该语言时的兜底

summarizer:
  provider: claude          # claude | openai | ollama
  model: claude-sonnet-5
  max_context_tokens: 180000
  chunk_strategy: auto       # auto | always | never

output_dir: ./output
cache_db: ./cache.sqlite
```

密钥单独放 `.env`,不提交仓库:

```
ANTHROPIC_API_KEY=...
OPENAI_API_KEY=...
```

---

## 7. 需要注意的坑

1. **pyannote/CAM++ 相关模型**部分是 gated model,需要先在 Hugging Face / ModelScope 申请访问权限
2. **说话人分离对背景音乐、多人抢话场景**效果会打折扣,要做好预期管理,不追求 100% 准确
3. **API key 管理**:多 provider 意味着多套密钥,统一放 `.env`,加入 `.gitignore`
4. **成本控制**:云端 LLM 总结按量计费,长视频建议先估算 token 数并展示给用户确认,再执行
5. **版权/合规**:批量下载他人视频内容做转写总结,定位为个人学习工具,不做成公开分发/商业化服务

---

## 8. MVP 路线图

- **v0.1**:URL → yt-dlp 字幕/音频提取 → faster-whisper 转写(不分说话人)→ 单一 LLM(Claude)总结,跑通 CLI 全流程
- **v0.2**:接入 FunASR 作为默认 ASR provider,whisper 降级为兜底
- **v0.3**:加入说话人分离,支持多人对话/播客场景的分角色总结
- **v0.4**:LLM provider 抽象层完善(支持 OpenAI 兼容接口、Ollama 本地模型切换)
- **v0.5**:接入 Gradio 界面,支持非命令行操作;加入 SQLite 缓存避免重复计算

---

## 9. 目录结构建议

```
video-summarizer/
├── DESIGN.md
├── config.yaml
├── .env.example
├── src/
│   ├── subtitle/          # 模块一:字幕获取
│   ├── audio/              # 模块二:音频提取
│   ├── asr/                 # 模块三:ASR + 说话人分离(provider 抽象)
│   │   ├── base.py
│   │   ├── funasr_provider.py
│   │   └── whisper_provider.py
│   ├── summarizer/          # 模块四:LLM 总结(provider 抽象)
│   │   ├── base.py
│   │   ├── claude_provider.py
│   │   ├── openai_provider.py
│   │   └── ollama_provider.py
│   ├── pipeline.py           # 串联四个模块
│   └── cli.py                 # click 命令行入口
├── output/                    # 转写+总结产物
└── cache.sqlite
```
