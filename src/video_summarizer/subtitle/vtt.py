"""WebVTT / SRT 解析：字幕文件 -> 纯文本 + 时间轴。

要处理的脏东西：WEBVTT 头、NOTE 注释块、时间戳后面的 cue 设置、`<c>`/`<v>` 内联标签、
HTML 实体（`&nbsp;` `&amp;` 等）、以及滚动字幕造成的连续重复行。
"""

from __future__ import annotations

import html
import re
from pathlib import Path

from ..models import Segment

# 00:01:02.345 --> 00:01:05.678 align:start position:0%
_CUE_RE = re.compile(
    r"^\s*(?P<start>(?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})"
    r"\s*-->\s*"
    r"(?P<end>(?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})"
    r"(?P<settings>.*)$"
)
_TAG_RE = re.compile(r"<[^>]+>")
_SRT_INDEX_RE = re.compile(r"^\d+$")


def parse_timestamp(value: str) -> float:
    """`HH:MM:SS.mmm` / `MM:SS,mmm` -> 秒。"""
    value = value.strip().replace(",", ".")
    parts = value.split(":")
    if len(parts) == 3:
        hours, minutes, seconds = parts
    elif len(parts) == 2:
        hours, (minutes, seconds) = "0", parts
    else:
        raise ValueError(f"无法解析的时间戳: {value!r}")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def clean_text(raw: str) -> str:
    """去掉内联标签和 HTML 实体，压掉多余空白。"""
    text = _TAG_RE.sub("", raw)
    text = html.unescape(text)
    text = text.replace(" ", " ").replace("​", "")
    return re.sub(r"\s+", " ", text).strip()


def parse(content: str) -> list[Segment]:
    """解析 VTT 或 SRT 文本，返回按时间排序、已去重的分句。"""
    segments: list[Segment] = []
    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]

        # NOTE / STYLE / REGION 块：跳到下一个空行
        stripped = line.strip()
        if stripped.startswith(("NOTE", "STYLE", "REGION")):
            i += 1
            while i < n and lines[i].strip():
                i += 1
            continue

        match = _CUE_RE.match(line)
        if not match:
            i += 1
            continue

        try:
            start = parse_timestamp(match.group("start"))
            end = parse_timestamp(match.group("end"))
        except ValueError:
            i += 1
            continue

        i += 1
        body: list[str] = []
        while i < n and lines[i].strip():
            # SRT 的下一条序号行可能紧贴着上一条正文
            if _SRT_INDEX_RE.match(lines[i].strip()) and i + 1 < n and _CUE_RE.match(lines[i + 1]):
                break
            body.append(lines[i])
            i += 1

        text = clean_text(" ".join(body))
        if text:
            segments.append(Segment(start=start, end=max(end, start), text=text))

    segments.sort(key=lambda s: (s.start, s.end))
    return _dedupe(segments)


def _dedupe(segments: list[Segment]) -> list[Segment]:
    """合并滚动字幕产生的连续重复行，以及被拆成两条的同一句话。"""
    result: list[Segment] = []
    for seg in segments:
        if not result:
            result.append(seg)
            continue
        prev = result[-1]
        if seg.text == prev.text:
            prev.end = max(prev.end, seg.end)
            continue
        # 前一条是后一条的前缀（滚动字幕逐字追加）：用更长的那条
        if seg.text.startswith(prev.text) and seg.start <= prev.end:
            prev.text = seg.text
            prev.end = max(prev.end, seg.end)
            continue
        result.append(seg)
    return result


def parse_file(path: Path) -> list[Segment]:
    return parse(Path(path).read_text(encoding="utf-8", errors="replace"))
