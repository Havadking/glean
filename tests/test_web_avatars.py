"""UP 主头像：借哪个视频去问、问不到怎么换。不碰网络。"""

from __future__ import annotations

from pathlib import Path

from video_summarizer.config import Config
from video_summarizer.web import avatars
from video_summarizer.web.library import LibraryEntry


def _entry(video_id: str) -> LibraryEntry:
    return LibraryEntry(video_id=video_id, title=video_id, source_url=f"https://www.douyin.com/video/{video_id}",
                        duration_sec=1, source_type="asr", language="zh", segment_count=1, speaker_count=0,
                        created_at="2026-09-16T00:00:00", uploader="模型先生", meta={"extractor": "Douyin"})


def test_falls_through_to_next_video_when_one_is_private(tmp_path: Path, monkeypatch):
    cfg = Config(cache_db=tmp_path / "cache.sqlite")
    asked: list[str] = []

    def fake_fetch(c, name, entry):
        asked.append(entry.video_id)
        if entry.video_id == "private":
            raise RuntimeError("抖音详情接口没返回 aweme_detail（作品权限或已被删除，status_self_see）")
        p = tmp_path / "avatars" / "x.jpg"
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(b"jpg")
        return p

    monkeypatch.setattr(avatars, "_fetch_locked", fake_fetch)
    avatars._failed_at.clear()
    path = avatars.fetch(cfg, "模型先生", [_entry("private"), _entry("ok"), _entry("never")])
    assert path is not None and asked == ["private", "ok"]


def test_gives_up_after_max_entries_and_remembers_failure(tmp_path: Path, monkeypatch):
    cfg = Config(cache_db=tmp_path / "cache.sqlite")
    asked: list[str] = []
    monkeypatch.setattr(avatars, "_fetch_locked", lambda c, n, e: (asked.append(e.video_id), None)[1])
    avatars._failed_at.clear()
    entries = [_entry(str(i)) for i in range(6)]
    assert avatars.fetch(cfg, "模型先生", entries) is None
    assert asked == ["0", "1", "2"]
    # 一小时内不再打站点
    assert avatars.fetch(cfg, "模型先生", entries) is None
    assert asked == ["0", "1", "2"]
