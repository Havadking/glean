"""SQLite 缓存（DESIGN.md 路线图 v0.5）。

重点是指纹的区分度：该命中的要命中，不该命中的绝不能命中 ——
拿着 sensevoice 的结果冒充 large-v3 的，比不缓存还糟。
"""

from __future__ import annotations

import pytest

from video_summarizer.cache import Cache, summary_key, transcript_key


@pytest.fixture
def cache(tmp_path):
    return Cache(tmp_path / "cache.sqlite")


# ---------- 指纹 ----------


def test_same_inputs_same_key():
    kw = dict(video_id="abc", extractor="Youtube", source_type="asr",
              asr_provider="funasr", asr_model="sensevoice-small", diarize=False)
    assert transcript_key(**kw) == transcript_key(**kw)


@pytest.mark.parametrize("field,value", [
    ("video_id", "other"),
    ("extractor", "BiliBili"),
    ("asr_provider", "whisper"),
    ("asr_model", "large-v3"),
    ("asr_language", "en"),
    ("diarize", True),
])
def test_changing_any_asr_input_changes_the_key(field, value):
    base = dict(video_id="abc", extractor="Youtube", source_type="asr",
                asr_provider="funasr", asr_model="sensevoice-small",
                asr_language=None, diarize=False)
    assert transcript_key(**base) != transcript_key(**{**base, field: value})


def test_subtitle_and_asr_never_collide():
    assert transcript_key(video_id="a", extractor="Youtube", source_type="subtitle",
                          subtitle_language="en") != \
           transcript_key(video_id="a", extractor="Youtube", source_type="asr",
                          asr_provider="funasr", asr_model="sensevoice-small")


def test_subtitle_language_matters():
    a = transcript_key(video_id="a", extractor="Youtube", source_type="subtitle",
                       subtitle_language="en")
    b = transcript_key(video_id="a", extractor="Youtube", source_type="subtitle",
                       subtitle_language="zh")
    assert a != b


def test_title_change_does_not_change_the_key():
    """视频改了标题、输出目录换了名字，转写内容不变 —— 该命中。"""
    kw = dict(video_id="abc", extractor="Youtube", source_type="subtitle",
              subtitle_language="en")
    assert transcript_key(**kw) == transcript_key(**kw)


@pytest.mark.parametrize("field,value", [
    ("provider_desc", "anthropic/claude-opus-5"),
    ("summary_type", "timeline"),
    ("language", "en"),
    ("extra", "只讲技术"),
])
def test_changing_any_summary_input_changes_the_key(field, value):
    base = dict(transcript_key="t1", provider_desc="api.deepseek.com/deepseek-chat",
                summary_type="overall", language="zh", extra=None)
    assert summary_key(**base) != summary_key(**{**base, field: value})


def test_summary_key_follows_the_transcript():
    base = dict(provider_desc="p", summary_type="overall", language="zh", extra=None)
    assert summary_key(transcript_key="t1", **base) != summary_key(transcript_key="t2", **base)


# ---------- 存取 ----------


def test_transcript_round_trip(cache, dialogue):
    cache.put_transcript("k1", dialogue)
    got = cache.get_transcript("k1")
    assert got is not None
    assert got.title == dialogue.title
    assert got.speakers == ["Speaker_1", "Speaker_2"]
    assert [s.text for s in got.segments] == [s.text for s in dialogue.segments]
    assert got.segments[0].speaker == "Speaker_1"


def test_miss_returns_none(cache):
    assert cache.get_transcript("没存过") is None
    assert cache.get_summary("没存过") is None


def test_summary_round_trip(cache, transcript):
    cache.put_summary("s1", transcript_key="k1", transcript=transcript,
                      provider_desc="p/m", summary_type="overall",
                      language="zh", content="# 总结正文")
    assert cache.get_summary("s1") == "# 总结正文"


def test_reput_overwrites(cache, transcript):
    cache.put_transcript("k1", transcript)
    transcript.title = "改了标题"
    cache.put_transcript("k1", transcript)
    entries = cache.list_transcripts()
    assert len(entries) == 1 and entries[0].title == "改了标题"


# ---------- 管理 ----------


def test_listing_reports_shape(cache, transcript, dialogue):
    cache.put_transcript("k1", transcript)
    cache.put_transcript("k2", dialogue)
    by_key = {e.key: e for e in cache.list_transcripts()}
    assert by_key["k1"].has_speakers is False
    assert by_key["k2"].has_speakers is True
    assert by_key["k1"].segment_count == len(transcript.segments)


def test_stats(cache, transcript):
    cache.put_transcript("k1", transcript)
    cache.put_summary("s1", transcript_key="k1", transcript=transcript,
                      provider_desc="p/m", summary_type="overall",
                      language="zh", content="x")
    stats = cache.stats()
    assert stats["enabled"] and stats["transcripts"] == 1 and stats["summaries"] == 1


def test_clear_everything(cache, transcript):
    cache.put_transcript("k1", transcript)
    cache.put_summary("s1", transcript_key="k1", transcript=transcript,
                      provider_desc="p/m", summary_type="overall",
                      language="zh", content="x")
    assert cache.clear() == (1, 1)
    assert cache.stats()["transcripts"] == 0


def test_clear_one_video_only(cache, transcript, dialogue):
    cache.put_transcript("k1", transcript)   # video_id=abc
    cache.put_transcript("k2", dialogue)     # video_id=talk
    cache.clear(video_id="abc")
    remaining = [e.video_id for e in cache.list_transcripts()]
    assert remaining == ["talk"]


# ---------- 降级 ----------


def test_disabled_cache_is_a_no_op(transcript):
    cache = Cache(None)
    assert not cache.enabled
    cache.put_transcript("k1", transcript)      # 不该抛
    assert cache.get_transcript("k1") is None
    assert cache.stats() == {"enabled": False}


def test_broken_cache_file_degrades_to_miss(tmp_path, transcript, caplog):
    """缓存坏了只该少一层加速，不该让整条流程失败。"""
    broken = tmp_path / "cache.sqlite"
    broken.write_bytes(b"this is definitely not a sqlite database" * 50)
    cache = Cache(broken)
    with caplog.at_level("WARNING"):
        assert cache.get_transcript("k1") is None
        cache.put_transcript("k1", transcript)


# ---------- 词表 ----------


def _c(src, dst, hits=2, state="applied"):
    from video_summarizer.correction import Correction
    return Correction(src, dst, "why", hits, state)


def test_terms_accumulate_per_uploader_and_share_within_group(cache):
    cache.learn_terms("A", [_c("英派", "鹰派"), _c("哥派", "鸽派", state="rejected")], video_id="v1")
    cache.learn_terms("A", [_c("英派", "鹰派", hits=5)], video_id="v2")
    cache.learn_terms("A", [_c("英派", "鹰派", hits=9)], video_id="v2")   # 同一条视频重跑不重复算
    cache.learn_terms("B", [_c("英派", "鹰派", hits=1), _c("一息", "议息")], video_id="v3")

    a = {(t.src, t.dst): (t.hits, t.videos, t.state) for t in cache.get_terms("A")}
    assert a == {("英派", "鹰派"): (7, 2, "applied"), ("哥派", "鸽派"): (2, 1, "rejected")}
    assert cache.hotwords("A") == ["鹰派"]
    assert cache.term_scope("A") == ["A"]

    # 分到同一组后互相看得见，命中数合并；A 否决过的在 B 那边也算否决
    cache.set_uploader_group("A", "财经")
    cache.set_uploader_group("B", "财经")
    assert cache.term_scope("B") == ["A", "B"]
    b = {(t.src, t.dst): (t.hits, t.state) for t in cache.get_terms("B")}
    assert b == {("英派", "鹰派"): (8, "applied"), ("一息", "议息"): (2, "applied"), ("哥派", "鸽派"): (2, "rejected")}
    assert cache.hotwords("B") == ["鹰派", "议息"]

    # 否决/恢复/删除
    cache.set_term_state("B", "一息", "议息", "rejected")
    assert cache.hotwords("B") == ["鹰派"]
    cache.set_term_state("B", "一息", "议息", "applied")
    assert cache.delete_term("B", "一息", "议息") and cache.hotwords("B") == ["鹰派"]


def test_terms_without_uploader_share_one_bucket(cache):
    cache.learn_terms(None, [_c("a1", "b1")])
    assert cache.term_scope(None) == [""] and cache.hotwords("") == ["b1"]
    assert cache.get_terms("某人") == []


def test_terms_survive_clear(cache, transcript):
    cache.put_transcript("k", transcript)
    cache.learn_terms("A", [_c("英派", "鹰派")])
    cache.clear()
    assert cache.hotwords("A") == ["鹰派"]
