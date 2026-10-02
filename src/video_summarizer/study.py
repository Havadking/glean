"""伴读：和 AI 一起看专业长视频（DESIGN.md v0.9）。

两件事：

1. **学习底稿**：看之前生成一次，之后免费复用。分两遍调用，来源在结构上就分开了：
   - 第一遍只看转写（沿用"不许用转写外知识"的约束）：章节与论证线、术语在视频里怎么说，全带时间戳。
     转写超预算时按块切，每块各出各的章节，时间戳天然保留
   - 第二遍不给转写，只给章节标题和术语表，**允许用模型自己的知识**：前置知识、术语的背景解释。
     这一遍的产出在界面上永远标「背景」
2. **定位提问**：用户在某个时间点停下来问。消息按「固定前缀 + 可变尾部」排：
   system + 全片转写 + 底稿大纲 对同一条视频每次都一样，吃服务端前缀缓存（DeepSeek 命中价约一折）；
   当前位置附近的原文、问题放最后。回答固定分「视频里说 / 背景补充 / 有出入」三节。
   能联网时给模型一个 web_search 工具，它觉得需要时自己调。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .errors import SummarizerError
from .models import Segment, Transcript
from .qa import Turn, extract_citations
from .summarizer.base import BaseSummarizer
from .summarizer.tokens import chunk_segments, estimate_tokens, format_timestamp, render_segments
from .websearch import SearchResult, WebSearcher, WebSearchError

log = logging.getLogger(__name__)

# 底稿结构变了就加一，老底稿会被标成"可以重新生成"
SHEET_VERSION = 1

# 第一遍每块转写的上限。实测 3 小时视频（51k token）整篇一次过，输出的 JSON 写到一万七千字
# 撞上 8k 输出上限被截断。按 15k（约 50 分钟口语）切，每块的输出稳稳在上限以内
_GROUNDED_CHUNK_TOKENS = 15000
# 解析失败时把块对半切了重来，最多切几层
_MAX_SPLIT_DEPTH = 2
# 第二遍最多带多少个术语（按出现次数挑），免得背景表本身又写爆
_BACKGROUND_MAX_TERMS = 60

# 估钱用：每次调用大致输出多少
_GROUNDED_OUTPUT_TOKENS = 3000
_BACKGROUND_OUTPUT_TOKENS = 2500
_PROMPT_OVERHEAD = 1500

# ---------- 底稿：第一遍（只看转写） ----------

GROUNDED_SYSTEM = (
    "你是一个帮人精读专业视频的助手。你会拿到一段带时间戳的视频转写，要整理出章节论证线和术语表。\n\n"
    "**只依据转写内容。** 即使你熟悉这个话题，也不要写入转写里没有的信息；这一步的产出会被标成"
    "「视频里说的」，混进外部知识会误导读者。转写来自语音识别，可能有错别字，结合上下文理解。\n"
    "只输出一个 JSON 对象，不要任何解释、不要代码块标记。"
)

GROUNDED_INSTRUCTION = """\
请输出如下结构的 JSON：
{
  "chapters": [
    {
      "start": "mm:ss",            // 本章开始的时间戳，必须是转写里出现过的
      "title": "章节标题，12 字以内",
      "claim": "本章的核心论点，一句话",
      "evidence": [{"text": "支撑论点的一条证据或推理步骤", "t": "mm:ss"}],
      "conclusion": "本章得出的结论或判断，一句话"
    }
  ],
  "terms": [
    {
      "term": "术语原文，和转写里的写法一致",
      "en": "英文全称或缩写，没有就留空",
      "video_says": "视频里是怎么解释或使用这个词的，一两句，只依据转写",
      "t": "mm:ss"                 // 视频里解释或第一次重点使用它的位置
    }
  ]
}

要求：
- 章节按话题切，不按时长平均切；大约每 8–25 分钟一章。每章 evidence 2–4 条，每条都要有时间戳
- 论点、证据、结论要体现「怎么推出来的」，不要只是复述内容
- terms 收录看懂这段视频必须懂的专业术语、缩写、机构名、交易/商业模式名，5–15 个；人人都懂的词不要
- 每条文字都写短：claim / conclusion 一句话，evidence 每条不超过 40 字，video_says 不超过 60 字
- 时间戳格式 mm:ss，超过一小时写 h:mm:ss
"""

# ---------- 底稿：第二遍（背景知识） ----------

BACKGROUND_SYSTEM = (
    "你是一位耐心的行业老师。用户准备看一期专业视频，你会拿到视频的章节和术语表（没有转写原文）。\n"
    "这一步**可以并且应该用你自己的知识**：给看视频的人补上视频默认观众已经懂的背景。\n"
    "要准确、克制：不确定的不写；不要写具体的最新数字、日期、排名（你的知识可能过时）。\n"
    "只输出一个 JSON 对象，不要任何解释、不要代码块标记。"
)

BACKGROUND_INSTRUCTION = """\
请输出如下结构的 JSON：
{
  "prerequisites": [
    {"term": "概念名", "explain": "两三句话讲清楚它是什么、为什么和这期视频有关", "chapter": 2}
  ],
  "backgrounds": {
    "术语原文": "这个术语的背景解释，两三句，讲清楚它在行业里的一般含义"
  }
}

要求：
- prerequisites 是看这期视频前最好先懂的 5–10 个概念，按理解的先后顺序排；chapter 是最先用到它的章节序号
- prerequisites 可以和术语表重叠，但更基础；不要列人人都懂的常识
- backgrounds 给术语表里的每个词各写一条，键必须和术语表里的写法完全一致
"""

# ---------- 提问 ----------

QA_SYSTEM = (
    "你是陪用户看一期专业视频的学习伙伴。用户正在看视频，会在某个时间点停下来提问。"
    "你能看到全片转写（带时间戳）和学习底稿。目标是让用户真正看懂，而不只是复述。\n\n"
    "回答用 Markdown，按需使用下面三个三级标题，顺序固定，用不到的整节省略：\n"
    "### 视频里说\n"
    "只写转写里有的内容，每条结论末尾标时间戳，如 [37:05]。视频没讲到的就直说「视频里没讲」。\n"
    "### 背景补充\n"
    "视频之外的知识：概念解释、行业背景、举例、为什么。来自检索结果的句子末尾标来源编号，如 〔2〕；"
    "没标编号的就是你自己的知识，所以不确定的不要说得很笃定。\n"
    "### 有出入\n"
    "只在视频的说法和背景知识或检索结果矛盾时写：说清楚矛盾在哪、可能的原因。不要悄悄用一方覆盖另一方。\n\n"
    "规则：\n"
    "1. 用户说「这里」「他说的」「刚才」，指的是当前位置附近的内容，优先结合那段原文回答。\n"
    "2. 涉及具体数字、日期、公司近况、最新进展，而你又能检索时，先检索再回答。\n"
    "3. 简洁直接，问什么答什么；能用列表就用列表。不要复述规则，不要开场白。\n"
    "4. 转写来自语音识别，可能有错别字，结合上下文理解。"
)

QA_ACK = "已读完全片转写和学习底稿，等你提问。"

SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "联网检索，用来核实视频里的说法、补充背景、查最新数据。查询词要具体，中英文都可以。",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "检索词"}},
            "required": ["query"],
        },
    },
}

# 每次提问里，留给可变尾部（附近原文、历史、检索结果）和输出的 token
_QA_TAIL_RESERVE = 14000
# 当前位置附近原文的窗口：往前 5 分钟、往后 1 分钟
_WINDOW_BEFORE = 300.0
_WINDOW_AFTER = 60.0
MAX_HISTORY_TURNS = 4

# ---------- 结构 ----------


@dataclass
class Evidence:
    text: str
    t: float | None


@dataclass
class Chapter:
    start: float
    end: float
    title: str
    claim: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    conclusion: str = ""


@dataclass
class Term:
    term: str
    en: str = ""
    video_says: str = ""
    t: float | None = None
    background: str = ""
    mentions: int = 0
    positions: list[float] = field(default_factory=list)


@dataclass
class Prerequisite:
    term: str
    explain: str
    chapter: int | None = None
    known: bool = False


@dataclass
class StudySheet:
    chapters: list[Chapter] = field(default_factory=list)
    glossary: list[Term] = field(default_factory=list)
    prerequisites: list[Prerequisite] = field(default_factory=list)
    version: int = SHEET_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "StudySheet":
        return cls(
            chapters=[Chapter(**{**c, "evidence": [Evidence(**e) for e in c.get("evidence", [])]})
                      for c in raw.get("chapters", [])],
            glossary=[Term(**g) for g in raw.get("glossary", [])],
            prerequisites=[Prerequisite(**p) for p in raw.get("prerequisites", [])],
            version=int(raw.get("version") or 0),
        )

    def chapter_at(self, sec: float) -> int | None:
        """sec 落在第几章（从 0 数）。没有章节返回 None。"""
        idx = None
        for i, c in enumerate(self.chapters):
            if sec >= c.start:
                idx = i
        return idx if idx is not None else (0 if self.chapters else None)


# ---------- 工具函数 ----------

_TS_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*$")


def parse_ts(value: Any) -> float | None:
    """"37:05" / "1:02:03" / 2225 → 秒。认不出返回 None。"""
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None
    m = _TS_RE.match(str(value or ""))
    if not m:
        return None
    a, b, c = m.groups()
    if c is not None:
        return int(a) * 3600 + int(b) * 60 + int(c)
    return int(a) * 60 + int(b)


def parse_json_object(text: str) -> dict[str, Any]:
    """模型输出里抠出第一个 JSON 对象。容忍 ```json 代码块和前后废话。"""
    s = (text or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        raise SummarizerError("模型没有按要求输出 JSON")
    try:
        obj = json.loads(s[start:end + 1])
    except ValueError as exc:
        raise SummarizerError(f"模型输出的 JSON 解析不了：{exc}") from exc
    if not isinstance(obj, dict):
        raise SummarizerError("模型输出的 JSON 不是对象")
    return obj


def _clean(text: Any, limit: int = 400) -> str:
    return " ".join(str(text or "").split())[:limit]


def _clamp(sec: float | None, duration: float) -> float | None:
    if sec is None:
        return None
    if duration and sec > duration + 5:
        return None
    return max(0.0, sec)


def _header(transcript: Transcript, uploader: str | None) -> str:
    lines = [f"视频标题：{transcript.title or '（未知）'}"]
    if uploader:
        lines.append(f"作者：{uploader}")
    lines.append(f"时长：{format_timestamp(transcript.duration_sec)}")
    return "\n".join(lines)


def input_key(transcript_key: str, corrections: str, provider_desc: str) -> str:
    raw = json.dumps({"t": transcript_key, "c": corrections, "p": provider_desc, "v": SHEET_VERSION},
                     sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


# ---------- 底稿：计划与生成 ----------


def _grounded_chunks(provider: BaseSummarizer, transcript: Transcript) -> list[list[Segment]]:
    budget = (provider.cfg.max_context_tokens - provider.cfg.max_output_tokens - _PROMPT_OVERHEAD)
    size = min(_GROUNDED_CHUNK_TOKENS, budget)
    body = render_segments(transcript.segments, with_time=True)
    if estimate_tokens(body) <= size:
        return [transcript.segments]
    return chunk_segments(transcript.segments, size, with_time=True)


def plan_sheet(provider: BaseSummarizer, transcript: Transcript) -> dict[str, Any]:
    """生成底稿大概要多少 token、几次调用。不发请求。"""
    body_tokens = estimate_tokens(render_segments(transcript.segments, with_time=True))
    chunks = len(_grounded_chunks(provider, transcript)) if transcript.segments else 0
    calls = chunks + 1
    return {
        "transcript_tokens": body_tokens,
        "chunks": chunks,
        "calls": calls,
        "input_tokens": body_tokens + chunks * _PROMPT_OVERHEAD + 3000,
        "output_tokens": chunks * _GROUNDED_OUTPUT_TOKENS + _BACKGROUND_OUTPUT_TOKENS,
    }


def _grounded_pass(provider: BaseSummarizer, header: str, chunk: list[Segment], label: str | None) -> dict[str, Any]:
    span = f"[{format_timestamp(chunk[0].start)}]–[{format_timestamp(chunk[-1].end)}]"
    part = (f"（这是全片的{label}，时间范围 {span}。只整理这一部分；"
            "章节可能从上一部分延续过来，照样从这部分的开头起一章。）\n" if label else "")
    # 告诉模型切几章：不给的话长视频每块都切得很碎（3 小时实测切出 43 章）
    minutes = max(1.0, (chunk[-1].end - chunk[0].start) / 60)
    part += f"（这一段约 {minutes:.0f} 分钟，切成 {max(1, round(minutes / 12))} 章左右。）\n"
    user = "\n".join([
        header, "", part + GROUNDED_INSTRUCTION, "",
        "=== 转写开始 ===", render_segments(chunk, with_time=True), "=== 转写结束 ===",
    ])
    return parse_json_object(provider.complete_json(GROUNDED_SYSTEM, user))


def _grounded_with_split(provider: BaseSummarizer, header: str, chunk: list[Segment], label: str | None,
                         depth: int = 0) -> list[dict[str, Any]]:
    """一块解析失败（多半是输出被截断）就对半切开各自重来，而不是整份底稿失败。"""
    try:
        return [_grounded_pass(provider, header, chunk, label)]
    except SummarizerError as exc:
        if depth >= _MAX_SPLIT_DEPTH or len(chunk) < 4:
            raise SummarizerError(f"整理 {format_timestamp(chunk[0].start)} 起的这段转写失败：{exc}") from exc
        log.warning("这块输出解析失败，对半切开重试：%s", exc)
        mid = len(chunk) // 2
        base = label or "一部分"
        return (_grounded_with_split(provider, header, chunk[:mid], f"{base}（前半）", depth + 1)
                + _grounded_with_split(provider, header, chunk[mid:], f"{base}（后半）", depth + 1))


def _merge_grounded(parts: list[dict[str, Any]], duration: float) -> tuple[list[Chapter], list[Term]]:
    chapters: list[Chapter] = []
    for part in parts:
        for c in part.get("chapters") or []:
            if not isinstance(c, dict):
                continue
            start = _clamp(parse_ts(c.get("start")), duration)
            title = _clean(c.get("title"), 40)
            if start is None or not title:
                continue
            evidence = []
            for e in c.get("evidence") or []:
                if isinstance(e, dict) and _clean(e.get("text")):
                    evidence.append(Evidence(_clean(e.get("text")), _clamp(parse_ts(e.get("t")), duration)))
            chapters.append(Chapter(start=start, end=start, title=title, claim=_clean(c.get("claim")),
                                    evidence=evidence[:5], conclusion=_clean(c.get("conclusion"))))
    chapters.sort(key=lambda c: c.start)
    # 同一时间点重复起章（块边界上常见）只留第一个
    deduped: list[Chapter] = []
    for c in chapters:
        if deduped and abs(c.start - deduped[-1].start) < 30:
            continue
        deduped.append(c)
    if deduped:
        deduped[0].start = 0.0
    for i, c in enumerate(deduped):
        c.end = deduped[i + 1].start if i + 1 < len(deduped) else duration

    terms: dict[str, Term] = {}
    for part in parts:
        for t in part.get("terms") or []:
            if not isinstance(t, dict):
                continue
            name = _clean(t.get("term"), 40)
            if not name:
                continue
            key = name.lower()
            if key in terms:
                if not terms[key].video_says:
                    terms[key].video_says = _clean(t.get("video_says"))
                continue
            terms[key] = Term(term=name, en=_clean(t.get("en"), 80), video_says=_clean(t.get("video_says")),
                              t=_clamp(parse_ts(t.get("t")), duration))
    return deduped, list(terms.values())


def _locate_mentions(terms: list[Term], segments: list[Segment]) -> None:
    """术语在转写里出现了几次、在哪。本地算，不花钱。"""
    lowered = [(s.start, s.text.lower()) for s in segments]
    for term in terms:
        needle = term.term.lower()
        count, positions = 0, []
        for start, text in lowered:
            n = text.count(needle)
            if n:
                count += n
                if len(positions) < 30:
                    positions.append(start)
        term.mentions = count
        term.positions = positions
        if term.t is None and positions:
            term.t = positions[0]


_PAREN_RE = re.compile(r"[（(][^）)]*[）)]")


def _term_key(name: Any) -> str:
    """背景表的键和术语对上用。模型常把英文全称一起抄进键里：「PCB（Printed Circuit Board）」。"""
    return re.sub(r"\s+", "", _PAREN_RE.sub("", str(name or ""))).strip("「」\"'").lower()


def _background_pass(provider: BaseSummarizer, header: str, chapters: list[Chapter],
                     terms: list[Term]) -> dict[str, Any]:
    lines = [header, "", "章节："]
    for i, c in enumerate(chapters, start=1):
        lines.append(f"{i}. {c.title}——{c.claim}")
    lines += ["", "术语表："]
    for t in sorted(terms, key=lambda x: -x.mentions)[:_BACKGROUND_MAX_TERMS]:
        lines.append(f"- {t.term}" + (f"（{t.en}）" if t.en else "") + (f"：{t.video_says}" if t.video_says else ""))
    lines += ["", BACKGROUND_INSTRUCTION]
    return parse_json_object(provider.complete_json(BACKGROUND_SYSTEM, "\n".join(lines)))


def generate_sheet(provider: BaseSummarizer, transcript: Transcript, *, uploader: str | None = None,
                   progress=None) -> StudySheet:
    """两遍调用生成底稿。progress(msg) 可选，给任务队列报进度。"""
    if not transcript.segments:
        raise SummarizerError("转写是空的，没法生成学习底稿")
    header = _header(transcript, uploader)
    chunks = _grounded_chunks(provider, transcript)
    parts: list[dict[str, Any]] = []
    for i, chunk in enumerate(chunks, start=1):
        if progress:
            progress(f"整理章节和术语（{i}/{len(chunks)}）")
        label = f"第 {i}/{len(chunks)} 部分" if len(chunks) > 1 else None
        parts.extend(_grounded_with_split(provider, header, chunk, label))
    chapters, terms = _merge_grounded(parts, transcript.duration_sec)
    if not chapters:
        raise SummarizerError("模型没整理出章节，换个模型或者重试一次")
    _locate_mentions(terms, transcript.segments)
    # 转写里一次都没出现、模型也没给位置的词，多半是模型自己发挥的，不要
    terms = [t for t in terms if t.mentions or t.t is not None]

    if progress:
        progress("补背景知识")
    # 背景是锦上添花：这一遍坏了，章节和术语照样能用，不要整份作废
    try:
        bg = _background_pass(provider, header, chapters, terms)
    except SummarizerError as exc:
        log.warning("补背景知识失败，底稿先不带背景：%s", exc)
        bg = {}
    backgrounds = bg.get("backgrounds") if isinstance(bg.get("backgrounds"), dict) else {}
    by_key = {_term_key(k): v for k, v in backgrounds.items()}
    for t in terms:
        t.background = _clean(by_key.get(_term_key(t.term)), 500)
    prereqs = []
    for p in bg.get("prerequisites") or []:
        if not isinstance(p, dict) or not _clean(p.get("term")):
            continue
        ch = p.get("chapter")
        ch = int(ch) if isinstance(ch, (int, float)) and 1 <= int(ch) <= len(chapters) else None
        prereqs.append(Prerequisite(term=_clean(p.get("term"), 40), explain=_clean(p.get("explain"), 500), chapter=ch))
    return StudySheet(chapters=chapters, glossary=terms, prerequisites=prereqs[:12])


# ---------- 提问 ----------


@dataclass
class StudyAnswer:
    question: str
    answer: str
    position: float
    chapter: int | None
    citations: list[float]
    sources: list[dict[str, Any]]      # [{n, title, url}]
    searches: list[str]                 # 模型发起的检索词
    spoiler_guard: bool
    context_mode: str                   # full = 全片转写进上下文；outline = 太长，只带大纲 + 附近几章原文
    provider: str
    input_tokens: int = 0               # 估算

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _outline(sheet: StudySheet | None) -> str:
    if sheet is None or not sheet.chapters:
        return "（还没有生成学习底稿）"
    lines = ["章节："]
    for i, c in enumerate(sheet.chapters, start=1):
        lines.append(f"第{i}章 [{format_timestamp(c.start)}–{format_timestamp(c.end)}] {c.title}：{c.claim}")
        for e in c.evidence:
            lines.append(f"  - {e.text}" + (f" [{format_timestamp(e.t)}]" if e.t is not None else ""))
        if c.conclusion:
            lines.append(f"  → {c.conclusion}")
    if sheet.glossary:
        lines += ["", "术语："]
        for t in sheet.glossary:
            lines.append(f"- {t.term}" + (f"（{t.en}）" if t.en else "") + (f"：{t.video_says}" if t.video_says else ""))
    return "\n".join(lines)


def _qa_budget(provider: BaseSummarizer) -> int:
    return provider.cfg.max_context_tokens - provider.cfg.max_output_tokens - _QA_TAIL_RESERVE


def build_prefix(provider: BaseSummarizer, transcript: Transcript, sheet: StudySheet | None,
                 uploader: str | None) -> tuple[str, str]:
    """固定前缀：同一条视频每次一样，才能吃前缀缓存。返回 (文本, full|outline)。"""
    outline = _outline(sheet)
    body = render_segments(transcript.segments, with_time=True)
    full = "\n".join([_header(transcript, uploader), "", "=== 学习底稿 ===", outline, "",
                      "=== 全片转写开始 ===", body, "=== 全片转写结束 ==="])
    if estimate_tokens(full) <= _qa_budget(provider):
        return full, "full"
    return "\n".join([_header(transcript, uploader), "", "=== 学习底稿 ===", outline, "",
                      "（视频太长，全片转写放不下；下面每次提问会附上当前位置附近几章的原文。）"]), "outline"


def _window(transcript: Transcript, sheet: StudySheet | None, position: float, mode: str,
            budget: int) -> tuple[float, float, str]:
    if mode == "outline" and sheet is not None and sheet.chapters:
        idx = sheet.chapter_at(position) or 0
        lo = sheet.chapters[max(0, idx - 1)].start
        hi = sheet.chapters[min(len(sheet.chapters) - 1, idx + 1)].end
    else:
        lo, hi = position - _WINDOW_BEFORE, position + _WINDOW_AFTER
    segs = [s for s in transcript.segments if s.end >= lo and s.start <= hi]
    text = render_segments(segs, with_time=True)
    if estimate_tokens(text) > budget:
        # 附近几章太长：退回到当前位置前后各一段
        segs = [s for s in transcript.segments if position - _WINDOW_BEFORE <= s.start <= position + _WINDOW_AFTER]
        text = render_segments(segs, with_time=True)
    lo = segs[0].start if segs else position
    hi = segs[-1].end if segs else position
    return lo, hi, text


def build_messages(provider: BaseSummarizer, transcript: Transcript, sheet: StudySheet | None, *,
                   question: str, position: float, history: list[tuple[Turn, float | None]],
                   spoiler_guard: bool, uploader: str | None) -> tuple[list[dict[str, Any]], str]:
    prefix, mode = build_prefix(provider, transcript, sheet, uploader)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": QA_SYSTEM},
        {"role": "user", "content": prefix},
        {"role": "assistant", "content": QA_ACK},
    ]
    for turn, pos in history[-MAX_HISTORY_TURNS:]:
        where = f"[我看到 {format_timestamp(pos)}] " if pos is not None else ""
        messages.append({"role": "user", "content": where + turn.question})
        messages.append({"role": "assistant", "content": turn.answer})

    idx = sheet.chapter_at(position) if sheet else None
    where = f"当前位置：[{format_timestamp(position)}]"
    if idx is not None and sheet is not None:
        where += f"，第 {idx + 1} 章「{sheet.chapters[idx].title}」"
    lo, hi, text = _window(transcript, sheet, position, mode, 12000 if mode == "outline" else 4000)
    tail = [where, "",
            f"当前位置附近的原文（[{format_timestamp(lo)}]–[{format_timestamp(hi)}]）：", text or "（这段没有转写）", ""]
    if spoiler_guard:
        tail.append(f"（用户开了防剧透：[{format_timestamp(position)}] 之后才讲到的内容，只能说"
                    "「后面 [mm:ss] 会讲到」，不要透露具体内容。）")
        tail.append("")
    tail.append(f"问题：{question.strip()}")
    messages.append({"role": "user", "content": "\n".join(tail)})
    return messages, mode


def _format_results(results: list[SearchResult], first: int) -> str:
    if not results:
        return "没有检索到结果。"
    lines = []
    for i, r in enumerate(results, start=first):
        lines.append(f"〔{i}〕{r.title}\n{r.url}\n{r.content}")
    return "\n\n".join(lines)


def ask(provider: BaseSummarizer, transcript: Transcript, sheet: StudySheet | None, *,
        question: str, position: float, history: list[tuple[Turn, float | None]] | None = None,
        spoiler_guard: bool = False, searcher: WebSearcher | None = None,
        uploader: str | None = None) -> StudyAnswer:
    question = (question or "").strip()
    if not question:
        raise ValueError("问题不能为空")
    position = max(0.0, min(float(position or 0), transcript.duration_sec or float(position or 0)))
    messages, mode = build_messages(provider, transcript, sheet, question=question, position=position,
                                    history=history or [], spoiler_guard=spoiler_guard, uploader=uploader)
    input_tokens = sum(estimate_tokens(str(m.get("content") or "")) for m in messages)

    use_tools = searcher is not None and searcher.enabled and provider.supports_tools
    max_rounds = searcher.cfg.max_calls if use_tools and searcher is not None else 0
    sources: list[SearchResult] = []
    searches: list[str] = []
    reply = None
    for round_no in range(max_rounds + 1):
        tools = [SEARCH_TOOL] if use_tools and round_no < max_rounds else None
        reply = provider.chat(messages, tools)
        if not reply.tool_calls or tools is None:
            break
        messages.append(reply.message)
        for call in reply.tool_calls:
            query = _clean(call.arguments.get("query"), 200)
            if call.name != "web_search" or not query:
                content = "无效的工具调用。"
            else:
                searches.append(query)
                try:
                    results = searcher.search(query) if searcher else []
                except WebSearchError as exc:
                    log.warning("联网检索失败：%s", exc)
                    content = f"检索失败：{exc}。请不依赖检索作答，并说明没能核实。"
                else:
                    content = _format_results(results, len(sources) + 1)
                    sources.extend(results)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
    text = (reply.content if reply else "").strip()
    if not text:
        raise SummarizerError("模型没有给出回答")

    idx = sheet.chapter_at(position) if sheet else None
    return StudyAnswer(
        question=question, answer=text, position=position, chapter=idx,
        citations=extract_citations(text),
        sources=[{"n": i, "title": r.title, "url": r.url} for i, r in enumerate(sources, start=1)],
        searches=searches, spoiler_guard=spoiler_guard, context_mode=mode,
        provider=provider.describe(), input_tokens=input_tokens,
    )


def plan_ask(provider: BaseSummarizer, transcript: Transcript, sheet: StudySheet | None,
             uploader: str | None = None) -> dict[str, Any]:
    """问一次的量级：固定前缀多大（大部分会命中缓存）、是不是退化成大纲模式。"""
    prefix, mode = build_prefix(provider, transcript, sheet, uploader)
    prefix_tokens = estimate_tokens(prefix) + estimate_tokens(QA_SYSTEM)
    return {"prefix_tokens": prefix_tokens, "input_tokens": prefix_tokens + 2500, "context_mode": mode}
