"""历史库：把缓存和输出目录合并成一份"处理过的视频"列表。

两个来源都要读：SQLite 缓存是 v0.5 才加的，在那之前跑的视频只有输出目录里的产物。
按 video_id 聚合，同一个视频有多份转写（比如先走字幕后来又强制跑了 ASR）时取最新的一份。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..cache import Cache
from ..config import Config
from ..models import Transcript
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
    uploader: str | None = None
    upload_date: str | None = None   # YYYYMMDD
    thumbnail: str | None = None
    cache_key: str | None = None

    @property
    def work_dir(self) -> Path | None:
        return self.transcript_path.parent if self.transcript_path else None

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


def load_transcript(cfg: Config, entry: LibraryEntry) -> Transcript | None:
    """读一个视频的转写：缓存优先，其次目录里的 transcript.json。"""
    if entry.cache_key:
        t = Cache(cfg.cache_db).get_transcript(entry.cache_key)
        if t is not None:
            return t
    if entry.transcript_path and entry.transcript_path.is_file():
        try:
            return Transcript.load(entry.transcript_path)
        except (OSError, ValueError, TypeError) as exc:
            log.warning("读不了 %s：%s", entry.transcript_path, exc)
    return None


@dataclass
class SummaryText:
    summary_type: str
    provider: str
    created_at: str
    content: str
    source: str          # cache | file

    @property
    def label(self) -> str:
        template = TEMPLATES.get(self.summary_type)
        return template.label if template else self.summary_type


def load_summaries(cfg: Config, entry: LibraryEntry) -> dict[str, SummaryText]:
    """一个视频每种类型最新的总结正文：缓存优先，其次目录里的 summary.md。"""
    out: dict[str, SummaryText] = {}
    for s in Cache(cfg.cache_db).summaries_for_video(entry.video_id):
        if s.summary_type in out or not s.content:
            continue
        out[s.summary_type] = SummaryText(s.summary_type, s.provider, s.created_at, s.content, "cache")
    for ref in entry.summaries:
        if ref.path is None or ref.summary_type in out or ref.summary_type == "unknown":
            continue
        try:
            text = ref.path.read_text(encoding="utf-8")
        except OSError:
            continue
        body = text.split("\n---\n", 1)[1] if "\n---\n" in text else text
        out[ref.summary_type] = SummaryText(ref.summary_type, ref.provider, ref.created_at, body.strip(), "file")
    return out


def audio_path(entry: LibraryEntry) -> Path | None:
    """ASR 跑过就会在产物目录下留一份 16k wav；字幕路径的视频没有，清过音频的也没有。"""
    if entry.work_dir is None:
        return None
    audio_dir = entry.work_dir / "audio"
    if not audio_dir.is_dir():
        return None
    try:
        wavs = [p for p in audio_dir.iterdir()
                if p.is_file() and p.suffix.lower() == ".wav" and p.stat().st_size > 0]
    except OSError:
        return None
    if not wavs:
        return None
    # 目录里正常只有一份；万一有多份，优先 <video_id>.wav
    return next((p for p in wavs if p.stem == entry.video_id), wavs[0])


def find_entry(cfg: Config, video_id: str) -> LibraryEntry | None:
    for e in load_library(cfg):
        if e.video_id == video_id:
            return e
    return None


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
            uploader=t.uploader, upload_date=t.upload_date, thumbnail=t.thumbnail,
            cache_key=t.key,
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
            uploader=meta.get("uploader"),
            upload_date=meta.get("upload_date"),
            thumbnail=meta.get("thumbnail"),
            cache_key=meta.get("cache_key"),
            summaries=[_summary_from_file(summary_path)] if summary_path.is_file() else [],
        ))
    return entries


_FRONT_RE = re.compile(r"^- (总结类型|总结模型|生成时间)：(.+)$", re.M)


def _summary_from_file(path: Path) -> SummaryRef:
    """summary.md 开头有一段来源信息，能读出类型和模型；读不出就标 unknown。"""
    fields: dict[str, str] = {}
    try:
        head = path.read_text(encoding="utf-8")[:2000]
        fields = {k: v.strip() for k, v in _FRONT_RE.findall(head)}
    except OSError:
        pass
    return SummaryRef(
        summary_type=fields.get("总结类型", "unknown"),
        provider=fields.get("总结模型", "—"),
        created_at=fields.get("生成时间") or _mtime_iso(path),
        path=path,
    )


def _merge(base: LibraryEntry, extra: LibraryEntry) -> None:
    """缓存条目缺的信息用产物补上，反之亦然。"""
    base.transcript_path = base.transcript_path or extra.transcript_path
    base.asr_model = base.asr_model or extra.asr_model
    base.meta = base.meta or extra.meta
    base.uploader = base.uploader or extra.uploader
    base.upload_date = base.upload_date or extra.upload_date
    base.thumbnail = base.thumbnail or extra.thumbnail
    base.cache_key = base.cache_key or extra.cache_key
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


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for f in path.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    except OSError:
        pass
    return total


def storage_report(cfg: Config) -> dict[str, Any]:
    """产物目录占了多少：音频（可删，重跑会再下）、其他（转写和总结，小）。"""
    audio = other = 0
    audio_dirs = 0
    if cfg.output_dir.is_dir():
        for d in cfg.output_dir.iterdir():
            if not d.is_dir():
                continue
            a = d / "audio"
            if a.is_dir():
                size = _dir_size(a)
                if size:
                    audio += size
                    audio_dirs += 1
            other += sum(_dir_size(p) if p.is_dir() else p.stat().st_size
                         for p in d.iterdir() if p.name != "audio")
    cache_bytes = cfg.cache_db.stat().st_size if cfg.cache_db and cfg.cache_db.is_file() else 0
    return {"audio_bytes": audio, "audio_dirs": audio_dirs, "other_bytes": other, "cache_bytes": cache_bytes,
            "output_dir": str(cfg.output_dir)}


def clear_audio(cfg: Config) -> tuple[int, int]:
    """删掉所有产物目录下的 audio/。返回 (删了几个目录, 释放字节)。转写还在，重跑 ASR 才会再下。"""
    import shutil

    n = freed = 0
    if not cfg.output_dir.is_dir():
        return (0, 0)
    for d in cfg.output_dir.iterdir():
        a = d / "audio"
        if d.is_dir() and a.is_dir():
            size = _dir_size(a)
            try:
                shutil.rmtree(a)
            except OSError as exc:
                log.warning("删不掉 %s：%s", a, exc)
                continue
            n += 1
            freed += size
    return (n, freed)


def group_by_uploader(entries: list[LibraryEntry]) -> list[tuple[str | None, list[LibraryEntry]]]:
    """按 UP 主分组，组内保持传入顺序；没有 UP 主信息的归到最后一组（None）。

    组的顺序按"该 UP 主最近一次处理"倒序，追得勤的排前面。
    """
    groups: dict[str | None, list[LibraryEntry]] = {}
    for e in entries:
        groups.setdefault(e.uploader or None, []).append(e)
    named = [(k, v) for k, v in groups.items() if k is not None]
    named.sort(key=lambda kv: max(e.created_at for e in kv[1]), reverse=True)
    if None in groups:
        named.append((None, groups[None]))
    return named
