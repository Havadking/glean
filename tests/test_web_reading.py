"""阅读视图的分段聚合。

这是 UI 改版里唯一有真实逻辑的一块，断句规则错了会直接影响可读性。
"""

from __future__ import annotations

from video_summarizer.models import Segment
from video_summarizer.web import reading, render


def _segs(*specs) -> list[Segment]:
    """(start, end, text[, speaker]) -> Segment 列表"""
    return [Segment(*s) if len(s) == 3 else Segment(s[0], s[1], s[2], s[3]) for s in specs]


def test_merges_short_fragments_into_one_paragraph():
    """字幕那种两三秒一条的，应该合成一段。"""
    segs = _segs(
        (0.0, 2.4, "就是我的老头子都要做精致的猪猪男孩，"),
        (2.4, 5.0, "希望大家今天能学到不少干货。"),
        (5.0, 8.0, "最近我又发现了一个美妆小心得。"),
    )
    paras = reading.to_paragraphs(segs)
    assert len(paras) == 1
    assert paras[0].start == 0.0 and paras[0].end == 8.0
    assert paras[0].segment_count == 3
    assert "猪猪男孩" in paras[0].text and "美妆小心得" in paras[0].text


def test_chinese_joins_without_space():
    paras = reading.to_paragraphs(_segs((0, 1, "你好，"), (1, 2, "世界。")))
    assert paras[0].text == "你好，世界。"


def test_latin_joins_with_space():
    paras = reading.to_paragraphs(_segs((0, 1, "hello there"), (1, 2, "world")))
    assert paras[0].text == "hello there world"


def test_speaker_change_always_breaks():
    segs = _segs(
        (0.0, 3.0, "你怎么看？", "Speaker_1"),
        (3.0, 6.0, "我觉得没那么简单。", "Speaker_2"),
        (6.0, 9.0, "展开说说。", "Speaker_1"),
    )
    paras = reading.to_paragraphs(segs)
    assert len(paras) == 3
    assert [p.speaker for p in paras] == ["Speaker_1", "Speaker_2", "Speaker_1"]


def test_long_pause_breaks():
    """停顿超过 3 秒，认为话题断了。"""
    segs = _segs((0.0, 2.0, "第一个话题。"), (10.0, 12.0, "换个话题。"))
    assert len(reading.to_paragraphs(segs)) == 2


def test_breaks_at_sentence_end_once_long_enough():
    """够长了要在句末断，不能断在半句话中间。"""
    segs = [Segment(i * 5.0, i * 5.0 + 5.0, f"这是第{i}句话。") for i in range(40)]
    paras = reading.to_paragraphs(segs, target_sec=30.0)
    assert len(paras) > 1
    for p in paras[:-1]:
        assert p.text.rstrip().endswith("。"), f"断在了半句话：{p.text[-20:]}"


def test_hard_cap_breaks_even_without_punctuation():
    """ASR 有时整段没有标点，不能因此堆成一整块。"""
    segs = [Segment(i * 5.0, i * 5.0 + 5.0, f"没有标点的第{i}句") for i in range(60)]
    paras = reading.to_paragraphs(segs, target_sec=30.0, max_sec=60.0)
    assert len(paras) > 1
    assert all(p.duration <= 70.0 for p in paras)


def test_skips_blank_segments():
    paras = reading.to_paragraphs(_segs((0, 1, "有内容"), (1, 2, "   "), (2, 3, "也有")))
    assert len(paras) == 1 and paras[0].segment_count == 2


def test_empty_input():
    assert reading.to_paragraphs([]) == []


def test_real_granularity_becomes_readable():
    """实测数据：FunASR 不分离是 4.6 秒 / 25 字一条，聚合后应该好读得多。"""
    segs = [Segment(i * 4.6, (i + 1) * 4.6, "大概二十五个字左右的一句话内容在这里。")
            for i in range(175)]
    paras = reading.to_paragraphs(segs)
    assert len(paras) < 30, f"175 条只聚成了 {len(paras)} 段，还是太碎"
    avg_chars = sum(len(p.text) for p in paras) / len(paras)
    assert avg_chars > 150, f"平均每段才 {avg_chars:.0f} 字"


def test_plain_text_export_has_timestamps_and_blank_lines():
    segs = _segs((0.0, 3.0, "第一段。", "Speaker_1"), (30.0, 33.0, "第二段。", "Speaker_2"))
    text = reading.to_plain_text(reading.to_paragraphs(segs))
    assert "[00:00] Speaker_1" in text
    assert "\n\n" in text


# ---------- 渲染 ----------


def test_html_escapes_user_content():
    """转写来自 ASR 和字幕，什么都可能有。"""
    paras = reading.to_paragraphs(_segs((0, 1, "<script>alert(1)</script>")))
    html = render.paragraphs_html(paras)
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_search_highlights_matches():
    paras = reading.to_paragraphs(_segs((0, 1, "喷雾补水很重要")))
    html = render.paragraphs_html(paras, query="喷雾")
    assert 'class="vs-hit"' in html


def test_search_highlight_cannot_inject_html():
    paras = reading.to_paragraphs(_segs((0, 1, "正常内容")))
    html = render.paragraphs_html(paras, query="<img src=x onerror=1>")
    assert "<img" not in html


def test_empty_transcript_renders_placeholder():
    assert "还没有转写" in render.paragraphs_html([])


def test_precise_view_keeps_raw_segments():
    """精确视图不聚合 —— 那是它存在的意义。"""
    segs = _segs((0.0, 2.4, "第一句"), (2.4, 5.0, "第二句"))
    rows = render.transcript_rows(segs)
    assert len(rows) == 2
    assert rows[0][0] == "00:00" and rows[0][1] == "00:02"
