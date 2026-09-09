"""Token 估算与分块。

不引 tiktoken：不同 provider 的 tokenizer 本来就不一致，装一个也只是换个不准法。
这里用够用的启发式（CJK 字符 ≈ 1 token，其余 ≈ 3.5 字符/token）配合安全余量，
目的是决定"整篇塞进去还是走 map-reduce"以及给用户看个成本量级，不需要精确。
"""

from __future__ import annotations

import re

from ..models import Segment

_CJK_RE = re.compile(
    "[぀-ヿ"   # 日文假名
    "㐀-䶿"    # CJK 扩展 A
    "一-鿿"    # CJK 基本区
    "豈-﫿"    # CJK 兼容表意
    "가-힯]"   # 谚文音节
)
_NON_CJK_CHARS_PER_TOKEN = 3.5


def estimate_tokens(text: str) -> int:
    """粗估 token 数。宁可高估，不要低估到超上下文。"""
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    rest = len(text) - cjk
    return int(cjk + rest / _NON_CJK_CHARS_PER_TOKEN) + 1


def format_timestamp(seconds: float) -> str:
    seconds = int(max(0.0, seconds))
    if seconds >= 3600:
        return f"{seconds // 3600:d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def render_segment(seg: Segment, with_time: bool = True) -> str:
    prefix = f"[{format_timestamp(seg.start)}] " if with_time else ""
    speaker = f"{seg.speaker}: " if seg.speaker else ""
    return f"{prefix}{speaker}{seg.text}"


def render_segments(segments: list[Segment], with_time: bool = True) -> str:
    return "\n".join(render_segment(s, with_time) for s in segments)


def chunk_segments(
    segments: list[Segment], max_tokens: int, with_time: bool = True
) -> list[list[Segment]]:
    """按 token 预算切块，只在分句边界切，不切碎句子。"""
    if max_tokens <= 0:
        return [segments] if segments else []

    chunks: list[list[Segment]] = []
    current: list[Segment] = []
    current_tokens = 0

    for seg in segments:
        cost = estimate_tokens(render_segment(seg, with_time)) + 1
        if current and current_tokens + cost > max_tokens:
            chunks.append(current)
            current, current_tokens = [], 0
        current.append(seg)
        current_tokens += cost

    if current:
        chunks.append(current)
    return chunks
