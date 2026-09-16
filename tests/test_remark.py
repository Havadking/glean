from __future__ import annotations

import json
from pathlib import Path
import pytest
from starlette.testclient import TestClient

from video_summarizer.cache import Cache
from video_summarizer.config import Config
from video_summarizer.models import Segment, Transcript
from video_summarizer.web import library as library_mod
from video_summarizer.web.api import create_app


@pytest.fixture
def cache(tmp_path):
    return Cache(tmp_path / "cache.sqlite")


def test_cache_remark_crud(cache):
    video_id = "v_12345"
    assert cache.get_remark(video_id) is None
    assert cache.get_all_remarks() == {}

    # 设置备注
    cache.set_remark(video_id, "我的视频备注")
    assert cache.get_remark(video_id) == "我的视频备注"
    assert cache.get_all_remarks() == {video_id: "我的视频备注"}

    # 更新备注
    cache.set_remark(video_id, "新备注名称")
    assert cache.get_remark(video_id) == "新备注名称"

    # 传空或空白字符，清空恢复
    cache.set_remark(video_id, "   ")
    assert cache.get_remark(video_id) is None
    assert cache.get_all_remarks() == {}


def test_library_entry_display_title():
    entry = library_mod.LibraryEntry(
        video_id="v_1",
        title="Douyin video #123",
        source_url="https://example.com",
        duration_sec=60.0,
        source_type="asr",
        language="zh",
        segment_count=5,
        speaker_count=1,
        created_at="2026-09-16T12:00:00Z",
    )
    # 没有备注时就是原名称
    assert entry.display_title == "Douyin video #123"

    # 有备注时优先展示备注
    entry.remark = "牛市心态分析"
    assert entry.display_title == "牛市心态分析"

    # 备注是纯空白时回退展示原名称
    entry.remark = "   "
    assert entry.display_title == "Douyin video #123"


def test_set_video_remark_and_persistence(tmp_path):
    cache_path = tmp_path / "cache.sqlite"
    out_dir = tmp_path / "output"
    video_dir = out_dir / "test_video_v1"
    video_dir.mkdir(parents=True)
    transcript_file = video_dir / "transcript.json"

    transcript = Transcript(
        source_url="https://example.com/v1",
        source_type="asr",
        language="zh",
        duration_sec=100.0,
        video_id="v1",
        title="原始名称",
        segments=[Segment(start=0.0, end=1.0, text="测试")],
    )
    transcript.save(transcript_file)

    cfg = Config()
    cfg.cache_db = cache_path
    cfg.output_dir = out_dir

    # 初始加载
    entries = library_mod.load_library(cfg)
    assert len(entries) == 1
    assert entries[0].video_id == "v1"
    assert entries[0].remark is None
    assert entries[0].display_title == "原始名称"

    # 设置备注
    saved_remark = library_mod.set_video_remark(cfg, "v1", "备注A")
    assert saved_remark == "备注A"

    # 再次加载
    entries2 = library_mod.load_library(cfg)
    assert entries2[0].remark == "备注A"
    assert entries2[0].display_title == "备注A"

    # 检查本地 transcript.json 的 meta 是否同步写入
    raw_saved = json.loads(transcript_file.read_text(encoding="utf-8"))
    assert raw_saved.get("meta", {}).get("remark") == "备注A"

    # 清空备注，恢复原名
    library_mod.set_video_remark(cfg, "v1", "")
    entries3 = library_mod.load_library(cfg)
    assert entries3[0].remark is None
    assert entries3[0].display_title == "原始名称"

    raw_saved_cleared = json.loads(transcript_file.read_text(encoding="utf-8"))
    assert "remark" not in raw_saved_cleared.get("meta", {})


def test_web_api_remark(tmp_path):
    cache_path = tmp_path / "cache.sqlite"
    out_dir = tmp_path / "output"
    video_dir = out_dir / "v2"
    video_dir.mkdir(parents=True)
    transcript_file = video_dir / "transcript.json"

    transcript = Transcript(
        source_url="https://example.com/v2",
        source_type="asr",
        language="zh",
        duration_sec=30.0,
        video_id="v2",
        title="Douyin video #7685",
        segments=[Segment(start=0.0, end=1.0, text="你好")],
    )
    transcript.save(transcript_file)

    import yaml
    from video_summarizer.config import load_config

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "output_dir": str(out_dir),
        "cache_db": str(cache_path),
        "download": {"batch_delay_sec": 0},
    }, allow_unicode=True), encoding="utf-8")
    cfg = load_config(cfg_path)

    app = create_app(cfg)
    client = TestClient(app)

    # 1. 查询视频详情，初始 remark 为 null
    res = client.get("/api/videos/v2")
    assert res.status_code == 200
    data = res.json()
    assert data["title"] == "Douyin video #7685"
    assert data["remark"] is None

    # 2. 修改备注
    res = client.post("/api/videos/v2/remark", json={"remark": "A股走势分析"})
    assert res.status_code == 200
    assert res.json() == {"video_id": "v2", "remark": "A股走势分析"}

    # 3. 再次查询详情与库列表
    res = client.get("/api/videos/v2")
    assert res.status_code == 200
    assert res.json()["remark"] == "A股走势分析"

    res_lib = client.get("/api/library")
    assert res_lib.status_code == 200
    lib_data = res_lib.json()
    entry = lib_data["groups"][0]["entries"][0]
    assert entry["video_id"] == "v2"
    assert entry["title"] == "Douyin video #7685"
    assert entry["remark"] == "A股走势分析"

    # 4. 清空备注，恢复原名
    res = client.post("/api/videos/v2/remark", json={"remark": ""})
    assert res.status_code == 200
    assert res.json()["remark"] is None

    res = client.get("/api/videos/v2")
    assert res.status_code == 200
    assert res.json()["remark"] is None
