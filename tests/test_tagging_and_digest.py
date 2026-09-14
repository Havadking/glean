"""标签和回顾的纯逻辑：标签解析归一化、周期计算、缓存里三种 source 的规则。不碰网络。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from video_summarizer import digest, tagging
from video_summarizer.cache import Cache


# ---------- 标签 ----------


def test_parse_tags_prefers_json_array_and_normalizes():
    assert tagging.parse_tags('好的，标签如下：["护肤", " #防晒 ", "Python", "护肤"]') == ["护肤", "防晒", "python"]


def test_parse_tags_falls_back_to_separators_and_caps_at_five():
    assert tagging.parse_tags("护肤、防晒，成分\n历史; 编程 | 投资, 多余的") == ["护肤", "防晒", "成分", "历史", "编程"]


def test_normalize_rejects_empty_and_too_long():
    assert tagging.normalize("  ") == ""
    assert tagging.normalize("#") == ""
    assert tagging.normalize("这" * 21) == ""
    assert tagging.normalize("「职场」") == "职场"


def test_prompt_carries_existing_vocab():
    p = tagging.build_prompt(title="t", uploader="u", kind="overall", text="正文", vocab=["护肤", "编程"])
    assert "已有标签（优先复用）：护肤、编程" in p and "内容总结" in p
    p2 = tagging.build_prompt(title="t", uploader=None, kind="transcript", text="正文", vocab=[])
    assert "库里还没有标签" in p2 and "转写开头" in p2


def test_cache_tag_sources(tmp_path: Path):
    cache = Cache(tmp_path / "c.sqlite")
    assert cache.set_ai_tags("v1", ["护肤", "防晒"]) == ["护肤", "防晒"]
    assert cache.add_tag("v1", "我的", "user") is True
    assert cache.add_tag("v1", "我的", "user") is False          # 已经有了
    assert [(t.tag, t.source) for t in cache.tags_for_video("v1")] == [("我的", "user"), ("护肤", "ai"), ("防晒", "ai")]

    # 删掉一个 AI 标签：变成 rejected，列表里不再出现，重新生成也不会回来
    assert cache.remove_tag("v1", "防晒") is True
    assert [t.tag for t in cache.tags_for_video("v1")] == ["我的", "护肤"]
    assert cache.set_ai_tags("v1", ["防晒", "成分"]) == ["成分"]
    assert [t.tag for t in cache.tags_for_video("v1")] == ["我的", "成分"]
    # 用户手动再加回来就算数
    assert cache.add_tag("v1", "防晒", "user") is True
    assert ("防晒", "user") in [(t.tag, t.source) for t in cache.tags_for_video("v1")]
    # 用户加的不会被 AI 重新生成覆盖，也不会重复写
    assert cache.set_ai_tags("v1", ["我的", "新"]) == ["新"]

    assert cache.videos_with_ai_tags() == {"v1"}
    cache.set_ai_tags("v2", ["防晒", "护肤"])
    assert cache.tag_counts()[0] == ("防晒", 2)     # v1 手动加的 + v2 AI 打的
    assert set(cache.tags_by_video()) == {"v1", "v2"}

    # 删视频连标签一起删
    cache.clear("v1")
    assert cache.tags_for_video("v1") == [] and cache.videos_with_ai_tags() == {"v2"}


# ---------- 回顾：周期 ----------


def test_week_key_and_range_are_iso_weeks():
    d = date(2026, 9, 14)   # 周一
    assert digest.period_key("week", d) == "2026-W38"
    assert digest.period_range("week", "2026-W38") == (date(2026, 9, 14), date(2026, 9, 20))
    assert digest.period_key("week", date(2026, 9, 20)) == "2026-W38"
    assert digest.period_key("week", date(2026, 9, 21)) == "2026-W39"
    assert digest.shift_key("week", "2026-W38", -1) == "2026-W37"
    assert digest.shift_key("week", "2026-W38", 1) == "2026-W39"
    assert digest.period_label("week", "2026-W38") == "9 月 14 日 – 20 日"
    assert digest.period_label("week", "2026-W40") == "9 月 28 日 – 10 月 4 日"


def test_day_key_and_range():
    assert digest.period_key("day", date(2026, 9, 14)) == "2026-09-14"
    assert digest.period_range("day", "2026-09-14") == (date(2026, 9, 14), date(2026, 9, 14))
    assert digest.shift_key("day", "2026-09-14", 1) == "2026-09-15"
    assert digest.period_label("day", "2026-09-14") == "9 月 14 日"


def test_local_date_handles_utc_and_naive():
    assert isinstance(digest.local_date("2026-09-13T20:00:00+00:00"), date)
    assert digest.local_date("2026-09-13T20:00:00") == date(2026, 9, 13)
    assert isinstance(digest.local_date("垃圾"), date)


def test_digest_prompt_and_citations():
    mats = [
        digest.Material(1, "a", "A 视频", "某 UP", "2026-09-13", ["护肤"], "overall", "讲了防晒。"),
        digest.Material(2, "b", "B 视频", None, "2026-09-14", [], "transcript", "讲了成分。"),
    ]
    p = digest.build_prompt("week", "2026-W38", mats)
    assert "收藏的视频数：2" in p and "【1】A 视频 · 某 UP（2026-09-13，标签：护肤）" in p and "【2】B 视频（2026-09-14）" in p

    class P:
        def complete(self, system, user):
            return "## 护肤\n防晒 【1】，成分 【2】【1】，瞎编 【9】。"
    text, cited = digest.generate(P(), "week", "2026-W38", mats)
    assert cited == ["a", "b"]


def test_cache_digest_roundtrip(tmp_path: Path):
    cache = Cache(tmp_path / "c.sqlite")
    assert cache.get_digest("week", "2026-W38") is None
    cache.put_digest("week", "2026-W38", ["a", "b"], "正文", "fake/model")
    d = cache.get_digest("week", "2026-W38")
    assert d is not None and d.video_ids == ["a", "b"] and d.content == "正文"
    cache.put_digest("week", "2026-W38", ["a"], "新正文", "fake/model")
    assert cache.get_digest("week", "2026-W38").content == "新正文"
    assert set(cache.digests_for("week")) == {"2026-W38"} and cache.digests_for("day") == {}
