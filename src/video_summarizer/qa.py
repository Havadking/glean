"""问视频：对一条转写提问，模型只依据转写回答，每条结论附时间戳。

时间戳是这个功能的核心，不是装饰：
- 读者点一下就跳到原文，能自己验证 —— 这是对"模型会编"最结构性的防御
- 没有时间戳可引的结论，就是转写里没有的，模型得直说"视频里没提"

转写整篇进上下文（带 [mm:ss] 时间戳的分句），超出预算时只能截断，界面上要提示。
问答走 provider 的 complete() 单次调用，不做 map-reduce：问答要的是精确定位，切块汇总反而丢时间戳。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .models import Transcript
from .summarizer.base import BaseSummarizer
from .summarizer.tokens import chunk_segments, estimate_tokens, format_timestamp, render_segments

SYSTEM = (
    "你是一个视频内容问答助手。用户会给你一段带时间戳的视频转写，然后提问。\n\n"
    "规则：\n"
    "1. **只依据转写内容回答。** 转写里没有的信息，哪怕你知道，也不要写。"
    "转写里没提到的，直接说\"视频里没有提到\"，不要猜、不要补充背景知识。\n"
    "2. **每条结论都要标注出处时间戳**，格式是方括号里的时间，如 [03:27]，放在对应句子的末尾。"
    "引用转写原话时也标。一条结论对应多处就标多个。\n"
    "3. 转写来自语音识别，可能有错别字和断句问题，结合上下文理解真实意思，不要逐字复述。\n"
    "4. 用用户提问的语言回答，简洁直接，用 Markdown。能用列表就用列表。\n"
    "5. 不要在回答里复述规则，不要加\"根据转写\"之类的开场白。"
)

# 每次调用里留给问题、历史和回答的 token
_OVERHEAD_TOKENS = 1500
# 带进上下文的历史轮数上限
MAX_HISTORY_TURNS = 4

# 回答里的时间戳：[03:27] 或 [1:02:03]
TIMESTAMP_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]")


@dataclass
class Turn:
    question: str
    answer: str


@dataclass
class Answer:
    question: str
    answer: str
    # 问视频：回答里出现的时间戳（秒）；问 UP 主：引用到的 video_id。都去重保序
    citations: list = field(default_factory=list)
    truncated: bool = False                                  # 转写太长被截断了
    input_tokens: int = 0                                    # 估算值
    provider: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question, "answer": self.answer, "citations": self.citations,
            "truncated": self.truncated, "input_tokens": self.input_tokens, "provider": self.provider,
        }


def parse_timestamp(m: re.Match) -> float:
    a, b, c = m.group(1), m.group(2), m.group(3)
    if c is not None:
        return int(a) * 3600 + int(b) * 60 + int(c)
    return int(a) * 60 + int(b)


def extract_citations(answer: str) -> list[float]:
    seen: list[float] = []
    for m in TIMESTAMP_RE.finditer(answer):
        t = parse_timestamp(m)
        if t not in seen:
            seen.append(t)
    return seen


def _fit_transcript(transcript: Transcript, budget: int) -> tuple[str, bool]:
    """转写整篇渲染；超预算就只保留开头能装下的部分。"""
    body = render_segments(transcript.segments, with_time=True)
    if estimate_tokens(body) <= budget:
        return body, False
    chunks = chunk_segments(transcript.segments, budget, with_time=True)
    kept = chunks[0] if chunks else []
    return render_segments(kept, with_time=True), True


def build_prompt(
    transcript: Transcript, question: str, history: list[Turn], budget: int,
) -> tuple[str, bool]:
    body, truncated = _fit_transcript(transcript, budget)
    parts = [
        f"视频标题：{transcript.title or '（无）'}",
        f"时长：{format_timestamp(transcript.duration_sec)}",
        "",
        "=== 转写开始 ===",
        body,
        "=== 转写结束 ===" + ("（转写太长，后半部分没有包含）" if truncated else ""),
        "",
    ]
    if history:
        parts.append("之前的问答（供上下文参考）：")
        for t in history[-MAX_HISTORY_TURNS:]:
            parts.append(f"问：{t.question}")
            parts.append(f"答：{t.answer}")
        parts.append("")
    parts.append(f"问题：{question.strip()}")
    return "\n".join(parts), truncated


def plan(provider: BaseSummarizer, transcript: Transcript) -> dict[str, Any]:
    """问一次大概花多少：转写 token 数、会不会被截断。"""
    budget = provider.cfg.max_context_tokens - provider.cfg.max_output_tokens - _OVERHEAD_TOKENS
    body = render_segments(transcript.segments, with_time=True)
    tokens = estimate_tokens(body)
    return {
        "transcript_tokens": tokens,
        "input_tokens": min(tokens, budget) + 300,
        "truncated": tokens > budget,
    }


def ask(
    provider: BaseSummarizer, transcript: Transcript, question: str,
    history: list[Turn] | None = None,
) -> Answer:
    if not question.strip():
        raise ValueError("问题不能为空")
    budget = provider.cfg.max_context_tokens - provider.cfg.max_output_tokens - _OVERHEAD_TOKENS
    user, truncated = build_prompt(transcript, question, history or [], budget)
    text = provider.complete(SYSTEM, user).strip()
    return Answer(
        question=question.strip(), answer=text, citations=extract_citations(text),
        truncated=truncated, input_tokens=estimate_tokens(user), provider=provider.describe(),
    )


# ---------- 问 UP 主：跨视频 ----------

UPLOADER_SYSTEM = (
    "你是一个视频内容分析助手。用户关注某位创作者，会给你这位创作者多条视频的内容摘要"
    "（每条带编号、标题、发布日期），然后提问。\n\n"
    "规则：\n"
    "1. **只依据给出的材料回答。** 材料里没有的信息，哪怕你知道这位创作者，也不要写。"
    "材料里没提到的，直接说\"这些视频里没有提到\"。\n"
    "2. **每条结论都要标注来自哪条视频**，用方括号加编号，如 【2】，放在对应句子末尾；"
    "多条视频都提到就标多个，如 【1】【3】。\n"
    "3. 不同视频说法不一致或前后有变化时，明确指出来，并按发布日期说明先后。\n"
    "4. 用用户提问的语言回答，简洁直接，用 Markdown，能用列表就用列表。\n"
    "5. 不要复述规则，不要加开场白。"
)

CITE_INDEX_RE = re.compile(r"【(\d{1,3})】")


@dataclass
class Material:
    index: int          # 从 1 开始
    video_id: str
    title: str
    date: str | None    # YYYY-MM-DD
    kind: str           # 用的是哪种总结 / transcript
    text: str


def build_uploader_prompt(uploader: str, materials: list[Material], question: str,
                          history: list[Turn]) -> str:
    parts = [f"创作者：{uploader}", f"视频数：{len(materials)}", ""]
    for m in materials:
        parts.append(f"=== 【{m.index}】{m.title}（{m.date or '日期不详'}）===")
        parts.append(m.text.strip())
        parts.append("")
    if history:
        parts.append("之前的问答（供上下文参考）：")
        for t in history[-MAX_HISTORY_TURNS:]:
            parts.append(f"问：{t.question}")
            parts.append(f"答：{t.answer}")
        parts.append("")
    parts.append(f"问题：{question.strip()}")
    return "\n".join(parts)


def ask_uploader(
    provider: BaseSummarizer, uploader: str, materials: list[Material], question: str,
    history: list[Turn] | None = None,
) -> Answer:
    if not question.strip():
        raise ValueError("问题不能为空")
    if not materials:
        raise ValueError("这位创作者还没有可用的材料")
    user = build_uploader_prompt(uploader, materials, question, history or [])
    text = provider.complete(UPLOADER_SYSTEM, user).strip()
    by_index = {m.index: m.video_id for m in materials}
    cited: list[str] = []
    for mm in CITE_INDEX_RE.finditer(text):
        vid = by_index.get(int(mm.group(1)))
        if vid and vid not in cited:
            cited.append(vid)
    return Answer(question=question.strip(), answer=text, citations=cited,  # type: ignore[arg-type]
                  truncated=False, input_tokens=estimate_tokens(user), provider=provider.describe())
