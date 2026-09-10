"""共享 fixture。

这套测试**不碰网络、不加载模型**，几秒就能跑完 —— 目的是每次改动都能顺手跑一遍。
需要真实模型或网络的验证标了 `@pytest.mark.slow`，默认不跑。
"""

from __future__ import annotations

import pytest

from video_summarizer.models import Segment, Transcript


@pytest.fixture
def segments() -> list[Segment]:
    return [
        Segment(start=0.0, end=4.0, text="第一句，讲了个开头。"),
        Segment(start=4.0, end=9.5, text="第二句，展开说明具体的做法。"),
        Segment(start=9.5, end=15.0, text="第三句，给了个数字：一共 42 个。"),
    ]


@pytest.fixture
def transcript(segments) -> Transcript:
    return Transcript(
        source_url="https://example.com/watch?v=abc",
        source_type="asr",
        language="zh",
        duration_sec=15.0,
        segments=segments,
        title="测试视频",
        video_id="abc",
        meta={"asr_provider": "funasr", "asr_model": "sensevoice-small"},
    )


@pytest.fixture
def dialogue() -> Transcript:
    """带说话人标签的转写。"""
    return Transcript(
        source_url="https://example.com/watch?v=talk",
        source_type="asr",
        language="zh",
        duration_sec=30.0,
        segments=[
            Segment(start=0.0, end=8.0, text="你怎么看这件事？", speaker="Speaker_1"),
            Segment(start=8.0, end=20.0, text="我觉得没那么简单。", speaker="Speaker_2"),
            Segment(start=20.0, end=30.0, text="展开说说。", speaker="Speaker_1"),
        ],
        title="对话",
        video_id="talk",
        meta={"diarization": True},
    )


@pytest.fixture
def long_segments() -> list[Segment]:
    """够长，能触发 map-reduce。"""
    return [
        Segment(start=i * 5.0, end=i * 5.0 + 5.0, text=f"这是第 {i} 句话的内容，说了一些东西。")
        for i in range(200)
    ]
