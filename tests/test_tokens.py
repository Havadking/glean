"""Token 估算和分块。估算只求量级对，但分块的边界必须准。"""

from __future__ import annotations

import pytest

from video_summarizer.models import Segment
from video_summarizer.summarizer import tokens


def test_cjk_counts_about_one_token_each():
    assert tokens.estimate_tokens("中文一二三") == pytest.approx(6, abs=1)


def test_latin_counts_fewer_tokens_than_chars():
    n = tokens.estimate_tokens("hello world")
    assert 2 <= n <= 5


def test_empty_is_zero():
    assert tokens.estimate_tokens("") == 0


def test_estimate_never_underestimates_badly():
    """宁可高估：低估会导致真实请求超上下文。"""
    text = "混合 mixed 内容 content 一二三 456"
    assert tokens.estimate_tokens(text) >= len(text) / 4


@pytest.mark.parametrize("seconds,expected", [(0, "00:00"), (65, "01:05"), (3661, "1:01:01")])
def test_timestamp_formatting(seconds, expected):
    assert tokens.format_timestamp(seconds) == expected


def test_chunking_splits_on_segment_boundaries(long_segments):
    chunks = tokens.chunk_segments(long_segments, max_tokens=200)
    assert len(chunks) > 1
    # 不丢句、不重复、顺序不变
    flat = [s for c in chunks for s in c]
    assert flat == long_segments


def test_chunking_respects_budget(long_segments):
    budget = 200
    for chunk in tokens.chunk_segments(long_segments, max_tokens=budget):
        # 单句本身就超预算时会独占一块，所以只检查多句块
        if len(chunk) > 1:
            assert tokens.estimate_tokens(tokens.render_segments(chunk)) <= budget * 1.2


def test_oversized_single_segment_gets_its_own_chunk():
    segs = [Segment(start=0, end=1, text="很长的一句话" * 200)]
    chunks = tokens.chunk_segments(segs, max_tokens=10)
    assert len(chunks) == 1 and chunks[0] == segs


def test_empty_input_yields_no_chunks():
    assert tokens.chunk_segments([], max_tokens=100) == []


def test_render_includes_speaker_and_time(dialogue):
    out = tokens.render_segments(dialogue.segments, with_time=True)
    assert "[00:00] Speaker_1: 你怎么看这件事？" in out
    assert "Speaker_2" in out


def test_render_can_drop_timestamps(dialogue):
    out = tokens.render_segments(dialogue.segments, with_time=False)
    assert "[00:00]" not in out
    assert out.startswith("Speaker_1: ")
