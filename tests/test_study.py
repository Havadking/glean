"""伴读：底稿两遍生成与合并、提问的前缀稳定性、检索工具循环。全部用假 provider，不碰网络。"""

from __future__ import annotations

import json

import pytest

from video_summarizer import study
from video_summarizer.config import SummarizerConfig, WebSearchConfig
from video_summarizer.errors import SummarizerError
from video_summarizer.models import Segment, Transcript
from video_summarizer.qa import Turn
from video_summarizer.summarizer.base import BaseSummarizer, ChatReply, ToolCall
from video_summarizer.websearch import SearchResult, WebSearcher, WebSearchError

GROUNDED = {
    "chapters": [
        {"start": "00:05", "title": "交易怎么拆", "claim": "总包是营销口径",
         "evidence": [{"text": "首付款不到 5%", "t": "00:20"}, {"text": "", "t": "00:30"}],
         "conclusion": "先看首付款占比"},
        {"start": "01:00", "title": "NewCo", "claim": "股权换估值",
         "evidence": [{"text": "美元基金出钱", "t": "1:05"}], "conclusion": "赌下一轮"},
        {"start": "bad", "title": "坏的"},
    ],
    "terms": [
        {"term": "首付款", "en": "Upfront", "video_says": "签约就付的钱", "t": "00:20"},
        {"term": "NewCo", "en": "", "video_says": "新设的公司", "t": "01:05"},
        {"term": "瞎编的词", "en": "", "video_says": "", "t": ""},
    ],
}
BACKGROUND = {
    "prerequisites": [
        {"term": "License-out", "explain": "对外授权", "chapter": 1},
        {"term": "超范围章节", "explain": "x", "chapter": 9},
    ],
    # 键的写法故意和术语表不完全一样：大小写不同、带了括号里的全称
    "backgrounds": {"首付款（Upfront）": "签约即付的确定对价", "newco": "新公司模式"},
}


@pytest.fixture
def talk() -> Transcript:
    texts = ["今天聊创新药出海。", "先看交易怎么拆。", "首付款一般不到总包的百分之五。", "首付款是确定的钱。",
             "里程碑就不一定了。", "下面讲 NewCo。", "NewCo 是新设的海外公司。", "美元基金出钱。"]
    segs = [Segment(i * 15.0, i * 15.0 + 15.0, t) for i, t in enumerate(texts)]
    return Transcript("file:///x.mp4", "asr", "zh", 120.0, segs, title="出海复盘", video_id="local-x")


class Scripted(BaseSummarizer):
    """按 system prompt 分辨是哪一步，回固定 JSON；记下每次收到的东西。"""

    name = "scripted"

    def __init__(self, cfg=None, grounded=None, background=None):
        super().__init__(cfg or SummarizerConfig(max_context_tokens=100_000, max_output_tokens=4000))
        self.grounded = grounded or [GROUNDED]
        self.background = background or BACKGROUND
        self.seen: list[tuple[str, str]] = []

    def _complete(self, system: str, user: str) -> str:
        self.seen.append((system, user))
        if system == study.GROUNDED_SYSTEM:
            return "```json\n" + json.dumps(self.grounded[min(len(self.seen) - 1, len(self.grounded) - 1)],
                                             ensure_ascii=False) + "\n```"
        if system == study.BACKGROUND_SYSTEM:
            return "好的：" + json.dumps(self.background, ensure_ascii=False)
        return "### 视频里说\n- 首付款不到 5% [00:30]\n### 背景补充\n- 期权式交易"


def test_parse_ts_and_json():
    assert study.parse_ts("37:05") == 2225 and study.parse_ts("1:02:03") == 3723
    assert study.parse_ts(12) == 12.0 and study.parse_ts("bad") is None and study.parse_ts(None) is None
    assert study.parse_json_object('前言 {"a": 1} 后记') == {"a": 1}
    with pytest.raises(SummarizerError):
        study.parse_json_object("没有 JSON")


def test_generate_sheet_two_passes_and_merge(talk):
    p = Scripted()
    progress: list[str] = []
    sheet = study.generate_sheet(p, talk, uploader="深水区", progress=progress.append)

    # 第一遍带转写、第二遍不带
    assert "[00:30] 首付款一般不到总包的百分之五。" in p.seen[0][1]
    assert p.seen[1][0] == study.BACKGROUND_SYSTEM and "[00:30]" not in p.seen[1][1]
    assert "交易怎么拆——总包是营销口径" in p.seen[1][1]
    assert progress == ["整理章节和术语（1/1）", "补背景知识"]

    # 章节：坏时间戳的丢掉，第一章从 0 开始，end 接下一章，最后一章到片尾
    assert [(c.title, c.start, c.end) for c in sheet.chapters] == [("交易怎么拆", 0.0, 60.0), ("NewCo", 60.0, 120.0)]
    assert [e.text for e in sheet.chapters[0].evidence] == ["首付款不到 5%"]
    assert sheet.chapters[1].evidence[0].t == 65

    # 术语：转写里没出现过、也没给位置的丢掉；背景按小写对上
    terms = {t.term: t for t in sheet.glossary}
    assert set(terms) == {"首付款", "NewCo"}
    assert terms["首付款"].mentions == 2 and terms["首付款"].positions == [30.0, 45.0]
    assert terms["首付款"].background == "签约即付的确定对价" and terms["NewCo"].background == "新公司模式"

    # 前置知识：超出章节数的 chapter 置空
    assert [(x.term, x.chapter) for x in sheet.prerequisites] == [("License-out", 1), ("超范围章节", None)]

    # 来回序列化不丢东西
    again = study.StudySheet.from_dict(json.loads(json.dumps(sheet.to_dict())))
    assert again == sheet and again.chapter_at(70) == 1 and again.chapter_at(0) == 0


def test_generate_sheet_chunks_long_transcript():
    segs = [Segment(i * 10.0, i * 10.0 + 10.0, "这是一句比较长的话，用来把转写撑长。" * 4) for i in range(120)]
    t = Transcript("u", "asr", "zh", 1200.0, segs, title="长", video_id="long")
    part1 = {"chapters": [{"start": "00:00", "title": "前半", "claim": "a", "evidence": []}], "terms": []}
    part2 = {"chapters": [{"start": "10:00", "title": "后半", "claim": "b", "evidence": []}], "terms": []}
    p = Scripted(SummarizerConfig(max_context_tokens=8000, max_output_tokens=1000, chunk_tokens=1500),
                 grounded=[part1, part2])
    plan = study.plan_sheet(p, t)
    assert plan["chunks"] >= 2 and plan["calls"] == plan["chunks"] + 1
    sheet = study.generate_sheet(p, t)
    assert "第 1/" in p.seen[0][1] and "部分" in p.seen[0][1]
    assert [c.title for c in sheet.chapters] == ["前半", "后半"]


def test_generate_sheet_without_chapters_fails(talk):
    p = Scripted(grounded=[{"chapters": [], "terms": []}])
    with pytest.raises(SummarizerError):
        study.generate_sheet(p, talk)


def _sheet(talk) -> study.StudySheet:
    return study.generate_sheet(Scripted(), talk)


def test_prefix_is_stable_across_questions(talk):
    """固定前缀不能随问题、位置、历史变化，否则吃不到前缀缓存。"""
    p, sheet = Scripted(), _sheet(talk)
    m1, mode = study.build_messages(p, talk, sheet, question="这里在说什么", position=30, history=[],
                                    spoiler_guard=False, uploader="深水区")
    m2, _ = study.build_messages(p, talk, sheet, question="为什么", position=95,
                                 history=[(Turn("前一问", "前一答"), 30.0)], spoiler_guard=True, uploader="深水区")
    assert mode == "full"
    assert m1[:3] == m2[:3]
    assert m1[0]["content"] == study.QA_SYSTEM and "=== 全片转写开始 ===" in m1[1]["content"]
    assert "第1章 [00:00–01:00] 交易怎么拆" in m1[1]["content"]

    tail = m2[-1]["content"]
    assert tail.startswith("当前位置：[01:35]，第 2 章「NewCo」")
    assert "防剧透" in tail and tail.rstrip().endswith("问题：为什么")
    assert m2[3] == {"role": "user", "content": "[我看到 00:30] 前一问"}
    assert m2[4] == {"role": "assistant", "content": "前一答"}
    assert "防剧透" not in m1[-1]["content"]


def test_outline_mode_when_transcript_too_long(talk):
    segs = [Segment(i * 10.0, i * 10.0 + 10.0, "很长很长的一句话。" * 20) for i in range(300)]
    t = Transcript("u", "asr", "zh", 3000.0, segs, title="超长", video_id="x")
    p = Scripted(SummarizerConfig(max_context_tokens=30000, max_output_tokens=2000))
    sheet = study.StudySheet(chapters=[study.Chapter(0, 1000, "一"), study.Chapter(1000, 2000, "二"),
                                       study.Chapter(2000, 3000, "三")])
    msgs, mode = study.build_messages(p, t, sheet, question="q", position=1500, history=[],
                                      spoiler_guard=False, uploader=None)
    assert mode == "outline" and "全片转写放不下" in msgs[1]["content"]
    assert "=== 全片转写开始 ===" not in msgs[1]["content"]
    assert "[25:00]" in msgs[-1]["content"]
    assert study.plan_ask(p, t, sheet)["context_mode"] == "outline"


def test_ask_without_tools_uses_plain_chat(talk):
    p = Scripted()
    a = study.ask(p, talk, _sheet(talk), question=" 首付款为什么低？ ", position=40)
    assert a.question == "首付款为什么低？" and a.citations == [30] and a.chapter == 0
    assert a.sources == [] and a.searches == [] and a.context_mode == "full"
    system, user = p.seen[-1]
    assert system == study.QA_SYSTEM and user.rstrip().endswith("问题：首付款为什么低？")


class ToolUser(Scripted):
    supports_tools = True

    def __init__(self, rounds_wanting_tools=1):
        super().__init__()
        self.rounds_wanting_tools = rounds_wanting_tools
        self.chats: list[tuple[list, list | None]] = []

    def chat(self, messages, tools=None):
        self.chats.append(([dict(m) for m in messages], tools))
        if tools and len(self.chats) <= self.rounds_wanting_tools:
            n = len(self.chats)
            return ChatReply(content="", tool_calls=[ToolCall(f"c{n}", "web_search", {"query": f"查询{n}"})],
                             message={"role": "assistant", "content": "", "tool_calls": [{"id": f"c{n}"}]})
        return ChatReply(content="### 背景补充\n- 统计在 5%–15% 〔2〕", message={})


class FakeSearcher(WebSearcher):
    def __init__(self, max_calls=3, fail=False):
        super().__init__(WebSearchConfig(max_calls=max_calls))
        self.fail = fail

    @property
    def enabled(self) -> bool:
        return True

    def search(self, query):
        self.calls += 1
        if self.fail:
            raise WebSearchError("没网")
        return [SearchResult(f"{query}-a", f"https://a/{query}", "内容a"),
                SearchResult(f"{query}-b", f"https://b/{query}", "内容b")]


def test_ask_runs_search_tool_and_numbers_sources(talk):
    p, s = ToolUser(rounds_wanting_tools=2), FakeSearcher()
    a = study.ask(p, talk, None, question="现在的统计是多少", position=10, searcher=s)
    assert a.searches == ["查询1", "查询2"] and s.calls == 2
    assert [x["n"] for x in a.sources] == [1, 2, 3, 4] and a.sources[2]["url"] == "https://a/查询2"
    # 第二轮的检索结果编号接着第一轮往下排
    second_round_msgs = p.chats[2][0]
    tool_msgs = [m for m in second_round_msgs if m["role"] == "tool"]
    assert tool_msgs[0]["content"].startswith("〔1〕查询1-a") and tool_msgs[1]["content"].startswith("〔3〕查询2-a")
    assert "〔2〕" in a.answer


def test_ask_caps_search_rounds(talk):
    p = ToolUser(rounds_wanting_tools=99)
    a = study.ask(p, talk, None, question="q", position=0, searcher=FakeSearcher(max_calls=2))
    assert len(a.searches) == 2
    assert p.chats[-1][1] is None   # 最后一轮不再给工具，逼它作答


def test_ask_survives_search_failure(talk):
    p = ToolUser(rounds_wanting_tools=1)
    a = study.ask(p, talk, None, question="q", position=0, searcher=FakeSearcher(fail=True))
    assert a.sources == [] and a.searches == ["查询1"]
    tool_msg = [m for m in p.chats[1][0] if m["role"] == "tool"][0]
    assert "检索失败" in tool_msg["content"]


def test_ask_rejects_empty_question(talk):
    with pytest.raises(ValueError):
        study.ask(Scripted(), talk, None, question="  ", position=0)


def test_websearcher_caches_per_day(tmp_path, monkeypatch):
    from video_summarizer.cache import Cache

    cache = Cache(tmp_path / "c.sqlite")
    s = WebSearcher(WebSearchConfig(), cache)
    fetched: list[str] = []
    monkeypatch.setattr(s, "_fetch", lambda q: fetched.append(q) or [SearchResult("t", "https://u", "c")])
    assert s.search("  ADC   授权 ")[0].url == "https://u"
    assert s.search("ADC 授权")[0].title == "t"
    assert fetched == ["ADC 授权"] and s.calls == 2 and s.paid_calls == 1
    assert s.search("   ") == []


class Flaky(Scripted):
    """第一次第一遍调用回一段被截断的 JSON（模拟撞上输出上限），之后正常。"""

    def __init__(self, bad_background=False):
        super().__init__()
        self.bad_background = bad_background
        self.broke = False

    def _complete(self, system, user):
        if system == study.GROUNDED_SYSTEM and not self.broke:
            self.broke = True
            self.seen.append((system, user))
            return '{"chapters": [{"start": "00:05", "title": "截断'
        if system == study.BACKGROUND_SYSTEM and self.bad_background:
            self.seen.append((system, user))
            return '{"prerequisites": [ 坏掉的'
        return super()._complete(system, user)


def test_truncated_chunk_is_split_and_retried(talk):
    p = Flaky()
    sheet = study.generate_sheet(p, talk)
    grounded = [u for s, u in p.seen if s == study.GROUNDED_SYSTEM]
    assert len(grounded) == 3                       # 坏一次 + 前后两半
    assert "（前半）" in grounded[1] and "（后半）" in grounded[2]
    assert "[00:00]" in grounded[1] and "[00:00]" not in grounded[2]
    assert sheet.chapters


def test_background_failure_keeps_chapters(talk):
    p = Flaky(bad_background=True)
    p.broke = True                                   # 第一遍正常
    sheet = study.generate_sheet(p, talk)
    assert sheet.chapters and sheet.glossary
    assert sheet.prerequisites == [] and all(t.background == "" for t in sheet.glossary)


def test_three_hour_transcript_is_chunked():
    # 约 51k token：实测把整篇一次塞进去，输出的 JSON 被 8k 上限截断
    segs = [Segment(i * 6.0, i * 6.0 + 6.0, "这是一句三十个字左右的口语转写内容用来模拟长视频的。") for i in range(1900)]
    t = Transcript("u", "asr", "zh", 11400.0, segs, title="长", video_id="x")
    plan = study.plan_sheet(Scripted(SummarizerConfig(max_context_tokens=120_000, max_output_tokens=8000)), t)
    assert plan["transcript_tokens"] > 45_000 and plan["chunks"] >= 4
