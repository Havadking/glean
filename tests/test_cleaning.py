"""转写清洗：只删独立填充词，只折叠多字重复，时间轴不动。"""

from __future__ import annotations

from video_summarizer import cleaning
from video_summarizer.models import Segment, Transcript


def test_fillers_at_boundaries_are_removed():
    assert cleaning.clean_text("嗯，就是给大家推荐，呃，男生也可以用。") == "就是给大家推荐，男生也可以用。"
    assert cleaning.clean_text("呃就是这样。") == "就是这样。"
    # 后面紧跟正文的"那个那个"只当结巴折叠，不整个删掉（可能真在指代）
    assert cleaning.clean_text("那个那个喷雾，这个这个不行") == "那个喷雾，这个不行"
    assert cleaning.clean_text("那个那个，喷雾") == "喷雾"


def test_agreement_and_reduplication_are_kept():
    assert cleaning.clean_text("好的，嗯嗯。") == "好的，嗯嗯。"
    assert cleaning.clean_text("看看谢谢慢慢妮妮") == "看看谢谢慢慢妮妮"
    # 词中间的"嗯"不碰（虽然少见）
    assert cleaning.clean_text("我嗯了一声") == "我嗯了一声"


def test_multi_char_repeats_collapse():
    assert cleaning.clean_text("就是就是就是小分子") == "就是小分子"
    assert cleaning.clean_text("我觉得我觉得不行") == "我觉得不行"
    assert cleaning.clean_text("然后然后然后走了") == "然后走了"
    # 单字重复保留：可能是结巴也可能是正常词，分不清就不动
    assert cleaning.clean_text("我我给大家") == "我我给大家"


def test_punctuation_is_tidied_after_removal():
    assert cleaning.clean_text("东西，呃，就是") == "东西，就是"
    assert cleaning.clean_text("好吧，。") == "好吧。"
    assert cleaning.clean_text("，，好") == "好"
    assert cleaning.clean_text("呃") == ""


def test_segments_keep_timing_and_drop_empties():
    segs = [Segment(0.0, 1.0, "呃"), Segment(1.0, 2.5, "嗯，好的"), Segment(2.5, 3.0, "就是就是这样")]
    out = cleaning.clean_segments(segs)
    assert [(s.start, s.end, s.text) for s in out] == [(1.0, 2.5, "好的"), (2.5, 3.0, "就是这样")]
    t = Transcript("u", "asr", "zh", 3.0, segs, video_id="v")
    ct = cleaning.clean_transcript(t)
    assert ct.meta["cleaned"] is True and len(t.segments) == 3      # 原件不动
    assert 0 < cleaning.removed_ratio(segs) < 1
    assert cleaning.removed_ratio([]) == 0.0
