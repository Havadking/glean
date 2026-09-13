"""HTTP 接口：用临时产物目录 + 假的总结 provider，不碰网络。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml

from video_summarizer.cache import Cache, summary_key, transcript_key
from video_summarizer.config import load_config
from video_summarizer.models import Segment, SummaryOptions, Transcript
from video_summarizer.pipeline import render_summary_markdown
from video_summarizer.summarizer.base import BaseSummarizer
from video_summarizer.web import api as api_mod

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402


class FakeSummarizer(BaseSummarizer):
    name = "fake"
    calls: list[str] = []

    def _complete(self, system: str, user: str) -> str:
        FakeSummarizer.calls.append(user)
        self._record_usage(1000, 500)      # 假装每次调用花了这么多
        if "视频内容问答助手" in system or "创作者" in system:
            return self._complete_for_qa(user)
        return "# 根\n## 分支\n- 点一\n- 点二\n" if "大纲" in system + user else "## 一句话总结\n假的总结。"

    def describe(self) -> str:
        return "fake/model"

    def _complete_for_qa(self, user: str) -> str:
        if "创作者：" in user:
            return "她说过第一句 【1】。"
        return "第一句在开头 [00:00]，第二句紧接着 [00:04]。"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch):
    """两个视频：一个在缓存里（有 UP 主），一个只在产物目录里（老版本跑的）。"""
    out = tmp_path / "output"
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "output_dir": str(out), "cache_db": str(tmp_path / "cache.sqlite"),
        "download": {"batch_delay_sec": 0},   # 测试里别真等
        "summarizer": {"provider": "openai", "model": "m", "price_input_per_m": 2.0, "price_output_per_m": 3.0},
    }, allow_unicode=True), encoding="utf-8")
    cfg = load_config(cfg_path)

    # 视频 A：缓存 + 产物，带 UP 主
    a = Transcript(
        source_url="https://www.bilibili.com/video/BVAAA", source_type="asr", language="zh",
        duration_sec=120.0, title="A 视频", video_id="BVAAA",
        segments=[Segment(0.0, 4.0, "第一句。"), Segment(4.0, 9.0, "第二句。")],
        meta={"extractor": "BiliBili", "asr_model": "sensevoice-small", "uploader": "某 UP",
              "upload_date": "20240102", "thumbnail": "http://x/a.jpg"},
    )
    key_a = transcript_key(video_id="BVAAA", extractor="BiliBili", source_type="asr",
                           asr_provider="funasr", asr_model="sensevoice-small", asr_language=None, diarize=False)
    a.meta["cache_key"] = key_a
    dir_a = out / "A 视频-BVAAA"
    a.save(dir_a / "transcript.json")
    cache = Cache(cfg.cache_db)
    cache.put_transcript(key_a, a)
    sum_key = summary_key(transcript_key=key_a, provider_desc="fake/model", summary_type="overall",
                          language="zh", extra=None)
    cache.put_summary(sum_key, transcript_key=key_a, transcript=a, provider_desc="fake/model",
                      summary_type="overall", language="zh", content="## 一句话总结\n缓存里的总结。")

    # 视频 B：只有产物目录，summary.md 是思维导图
    b = Transcript(
        source_url="https://www.youtube.com/watch?v=bbb", source_type="subtitle", language="en",
        duration_sec=30.0, title="B video", video_id="bbb",
        segments=[Segment(0.0, 3.0, "hello"), Segment(3.0, 6.0, "world")],
        meta={"extractor": "Youtube", "subtitle_language": "en"},
    )
    dir_b = out / "B video-bbb"
    b.save(dir_b / "transcript.json")
    (dir_b / "summary.md").write_text(
        render_summary_markdown("# B 根\n## 枝\n- 叶", b, "fake/model", SummaryOptions(summary_type="mindmap")),
        encoding="utf-8",
    )

    FakeSummarizer.calls.clear()
    monkeypatch.setattr(api_mod, "get_summarizer", lambda scfg: FakeSummarizer(scfg))
    return {"cfg": cfg, "out": out, "dir_a": dir_a, "dir_b": dir_b, "key_a": key_a}


@pytest.fixture
def client(workspace):
    app = api_mod.create_app(workspace["cfg"])
    with TestClient(app) as c:
        yield c


def _wait_job(client, job_id, timeout=10.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in ("done", "failed", "cancelled"):
            return j
        time.sleep(0.02)
    raise AssertionError("任务没结束")


# ---------- 元信息与库 ----------


def test_meta_lists_summary_types_and_pricing(client):
    m = client.get("/api/meta").json()
    assert m["app"] == "拾光笺"
    assert [t["key"] for t in m["summary_types"]] == ["overall", "by_speaker", "timeline", "key_points", "mindmap"]
    assert all(t["hint"] for t in m["summary_types"])
    assert m["priced"] is True and m["currency"] == "¥"
    assert m["provider"] == "fake/model"


def test_library_groups_by_uploader_and_merges_sources(client):
    lib = client.get("/api/library").json()
    assert lib["stats"]["videos"] == 2
    assert lib["stats"]["summaries"] == 2 and lib["stats"]["mindmaps"] == 1
    names = [g["uploader"] for g in lib["groups"]]
    assert names == ["某 UP", None]          # 没有 UP 主的排最后
    a = lib["groups"][0]["entries"][0]
    assert a["video_id"] == "BVAAA" and a["thumbnail"] == "http://x/a.jpg"
    assert a["upload_date"] == "2024-01-02"
    assert a["work_dir"].endswith("A 视频-BVAAA")
    assert [s["type"] for s in a["summaries"]] == ["overall"]
    b = lib["groups"][1]["entries"][0]
    # 目录里的 summary.md 靠开头的来源信息识别出类型
    assert [s["type"] for s in b["summaries"]] == ["mindmap"]
    assert b["source_label"] == "官方字幕"


def test_video_detail_has_paragraphs_and_summaries(client):
    v = client.get("/api/videos/BVAAA").json()
    assert v["title"] == "A 视频"
    assert len(v["paragraphs"]) == 1 and v["paragraphs"][0]["text"].startswith("第一句")
    assert v["summaries"]["overall"]["content"].startswith("## 一句话总结")
    assert v["summaries"]["overall"]["source"] == "cache"

    b = client.get("/api/videos/bbb").json()
    mm = b["summaries"]["mindmap"]
    assert mm["source"] == "file"
    assert mm["tree"]["content"] == "B 根" and mm["tree"]["children"][0]["content"] == "枝"
    assert mm["nodes"] == 3


def test_unknown_video_is_404(client):
    assert client.get("/api/videos/nope").status_code == 404
    assert client.get("/api/videos/nope/estimate").status_code == 404


def test_estimate_converts_tokens_to_money(client):
    e = client.get("/api/videos/BVAAA/estimate?type=timeline").json()
    assert e["provider"] == "fake/model"
    assert e["calls"] == 1 and e["strategy"] == "single"
    assert e["cost"] == pytest.approx(e["input_tokens"] * 2 / 1e6 + e["output_tokens"] * 3 / 1e6, abs=5e-5)
    assert client.get("/api/videos/BVAAA/estimate?type=nope").status_code == 400


# ---------- 总结任务 ----------


def test_existing_summary_is_returned_without_a_job(client):
    r = client.post("/api/videos/BVAAA/summaries", json={"type": "overall"}).json()
    assert r["cached"] is True and r["job"] is None
    assert "缓存里的总结" in r["summary"]["content"]
    assert FakeSummarizer.calls == []


def test_new_summary_runs_as_job_and_lands_in_cache_and_file(client, workspace):
    r = client.post("/api/videos/BVAAA/summaries", json={"type": "key_points"}).json()
    assert r["cached"] is False and r["job"]["kind"] == "summarize"
    job = _wait_job(client, r["job"]["id"])
    assert job["status"] == "done", job["error"]
    assert job["result"]["type"] == "key_points" and "假的总结" in job["result"]["content"]
    assert job["result"]["estimate"]["cost"] is not None
    assert job["result"]["used"] == {"input_tokens": 1000, "output_tokens": 500, "calls": 1,
                                     "cost": pytest.approx(1000 * 2 / 1e6 + 500 * 3 / 1e6), "currency": "¥"}
    assert len(FakeSummarizer.calls) == 1

    # 落了盘、进了缓存、详情里能看到
    assert "假的总结" in (workspace["dir_a"] / "summary.md").read_text(encoding="utf-8")
    v = client.get("/api/videos/BVAAA").json()
    assert set(v["summaries"]) == {"overall", "key_points"}
    # 再要一次直接命中
    again = client.post("/api/videos/BVAAA/summaries", json={"type": "key_points"}).json()
    assert again["cached"] is True and len(FakeSummarizer.calls) == 1


def test_mindmap_job_writes_html_and_tree(client, workspace):
    r = client.post("/api/videos/BVAAA/summaries", json={"type": "mindmap"}).json()
    job = _wait_job(client, r["job"]["id"])
    assert job["status"] == "done", job["error"]
    assert (workspace["dir_a"] / "mindmap.html").is_file()
    v = client.get("/api/videos/BVAAA").json()
    assert v["summaries"]["mindmap"]["tree"]["children"][0]["content"] == "分支"


def test_duplicate_summary_job_is_not_queued_twice(client, monkeypatch):
    import threading
    gate = threading.Event()
    orig = FakeSummarizer._complete

    def slow(self, system, user):
        gate.wait(3)
        return orig(self, system, user)

    monkeypatch.setattr(FakeSummarizer, "_complete", slow)
    r1 = client.post("/api/videos/BVAAA/summaries", json={"type": "timeline"}).json()
    r2 = client.post("/api/videos/BVAAA/summaries", json={"type": "timeline"}).json()
    assert r2["duplicate"] is True and r2["job"]["id"] == r1["job"]["id"]
    gate.set()
    _wait_job(client, r1["job"]["id"])


def test_job_events_stream_ends_with_terminal_status(client):
    r = client.post("/api/videos/BVAAA/summaries", json={"type": "by_speaker"}).json()
    job_id = r["job"]["id"]
    _wait_job(client, job_id)
    with client.stream("GET", f"/api/jobs/{job_id}/events") as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = "".join(resp.iter_text())
    assert "event: stage" in body and "event: status" in body
    assert body.rstrip().endswith("}") and '"status": "done"' in body
    # 续传：after 之后没有新事件、任务又已结束，流应该直接关掉，不让客户端干等
    with client.stream("GET", f"/api/jobs/{job_id}/events?after=9999") as resp:
        assert "".join(resp.iter_text()).strip() == ""


# ---------- 处理任务 ----------


def test_process_job_rejects_unknown_type_and_dedups(client, monkeypatch):
    assert client.post("/api/jobs", json={"url": "u", "summary_type": "nope"}).status_code == 400
    assert client.post("/api/jobs", json={"url": "   "}).status_code == 400

    import threading
    gate = threading.Event()

    def fake_run(url, cfg, **kw):
        gate.wait(3)
        raise RuntimeError("这只是个测试")

    monkeypatch.setattr(api_mod, "run_pipeline", fake_run)
    r1 = client.post("/api/jobs", json={"url": "https://x/v1"}).json()
    r2 = client.post("/api/jobs", json={"url": "https://x/v1"}).json()
    assert r1["duplicate"] is False and r2["duplicate"] is True
    gate.set()
    job = _wait_job(client, r1["job"]["id"])
    assert job["status"] == "failed" and "测试" in job["error"]


def test_process_job_passes_options_through(client, monkeypatch):
    seen = {}

    def fake_run(url, cfg, **kw):
        seen["url"] = url
        seen["asr"] = (cfg.asr.provider, cfg.asr.model, cfg.asr.diarize)
        seen["skip"] = kw["skip_summary"]
        kw["on_stage"]("probe", "x")

        class R:
            class info:
                video_id = "vid"
                title = "t"
            transcript = Transcript("u", "asr", "zh", 1.0, [], video_id="vid")
            summary = None
            summary_skipped_reason = "skip"
            summary_provider = None
        return R()

    monkeypatch.setattr(api_mod, "run_pipeline", fake_run)
    r = client.post("/api/jobs", json={"url": "https://x/v2", "asr_model": "whisper", "diarize": False}).json()
    job = _wait_job(client, r["job"]["id"])
    assert job["status"] == "done"
    assert seen["asr"] == ("whisper", "large-v3", False) and seen["skip"] is True
    assert job["result"]["video_id"] == "vid"
    assert any(e["kind"] == "stage" and e["stage"] == "probe" for e in job["events"])


def test_cancel_queued_job(client, monkeypatch):
    import threading
    gate = threading.Event()
    monkeypatch.setattr(api_mod, "run_pipeline", lambda url, cfg, **kw: (gate.wait(3), (_ for _ in ()).throw(RuntimeError("x")))[1])
    r1 = client.post("/api/jobs", json={"url": "https://x/a"}).json()
    r2 = client.post("/api/jobs", json={"url": "https://x/b"}).json()
    assert client.delete(f"/api/jobs/{r1['job']['id']}").json()["cancelled"] is False
    assert client.delete(f"/api/jobs/{r2['job']['id']}").json()["cancelled"] is True
    gate.set()
    _wait_job(client, r1["job"]["id"])
    ids = [j["id"] for j in client.get("/api/jobs").json()["jobs"]]
    assert r1["job"]["id"] in ids and r2["job"]["id"] in ids


# ---------- 花费 ----------


def test_usage_is_recorded_for_summaries_and_questions(client):
    assert client.get("/api/usage").json()["total"]["calls"] == 0
    r = client.post("/api/videos/BVAAA/summaries", json={"type": "timeline"}).json()
    _wait_job(client, r["job"]["id"])
    client.post("/api/videos/BVAAA/ask", json={"question": "q"})
    u = client.get("/api/usage").json()
    assert u["total"]["calls"] == 2 and u["month"]["calls"] == 2
    assert u["total"]["input_tokens"] == 2000 and u["total"]["cost"] == pytest.approx(2 * (1000 * 2 / 1e6 + 500 * 3 / 1e6))
    assert [x["kind"] for x in u["recent"]] == ["qa", "summary"]
    assert u["recent"][1]["detail"] == "timeline"
    lib = client.get("/api/library").json()
    assert lib["stats"]["cost"] == pytest.approx(u["total"]["cost"]) and lib["stats"]["calls"] == 2
    a = lib["groups"][0]["entries"][0]
    assert a["cost"] == pytest.approx(u["total"]["cost"])
    assert client.get("/api/videos/BVAAA").json()["usage"]["calls"] == 2
    # 命中缓存的不记账
    client.post("/api/videos/BVAAA/summaries", json={"type": "timeline"})
    assert client.get("/api/usage").json()["total"]["calls"] == 2


# ---------- 批量 ----------


def _fake_pipeline(gate=None, video_id="vid"):
    def fake_run(url, cfg, **kw):
        if gate is not None:
            gate.wait(3)

        class R:
            class info:
                pass
            transcript = Transcript("u", "asr", "zh", 1.0, [], video_id=video_id)
            summary = None
            summary_skipped_reason = "skip"
            summary_provider = None
        R.info.video_id = video_id
        R.info.title = url
        return R()
    return fake_run


def test_probe_returns_listing_for_space_and_marks_known(client, monkeypatch):
    from video_summarizer import listing
    page = listing.ListPage(kind="space", title="某人 的投稿", url="https://space.bilibili.com/1/video",
                            page=1, page_size=30, total=2, entries=[
                                listing.ListedVideo("BVAAA", "https://www.bilibili.com/video/BVAAA", "A 视频", 120.0, upload_date="20240102"),
                                listing.ListedVideo("BVNEW", "https://www.bilibili.com/video/BVNEW", "新的", 30.0),
                            ])
    lp = page
    monkeypatch.setattr(api_mod.listing_mod, "probe_any", lambda url, cfg, page=1, keyword="": lp)
    r = client.post("/api/probe", json={"url": "https://space.bilibili.com/1/video", "keyword": "x"}).json()
    assert r["kind"] == "list" and r["total"] == 2 and r["has_more"] is False
    a, new = r["entries"]
    assert a["in_library"] is True and a["summaries_done"] == ["overall"] and a["upload_date"] == "2024-01-02"
    assert new["in_library"] is False and new["queued"] is False


def test_probe_single_video_goes_through_probe_any(client, monkeypatch):
    from video_summarizer.ytdlp_base import VideoInfo
    info = VideoInfo(url="https://x/v", video_id="BVAAA", title="A 视频", duration_sec=120.0, extractor="BiliBili",
                     uploader="某 UP")
    monkeypatch.setattr(api_mod.listing_mod, "probe_any", lambda url, cfg, page=1, keyword="": info)
    r = client.post("/api/probe", json={"url": "https://x/v"}).json()
    assert r["kind"] == "video" and r["already_in_library"] is True and r["transcript_cached"] is True


def test_batch_queues_serially_dedups_and_cancels(client, monkeypatch):
    import threading
    gate = threading.Event()
    monkeypatch.setattr(api_mod, "run_pipeline", _fake_pipeline(gate))
    r = client.post("/api/jobs/batch", json={"items": [
        {"url": "https://x/1", "title": "一"}, {"url": "https://x/2", "title": "二"},
        {"url": "https://x/1"}, {"url": "  "},
    ], "summary_type": "overall"}).json()
    assert r["queued"] == 2 and r["duplicates"] == 1
    ids = [j["id"] for j in r["jobs"]]
    assert ids[0] == ids[2]
    jobs = client.get("/api/jobs").json()["jobs"]
    assert {j["params"]["url"] for j in jobs if j["status"] in ("queued", "running")} == {"https://x/1", "https://x/2"}
    assert all(j["params"]["batch"] for j in jobs)
    # 清空排队的：正在跑的那个不受影响
    assert client.delete("/api/jobs").json()["cancelled"] == 1
    gate.set()
    done = _wait_job(client, ids[0])
    assert done["status"] == "done" and done["title"] == "一"
    assert client.post("/api/jobs/batch", json={"items": [], "summary_type": "nope"}).status_code == 400


def test_queued_jobs_survive_restart(workspace, monkeypatch):
    import threading
    gate = threading.Event()
    monkeypatch.setattr(api_mod, "run_pipeline", _fake_pipeline(gate))
    with TestClient(api_mod.create_app(workspace["cfg"])) as c1:
        r = c1.post("/api/jobs/batch", json={"items": [{"url": "https://x/a", "title": "甲"}, {"url": "https://x/b", "title": "乙"}]}).json()
        time.sleep(0.05)
        first = c1.get(f"/api/jobs/{r['jobs'][0]['id']}").json()
        assert first["status"] == "running"
    # "服务重启"：库里还记着排队的乙（甲正在跑，也还没被删）
    from video_summarizer.cache import Cache
    pending = Cache(workspace["cfg"].cache_db).pending_jobs()
    assert {p["title"] for p in pending} == {"甲", "乙"}
    with TestClient(api_mod.create_app(workspace["cfg"])) as c2:
        jobs = c2.get("/api/jobs").json()["jobs"]
        assert {j["title"] for j in jobs} == {"甲", "乙"}
        assert all(j["params"]["batch"] for j in jobs)
        gate.set()   # 旧进程"死"了才放行，否则它自己跑完会把待办删掉
        for j in jobs:
            _wait_job(c2, j["id"])
    assert Cache(workspace["cfg"].cache_db).pending_jobs() == []


# ---------- 删除 ----------


def test_delete_video_clears_cache_and_directory(client, workspace):
    r = client.delete("/api/videos/BVAAA").json()
    assert r["transcripts"] == 1 and r["summaries"] == 1 and r["removed_dir"] is True
    assert not workspace["dir_a"].exists()
    assert client.get("/api/videos/BVAAA").status_code == 404
    assert client.get("/api/library").json()["stats"]["videos"] == 1


def test_delete_refuses_directories_outside_output_dir(client, tmp_path, monkeypatch):
    """产物目录只删 output_dir 之下的；库条目指到别处（比如用户手动挪过）就只清缓存。"""
    from video_summarizer.web import library as lib_mod
    from video_summarizer.web.library import LibraryEntry

    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "transcript.json").write_text("{}", encoding="utf-8")
    entry = LibraryEntry(video_id="zzz", title="Z", source_url="u", duration_sec=1, source_type="subtitle",
                         language="en", segment_count=0, speaker_count=0, created_at="x",
                         transcript_path=outside / "transcript.json")
    monkeypatch.setattr(lib_mod, "load_library", lambda cfg: [entry])
    r = client.delete("/api/videos/zzz").json()
    assert r["removed_dir"] is False
    assert (outside / "transcript.json").is_file()


# ---------- 问视频 ----------


def test_ask_answers_with_citations_and_persists(client):
    r0 = client.get("/api/videos/BVAAA/questions").json()
    assert r0["questions"] == [] and r0["estimate"]["truncated"] is False
    assert r0["estimate"]["cost"] is not None

    r = client.post("/api/videos/BVAAA/ask", json={"question": "讲了什么？"}).json()
    assert r["citations"] == [0, 4] and r["truncated"] is False
    assert r["provider"] == "fake/model" and r["cost"] is not None
    assert "[00:00]" in FakeSummarizer.calls[-1] and "问题：讲了什么？" in FakeSummarizer.calls[-1]

    # 第二问带历史
    client.post("/api/videos/BVAAA/ask", json={"question": "再详细点", "history": [{"question": "讲了什么？", "answer": r["answer"]}]})
    assert "之前的问答" in FakeSummarizer.calls[-1]

    qs = client.get("/api/videos/BVAAA/questions").json()["questions"]
    assert [q["question"] for q in qs] == ["讲了什么？", "再详细点"]
    assert qs[0]["citations"] == [0, 4]

    assert client.delete(f"/api/questions/{qs[0]['id']}").json()["deleted"] is True
    assert len(client.get("/api/videos/BVAAA/questions").json()["questions"]) == 1
    assert client.post("/api/videos/BVAAA/ask", json={"question": "  "}).status_code == 400
    assert client.post("/api/videos/nope/ask", json={"question": "x"}).status_code == 404


def test_deleting_video_removes_its_questions(client):
    client.post("/api/videos/BVAAA/ask", json={"question": "q"})
    client.delete("/api/videos/BVAAA")
    # 视频没了，问答记录也不该留着
    assert client.get("/api/videos/BVAAA/questions").status_code == 404


# ---------- 问 UP 主 ----------


def test_uploader_materials_prefer_summaries_and_fall_back_to_transcript(client):
    r = client.get("/api/uploaders/某 UP").json()
    assert [v["material"] for v in r["videos"]] == ["overall"]
    assert r["no_summary"] == 0 and r["estimate"]["cost"] is not None
    assert client.get("/api/uploaders/没有的人").status_code == 404

    a = client.post("/api/uploaders/某 UP/ask", json={"question": "说过什么？"}).json()
    assert a["citations"] == ["BVAAA"] and a["used"]["calls"] == 1
    assert "=== 【1】A 视频" in FakeSummarizer.calls[-1] and "缓存里的总结" in FakeSummarizer.calls[-1]
    qs = client.get("/api/uploaders/某 UP").json()["questions"]
    assert len(qs) == 1 and qs[0]["citations"] == ["BVAAA"]
    assert client.get("/api/usage").json()["recent"][0]["kind"] == "uploader_qa"
    assert client.post("/api/uploaders/某 UP/ask", json={"question": " "}).status_code == 400


def test_uploader_without_any_summary_uses_transcript_head(client, workspace):
    # 把 A 的总结删掉，只剩转写
    from video_summarizer.cache import Cache
    Cache(workspace["cfg"].cache_db).clear("BVAAA")
    Cache(workspace["cfg"].cache_db).put_transcript(workspace["key_a"], __import__("video_summarizer.models", fromlist=["Transcript"]).Transcript.load(workspace["dir_a"] / "transcript.json"))
    r = client.get("/api/uploaders/某 UP").json()
    assert [v["material"] for v in r["videos"]] == ["transcript"] and r["no_summary"] == 1


# ---------- 搜索 ----------


def test_search_indexes_library_on_startup_and_groups_by_video(client):
    r = client.get("/api/search?q=第二句").json()
    assert r["transcript_hits"] == 1 and r["summary_hits"] == 0
    assert [v["video_id"] for v in r["videos"]] == ["BVAAA"]
    hit = r["videos"][0]["hits"][0]
    assert hit["kind"] == "transcript" and hit["start"] == 0.0 and "第二句" in hit["snippet"]
    assert r["videos"][0]["uploader"] == "某 UP"

    r = client.get("/api/search?q=缓存里").json()
    assert r["summary_hits"] == 1 and r["videos"][0]["hits"][0]["ref"] == "overall"
    assert client.get("/api/search?q=").json()["videos"] == []


def test_search_index_follows_summary_jobs_and_deletes(client):
    assert client.get("/api/search?q=假的总结").json()["videos"] == []
    r = client.post("/api/videos/BVAAA/summaries", json={"type": "key_points"}).json()
    _wait_job(client, r["job"]["id"])
    hits = client.get("/api/search?q=假的总结").json()
    assert hits["summary_hits"] == 1 and hits["videos"][0]["hits"][0]["ref"] == "key_points"
    client.delete("/api/videos/BVAAA")
    assert client.get("/api/search?q=第二句").json()["videos"] == []
    assert client.post("/api/search/reindex").json()["videos"] == 1


# ---------- 存储 ----------


def test_storage_report_and_clear_audio(client, workspace):
    audio = workspace["dir_a"] / "audio"
    audio.mkdir()
    (audio / "BVAAA.wav").write_bytes(bytes(4096))
    r = client.get("/api/storage").json()
    assert r["audio_bytes"] == 4096 and r["audio_dirs"] == 1 and r["other_bytes"] > 0
    c = client.post("/api/storage/clear-audio").json()
    assert c == {"dirs": 1, "freed_bytes": 4096}
    assert not audio.exists() and (workspace["dir_a"] / "transcript.json").is_file()
    assert client.get("/api/storage").json()["audio_bytes"] == 0


# ---------- 静态前端 ----------


def test_spa_fallback_serves_index_when_built(client):
    r = client.get("/library")
    assert r.status_code == 200
    if (api_mod.DIST_DIR / "index.html").is_file():
        assert "<div id=\"root\">" in r.text
    else:
        assert "前端还没构建" in r.text
