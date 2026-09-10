"""HTML 片段渲染。

Gradio 的 Markdown 组件排版控制不够（行高、段间距、时间戳锚点的对齐都调不了），
阅读视图和历史卡片直接出 HTML。所有用户内容都要过 html.escape —— 转写文本来自
ASR 和字幕，里面什么都可能有。

配色一律用 CSS 变量，跟着 Gradio 主题走亮/暗模式，不写死颜色。
"""

from __future__ import annotations

from html import escape

from ..summarizer.tokens import format_timestamp
from .library import LibraryEntry
from .reading import Paragraph

# 阅读视图：给人读的排版，不是给机器看的表格
# Gradio 自带的 .prose 规则特异性比单类选择器高，会把这里的字号行高盖掉，
# 所以关键属性上加 !important —— 只作用在这个组件里，不会外溢。
READING_CSS = """
<style>
.vs-read{max-width:68ch;margin:0}
.vs-read .vs-para{display:flex;gap:14px;margin:0 0 20px !important;align-items:flex-start}
.vs-read .vs-ts{flex:none;width:52px;padding-top:4px;font-size:12px !important;
  font-variant-numeric:tabular-nums;color:var(--block-title-text-color) !important;
  user-select:none;line-height:1.4 !important}
.vs-read .vs-body{flex:1;min-width:0}
.vs-read .vs-spk{font-size:12px !important;font-weight:600 !important;
  color:var(--color-accent) !important;margin:0 0 4px !important;line-height:1.4 !important}
.vs-read .vs-text{margin:0 !important;font-size:15.5px !important;line-height:1.85 !important;
  word-break:break-word;color:var(--body-text-color)}
.vs-empty{color:var(--block-title-text-color) !important;font-size:14px;padding:24px 0}
.vs-read .vs-hit{background:var(--color-accent-soft);
  box-shadow:0 0 0 2px var(--color-accent-soft);border-radius:2px}
</style>
"""

CARD_CSS = """
<style>
.vs-card{border:1px solid var(--border-color-primary);border-radius:10px;
         padding:13px 15px;background:var(--background-fill-primary)}
.vs-card h4{margin:0 0 6px !important;font-size:15px !important;font-weight:600 !important;
            line-height:1.45 !important;color:var(--body-text-color)}
.vs-card .vs-meta{margin:0 0 9px !important;font-size:12px !important;
            line-height:1.5 !important;color:var(--block-title-text-color) !important}
.vs-card .vs-badges{display:flex;flex-wrap:wrap;gap:5px}
.vs-card .vs-badge{font-size:11px;padding:2px 8px;border-radius:6px;
          background:var(--color-accent-soft);color:var(--body-text-color)}
.vs-card .vs-badge.vs-none{background:var(--background-fill-secondary);
                  color:var(--block-title-text-color)}
.vs-card .vs-spk-tag{color:var(--color-accent)}
</style>
"""


def paragraphs_html(paragraphs: list[Paragraph], query: str = "") -> str:
    """阅读视图。query 非空时高亮命中的词。"""
    if not paragraphs:
        return READING_CSS + '<div class="vs-empty">还没有转写。</div>'

    needle = (query or "").strip()
    blocks = []
    for p in paragraphs:
        text = escape(p.text)
        if needle:
            text = _highlight(text, escape(needle))
        speaker = f'<p class="vs-spk">{escape(p.speaker)}</p>' if p.speaker else ""
        blocks.append(
            f'<div class="vs-para">'
            f'<span class="vs-ts">{format_timestamp(p.start)}</span>'
            f'<div class="vs-body">{speaker}<p class="vs-text">{text}</p></div>'
            f"</div>"
        )
    return READING_CSS + f'<div class="vs-read">{"".join(blocks)}</div>'


def _highlight(escaped_text: str, escaped_needle: str) -> str:
    """在已转义的文本上做不区分大小写的高亮。"""
    if not escaped_needle:
        return escaped_text
    out, low_text, low_needle = [], escaped_text.lower(), escaped_needle.lower()
    i = 0
    while True:
        j = low_text.find(low_needle, i)
        if j < 0:
            out.append(escaped_text[i:])
            return "".join(out)
        out.append(escaped_text[i:j])
        out.append(f'<span class="vs-hit">{escaped_text[j:j + len(escaped_needle)]}</span>')
        i = j + len(escaped_needle)


def card_html(entry: LibraryEntry) -> str:
    """历史列表里的一张卡。标题就是视频名。"""
    bits = [format_timestamp(entry.duration_sec), entry.source_label]
    if entry.language:
        bits.append(entry.language)
    bits.append(f"{entry.segment_count} 句")
    if entry.speaker_count > 1:
        bits.append(f'<span class="vs-spk-tag">{entry.speaker_count} 位说话人</span>')
    if entry.created_at:
        bits.append(entry.created_at[:10])

    if entry.summaries:
        badges = "".join(
            f'<span class="vs-badge">{escape(s.label)}</span>' for s in entry.summaries
        )
    else:
        badges = '<span class="vs-badge vs-none">还没有总结</span>'

    return (
        CARD_CSS
        + f'<div class="vs-card"><h4>{escape(entry.title)}</h4>'
        + f'<p class="vs-meta">{" · ".join(bits)}</p>'
        + f'<div class="vs-badges">{badges}</div></div>'
    )


def transcript_rows(segments) -> list[list[str]]:
    """精确视图的表格：原始分句，不做聚合。

    阅读视图为了好读把句子合并了，代价是丢掉精确的起止时间。
    这个视图就是那个代价的补偿 —— 要查某句话具体在第几秒，看这里。
    """
    return [
        [format_timestamp(s.start), format_timestamp(s.end), s.speaker or "—", s.text]
        for s in segments
    ]
