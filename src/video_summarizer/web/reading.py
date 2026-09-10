"""把碎分句聚成适合阅读的段落。

为什么需要：各来源的分句粒度实测下来是这样的 ——

    来源                 分句数   平均时长   平均字数
    字幕                    —     2.4 秒     35 字
    whisper                49     3.0 秒     36 字
    FunASR（不分离）       175     4.6 秒     25 字
    FunASR + 说话人分离     31    42.9 秒    222 字

只有开了说话人分离那条是能读的 —— 因为它在 ASR 层就把同一个人连续说的合并成了
"一轮发言"。其余三条都是两三秒一行、二三十个字，等于让人一行行读字幕；
一个 30 分钟的视频按字幕粒度有 600 多行。

这里把同样的合并思路搬到显示层，对所有来源生效。**只影响显示**，
transcript.json 保持精确时间轴不动 —— 时间轴是产物的一部分，不能为了好看牺牲。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import Segment

# 一段的目标时长：到了这个长度就找机会断开
TARGET_SEC = 60.0
# 硬上限：再长也得断，否则遇到没有标点的转写会堆成一整块
MAX_SEC = 150.0
# 停顿超过这么久，认为话题断了，即使段落还不够长也断开
LONG_PAUSE_SEC = 3.0

# 句末标点。中英文都要认，ASR 出来的中文里也常混英文标点。
_SENTENCE_END = ("。", "！", "？", "…", ".", "!", "?", "；", ";")
# 判断要不要在拼接处补空格。范围里必须带上 CJK 标点和全角符号 ——
# 中文句子多半以「，」「。」结尾，只查汉字区间会漏掉它们，于是拼出 "你好， 世界"。
_CJK_RANGES = (
    (0x3000, 0x303F),   # CJK 标点：、。〈〉《》「」…
    (0x3040, 0x30FF),   # 日文假名
    (0x3400, 0x4DBF),   # CJK 扩展 A
    (0x4E00, 0x9FFF),   # CJK 基本区
    (0xAC00, 0xD7AF),   # 谚文音节
    (0xF900, 0xFAFF),   # CJK 兼容表意
    (0xFF01, 0xFF60),   # 全角形式：！？：；（），．
)


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _CJK_RANGES)


@dataclass
class Paragraph:
    """一个用于阅读的段落。start 是段首时间，用来做锚点。"""

    start: float
    end: float
    text: str
    speaker: str | None = None
    segment_count: int = 0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def _ends_sentence(text: str) -> bool:
    return text.rstrip().endswith(_SENTENCE_END)


def _join(left: str, right: str) -> str:
    """拼两段文本。中文之间不加空格，涉及西文的加一个。"""
    if not left:
        return right
    if not right:
        return left
    if _is_cjk(left[-1]) or _is_cjk(right[0]):
        return left + right
    if left[-1].isspace():
        return left + right
    return f"{left} {right}"


def to_paragraphs(
    segments: list[Segment],
    *,
    target_sec: float = TARGET_SEC,
    max_sec: float = MAX_SEC,
) -> list[Paragraph]:
    """把分句聚成段落。

    断开的条件，按优先级：
    1. 换了说话人 —— 永远断开
    2. 超过硬上限 —— 必须断开
    3. 停顿超过 LONG_PAUSE_SEC —— 话题多半断了
    4. 够长了（>= target_sec）且上一句以句末标点收尾 —— 在自然的地方断
    """
    paragraphs: list[Paragraph] = []
    current: Paragraph | None = None

    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue

        if current is None:
            current = Paragraph(start=seg.start, end=seg.end, text=text,
                                speaker=seg.speaker, segment_count=1)
            continue

        if _should_break(current, seg, target_sec, max_sec):
            paragraphs.append(current)
            current = Paragraph(start=seg.start, end=seg.end, text=text,
                                speaker=seg.speaker, segment_count=1)
            continue

        current.text = _join(current.text, text)
        current.end = max(current.end, seg.end)
        current.segment_count += 1

    if current is not None:
        paragraphs.append(current)
    return paragraphs


def _should_break(current: Paragraph, seg: Segment, target_sec: float, max_sec: float) -> bool:
    if seg.speaker != current.speaker:
        return True
    if seg.end - current.start > max_sec:
        return True
    if seg.start - current.end >= LONG_PAUSE_SEC:
        return True
    if current.duration >= target_sec and _ends_sentence(current.text):
        return True
    return False


def to_plain_text(paragraphs: list[Paragraph], with_time: bool = True) -> str:
    """导出成纯文本，段落之间空一行。用于复制和下载。"""
    from ..summarizer.tokens import format_timestamp

    blocks = []
    for p in paragraphs:
        head = f"[{format_timestamp(p.start)}]" if with_time else ""
        if p.speaker:
            head = f"{head} {p.speaker}".strip()
        blocks.append(f"{head}\n{p.text}" if head else p.text)
    return "\n\n".join(blocks)
