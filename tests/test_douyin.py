"""抖音浏览器兜底：URL 识别、触发条件、cookie 合并、探测/下载的接管。不碰网络、不开浏览器。"""

from __future__ import annotations

from pathlib import Path

import pytest
from yt_dlp.utils import DownloadError as YTDLPDownloadError

from video_summarizer import douyin, ytdlp_base
from video_summarizer.config import DownloadConfig
from video_summarizer.errors import DownloadError
from video_summarizer.ytdlp_base import VideoInfo


def test_url_and_id_recognition():
    assert douyin.is_douyin_url("https://www.douyin.com/video/7685008180196722164")
    assert douyin.is_douyin_url("https://v.douyin.com/abc123/")
    assert douyin.is_douyin_url("https://www.iesdouyin.com/share/video/7685008180196722164/")
    assert not douyin.is_douyin_url("https://www.bilibili.com/video/BV1x")
    assert not douyin.is_douyin_url("https://notdouyin.com/video/123")

    assert douyin.video_id_from_url("https://www.douyin.com/video/7685008180196722164?x=1") == "7685008180196722164"
    assert douyin.video_id_from_url("https://www.douyin.com/note/7685008180196722164") == "7685008180196722164"
    assert douyin.video_id_from_url("https://www.iesdouyin.com/share/video/7685008180196722164/") == "7685008180196722164"
    assert douyin.video_id_from_url("https://v.douyin.com/abc123/") is None


def test_trigger_conditions():
    assert douyin.needs_browser(Exception("ERROR: [Douyin] 1: Fresh cookies (not necessarily logged in) are needed"))
    assert douyin.needs_browser(Exception("Unable to download JSON metadata: HTTP Error 403: Forbidden"))
    assert not douyin.needs_browser(Exception("HTTP Error 412: Precondition Failed"))
    assert not douyin.needs_browser(Exception("Video unavailable"))

    assert douyin.enabled(DownloadConfig())
    assert douyin.enabled(DownloadConfig(douyin_browser="chrome"))
    for off in ("off", "false", "", "none"):
        assert not douyin.enabled(DownloadConfig(douyin_browser=off))


def test_merge_keeps_other_sites_and_replaces_douyin(tmp_path: Path):
    path = tmp_path / "cookies.txt"
    path.write_text(
        "# Netscape HTTP Cookie File\n"
        ".bilibili.com\tTRUE\t/\tTRUE\t2000000000\tSESSDATA\tkeep-me\n"
        ".douyin.com\tTRUE\t/\tTRUE\t2000000000\tttwid\told-value\n"
        "www.douyin.com\tFALSE\t/\tTRUE\t2000000000\tstale_only_in_old_file\tx\n",
        encoding="utf-8",
    )
    douyin.merge_cookies_into_file(path, [
        {"name": "ttwid", "value": "new-value", "domain": ".douyin.com", "path": "/", "expires": 2100000000, "secure": True},
        {"name": "__ac_nonce", "value": "n", "domain": "www.douyin.com", "path": "/", "expires": -1, "secure": False},
    ])
    text = path.read_text(encoding="utf-8")
    assert "SESSDATA\tkeep-me" in text                 # B 站的原样保留
    assert "ttwid\tnew-value" in text and "old-value" not in text
    assert "stale_only_in_old_file" not in text        # 抖音域整体换新，旧的不留
    assert "__ac_nonce\tn" in text                     # 会话 cookie（expires=-1）也写进去


def test_merge_creates_file_when_missing(tmp_path: Path):
    path = tmp_path / "sub" / "cookies.txt"
    douyin.merge_cookies_into_file(path, [
        {"name": "ttwid", "value": "v", "domain": ".douyin.com", "path": "/", "expires": 2100000000, "secure": True},
    ])
    assert "ttwid\tv" in path.read_text(encoding="utf-8")


class _FailingYDL:
    """extract_info 永远报 yt-dlp 的那句 Fresh cookies。"""

    calls = 0

    def __init__(self, opts):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        _FailingYDL.calls += 1
        raise YTDLPDownloadError("ERROR: [Douyin] 1: Fresh cookies (not necessarily logged in) are needed")

    def sanitize_info(self, info):
        return info


def test_probe_hands_douyin_over_to_browser(monkeypatch):
    browser_info = {"id": "7685008180196722164", "title": "来自浏览器", "duration": 47, "extractor_key": "Douyin",
                    "webpage_url": "https://www.douyin.com/video/7685008180196722164", "channel": "某人"}
    monkeypatch.setattr(ytdlp_base, "YoutubeDL", _FailingYDL)
    monkeypatch.setattr(douyin, "probe_via_browser", lambda url, cfg: browser_info)
    monkeypatch.setattr(ytdlp_base.time, "sleep", lambda s: None)
    _FailingYDL.calls = 0

    info = ytdlp_base.probe("https://www.douyin.com/video/7685008180196722164", DownloadConfig())
    assert info.title == "来自浏览器" and info.uploader == "某人" and info.raw is browser_info
    assert _FailingYDL.calls == 1      # 抖音的 403 不退避重试，第一次失败就换浏览器


def test_probe_leaves_other_sites_and_disabled_config_alone(monkeypatch):
    monkeypatch.setattr(ytdlp_base, "YoutubeDL", _FailingYDL)
    monkeypatch.setattr(ytdlp_base.time, "sleep", lambda s: None)

    def _boom(url, cfg):
        raise AssertionError("不该开浏览器")

    monkeypatch.setattr(douyin, "probe_via_browser", _boom)
    with pytest.raises(DownloadError):
        ytdlp_base.probe("https://www.bilibili.com/video/BV1x", DownloadConfig())
    with pytest.raises(DownloadError):
        ytdlp_base.probe("https://www.douyin.com/video/7685008180196722164", DownloadConfig(douyin_browser="off"))


def test_download_reprobes_when_cdn_url_expired(monkeypatch):
    attempts: list[dict] = []

    def _fake_download(raw, opts):
        attempts.append(raw)
        if raw.get("stale"):
            raise YTDLPDownloadError("ERROR: unable to download video data: HTTP Error 403: Forbidden")

    monkeypatch.setattr(ytdlp_base, "_download_from_info", _fake_download)
    monkeypatch.setattr(douyin, "probe_via_browser", lambda url, cfg: {"fresh": True})
    info = VideoInfo(url="https://www.douyin.com/video/7685008180196722164", video_id="1", title="t",
                     duration_sec=1.0, extractor="Douyin", raw={"stale": True})

    ytdlp_base.download(info, {}, DownloadConfig())
    assert [a.get("fresh", False) for a in attempts] == [False, True]

    # 没传 cfg（字幕那条路）就保持原来的行为：直接把错误抛出去
    attempts.clear()
    with pytest.raises(YTDLPDownloadError):
        ytdlp_base.download(info, {})
    assert len(attempts) == 1


def test_web_probe_path_also_hands_douyin_over_to_browser(monkeypatch):
    """界面贴链接走的是 listing.probe_any，不是 ytdlp_base.probe，兜底得两边都有。"""
    from video_summarizer import listing

    browser_info = {"id": "7682431802958450417", "title": "来自浏览器", "duration": 341, "extractor_key": "Douyin",
                    "webpage_url": "https://www.douyin.com/video/7682431802958450417"}
    monkeypatch.setattr(listing, "YoutubeDL", _FailingYDL)
    monkeypatch.setattr(douyin, "probe_via_browser", lambda url, cfg: browser_info)
    result = listing.probe_any("https://v.douyin.com/0VCny7Gq2xI/", DownloadConfig())
    assert isinstance(result, VideoInfo) and result.title == "来自浏览器" and result.duration_sec == 341
