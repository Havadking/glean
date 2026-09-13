"""全文搜索：中文按字切、拉丁前缀、摘录、和库同步。"""

from __future__ import annotations

import pytest

from video_summarizer import search
from video_summarizer.models import Segment, Transcript


# ---------- 切词与查询 ----------


def test_tokenize_splits_cjk_and_keeps_latin_words():
    assert search.tokenize("防晒棒") == "防 晒 棒"
    assert search.tokenize("用 SenseVoice 识别") == "用 SenseVoice 识 别"
    assert search.tokenize("A/B test") == "A/B test"


def test_build_query_makes_phrases_and_prefixes_latin():
    assert search.build_query("防晒") == '"防 晒"'
    assert search.build_query("防晒 冬天") == '"防 晒" "冬 天"'
    assert search.build_query("elephant") == '"elephant"*'
    assert search.build_query("甘油 v3") == '"甘 油" "v3"*'


def test_build_query_drops_quotes_and_punctuation():
    assert search.build_query('"甘油"') == '"甘 油"'
    assert search.build_query("，。") is None
    assert search.build_query("   ") is None


def test_snippet_centers_on_first_hit():
    text = "前面" * 30 + "关键词" + "后面" * 30
    snip = search.make_snippet(text, "关键词", context=5)
    assert snip.startswith("…") and snip.endswith("…")
    assert "关键词" in snip and len(snip) == 2 + 5 + 3 + 5
    assert search.make_snippet("短句", "关键词") == "短句"


# ---------- 索引 ----------


def _t(vid: str, *texts: str) -> Transcript:
    segs = [Segment(i * 70.0, i * 70.0 + 60.0, t) for i, t in enumerate(texts)]
    return Transcript("u", "asr", "zh", 70.0 * len(texts), segs, title=vid, video_id=vid)


@pytest.fixture
def index(tmp_path):
    return search.SearchIndex(tmp_path / "cache.sqlite")


def test_index_and_search_chinese_short_words(index):
    index.index_video("v1", _t("v1", "今天推荐一个二十块的护肤甘油。", "这段没有关键词。"))
    hits = index.search("甘油")
    assert len(hits) == 1
    assert hits[0].kind == "transcript" and hits[0].start == 0.0
    assert "甘油" in hits[0].snippet
    # 一个字也能搜
    assert [h.start for h in index.search("段")] == [70.0]


def test_phrase_must_be_adjacent(index):
    index.index_video("v1", _t("v1", "甘草和油"))
    assert index.search("甘油") == []


def test_multiple_words_are_anded(index):
    index.index_video("v1", _t("v1", "冬天要防晒", "夏天也要防晒", "冬天很冷"))
    assert [h.start for h in index.search("防晒 冬天")] == [0.0]


def test_latin_prefix_and_case_insensitive(index):
    index.index_video("v1", _t("v1", "in front of the Elephants"))
    assert len(index.search("elephant")) == 1
    assert len(index.search("ELEPHANTS")) == 1
    assert index.search("lephant") == []


def test_summaries_are_indexed_per_line_and_ordered_after_transcript(index):
    index.index_video("v1", _t("v1", "转写里有蜂花"), [("overall", "## 推荐\n- 蜂花洗发水\n- 别的"),
                                                    ("mindmap", "# 根\n## 蜂花")])
    hits = index.search("蜂花")
    assert [h.kind for h in hits] == ["transcript", "summary", "summary"]
    assert {h.ref for h in hits if h.kind == "summary"} == {"overall", "mindmap"}
    # 行首的 Markdown 标记不进正文
    assert all(not h.text.startswith(("#", "-")) for h in hits)


def test_reindex_replaces_and_remove_deletes(index):
    index.index_video("v1", _t("v1", "旧内容"))
    index.index_video("v1", _t("v1", "新内容"))
    assert index.search("旧内容") == [] and len(index.search("新内容")) == 1
    index.index_video("v2", _t("v2", "另一个"))
    assert index.indexed_videos() == {"v1", "v2"}
    index.remove_video("v1")
    assert index.indexed_videos() == {"v2"}
    index.clear()
    assert index.count() == 0


def test_search_can_be_limited_to_one_video(index):
    index.index_video("v1", _t("v1", "共同词"))
    index.index_video("v2", _t("v2", "共同词"))
    assert len(index.search("共同词")) == 2
    assert [h.video_id for h in index.search("共同词", video_id="v2")] == ["v2"]


def test_disabled_index_is_a_noop():
    idx = search.SearchIndex(None)
    assert idx.index_video("v", _t("v", "x")) == 0
    assert idx.search("x") == [] and idx.count() == 0


# ---------- 和库同步 ----------


def test_sync_index_fills_missing_and_force_rebuilds(tmp_path, monkeypatch):
    from video_summarizer.cache import Cache, transcript_key
    from video_summarizer.config import Config

    cfg = Config(output_dir=tmp_path / "out", cache_db=tmp_path / "cache.sqlite")
    cache = Cache(cfg.cache_db)
    for vid, text in (("a", "苹果"), ("b", "香蕉")):
        t = _t(vid, text)
        key = transcript_key(video_id=vid, extractor="x", source_type="asr", asr_provider="p",
                             asr_model="m", asr_language=None, diarize=False)
        t.meta["cache_key"] = key
        cache.put_transcript(key, t)

    assert search.sync_index(cfg) == (2, 2)
    assert search.sync_index(cfg) == (0, 0)          # 都有了
    idx = search.SearchIndex(cfg.cache_db)
    assert [h.video_id for h in idx.search("香蕉")] == ["b"]

    # 加一条总结后 index_one 只更新那一个
    key_a = [k for k in idx.indexed_videos()]
    cache.put_summary("s", transcript_key=cache.list_transcripts()[0].key, transcript=_t("a", "x"),
                      provider_desc="p", summary_type="overall", language="zh", content="- 苹果很甜")
    assert search.index_one(cfg, "a") == 2
    assert [h.kind for h in idx.search("苹果")] == ["transcript", "summary"]
    assert len(key_a) == 2

    assert search.sync_index(cfg, force=True) == (2, 3)
    assert search.index_one(cfg, "没有这个") == 0
