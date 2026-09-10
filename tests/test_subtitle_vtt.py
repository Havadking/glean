"""字幕解析。真实字幕文件里的脏东西都在这儿覆盖。"""

from __future__ import annotations

import pytest

from video_summarizer.subtitle import vtt


def test_parses_vtt_and_strips_noise():
    content = """WEBVTT
Kind: captions
Language: zh-Hans

NOTE 这是注释
跨了两行

00:00:01.000 --> 00:00:04.500 align:start position:0%
<c>大家好</c>，欢迎收听&nbsp;本期节目

00:00:06.000 --> 00:00:09.250
今天聊聊 <v Speaker>AT&amp;T</v> 的财报
"""
    segs = vtt.parse(content)
    assert len(segs) == 2
    # 内联标签、HTML 实体、cue 设置都要处理掉
    assert segs[0].text == "大家好，欢迎收听 本期节目"
    assert segs[1].text == "今天聊聊 AT&T 的财报"
    assert segs[0].start == 1.0 and segs[0].end == 4.5


def test_merges_rolling_duplicate_lines():
    """滚动字幕会把同一句重复推送，合并成一条并延长结束时间。"""
    content = """WEBVTT

00:00:01.000 --> 00:00:04.000
大家好，欢迎收听

00:00:04.000 --> 00:00:06.000
大家好，欢迎收听
"""
    segs = vtt.parse(content)
    assert len(segs) == 1
    assert segs[0].start == 1.0 and segs[0].end == 6.0


def test_merges_prefix_growth():
    """逐字追加型的滚动字幕：后一条包含前一条，取更长的那条。"""
    content = """WEBVTT

00:00:01.000 --> 00:00:03.000
今天我们

00:00:03.000 --> 00:00:05.000
今天我们聊聊财报
"""
    segs = vtt.parse(content)
    assert len(segs) == 1
    assert segs[0].text == "今天我们聊聊财报"


def test_parses_srt_with_comma_timestamps():
    content = """1
00:00:00,500 --> 00:00:02,000
Hello there

2
00:01:03,250 --> 00:01:05,000
second line
"""
    segs = vtt.parse(content)
    assert [s.text for s in segs] == ["Hello there", "second line"]
    assert segs[1].start == pytest.approx(63.25)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("00:00:01.500", 1.5),
        ("01:02.500", 62.5),
        ("1:00:00.000", 3600.0),
        ("00:00:02,250", 2.25),
    ],
)
def test_timestamp_forms(raw, expected):
    assert vtt.parse_timestamp(raw) == pytest.approx(expected)


def test_rejects_garbage_timestamp():
    with pytest.raises(ValueError):
        vtt.parse_timestamp("不是时间戳")


def test_empty_input_yields_nothing():
    assert vtt.parse("WEBVTT\n\n") == []
