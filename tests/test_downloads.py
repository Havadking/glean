"""「存视频」：格式选择、sidecar、进度合并、队列，以及 /api/downloads 接口。不碰网络。"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
import yaml

from video_summarizer import downloads as dl
from video_summarizer.config import load_config
from video_summarizer.errors import ConfigError
from video_summarizer.ytdlp_base import VideoInfo


def _info(video_id: str = "BV1test", title: str = "测试视频", **raw) -> VideoInfo:
    return VideoInfo(
        url=f"https://www.bilibili.com/video/{video_id}", video_id=video_id, title=title,
        duration_sec=61.0, extractor="BiliBili", uploader="某UP", upload_date="20250912",
        thumbnail="https://i0.hdslb.com/x.jpg",
        raw={"description": "简介" * 400, "channel_url": "https://space.bilibili.com/1", **raw},
    )


# ---------- 纯函数 ----------


def test_format_selector_prefers_h264_mp4_then_falls_back():
    sel = dl.format_selector("1080")
    parts = sel.split("/")
    assert parts[0] == "bestvideo[height<=1080][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]"
    assert "bestvideo[height<=1080]+bestaudio" in parts
    assert parts[-1] == "best"
    # best 不限高度；关掉 h264 偏好就没有 avc1 那两段
    assert "[height" not in dl.format_selector("best")
    assert "avc1" not in dl.format_selector("720", prefer_h264=False)


def test_normalize_quality_accepts_variants_and_falls_back():
    assert dl.normalize_quality(1080) == "1080"
    assert dl.normalize_quality("720p") == "720"
    assert dl.normalize_quality("BEST") == "best"
    assert dl.normalize_quality("4k", default=720) == "720"
    assert dl.normalize_quality(None, default="nonsense") == "best"


def test_sidecar_path_only_swaps_last_suffix():
    assert dl.sidecar_path_for(Path("a/标题 v1.5 [BV1x].mp4")) == Path("a/标题 v1.5 [BV1x].suishi.json")


def test_available_heights_ignores_audio_only_formats():
    info = _info(formats=[
        {"format_id": "30080", "vcodec": "avc1", "height": 1080},
        {"format_id": "30064", "vcodec": "hev1", "height": 720},
        {"format_id": "30280", "vcodec": "none", "height": None},
        "garbage",
    ])
    assert dl.available_heights(info) == [1080, 720]


def test_build_sidecar_trims_description_and_keeps_full_title(tmp_path):
    d = dl.build_sidecar(_info(title="很长的标题" * 30), quality="1080", file_path=tmp_path / "x [BV1test].mp4")
    assert d["schema"] == 1
    assert d["site"] == "BiliBili" and d["video_id"] == "BV1test"
    assert d["title"] == "很长的标题" * 30
    assert len(d["description"]) == 500
    assert d["uploader_url"] == "https://space.bilibili.com/1"
    assert d["file_name"] == "x [BV1test].mp4"


def test_overall_progress_weights_video_and_audio_tracks():
    fmts = [{"format_id": "v", "filesize": 900}, {"format_id": "a", "filesize": 100}]
    # 视频轨下了一半 → 整体 45%
    hook = {"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100,
            "info_dict": {"requested_formats": fmts, "format_id": "v"}}
    assert dl.overall_progress(hook) == pytest.approx(0.45)
    # 音频轨下了一半 → 90% + 5%
    hook["info_dict"]["format_id"] = "a"
    assert dl.overall_progress(hook) == pytest.approx(0.95)
    # 单流：就是自己的比例
    assert dl.overall_progress({"status": "downloading", "downloaded_bytes": 25, "total_bytes_estimate": 100}) == 0.25
    # 体积不全按轨数平均
    hook["info_dict"]["requested_formats"] = [{"format_id": "v"}, {"format_id": "a", "filesize": 5}]
    hook["info_dict"]["format_id"] = "a"
    assert dl.overall_progress(hook) == pytest.approx(0.75)
    assert dl.overall_progress({"status": "finished"}) is None
    assert dl.overall_progress({"status": "downloading", "downloaded_bytes": 3}) is None


# ---------- 队列 ----------


def _fake_download(video_dir: Path, *, fail: bool = False):
    """替代 ytdlp_base.download：按 outtmpl 造一个文件，顺手触发进度和合流回调。"""

    def fake(info, opts, cfg=None):
        if fail:
            raise RuntimeError("HTTP Error 412: Precondition Failed")
        for hook in opts.get("progress_hooks", []):
            hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100, "_speed_str": "1MiB/s",
                  "eta": 3, "info_dict": {}})
        target = video_dir / info.extractor / (info.uploader or "未知作者") / f"{info.title} [{info.video_id}].mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x00" * 16)
        for hook in opts.get("postprocessor_hooks", []):
            hook({"status": "started", "postprocessor": "Merger", "info_dict": {}})
            hook({"status": "finished", "postprocessor": "Merger", "info_dict": {"filepath": str(target)}})
        return None

    return fake


@pytest.fixture
def cfg(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "output_dir": str(tmp_path / "output"), "cache_db": str(tmp_path / "cache.sqlite"),
        "download": {"batch_delay_sec": 0, "video_dir": "videos", "video_quality": 720},
        "web": {"cors_origins": ["http://localhost:8964"]},
    }, allow_unicode=True), encoding="utf-8")
    return load_config(cfg_path)


def _wait(job, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if job.finished:
            return job
        time.sleep(0.01)
    raise AssertionError(f"任务没结束：{job.status}")


def test_config_resolves_video_dir_and_validates(cfg, tmp_path):
    assert Path(cfg.download.video_dir) == tmp_path / "videos"
    assert cfg.web.cors_origins == ["http://localhost:8964"]

    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"web": {"cors_origins": ["https://evil.example"]}}), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(bad)
    bad.write_text(yaml.safe_dump({"download": {"video_quality": "4k"}}), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(bad)


def test_manager_downloads_writes_sidecar_and_persists(cfg, monkeypatch):
    video_dir = Path(cfg.download.video_dir)
    monkeypatch.setattr(dl, "download", _fake_download(video_dir))
    persisted: list[dict] = []
    done: list[dl.DownloadJob] = []
    m = dl.DownloadManager(lambda: cfg, on_done=done.append, persist=persisted.append)

    q = m.subscribe()
    job, dup = m.submit(_info(), url="https://www.bilibili.com/video/BV1test", quality="720", then_summarize="")
    assert not dup and job.status == "queued"
    _wait(job)

    assert job.status == "done", job.error
    fp = Path(job.file_path)
    assert fp.is_file() and fp.name == "测试视频 [BV1test].mp4"
    side = json.loads(Path(job.sidecar_path).read_text(encoding="utf-8"))
    assert side["video_id"] == "BV1test" and side["quality"] == "720" and side["url"].endswith("BV1test")
    assert dl.sidecar_path_for(fp) == Path(job.sidecar_path)
    assert done and done[0].id == job.id

    statuses = [row["status"] for row in persisted]
    assert statuses[0] == "queued" and statuses[-1] == "done" and "merging" in statuses
    # 进度事件在事件流里、但不落库
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    assert any(e.job["speed"] == "1MiB/s" for e in events)
    assert not any(row.get("speed") for row in persisted)
    assert events[-1].job["file_exists"] is True


def test_manager_dedupes_finished_and_active(cfg, monkeypatch):
    video_dir = Path(cfg.download.video_dir)
    monkeypatch.setattr(dl, "download", _fake_download(video_dir))
    m = dl.DownloadManager(lambda: cfg)
    job, _ = m.submit(_info(), url="u1", quality="720")
    _wait(job)

    # 文件在、没 force → 直接返回旧记录
    again, dup = m.submit(_info(), url="u1", quality="720", existing=job.to_dict())
    assert dup and again.id == job.id
    # 文件被删了 → 允许重下
    Path(job.file_path).unlink()
    redo, dup = m.submit(_info(), url="u1", quality="720", existing=job.to_dict())
    assert not dup and redo.id != job.id
    _wait(redo)
    # 排队中的同一条不重复入队
    m2 = dl.DownloadManager(lambda: cfg)
    a, _ = m2.submit(_info("BVa"), url="ua", quality="720")
    b, dup = m2.submit(_info("BVa"), url="ua-other-form", quality="720")
    assert dup and b.id == a.id


def test_manager_failure_is_friendly_and_cancel_only_queued(cfg, monkeypatch):
    monkeypatch.setattr(dl, "download", _fake_download(Path(cfg.download.video_dir), fail=True))
    m = dl.DownloadManager(lambda: cfg)
    job, _ = m.submit(_info(), url="u", quality="720")
    _wait(job)
    assert job.status == "failed" and "412" in job.error and "限流" in job.error
    assert m.cancel(job.id) is False
    assert m.forget(job.id) is True and m.get(job.id) is None


def test_manager_load_restores_history_and_requeues_unfinished(cfg, monkeypatch):
    monkeypatch.setattr(dl, "download", _fake_download(Path(cfg.download.video_dir)))
    monkeypatch.setattr(dl, "probe", lambda url, c: _info("BVold", title="重启前没下完"))
    m = dl.DownloadManager(lambda: cfg)
    rows = [
        {"id": "done1", "url": "u-done", "video_id": "BVdone", "title": "早就下好了", "quality": "720",
         "status": "done", "file_path": "nowhere.mp4", "created_at": "2025-01-01T00:00:00+00:00"},
        {"id": "half1", "url": "u-half", "video_id": "BVold", "title": "重启前没下完", "quality": "720",
         "status": "downloading", "progress": 0.4, "created_at": "2025-01-02T00:00:00+00:00"},
    ]
    m.load(rows)
    half = m.get("half1")
    assert half is not None
    _wait(half)
    assert half.status == "done" and half.file_path.endswith("重启前没下完 [BVold].mp4")
    assert m.get("done1").status == "done"
    assert [j.id for j in m.list()] == ["half1", "done1"]


# ---------- 接口 ----------


pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from video_summarizer import listing as listing_mod  # noqa: E402
from video_summarizer.web import api as api_mod  # noqa: E402


@pytest.fixture
def client(cfg, monkeypatch):
    monkeypatch.setattr(dl, "download", _fake_download(Path(cfg.download.video_dir)))
    monkeypatch.setattr(listing_mod, "probe_any", lambda url, c, **kw: _info(
        formats=[{"format_id": "v", "vcodec": "avc1", "height": 1080}]))
    app = api_mod.create_app(cfg)
    with TestClient(app) as c:
        yield c


def _wait_api(client, job_id, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        j = client.get(f"/api/downloads/{job_id}").json()
        if j["status"] in ("done", "failed", "cancelled"):
            return j
        time.sleep(0.02)
    raise AssertionError("任务没结束")


def test_api_config_and_cors(client, cfg):
    r = client.get("/api/downloads/config", headers={"Origin": "http://localhost:8964"})
    assert r.status_code == 200
    d = r.json()
    assert d["enabled"] and d["dir_name"] == "videos" and d["quality"] == "720" and d["dir_ok"]
    assert r.headers.get("access-control-allow-origin") == "http://localhost:8964"
    # 没放行的来源拿不到 CORS 头
    r2 = client.get("/api/downloads/config", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in r2.headers


def test_api_disabled_when_no_video_dir(tmp_path):
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(yaml.safe_dump({"cache_db": str(tmp_path / "c.sqlite"), "output_dir": str(tmp_path / "o")}),
                        encoding="utf-8")
    with TestClient(api_mod.create_app(load_config(cfg_path))) as c:
        assert c.get("/api/downloads/config").json()["enabled"] is False
        assert c.post("/api/downloads/probe", json={"url": "https://b23.tv/x"}).status_code == 404


def test_api_probe_download_list_events_delete(client, cfg):
    text = "8.52 复制打开抖音，看看 https://www.bilibili.com/video/BV1test 的内容"
    p = client.post("/api/downloads/probe", json={"url": text}).json()
    assert p["kind"] == "video" and p["video_id"] == "BV1test" and p["heights"] == [1080]
    assert p["already_downloaded"] is False and p["in_library"] is False

    r = client.post("/api/downloads", json={"url": text, "quality": "1080p"}).json()
    assert r["duplicate"] is False and r["job"]["quality"] == "1080"
    job = _wait_api(client, r["job"]["id"])
    assert job["status"] == "done" and job["file_exists"]

    # 再探测：已下载；再提交：重复
    p2 = client.post("/api/downloads/probe", json={"url": text}).json()
    assert p2["already_downloaded"] is True and p2["download"]["id"] == job["id"]
    r2 = client.post("/api/downloads", json={"url": text}).json()
    assert r2["duplicate"] is True and r2["job"]["id"] == job["id"]

    lst = client.get("/api/downloads").json()
    assert [j["id"] for j in lst["jobs"]] == [job["id"]] and lst["config"]["enabled"]

    # 事件流：从头补发，第一条是 queued、最后一条 done（once=1 补发完就断，测试里不能挂长连接）
    body = client.get("/api/downloads/events", params={"after": 0, "once": 1}).text
    datas = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
    assert datas[0]["job"]["status"] == "queued" and datas[-1]["job"]["status"] == "done"
    assert all(d["seq"] == i + 1 for i, d in enumerate(datas))
    # 带 after 只拿后面的
    later = client.get("/api/downloads/events", params={"after": datas[-1]["seq"] - 1, "once": 1}).text
    assert later.count("data: ") == 1

    # 删记录不删文件
    fp = Path(job["file_path"])
    assert client.delete(f"/api/downloads/{job['id']}").json()["removed"] is True
    assert fp.is_file()
    assert client.get(f"/api/downloads/{job['id']}").status_code == 404
    assert client.get("/api/downloads").json()["jobs"] == []


def test_api_probe_list_link_is_reported(client, monkeypatch):
    monkeypatch.setattr(listing_mod, "probe_any", lambda url, c, **kw: listing_mod.ListPage(
        kind="space", title="某人的空间", url=url, page=1, page_size=30, total=99))
    p = client.post("/api/downloads/probe", json={"url": "https://space.bilibili.com/1"}).json()
    assert p["kind"] == "list" and p["list_kind"] == "space" and p["total"] == 99


def test_api_thumb_proxy_only_allows_known_hosts(client):
    assert client.get("/api/downloads/thumb", params={"url": "https://evil.example/a.jpg"}).status_code == 400
    assert client.get("/api/downloads/thumb", params={"url": "ftp://i0.hdslb.com/a.jpg"}).status_code == 400
