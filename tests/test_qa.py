"""问视频：prompt 构造、时间戳引用解析、截断、历史。"""

from __future__ import annotations

from video_summarizer import qa
from video_summarizer.config import SummarizerConfig
from video_summarizer.models import Segment, Transcript
from video_summarizer.summarizer.base import BaseSummarizer


class Echo(BaseSummarizer):
    """把收到的 prompt 记下来，回一个带时间戳的固定答案。"""

    name = "echo"

    def __init__(self, cfg=None, reply="推荐颐莲 [00:44] [08:03]，理由见 [08:03]。"):
        super().__init__(cfg or SummarizerConfig(max_context_tokens=100_000, max_output_tokens=4000))
        self.reply = reply
        self.seen: list[tuple[str, str]] = []

    def _complete(self, system: str, user: str) -> str:
        self.seen.append((system, user))
        return self.reply


def test_extract_citations_dedups_and_keeps_order():
    assert qa.extract_citations("a [00:44] b [08:03] c [00:44] d [1:02:03]") == [44, 483, 3723]
    assert qa.extract_citations("没有时间戳") == []


def test_ask_builds_prompt_with_timestamps_and_parses_answer(transcript):
    p = Echo()
    a = qa.ask(p, transcript, "  一共多少个？ ")
    system, user = p.seen[0]
    assert "只依据转写内容回答" in system and "时间戳" in system
    assert "[00:00] 第一句" in user and "[00:09] 第三句" in user
    assert user.rstrip().endswith("问题：一共多少个？")
    assert a.question == "一共多少个？"
    assert a.citations == [44, 483]
    assert a.truncated is False and a.provider == "echo/deepseek-chat"
    assert a.input_tokens > 0


def test_history_is_included_but_capped(transcript):
    p = Echo()
    history = [qa.Turn(f"问{i}", f"答{i}") for i in range(6)]
    qa.ask(p, transcript, "现在呢", history)
    _, user = p.seen[0]
    assert "问0" not in user and "问1" not in user     # 只带最近 4 轮
    assert "问2" in user and "答5" in user
    assert user.index("答5") < user.index("问题：现在呢")


def test_long_transcript_is_truncated_and_flagged():
    segs = [Segment(i * 5.0, i * 5.0 + 5.0, "这是一句很长很长的话，" * 10) for i in range(400)]
    t = Transcript("u", "asr", "zh", 2000.0, segs, title="长", video_id="long")
    p = Echo(SummarizerConfig(max_context_tokens=6000, max_output_tokens=1000))
    plan = qa.plan(p, t)
    assert plan["truncated"] is True and plan["input_tokens"] < plan["transcript_tokens"]
    a = qa.ask(p, t, "问")
    _, user = p.seen[0]
    assert a.truncated is True
    assert "后半部分没有包含" in user
    assert "[33:15]" not in user            # 最后一句没进去


def test_plan_reports_tokens_when_it_fits(transcript):
    plan = qa.plan(Echo(), transcript)
    assert plan["truncated"] is False
    assert plan["input_tokens"] == plan["transcript_tokens"] + 300


def test_empty_question_is_rejected(transcript):
    import pytest

    with pytest.raises(ValueError):
        qa.ask(Echo(), transcript, "   ")
