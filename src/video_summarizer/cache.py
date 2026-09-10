"""SQLite 缓存（DESIGN.md 路线图 v0.5）。

存两样东西：转写和总结。前者是为了省时间（ASR 是整条链路里最耗时的一步），
后者是为了省钱（同样的转写 + 同样的模型 + 同样的总结类型，没必要再花一次钱）。

**按输入指纹索引，不是按文件路径。** 这修掉了原来"只要 output 目录里有
transcript.json 就复用"的毛病：换了 ASR 模型、开了说话人分离，指纹就变了，
不会再静默拿旧结果；反过来视频改了标题、输出目录换了名字，指纹没变，照样命中。

缓存是索引层，不是产物本身 —— transcript.json 和 summary.md 照旧写到输出目录。
删掉 cache.sqlite 只会让下次重算，不会丢东西。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Transcript

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transcripts (
    key           TEXT PRIMARY KEY,
    video_id      TEXT NOT NULL,
    source_url    TEXT NOT NULL,
    title         TEXT,
    source_type   TEXT NOT NULL,
    language      TEXT,
    duration_sec  REAL,
    segment_count INTEGER NOT NULL,
    has_speakers  INTEGER NOT NULL DEFAULT 0,
    payload       TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transcripts_video ON transcripts(video_id);
CREATE INDEX IF NOT EXISTS idx_transcripts_created ON transcripts(created_at);

CREATE TABLE IF NOT EXISTS summaries (
    key            TEXT PRIMARY KEY,
    transcript_key TEXT NOT NULL,
    video_id       TEXT NOT NULL,
    title          TEXT,
    provider       TEXT NOT NULL,
    summary_type   TEXT NOT NULL,
    language       TEXT NOT NULL,
    content        TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summaries_transcript ON summaries(transcript_key);
CREATE INDEX IF NOT EXISTS idx_summaries_video ON summaries(video_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fingerprint(parts: dict[str, Any]) -> str:
    """把决定结果的输入拍成一个短哈希。字段顺序无关。"""
    blob = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def transcript_key(
    *,
    video_id: str,
    extractor: str,
    source_type: str,
    subtitle_language: str | None = None,
    automatic_captions: bool = False,
    asr_provider: str | None = None,
    asr_model: str | None = None,
    asr_language: str | None = None,
    diarize: bool = False,
) -> str:
    """转写的指纹。凡是会改变转写内容的输入都要进来。"""
    if source_type == "subtitle":
        parts = {
            "kind": "subtitle",
            "video_id": video_id,
            "extractor": extractor,
            "lang": subtitle_language,
            "auto": automatic_captions,
        }
    else:
        parts = {
            "kind": "asr",
            "video_id": video_id,
            "extractor": extractor,
            "provider": asr_provider,
            "model": asr_model,
            "language": asr_language,
            "diarize": diarize,
        }
    return _fingerprint(parts)


def summary_key(
    *,
    transcript_key: str,
    provider_desc: str,
    summary_type: str,
    language: str,
    extra: str | None,
) -> str:
    """总结的指纹。同样的转写换个模型或换个总结类型，都算不同的东西。"""
    return _fingerprint({
        "transcript": transcript_key,
        "provider": provider_desc,
        "type": summary_type,
        "lang": language,
        "extra": extra or "",
    })


@dataclass
class TranscriptEntry:
    key: str
    video_id: str
    source_url: str
    title: str | None
    source_type: str
    language: str | None
    duration_sec: float
    segment_count: int
    has_speakers: bool
    created_at: str


@dataclass
class SummaryEntry:
    key: str
    transcript_key: str
    video_id: str
    title: str | None
    provider: str
    summary_type: str
    language: str
    created_at: str


class Cache:
    """SQLite 缓存。所有失败都降级成"没命中"，缓存坏了不该拖垮主流程。"""

    def __init__(self, path: Path | None) -> None:
        self.path = Path(path) if path else None
        self._ready = False

    # ---------- 连接 ----------

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def _connect(self) -> sqlite3.Connection | None:
        if self.path is None:
            return None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=10.0)
            conn.row_factory = sqlite3.Row
            if not self._ready:
                conn.executescript(_SCHEMA)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                conn.commit()
                self._ready = True
            return conn
        except sqlite3.Error as exc:
            log.warning("打不开缓存库 %s，这次不走缓存：%s", self.path, exc)
            self.path = None  # 别每次调用都重试同一个坏文件
            return None

    # ---------- 转写 ----------

    def get_transcript(self, key: str) -> Transcript | None:
        conn = self._connect()
        if conn is None:
            return None
        try:
            with closing(conn):
                row = conn.execute(
                    "SELECT payload FROM transcripts WHERE key = ?", (key,)
                ).fetchone()
        except sqlite3.Error as exc:
            log.warning("读缓存失败，按未命中处理：%s", exc)
            return None
        if row is None:
            return None
        try:
            return Transcript.from_dict(json.loads(row["payload"]))
        except (ValueError, TypeError, KeyError) as exc:
            log.warning("缓存里的转写解析不了，按未命中处理：%s", exc)
            return None

    def put_transcript(self, key: str, transcript: Transcript) -> None:
        conn = self._connect()
        if conn is None:
            return
        try:
            with closing(conn):
                conn.execute(
                    "INSERT OR REPLACE INTO transcripts "
                    "(key, video_id, source_url, title, source_type, language, "
                    " duration_sec, segment_count, has_speakers, payload, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        key,
                        transcript.video_id or "unknown",
                        transcript.source_url,
                        transcript.title,
                        transcript.source_type,
                        transcript.language,
                        transcript.duration_sec,
                        len(transcript.segments),
                        int(bool(transcript.speakers)),
                        json.dumps(transcript.to_dict(), ensure_ascii=False),
                        _now(),
                    ),
                )
                conn.commit()
        except sqlite3.Error as exc:
            log.warning("写缓存失败（不影响本次结果）：%s", exc)

    # ---------- 总结 ----------

    def get_summary(self, key: str) -> str | None:
        conn = self._connect()
        if conn is None:
            return None
        try:
            with closing(conn):
                row = conn.execute(
                    "SELECT content FROM summaries WHERE key = ?", (key,)
                ).fetchone()
        except sqlite3.Error as exc:
            log.warning("读缓存失败，按未命中处理：%s", exc)
            return None
        return row["content"] if row else None

    def put_summary(
        self,
        key: str,
        *,
        transcript_key: str,
        transcript: Transcript,
        provider_desc: str,
        summary_type: str,
        language: str,
        content: str,
    ) -> None:
        conn = self._connect()
        if conn is None:
            return
        try:
            with closing(conn):
                conn.execute(
                    "INSERT OR REPLACE INTO summaries "
                    "(key, transcript_key, video_id, title, provider, summary_type, "
                    " language, content, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        key, transcript_key, transcript.video_id or "unknown",
                        transcript.title, provider_desc, summary_type, language,
                        content, _now(),
                    ),
                )
                conn.commit()
        except sqlite3.Error as exc:
            log.warning("写缓存失败（不影响本次结果）：%s", exc)

    # ---------- 管理 ----------

    def list_transcripts(self, limit: int = 50) -> list[TranscriptEntry]:
        conn = self._connect()
        if conn is None:
            return []
        try:
            with closing(conn):
                rows = conn.execute(
                    "SELECT key, video_id, source_url, title, source_type, language,"
                    " duration_sec, segment_count, has_speakers, created_at"
                    " FROM transcripts ORDER BY created_at DESC LIMIT ?", (limit,),
                ).fetchall()
        except sqlite3.Error:
            return []
        return [
            TranscriptEntry(
                key=r["key"], video_id=r["video_id"], source_url=r["source_url"],
                title=r["title"], source_type=r["source_type"], language=r["language"],
                duration_sec=r["duration_sec"] or 0.0, segment_count=r["segment_count"],
                has_speakers=bool(r["has_speakers"]), created_at=r["created_at"],
            )
            for r in rows
        ]

    def list_summaries(self, limit: int = 50) -> list[SummaryEntry]:
        conn = self._connect()
        if conn is None:
            return []
        try:
            with closing(conn):
                rows = conn.execute(
                    "SELECT key, transcript_key, video_id, title, provider,"
                    " summary_type, language, created_at"
                    " FROM summaries ORDER BY created_at DESC LIMIT ?", (limit,),
                ).fetchall()
        except sqlite3.Error:
            return []
        return [
            SummaryEntry(
                key=r["key"], transcript_key=r["transcript_key"], video_id=r["video_id"],
                title=r["title"], provider=r["provider"], summary_type=r["summary_type"],
                language=r["language"], created_at=r["created_at"],
            )
            for r in rows
        ]

    def stats(self) -> dict[str, Any]:
        conn = self._connect()
        if conn is None:
            return {"enabled": False}
        try:
            with closing(conn):
                transcripts = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(duration_sec),0) d"
                                           " FROM transcripts").fetchone()
                summaries = conn.execute("SELECT COUNT(*) c FROM summaries").fetchone()
        except sqlite3.Error:
            return {"enabled": False}
        size = self.path.stat().st_size if self.path and self.path.is_file() else 0
        return {
            "enabled": True,
            "path": str(self.path),
            "transcripts": transcripts["c"],
            "cached_audio_sec": transcripts["d"] or 0.0,
            "summaries": summaries["c"],
            "size_bytes": size,
        }

    def clear(self, video_id: str | None = None) -> tuple[int, int]:
        """清缓存。给了 video_id 就只清那个视频的。返回 (转写数, 总结数)。"""
        conn = self._connect()
        if conn is None:
            return (0, 0)
        try:
            with closing(conn):
                if video_id:
                    t = conn.execute("DELETE FROM transcripts WHERE video_id = ?", (video_id,))
                    s = conn.execute("DELETE FROM summaries WHERE video_id = ?", (video_id,))
                else:
                    t = conn.execute("DELETE FROM transcripts")
                    s = conn.execute("DELETE FROM summaries")
                conn.commit()
                counts = (t.rowcount, s.rowcount)
                conn.execute("VACUUM")
                return counts
        except sqlite3.Error as exc:
            log.warning("清缓存失败：%s", exc)
            return (0, 0)
