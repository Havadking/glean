"""转写清洗：去口水话和结巴重复。可选，默认不开。

ASR 出来的口语原样进阅读视图和 prompt："呃"、"嗯"、"就是就是"、"那个那个"。
清洗一遍省 token 也好读。但有人要原汁原味（口头禅是风格的一部分），
所以只做成开关：阅读视图一个按钮，总结/问答走 config 的 summarizer.clean_transcript。

规则刻意保守 —— 宁可漏删，不要误删：
- "呃""嗯"前面是句首或标点就删，但"嗯嗯"表示同意，不碰
- 多字填充词（"那个那个"）前后都得是边界才删
- 只折叠两字以上词的紧邻重复（"就是就是" -> "就是"），单字叠词不碰：
  "看看""谢谢""慢慢""妮妮"都是正常词
- 时间轴不动：清洗只改 text，segment 的 start/end 原样
"""

from __future__ import annotations

import re
from dataclasses import replace

from .models import Segment, Transcript

SINGLE_FILLERS = ("呃", "嗯")
PHRASE_FILLERS = ("呃呃", "额呃", "那个那个", "这个这个", "对吧对吧")

_PUNCT = r"\s，。！？、；：,.!?…—\-()（）\[\]【】“”\"'"
_LEFT = "(?:^|(?<=[" + _PUNCT + "]))"
_RIGHT = "(?=$|[" + _PUNCT + "])"
_SINGLE_RE = re.compile(_LEFT + "([" + "".join(SINGLE_FILLERS) + "])(?!\\1)")
_PHRASE_RE = re.compile(_LEFT + "(?:" + "|".join(map(re.escape, PHRASE_FILLERS)) + ")" + _RIGHT)
# 紧邻重复：2~4 个字的词紧接着再出现一次或多次（"就是就是就是" -> "就是"）
_REPEAT_RE = re.compile("([㐀-鿿]{2,4})(?:\\1)+")
# 删完剩下的孤零零标点：弱标点跟着强标点时只留强的（"，。" -> "。"），连着的弱标点只留一个
_WEAK_BEFORE_STRONG_RE = re.compile(r"[，、；：\s]+(?=[。！？])")
_DUP_PUNCT_RE = re.compile(r"([，。！？、；：])[\s，、；：]+")
_LEAD_PUNCT_RE = re.compile(r"^[\s，、；：]+")
_SPACE_RE = re.compile(r"[ \t]+")


def clean_text(text: str) -> str:
    if not text:
        return text
    out = _PHRASE_RE.sub("", text)
    out = _SINGLE_RE.sub("", out)
    out = _REPEAT_RE.sub(r"\1", out)
    out = _WEAK_BEFORE_STRONG_RE.sub("", out)
    out = _DUP_PUNCT_RE.sub(r"\1", out)
    out = _LEAD_PUNCT_RE.sub("", out)
    return _SPACE_RE.sub(" ", out).strip()


def clean_segments(segments: list[Segment]) -> list[Segment]:
    """清洗后的新列表；清空的分句丢掉。"""
    out = []
    for s in segments:
        t = clean_text(s.text)
        if t:
            out.append(replace(s, text=t))
    return out


def clean_transcript(transcript: Transcript) -> Transcript:
    return replace(transcript, segments=clean_segments(transcript.segments),
                   meta={**transcript.meta, "cleaned": True})


def removed_ratio(segments: list[Segment]) -> float:
    """清洗掉了多少（按字符数）。给界面上的开关做提示。"""
    before = sum(len(s.text) for s in segments)
    if not before:
        return 0.0
    after = sum(len(s.text) for s in clean_segments(segments))
    return max(0.0, 1 - after / before)
