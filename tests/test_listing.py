"""合集 / 空间列表：URL 识别、flat 结果解析、单视频直通。不碰网络。"""

from __future__ import annotations

from video_summarizer import listing
from video_summarizer.config import DownloadConfig
from video_summarizer.ytdlp_base import VideoInfo


def test_space_id_matches_bilibili_space_urls():
    assert listing.space_id("https://space.bilibili.com/156593006/video") == "156593006"
    assert listing.space_id("https://space.bilibili.com/156593006") == "156593006"
    assert listing.space_id("https://space.bilibili.com/156593006/upload/video?tid=0") == "156593006"
    assert listing.space_id("https://www.bilibili.com/video/BV1ZN411k7Aj/") is None
    assert listing.space_id("https://space.bilibili.com/156593006/lists/123?type=season") is None


def test_parse_length():
    assert listing._parse_length("03:27") == 207
    assert listing._parse_length("1:02:03") == 3723
    assert listing._parse_length("") is None and listing._parse_length("abc") is None


def test_flat_playlist_becomes_a_page():
    info = {
        "_type": "playlist", "title": "某合集", "webpage_url": "https://x/list", "playlist_count": 45,
        "uploader": "某人",
        "entries": [
            {"id": "a", "url": "https://x/a", "title": "第一集", "duration": 60, "thumbnails": [{"url": "http://t/a"}]},
            None,
            {"id": "b", "url": "https://x/b"},
            {"id": "", "url": "https://x/none"},
        ],
    }
    page = listing._page_from_flat(info, "https://x/list", page=2)
    assert page.kind == "playlist" and page.title == "某合集" and page.total == 45
    assert page.page == 2 and page.has_more is False          # 2*30 >= 45
    assert [e.video_id for e in page.entries] == ["a", "b"]
    assert page.entries[0].thumbnail == "http://t/a" and page.entries[0].uploader == "某人"
    assert page.entries[1].title == "b"                      # 没标题就用 id 顶着
    d = page.to_dict()
    assert d["has_more"] is False and len(d["entries"]) == 2


def test_has_more_without_total():
    page = listing.ListPage(kind="playlist", title="t", url="u", page=1, page_size=30, total=None,
                            entries=[listing.ListedVideo(str(i), f"u{i}", f"t{i}") for i in range(30)])
    assert page.has_more is True
    page.entries.pop()
    assert page.has_more is False


def test_probe_any_returns_video_info_for_single_video(monkeypatch):
    single = {"id": "BV1", "title": "单个", "duration": 12.0, "extractor_key": "BiliBili",
              "webpage_url": "https://www.bilibili.com/video/BV1", "uploader": "up"}
    monkeypatch.setattr(listing, "_flat_extract", lambda url, cfg, page: single)
    r = listing.probe_any("https://www.bilibili.com/video/BV1", DownloadConfig())
    assert isinstance(r, VideoInfo) and r.video_id == "BV1" and r.uploader == "up"
    assert listing.probe_listing("https://www.bilibili.com/video/BV1", DownloadConfig()) is None


def test_probe_any_routes_space_urls_to_space_api(monkeypatch):
    seen = {}

    def fake_space(mid, cfg, page, keyword):
        seen.update(mid=mid, page=page, keyword=keyword)
        return listing.ListPage(kind="space", title="x", url="u", page=page, page_size=30, total=0)

    monkeypatch.setattr(listing, "_bilibili_space", fake_space)
    r = listing.probe_any("https://space.bilibili.com/42/video", DownloadConfig(), page=3, keyword=" 护肤 ")
    assert isinstance(r, listing.ListPage) and r.kind == "space"
    assert seen == {"mid": "42", "page": 3, "keyword": "护肤"}


def test_backoff_retries_rate_limits_then_gives_up(monkeypatch):
    from yt_dlp.utils import ExtractorError

    from video_summarizer.errors import DownloadError

    monkeypatch.setattr(listing.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ExtractorError("HTTP Error 412: Precondition Failed")
        return "ok"

    assert listing._with_backoff("x", flaky, waits=(1, 1)) == "ok" and calls["n"] == 3

    def always():
        raise ExtractorError("HTTP Error 412: Precondition Failed")

    import pytest
    with pytest.raises(DownloadError, match="限流"):
        listing._with_backoff("x", always)

    def other():
        raise ExtractorError("Unsupported URL")

    with pytest.raises(DownloadError):
        listing._with_backoff("x", other)


def test_multipart_bilibili_entries_without_ids_are_kept():
    """B 站多 P 视频 flat 出来只有 url：id 按 yt-dlp 完整探测的格式 BVxxx_pN 推出来，标题用总标题加 P 号。"""
    info = {
        "_type": "playlist", "title": "某视频", "webpage_url": "https://www.bilibili.com/video/BV1zUh56RE8k/",
        "playlist_count": 2, "uploader": "某人",
        "entries": [
            {"_type": "url", "url": "https://www.bilibili.com/video/BV1zUh56RE8k?p=1", "ie_key": "BiliBili"},
            {"_type": "url", "url": "https://www.bilibili.com/video/BV1zUh56RE8k?p=2", "ie_key": "BiliBili"},
            {"_type": "url", "url": ""},
        ],
    }
    page = listing._page_from_flat(info, info["webpage_url"], page=1)
    assert [e.video_id for e in page.entries] == ["BV1zUh56RE8k_p1", "BV1zUh56RE8k_p2"]
    assert page.entries[1].title == "某视频 · P2" and page.entries[1].uploader == "某人"
    assert listing._flat_entry_id("https://www.youtube.com/watch?v=abc") is None   # 别的站不乱编
