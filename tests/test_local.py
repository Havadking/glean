"""本地视频：指纹、探测、流水线的本地分支、手改主播 / 日期。用 ffmpeg 现造一个 3 秒的小视频，不碰网络。"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from video_summarizer import local, pipeline
from video_summarizer.cache import Cache
from video_summarizer.config import load_config
from video_summarizer.models import Segment, SummaryOptions
from video_summarizer.web import library as library_mod

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="没装 ffmpeg")


@pytest.fixture
def cfg(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({
        "output_dir": str(tmp_path / "output"), "cache_db": str(tmp_path / "cache.sqlite"),
        "summarizer": {"provider": "openai", "model": "m", "correct_terms": False, "auto_tags": False},
    }), encoding="utf-8")
    return load_config(p)


@pytest.fixture
def clip(tmp_path: Path) -> Path:
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("没装 ffmpeg")
    path = tmp_path / "录播" / "某主播 2024-05-12 晚场.mp4"
    path.parent.mkdir()
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=duration=3:size=160x120:rate=10",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
        "-shortest", "-c:v", "libx264", "-c:a", "aac", str(path),
    ], check=True)
    return path


def test_is_local_and_to_path():
    assert local.is_local(r"E:\录播\a.mp4")
    assert local.is_local('"E:\\录播\\a.mp4"')       # 资源管理器「复制文件地址」带引号
    assert local.is_local("file:///E:/a.mp4")
    assert local.is_local(r"\\nas\share\a.mp4")
    assert not local.is_local("https://www.bilibili.com/video/BV1")
    assert not local.is_local("8.52 复制打开抖音 https://v.douyin.com/x/")
    assert local.to_path('"E:\\a b\\c.mp4"') == Path("E:\\a b\\c.mp4")
    assert local.to_path("file:///E:/a%20b/c.mp4") == Path("E:/a b/c.mp4")


def test_guess_date(tmp_path: Path):
    for name, want in [("录制-123-20240512-201530.flv", "20240512"), ("2024_05_12 晚.mp4", "20240512"),
                       ("2024.5.12.mp4", None), ("房间 1234567890.mp4", None)]:
        p = tmp_path / name
        p.write_bytes(b"x")
        got = local.guess_date(p)
        if want:
            assert got == want, name
        else:
            assert len(got) == 8 and got.startswith("20"), name   # 认不出就用修改时间


def test_fingerprint_survives_rename(tmp_path: Path):
    a = tmp_path / "a.mp4"
    a.write_bytes(b"0123456789" * 1_000_000)   # 10MB，头尾各取 4MB
    fp = local.fingerprint(a)
    b = tmp_path / "sub" / "renamed.mp4"
    b.parent.mkdir()
    a.rename(b)
    assert local.fingerprint(b) == fp and fp.startswith("local-")
    b.write_bytes(b"0123456789" * 1_000_000 + b"!")
    assert local.fingerprint(b) != fp


def test_check_file_rejects_bad_input(tmp_path: Path):
    from video_summarizer.errors import DownloadError

    with pytest.raises(DownloadError, match="找不到"):
        local.check_file(tmp_path / "nope.mp4")
    doc = tmp_path / "a.txt"
    doc.write_text("x")
    with pytest.raises(DownloadError, match="不像是音视频"):
        local.check_file(doc)


@needs_ffmpeg
def test_probe_reads_duration_date_thumb_and_remembers_meta(cfg, clip):
    info = local.probe(str(clip), cfg)
    assert info.extractor == "local" and info.video_id.startswith("local-")
    assert 2.5 < info.duration_sec < 3.5
    assert info.upload_date == "20240512"
    assert info.title == clip.stem and info.uploader is None
    assert info.thumbnail == f"/api/local/{info.video_id}/thumb"
    assert local.thumb_path(cfg, info.video_id).is_file()

    # 填过的标题、主播、日期下次探测还在；文件挪了地方会记下新位置
    cache = Cache(cfg.cache_db)
    cache.put_local_source(info.video_id, path=info.url, size=clip.stat().st_size, title="五月十二日晚场")
    cache.set_video_meta(info.video_id, uploader="某主播", upload_date="20240513")
    moved = clip.parent.parent / "挪走了.mp4"
    clip.rename(moved)
    again = local.probe(str(moved), cfg)
    assert again.video_id == info.video_id
    assert (again.title, again.uploader, again.upload_date) == ("五月十二日晚场", "某主播", "20240513")
    assert cache.get_local_source(info.video_id)["path"] == str(moved.resolve())
    assert local.locate(cfg, info.video_id) == moved.resolve()


@needs_ffmpeg
def test_pipeline_runs_local_file_without_touching_source(cfg, clip, monkeypatch):
    seen = {}

    def fake_asr(asr_cfg, audio_path, *, diarize, hotwords):
        seen["audio"] = Path(audio_path)
        seen["hotwords"] = hotwords
        return SimpleNamespace(language="zh", segments=[Segment(0.0, 2.0, "大家晚上好。")],
                               meta={"asr_provider": "funasr", "asr_model": "fake"})

    monkeypatch.setattr(pipeline.asr_worker, "transcribe", fake_asr)
    info = local.probe(str(clip), cfg)
    cache = Cache(cfg.cache_db)
    cache.set_video_meta(info.video_id, uploader="某主播")
    cache.learn_terms("某主播", [SimpleNamespace(src="某注播", dst="某主播", why="", hits=1, applied=True)],
                      video_id="old")

    size = clip.stat().st_size
    result = pipeline.run(str(clip), cfg, options=SummaryOptions(), skip_summary=True)
    assert clip.is_file() and clip.stat().st_size == size          # 原文件原封不动
    assert seen["audio"].suffix == ".wav" and seen["audio"].is_file()
    assert seen["hotwords"], "手填的主播应该带出他的词表当热词"
    assert result.info.uploader == "某主播"
    assert result.transcript.meta["extractor"] == "local"
    assert result.transcript.duration_sec > 2.5

    entry = library_mod.find_entry(cfg, info.video_id)
    assert entry is not None and entry.is_local and entry.uploader == "某主播"
    assert library_mod.audio_path(entry) is not None


def test_video_meta_override_in_library(cfg):
    from video_summarizer.models import Transcript

    t = Transcript("https://x/v", "asr", "zh", 10.0, [Segment(0, 1, "a")], title="t", video_id="v1",
                   meta={"uploader": "原名", "upload_date": "20240101"})
    cache = Cache(cfg.cache_db)
    cache.put_transcript("k1", t)
    assert library_mod.find_entry(cfg, "v1").uploader == "原名"

    cache.set_video_meta("v1", uploader="大号")
    e = library_mod.find_entry(cfg, "v1")
    assert (e.uploader, e.upload_date) == ("大号", "20240101")
    cache.set_video_meta("v1", upload_date="20240202")
    assert cache.get_video_meta("v1") == {"uploader": "大号", "upload_date": "20240202"}
    cache.set_video_meta("v1", uploader="")            # 撤销
    assert library_mod.find_entry(cfg, "v1").uploader == "原名"
    cache.clear("v1")
    assert cache.get_video_meta("v1") == {}


# ---------- 接口 ----------

@pytest.fixture
def client(cfg):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from video_summarizer.web import api as api_mod

    with TestClient(api_mod.create_app(cfg)) as c:
        yield c, api_mod


def _wait(c, job_id):
    for _ in range(500):
        j = c.get(f"/api/jobs/{job_id}").json()
        if j["status"] in ("done", "failed", "cancelled"):
            return j
        time.sleep(0.02)
    raise AssertionError("任务没结束")


@needs_ffmpeg
def test_api_probe_and_import_local(client, cfg, clip, monkeypatch):
    c, api_mod = client
    r = c.post("/api/probe", json={"url": f'"{clip}"'}).json()
    assert r["kind"] == "video" and r["is_local"] is True
    assert r["upload_date"] == "2024-05-12" and r["subtitle"] is None
    vid = r["video_id"]
    assert c.get(f"/api/local/{vid}/thumb").headers["content-type"] == "image/jpeg"

    seen = {}

    def fake_run(url, cfg_, **kw):
        seen["url"], seen["info"] = url, kw["info"]
        return SimpleNamespace(info=kw["info"], transcript=SimpleNamespace(segments=[]), summary=None,
                               summary_skipped_reason=None, summary_provider=None,
                               correction_usage=None, correction_provider_desc=None)

    monkeypatch.setattr(api_mod, "run_pipeline", fake_run)
    monkeypatch.setattr(api_mod.search_mod, "index_one", lambda *a, **k: None)
    r = c.post("/api/jobs", json={"url": str(clip), "title": "五一二晚场", "uploader": "某主播",
                                  "upload_date": "2024-05-11"}).json()
    assert r["job"]["title"] == "五一二晚场"
    assert _wait(c, r["job"]["id"])["status"] == "done"
    assert seen["url"] == str(clip.resolve())
    assert (seen["info"].title, seen["info"].uploader, seen["info"].upload_date) == ("五一二晚场", "某主播", "20240511")
    cache = Cache(cfg.cache_db)
    assert cache.get_local_source(vid)["title"] == "五一二晚场"
    assert cache.get_video_meta(vid) == {"uploader": "某主播", "upload_date": "20240511"}


def test_api_edit_uploader(client, cfg):
    from video_summarizer.models import Transcript

    c, _ = client
    Cache(cfg.cache_db).put_transcript("k1", Transcript("https://x/v", "asr", "zh", 10.0, [Segment(0, 1, "a")],
                                                        title="t", video_id="v1", meta={"uploader": "原名"}))
    r = c.post("/api/videos/v1/meta", json={"uploader": "新名", "upload_date": "2024/03/04"}).json()
    assert r == {"video_id": "v1", "uploader": "新名", "upload_date": "2024-03-04"}
    assert c.get("/api/videos/v1").json()["uploader"] == "新名"
    assert c.post("/api/videos/v1/meta", json={"upload_date": "昨天"}).status_code == 400
    assert c.post("/api/videos/v1/meta", json={"uploader": ""}).json()["uploader"] == "原名"
    assert c.post("/api/videos/nope/meta", json={"uploader": "x"}).status_code == 404
