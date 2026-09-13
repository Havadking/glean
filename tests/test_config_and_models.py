"""配置加载和中间数据结构（DESIGN.md 5、6 节）。"""

from __future__ import annotations

import json

import pytest

from video_summarizer.config import load_config
from video_summarizer.errors import ConfigError
from video_summarizer.models import Segment, SummaryOptions, Transcript


# ---------- Transcript ----------


def test_json_round_trip_preserves_speakers(tmp_path, dialogue):
    path = tmp_path / "transcript.json"
    dialogue.save(path)
    back = Transcript.load(path)
    assert back.speakers == dialogue.speakers
    assert back.segments[0].speaker == "Speaker_1"
    assert back.meta == dialogue.meta


def test_saved_json_matches_the_documented_shape(tmp_path, transcript):
    """DESIGN.md 5.1 规定的字段，别改着改着就漂了。"""
    path = tmp_path / "t.json"
    transcript.save(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert set(raw) >= {"source_url", "source_type", "language", "duration_sec", "segments"}
    assert set(raw["segments"][0]) == {"start", "end", "text", "speaker"}


def test_from_dict_does_not_mutate_input(transcript):
    raw = transcript.to_dict()
    Transcript.from_dict(raw)
    assert "segments" in raw, "from_dict 不该把调用方的字典掏空"


def test_full_text_skips_blank_segments():
    t = Transcript(source_url="u", source_type="asr", language="zh", duration_sec=3.0,
                   segments=[Segment(0, 1, "有内容"), Segment(1, 2, "   "), Segment(2, 3, "也有")])
    assert t.full_text == "有内容\n也有"


def test_speakers_preserve_first_appearance_order():
    t = Transcript(source_url="u", source_type="asr", language="zh", duration_sec=4.0,
                   segments=[Segment(0, 1, "a", speaker="B"), Segment(1, 2, "b", speaker="A"),
                             Segment(2, 3, "c", speaker="B")])
    assert t.speakers == ["B", "A"]


def test_no_speakers_when_unlabelled(transcript):
    assert transcript.speakers == []


def test_segment_duration_never_negative():
    assert Segment(start=5.0, end=1.0, text="时间戳反了").duration == 0.0


# ---------- 配置 ----------


def test_missing_config_falls_back_to_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = load_config(None)
    assert cfg.asr.provider == "funasr"
    assert cfg.summarizer.provider == "openai"


def test_relative_paths_resolve_against_the_config_file(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "output_dir: ./产物\ncache_db: ./c.sqlite\n", encoding="utf-8")
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.output_dir == tmp_path / "产物"
    assert cfg.cache_db == tmp_path / "c.sqlite"


def test_unknown_field_is_rejected_loudly(tmp_path):
    """配置项写错字应该报出来，而不是静默用默认值。"""
    (tmp_path / "config.yaml").write_text(
        "asr:\n  provder: funasr\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="provder"):
        load_config(tmp_path / "config.yaml")


def test_section_must_be_a_mapping(tmp_path):
    (tmp_path / "config.yaml").write_text("asr: 就一个字符串\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="映射"):
        load_config(tmp_path / "config.yaml")


def test_missing_config_file_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="不存在"):
        load_config(tmp_path / "没有这个.yaml")


def test_api_key_comes_from_the_named_env_var(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(
        "summarizer:\n  api_key_env: MY_TEST_KEY\n", encoding="utf-8")
    monkeypatch.setenv("MY_TEST_KEY", "sk-test")
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.summarizer.api_key == "sk-test"

    monkeypatch.delenv("MY_TEST_KEY")
    assert load_config(tmp_path / "config.yaml").summarizer.api_key is None


def test_shipped_config_parses(monkeypatch):
    """仓库里那份 config.yaml 本身得是合法的。

    load_config 会顺带加载 .env，那会把真实密钥灌进 pytest 进程的环境变量里，
    影响后面所有测试（写这套测试时就因此真的往外发过一次请求）。这里屏蔽掉。
    """
    from pathlib import Path

    import video_summarizer.config as config_mod

    monkeypatch.setattr(config_mod, "load_dotenv", lambda *a, **kw: False)
    root = Path(__file__).resolve().parents[1]
    cfg = load_config(root / "config.yaml")
    assert cfg.asr.provider and cfg.summarizer.provider


# ---------- SummaryOptions ----------


def test_summary_options_defaults():
    o = SummaryOptions()
    assert o.summary_type == "overall" and o.language == "zh"
    assert o.extra_instructions is None


def test_extract_url_from_share_text():
    from video_summarizer.ytdlp_base import extract_url

    assert extract_url("  https://www.bilibili.com/video/BV1x/  ") == "https://www.bilibili.com/video/BV1x/"
    assert extract_url("8.52 复制打开抖音，看看【杨超越的作品】 https://v.douyin.com/iAbCdEf/ 复制此链接。") == "https://v.douyin.com/iAbCdEf/"
    assert extract_url("链接在这（https://x.y/z），看看") == "https://x.y/z"
    assert extract_url("不是链接") == "不是链接"
    assert extract_url("") == ""
