"""长文本策略：什么时候整篇塞进去，什么时候走 map-reduce（DESIGN.md 3.4）。

用假 provider，不发任何请求。
"""

from __future__ import annotations

import pytest

from video_summarizer.config import SummarizerConfig
from video_summarizer.errors import SummarizerError
from video_summarizer.models import SummaryOptions, Transcript
from video_summarizer.summarizer import prompts
from video_summarizer.summarizer.base import BaseSummarizer


class FakeSummarizer(BaseSummarizer):
    """记录每次请求，返回固定内容。"""

    name = "fake"

    def __init__(self, cfg: SummarizerConfig) -> None:
        super().__init__(cfg)
        self.calls: list[str] = []

    def _complete(self, system: str, user: str) -> str:
        self.calls.append(user)
        return f"结果 {len(self.calls)}"


def _long_transcript(long_segments) -> Transcript:
    return Transcript(
        source_url="https://example.com/x", source_type="asr", language="zh",
        duration_sec=1000.0, segments=long_segments, title="长视频", video_id="x",
    )


def test_short_transcript_goes_single_pass(transcript):
    s = FakeSummarizer(SummarizerConfig(max_context_tokens=100_000, max_output_tokens=4000))
    plan = s.plan(transcript, SummaryOptions())
    assert plan.strategy == "single"
    assert plan.chunks == 1

    s.summarize(transcript, SummaryOptions())
    assert len(s.calls) == 1


def test_long_transcript_goes_map_reduce(long_segments):
    t = _long_transcript(long_segments)
    s = FakeSummarizer(SummarizerConfig(
        max_context_tokens=3400, max_output_tokens=400, chunk_tokens=400,
    ))
    plan = s.plan(t, SummaryOptions())
    assert plan.strategy == "map-reduce"
    assert plan.chunks > 1

    s.summarize(t, SummaryOptions())
    # 每块一次 + 最后汇总一次
    assert len(s.calls) == plan.chunks + 1
    assert "各段要点" in s.calls[-1]


def test_chunk_strategy_never_forces_single(long_segments):
    t = _long_transcript(long_segments)
    s = FakeSummarizer(SummarizerConfig(
        max_context_tokens=3400, max_output_tokens=400, chunk_strategy="never",
    ))
    assert s.plan(t, SummaryOptions()).strategy == "single"


def test_chunk_strategy_always_forces_map_reduce(transcript):
    s = FakeSummarizer(SummarizerConfig(
        max_context_tokens=100_000, max_output_tokens=4000, chunk_strategy="always",
    ))
    assert s.plan(transcript, SummaryOptions()).strategy == "map-reduce"


def test_unknown_chunk_strategy_is_rejected(transcript):
    s = FakeSummarizer(SummarizerConfig(chunk_strategy="有时候"))
    with pytest.raises(SummarizerError, match="chunk_strategy"):
        s.plan(transcript, SummaryOptions())


def test_impossible_budget_is_rejected(transcript):
    """输出额度比上下文还大，说明配置写错了，早点报出来。"""
    s = FakeSummarizer(SummarizerConfig(max_context_tokens=1000, max_output_tokens=5000))
    with pytest.raises(SummarizerError, match="max_context_tokens"):
        s.plan(transcript, SummaryOptions())


def test_empty_transcript_is_rejected():
    t = Transcript(source_url="u", source_type="asr", language="zh",
                   duration_sec=0.0, segments=[])
    s = FakeSummarizer(SummarizerConfig())
    with pytest.raises(SummarizerError, match="空"):
        s.summarize(t, SummaryOptions())


def test_timeline_type_always_includes_timestamps(transcript):
    s = FakeSummarizer(SummarizerConfig())
    s.summarize(transcript, SummaryOptions(summary_type="timeline"))
    assert "[00:00]" in s.calls[0]


def test_speakers_appear_in_prompt_header(dialogue):
    s = FakeSummarizer(SummarizerConfig())
    s.summarize(dialogue, SummaryOptions(summary_type="by_speaker"))
    assert "说话人标签：Speaker_1, Speaker_2" in s.calls[0]
    assert "Speaker_1: 你怎么看这件事？" in s.calls[0]


def test_extra_instructions_reach_the_prompt(transcript):
    s = FakeSummarizer(SummarizerConfig())
    s.summarize(transcript, SummaryOptions(extra_instructions="只讲技术细节"))
    assert "只讲技术细节" in s.calls[0]


@pytest.mark.parametrize("key", list(prompts.TEMPLATES))
def test_every_template_renders(key, transcript):
    s = FakeSummarizer(SummarizerConfig())
    s.summarize(transcript, SummaryOptions(summary_type=key))
    assert s.calls and transcript.title in s.calls[0]


def test_unknown_template_is_rejected(transcript):
    s = FakeSummarizer(SummarizerConfig())
    with pytest.raises(Exception, match="未知的总结类型"):
        s.summarize(transcript, SummaryOptions(summary_type="随便写的"))


def test_system_prompt_forbids_outside_knowledge():
    """实测过模型会从标题认出视频然后编背景知识，这条约束不能被改没了。"""
    assert "只依据转写内容作答" in prompts.SYSTEM
    assert "上传者" in prompts.SYSTEM
