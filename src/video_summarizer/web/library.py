"""历史库：把缓存和输出目录合并成一份"处理过的视频"列表。

两个来源都要读：SQLite 缓存是 v0.5 才加的，在那之前跑的视频只有输出目录里的产物。
按 video_id 聚合，同一个视频有多份转写（比如先走字幕后来又强制跑了 ASR）时取最新的一份。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..cache import Cache
from ..config import Config
from ..summarizer.prompts import TEMPLATES

log = logging.getLogger(__name__)


@dataclass
class SummaryRef:
    """一份已经生成过的总结。"""

    summary_type: str
    provider: str
    created_at: str
    cache_key: str | None = None
    path: Path | None = None

    @property
    def label(self) -> str:
        template = TEMPLATES.get(self.summary_type)
        if template:
            return template.label
        # 目录里的 summary.md 没记类型，别把内部标记直接甩给用户看
        return "已有总结" if self.summary_type == "unknown" else self.summary_type


@dataclass
class LibraryEntry:
    """历史列表里的一张卡片 = 一个视频。"""

    video_id: str
    title: str
    source_url: str
    duration_sec: float
    source_type: str          # subtitle | asr
    language: str | None
    segment_count: int
    speaker_count: int
    created_at: str
    asr_model: str | None = None
    transcript_path: Path | None = None
    summaries: list[SummaryRef] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def source_label(self) -> str:
        if self.source_type == "subtitle":
            return "官方字幕"
        return f"语音识别 {self.asr_model}" if self.asr_model else "语音识别"

    @property
    def missing_types(self) -> list[str]:
        done = {s.summary_type for s in self.summaries}
        return [k for k in TEMPLATES if k not in done]


def load_library(cfg: Config) -> list[LibraryEntry]:
    """读出全部历史，按处理时间倒序。"""
    entries: dict[str, LibraryEntry] = {}

    for entry in _from_cache(cfg):
        entries[entry.video_id] = entry
    for entry in _from_output_dir(cfg):
        existing = entries.get(entry.video_id)
        if existing is None:
            entries[entry.video_id] = entry
        else:
            _merge(existing, entry)

    result = list(entries.values())
    result.sort(key=lambda e: e.created_at, reverse=True)
    return result


def _from_cache(cfg: Config) -> list[LibraryEntry]:
    cache = Cache(cfg.cache_db)
    if not cache.enabled:
        return []

    summaries_by_transcript: dict[str, list[SummaryRef]] = {}
    for s in cache.list_summaries(limit=500):
        summaries_by_transcript.setdefault(s.transcript_key, []).append(
            SummaryRef(summary_type=s.summary_type, provider=s.provider,
                       created_at=s.created_at, cache_key=s.key)
        )

    entries = []
    for t in cache.list_transcripts(limit=500):
        entries.append(LibraryEntry(
            video_id=t.video_id,
            title=t.title or t.video_id,
            source_url=t.source_url,
            duration_sec=t.duration_sec,
            source_type=t.source_type,
            language=t.language,
            segment_count=t.segment_count,
            # 缓存里只记了"有没有说话人"，具体几个要读产物才知道
            speaker_count=2 if t.has_speakers else 0,
            created_at=t.created_at,
            summaries=sorted(summaries_by_transcript.get(t.key, []),
                             key=lambda s: s.created_at, reverse=True),
        ))
    # 同一个视频有多份转写时保留最新的
    newest: dict[str, LibraryEntry] = {}
    for e in sorted(entries, key=lambda e: e.created_at):
        newest[e.video_id] = e
    return list(newest.values())


def _from_output_dir(cfg: Config) -> list[LibraryEntry]:
    """扫输出目录。v0.5 之前跑的视频只有这里有。"""
    if not cfg.output_dir.is_dir():
        return []

    entries = []
    for path in cfg.output_dir.glob("*/transcript.json"):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.debug("跳过读不了的转写 %s：%s", path, exc)
            continue

        segments = raw.get("segments") or []
        speakers = {s.get("speaker") for s in segments if s.get("speaker")}
        meta = raw.get("meta") or {}
        summary_path = path.parent / "summary.md"

        entries.append(LibraryEntry(
            video_id=str(raw.get("video_id") or path.parent.name),
            title=raw.get("title") or path.parent.name,
            source_url=raw.get("source_url") or "",
            duration_sec=float(raw.get("duration_sec") or 0.0),
            source_type=raw.get("source_type") or "asr",
            language=raw.get("language"),
            segment_count=len(segments),
            speaker_count=len(speakers),
            # 产物没记处理时间，用文件修改时间凑合
            created_at=_mtime_iso(path),
            asr_model=meta.get("asr_model"),
            transcript_path=path,
            meta=meta,
            summaries=(
                # 目录里的 summary.md 不知道是哪种类型，标成 unknown 但仍然可读
                [SummaryRef(summary_type=meta.get("summary_type", "unknown"),
                            provider=meta.get("summary_provider", "—"),
                            created_at=_mtime_iso(summary_path), path=summary_path)]
                if summary_path.is_file() else []
            ),
        ))
    return entries


def _merge(base: LibraryEntry, extra: LibraryEntry) -> None:
    """缓存条目缺的信息用产物补上，反之亦然。"""
    base.transcript_path = base.transcript_path or extra.transcript_path
    base.asr_model = base.asr_model or extra.asr_model
    base.meta = base.meta or extra.meta
    if not base.speaker_count and extra.speaker_count:
        base.speaker_count = extra.speaker_count
    if not base.source_url:
        base.source_url = extra.source_url
    # 缓存里已经有结构化的总结记录时，就不要再把目录里那份 unknown 的塞进来了
    if not base.summaries:
        base.summaries = extra.summaries


def _mtime_iso(path: Path) -> str:
    from datetime import datetime, timezone

    try:
        ts = path.stat().st_mtime
    except OSError:
        return ""
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")
