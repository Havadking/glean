"""存到 Obsidian：渲染、文件名、按 vsum_id 找回挪走的笔记、更新时保留「我的笔记」。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
import yaml

from video_summarizer.cache import Cache, summary_key, transcript_key
from video_summarizer.config import load_config
from video_summarizer.models import Segment, Transcript
from video_summarizer.web import library as library_mod
from video_summarizer.web import obsidian as ob


def test_sanitize_filename_strips_forbidden_chars():
    assert ob.sanitize_filename("9月17日解读#美联储 #加息 a/b:c?", "x") == "9月17日解读 美联储 加息 a b c"
    assert ob.sanitize_filename("  ...  ", "BV1") == "BV1"
    assert len(ob.sanitize_filename("长" * 200, "x")) == 80


def test_clean_tag():
    assert ob.clean_tag("成分 分析") == "成分-分析"
    assert ob.clean_tag("#C++") == "C"
    assert ob.clean_tag("2024") is None
    assert ob.clean_tag("AI/Agent") == "AI/Agent"


def test_timestamp_url_per_site():
    assert ob.timestamp_url("https://www.bilibili.com/video/BV1/?p=2", 83) == "https://www.bilibili.com/video/BV1/?p=2&t=83"
    assert ob.timestamp_url("https://www.youtube.com/watch?v=abc&t=5s", 61.9) == "https://www.youtube.com/watch?v=abc&t=61s"
    assert ob.timestamp_url("https://www.douyin.com/video/1", 10) is None


def test_link_timestamps_skips_existing_links_and_unknown_sites():
    url = "https://www.bilibili.com/video/BV1/"
    out = ob.link_timestamps("- [01:02] 开头 [1:00:03] 已有 [00:05](x)", url)
    assert "[01:02](https://www.bilibili.com/video/BV1/?t=62)" in out
    assert "[1:00:03](https://www.bilibili.com/video/BV1/?t=3603)" in out
    assert "[00:05](x)" in out
    assert ob.link_timestamps("[01:02]", "https://www.douyin.com/video/1") == "[01:02]"


def test_demote_headings_keeps_relative_levels_and_code():
    text = "# 根\n## 枝\n```\n# 注释\n```\n- 叶"
    assert ob.demote_headings(text) == "### 根\n#### 枝\n```\n# 注释\n```\n- 叶"
    assert ob.demote_headings("### 已经够深") == "### 已经够深"


def test_build_frontmatter_merges_user_fields():
    entry = library_mod.LibraryEntry(
        video_id="BV1", title="t", source_url="u", duration_sec=65, source_type="asr", language="zh",
        segment_count=1, speaker_count=0, created_at="", uploader="某 UP", upload_date="20240102",
    )
    fresh = ob.build_frontmatter(entry, ["美食"], today=date(2026, 9, 27))
    assert fresh["author"] == "[[某 UP]]" and fresh["published"] == date(2024, 1, 2)
    assert fresh["duration"] == "01:05" and fresh["tags"] == ["clippings", "video", "美食"]

    old = {"status": "done", "created": date(2025, 1, 1), "tags": ["mine"], "rating": 5, "url": "old"}
    merged = ob.build_frontmatter(entry, ["美食"], old, today=date(2026, 9, 27))
    assert merged["status"] == "done" and merged["created"] == date(2025, 1, 1) and merged["rating"] == 5
    assert merged["url"] == "u"
    assert merged["tags"] == ["mine", "clippings", "video", "美食"]


# ---------- 端到端 ----------


@pytest.fixture
def setup(tmp_path: Path):
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "output_dir": str(tmp_path / "output"), "cache_db": str(tmp_path / "cache.sqlite"),
        "obsidian": {"vault_path": str(vault), "folder": "Inbox/Video", "attachment_folder": "att"},
    }, allow_unicode=True), encoding="utf-8")
    cfg = load_config(cfg_path)

    t = Transcript(
        source_url="https://www.bilibili.com/video/BVX/", source_type="asr", language="zh",
        duration_sec=70.0, title="炒韭菜 #美食", video_id="BVX",
        segments=[Segment(0.0, 4.0, "第一句。"), Segment(65.0, 70.0, "最后一句。")],
        meta={"extractor": "BiliBili", "uploader": "厨子", "upload_date": "20240102",
              "thumbnail": "https://i0.hdslb.com/a.jpg"},
    )
    key = transcript_key(video_id="BVX", extractor="BiliBili", source_type="asr", asr_provider="funasr",
                         asr_model="m", asr_language=None, diarize=False)
    t.save(tmp_path / "output" / "x-BVX" / "transcript.json")
    cache = Cache(cfg.cache_db)
    cache.put_transcript(key, t)
    for typ, content in (("overall", "# 总结\n讲了炒韭菜 [01:05]。"), ("mindmap", "# 根\n## 枝\n- 叶")):
        cache.put_summary(summary_key(transcript_key=key, provider_desc="p", summary_type=typ, language="zh",
                                      extra=None),
                          transcript_key=key, transcript=t, provider_desc="p", summary_type=typ,
                          language="zh", content=content)
    cache.add_tag("BVX", "家常 菜")
    return cfg, vault


def _fake_thumb(calls: list[str]):
    def fetch(url: str):
        calls.append(url)
        return b"\xff\xd8jpg", "image/jpeg"
    return fetch


def test_export_creates_then_updates_in_place(setup):
    cfg, vault = setup
    calls: list[str] = []
    entry = library_mod.find_entry(cfg, "BVX")
    res = ob.export(cfg, entry, app_url="http://127.0.0.1:8000", fetch_thumb=_fake_thumb(calls))

    assert res.action == "created"
    assert res.rel == "Inbox/Video/炒韭菜 美食.md"
    text = res.path.read_text(encoding="utf-8")
    fm, body = ob.split_note(text)
    assert fm["vsum_id"] == "BVX" and fm["source"] == "video-summarizer" and fm["platform"] == "bilibili"
    assert fm["tags"] == ["clippings", "video", "家常-菜"]
    assert "![[att/vsum-BVX.jpg]]" in body and (vault / "att" / "vsum-BVX.jpg").is_file()
    assert "## 总体\n\n### 总结\n讲了炒韭菜 [01:05](https://www.bilibili.com/video/BVX/?t=65)。" in body
    assert "## 思维导图\n\n### 根\n#### 枝" in body
    assert "> [!quote]- 展开（" in body and "**[01:05](https://www.bilibili.com/video/BVX/?t=65)** 最后一句。" in body
    assert "[在拾光笺中打开](http://127.0.0.1:8000/video/BVX)" in body
    assert body.rstrip().endswith(ob.MY_NOTES_HEADING)
    assert res.uri.startswith("obsidian://open?path=")

    # 用户在 Obsidian 里：挪走、改名、改状态、写笔记
    moved = vault / "30 AI Lab" / "韭菜.md"
    moved.parent.mkdir()
    fm["status"] = "done"
    moved.write_text(ob.dump_frontmatter(fm) + body.replace(ob.MY_NOTES_HEADING, ob.MY_NOTES_HEADING + "\n好吃！"),
                     encoding="utf-8")
    res.path.unlink()
    assert ob.status(cfg, "BVX")["path"] == "30 AI Lab/韭菜.md"

    res2 = ob.export(cfg, entry, include=["summaries"], fetch_thumb=_fake_thumb(calls))
    assert res2.action == "updated" and res2.path == moved
    fm2, body2 = ob.split_note(moved.read_text(encoding="utf-8"))
    assert fm2["status"] == "done"
    assert body2.rstrip().endswith(ob.MY_NOTES_HEADING + "\n好吃！")
    assert "转写全文" not in body2
    assert len(calls) == 1          # 封面已经在附件目录里，不再下
    assert not (vault / "Inbox" / "Video" / "炒韭菜 美食.md").exists()


def test_export_does_not_clobber_same_named_note(setup):
    cfg, vault = setup
    folder = vault / "Inbox" / "Video"
    folder.mkdir(parents=True)
    (folder / "炒韭菜 美食.md").write_text("别人的笔记", encoding="utf-8")
    res = ob.export(cfg, library_mod.find_entry(cfg, "BVX"), fetch_thumb=_fake_thumb([]))
    assert res.rel == "Inbox/Video/炒韭菜 美食 (BVX).md"
    assert (folder / "炒韭菜 美食.md").read_text(encoding="utf-8") == "别人的笔记"


def test_export_without_vault_raises(setup, tmp_path):
    cfg, _vault = setup
    cfg.obsidian.vault_path = str(tmp_path / "nope")
    with pytest.raises(ob.ObsidianError):
        ob.export(cfg, library_mod.find_entry(cfg, "BVX"))
    cfg.obsidian.vault_path = None
    assert ob.status(cfg, "BVX") is None
