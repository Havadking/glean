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

# v5：加 terms 表，并把库里已有的纠错表一次性灌进去
SCHEMA_VERSION = 5

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
    created_at    TEXT NOT NULL,
    uploader      TEXT,
    upload_date   TEXT,
    thumbnail     TEXT
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

CREATE TABLE IF NOT EXISTS questions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id    TEXT NOT NULL,
    question    TEXT NOT NULL,
    answer      TEXT NOT NULL,
    provider    TEXT NOT NULL,
    citations   TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_questions_video ON questions(video_id);

CREATE TABLE IF NOT EXISTS usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id      TEXT NOT NULL,
    kind          TEXT NOT NULL,
    detail        TEXT,
    provider      TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    calls         INTEGER NOT NULL DEFAULT 1,
    cost          REAL,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_video ON usage(video_id);
CREATE INDEX IF NOT EXISTS idx_usage_created ON usage(created_at);

CREATE TABLE IF NOT EXISTS pending_jobs (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL,
    params      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- 标签。source 三种：ai 是模型打的，user 是手动加的，
-- rejected 是用户删掉的 AI 标签 —— 留着是为了重新生成时不再冒出来
CREATE TABLE IF NOT EXISTS tags (
    video_id    TEXT NOT NULL,
    tag         TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'ai',
    created_at  TEXT NOT NULL,
    PRIMARY KEY (video_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);

-- 回顾：一段时间（一周 / 一天）里收藏的视频的综合总结。按需生成，生成过的存这里
CREATE TABLE IF NOT EXISTS digests (
    period      TEXT NOT NULL,
    key         TEXT NOT NULL,
    video_ids   TEXT NOT NULL,
    content     TEXT NOT NULL,
    provider    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (period, key)
);

-- UP 主分组
CREATE TABLE IF NOT EXISTS uploader_groups (
    uploader    TEXT PRIMARY KEY,
    group_name  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_uploader_groups_group ON uploader_groups(group_name);

-- 词表：纠错阶段确认过的专名替换，按 UP 主累积。
-- 同一分组里的 UP 主共享（查的时候按分组合并，不在这里存分组名，分组变了自动跟着走）。
-- 下一个视频先按这张表确定性地套一遍，再把 dst 当热词喂给 ASR、当已知术语喂给纠错模型。
-- state=rejected 是用户在某条视频上否决过的，之后同一范围内不再自动套用。
CREATE TABLE IF NOT EXISTS terms (
    uploader    TEXT NOT NULL,
    src         TEXT NOT NULL,
    dst         TEXT NOT NULL,
    why         TEXT NOT NULL DEFAULT '',
    hits        INTEGER NOT NULL DEFAULT 0,
    videos      INTEGER NOT NULL DEFAULT 0,
    last_video  TEXT,
    state       TEXT NOT NULL DEFAULT 'applied',
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (uploader, src, dst)
);
CREATE INDEX IF NOT EXISTS idx_terms_uploader ON terms(uploader);
"""


# v2 加的列。老库用 ALTER TABLE 补上，列名 -> 类型
_MIGRATE_COLUMNS = {
    "transcripts": {"uploader": "TEXT", "upload_date": "TEXT", "thumbnail": "TEXT"},
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in _MIGRATE_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, ctype in columns.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ctype}")


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
    corrections: str = "",
) -> str:
    """总结的指纹。同样的转写换个模型或换个总结类型，都算不同的东西。

    corrections 是生效的纠错替换表（correction.fingerprint）：表变了模型看到的正文就变了。
    没有表时不掺进去，老的缓存条目照样能命中。
    """
    parts = {
        "transcript": transcript_key,
        "provider": provider_desc,
        "type": summary_type,
        "lang": language,
        "extra": extra or "",
    }
    if corrections:
        parts["corrections"] = corrections
    return _fingerprint(parts)


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
    uploader: str | None = None
    upload_date: str | None = None
    thumbnail: str | None = None


@dataclass
class QuestionEntry:
    id: int
    video_id: str
    question: str
    answer: str
    provider: str
    citations: list[float]
    created_at: str


@dataclass
class TagEntry:
    video_id: str
    tag: str
    source: str          # ai | user
    created_at: str


@dataclass
class TermEntry:
    uploader: str
    src: str
    dst: str
    why: str
    hits: int
    videos: int
    state: str

    @property
    def applied(self) -> bool:
        return self.state == "applied"

    def to_dict(self) -> dict[str, Any]:
        return {"uploader": self.uploader, "from": self.src, "to": self.dst, "why": self.why,
                "hits": self.hits, "videos": self.videos, "state": self.state}


@dataclass
class DigestEntry:
    period: str          # week | day
    key: str             # 2026-W37 / 2026-09-14
    video_ids: list[str]
    content: str
    provider: str
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
    content: str | None = None


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
                was = conn.execute("PRAGMA user_version").fetchone()[0]
                conn.executescript(_SCHEMA)
                _migrate(conn)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                conn.commit()
                self._ready = True
                if 0 < was < 5:
                    self._backfill_terms(conn)
            return conn
        except sqlite3.Error as exc:
            log.warning("打不开缓存库 %s，这次不走缓存：%s", self.path, exc)
            self.path = None  # 别每次调用都重试同一个坏文件
            return None

    def _backfill_terms(self, conn: sqlite3.Connection) -> None:
        """升到 v5 时把每条转写 meta 里已有的纠错表灌进 terms。只跑一次。"""
        from .correction import items_of

        n = 0
        try:
            rows = conn.execute("SELECT video_id, uploader, payload FROM transcripts").fetchall()
        except sqlite3.Error as exc:
            log.warning("回填词表时读不了转写：%s", exc)
            return
        for r in rows:
            try:
                items = items_of(Transcript.from_dict(json.loads(r["payload"])))
            except (ValueError, TypeError, KeyError):
                continue
            if items:
                n += self.learn_terms(r["uploader"], items, video_id=r["video_id"])
        if n:
            log.info("词表：从已有的纠错表回填了 %d 条", n)

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
                    " duration_sec, segment_count, has_speakers, payload, created_at, "
                    " uploader, upload_date, thumbnail) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                        transcript.meta.get("uploader"),
                        transcript.meta.get("upload_date"),
                        transcript.meta.get("thumbnail"),
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
                    " duration_sec, segment_count, has_speakers, created_at,"
                    " uploader, upload_date, thumbnail"
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
                uploader=r["uploader"], upload_date=r["upload_date"], thumbnail=r["thumbnail"],
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

    def summaries_for_video(self, video_id: str) -> list[SummaryEntry]:
        """某个视频的全部总结，带正文。详情页用。"""
        conn = self._connect()
        if conn is None:
            return []
        try:
            with closing(conn):
                rows = conn.execute(
                    "SELECT key, transcript_key, video_id, title, provider,"
                    " summary_type, language, content, created_at"
                    " FROM summaries WHERE video_id = ? ORDER BY created_at DESC", (video_id,),
                ).fetchall()
        except sqlite3.Error:
            return []
        return [
            SummaryEntry(
                key=r["key"], transcript_key=r["transcript_key"], video_id=r["video_id"],
                title=r["title"], provider=r["provider"], summary_type=r["summary_type"],
                language=r["language"], created_at=r["created_at"], content=r["content"],
            )
            for r in rows
        ]

    # ---------- 问答 ----------

    def add_question(
        self, video_id: str, question: str, answer: str, provider: str, citations: list[float],
    ) -> int | None:
        conn = self._connect()
        if conn is None:
            return None
        try:
            with closing(conn):
                cur = conn.execute(
                    "INSERT INTO questions (video_id, question, answer, provider, citations, created_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (video_id, question, answer, provider, json.dumps(citations), _now()),
                )
                conn.commit()
                return cur.lastrowid
        except sqlite3.Error as exc:
            log.warning("写问答记录失败：%s", exc)
            return None

    def questions_for_video(self, video_id: str, limit: int = 100) -> list[QuestionEntry]:
        conn = self._connect()
        if conn is None:
            return []
        try:
            with closing(conn):
                rows = conn.execute(
                    "SELECT id, video_id, question, answer, provider, citations, created_at"
                    " FROM questions WHERE video_id = ? ORDER BY id LIMIT ?", (video_id, limit),
                ).fetchall()
        except sqlite3.Error:
            return []
        out = []
        for r in rows:
            try:
                cites = json.loads(r["citations"])
            except ValueError:
                cites = []
            out.append(QuestionEntry(r["id"], r["video_id"], r["question"], r["answer"],
                                     r["provider"], cites, r["created_at"]))
        return out

    def delete_question(self, question_id: int) -> bool:
        conn = self._connect()
        if conn is None:
            return False
        try:
            with closing(conn):
                cur = conn.execute("DELETE FROM questions WHERE id = ?", (question_id,))
                conn.commit()
                return cur.rowcount > 0
        except sqlite3.Error:
            return False

    # ---------- 标签 ----------

    def tags_for_video(self, video_id: str) -> list[TagEntry]:
        conn = self._connect()
        if conn is None:
            return []
        try:
            with closing(conn):
                rows = conn.execute(
                    "SELECT video_id, tag, source, created_at FROM tags"
                    " WHERE video_id = ? AND source != 'rejected' ORDER BY source DESC, rowid",
                    (video_id,),
                ).fetchall()
        except sqlite3.Error:
            return []
        return [TagEntry(r["video_id"], r["tag"], r["source"], r["created_at"]) for r in rows]

    def tags_by_video(self) -> dict[str, list[TagEntry]]:
        """全库的标签，按视频归堆。库页面一次查完，别每个视频查一遍。"""
        conn = self._connect()
        if conn is None:
            return {}
        try:
            with closing(conn):
                rows = conn.execute(
                    "SELECT video_id, tag, source, created_at FROM tags"
                    " WHERE source != 'rejected' ORDER BY source DESC, rowid"
                ).fetchall()
        except sqlite3.Error:
            return {}
        out: dict[str, list[TagEntry]] = {}
        for r in rows:
            out.setdefault(r["video_id"], []).append(TagEntry(r["video_id"], r["tag"], r["source"], r["created_at"]))
        return out

    def tag_counts(self) -> list[tuple[str, int]]:
        """每个标签挂了几条视频，多的在前。给模型当词表、给界面画标签云。"""
        conn = self._connect()
        if conn is None:
            return []
        try:
            with closing(conn):
                rows = conn.execute(
                    "SELECT tag, COUNT(*) c FROM tags WHERE source != 'rejected'"
                    " GROUP BY tag ORDER BY c DESC, tag"
                ).fetchall()
        except sqlite3.Error:
            return []
        return [(r["tag"], r["c"]) for r in rows]

    def add_tag(self, video_id: str, tag: str, source: str = "user") -> bool:
        """加一个标签。手动加的能覆盖之前删掉的；AI 打的碰到用户删过的就跳过。"""
        conn = self._connect()
        if conn is None:
            return False
        try:
            with closing(conn):
                row = conn.execute("SELECT source FROM tags WHERE video_id = ? AND tag = ?",
                                   (video_id, tag)).fetchone()
                if row is None:
                    conn.execute("INSERT INTO tags (video_id, tag, source, created_at) VALUES (?,?,?,?)",
                                 (video_id, tag, source, _now()))
                elif row["source"] == "rejected" and source == "user":
                    conn.execute("UPDATE tags SET source = 'user', created_at = ? WHERE video_id = ? AND tag = ?",
                                 (_now(), video_id, tag))
                else:
                    return False
                conn.commit()
                return True
        except sqlite3.Error as exc:
            log.warning("写标签失败：%s", exc)
            return False

    def remove_tag(self, video_id: str, tag: str) -> bool:
        """删标签。AI 打的标成 rejected 而不是真删，重新生成时才不会又加回来。"""
        conn = self._connect()
        if conn is None:
            return False
        try:
            with closing(conn):
                row = conn.execute("SELECT source FROM tags WHERE video_id = ? AND tag = ?",
                                   (video_id, tag)).fetchone()
                if row is None or row["source"] == "rejected":
                    return False
                if row["source"] == "ai":
                    conn.execute("UPDATE tags SET source = 'rejected' WHERE video_id = ? AND tag = ?",
                                 (video_id, tag))
                else:
                    conn.execute("DELETE FROM tags WHERE video_id = ? AND tag = ?", (video_id, tag))
                conn.commit()
                return True
        except sqlite3.Error as exc:
            log.warning("删标签失败：%s", exc)
            return False

    def set_ai_tags(self, video_id: str, tags: list[str]) -> list[str]:
        """用模型这次给的替换掉上次 AI 打的。用户加的、用户删过的都不动。返回实际写进去的。"""
        conn = self._connect()
        if conn is None:
            return []
        try:
            with closing(conn):
                keep = {r["tag"] for r in conn.execute(
                    "SELECT tag FROM tags WHERE video_id = ? AND source != 'ai'", (video_id,))}
                conn.execute("DELETE FROM tags WHERE video_id = ? AND source = 'ai'", (video_id,))
                written = []
                now = _now()
                for t in tags:
                    if t in keep:
                        continue
                    conn.execute("INSERT INTO tags (video_id, tag, source, created_at) VALUES (?,?,'ai',?)",
                                 (video_id, t, now))
                    written.append(t)
                conn.commit()
                return written
        except sqlite3.Error as exc:
            log.warning("写 AI 标签失败：%s", exc)
            return []

    def videos_with_ai_tags(self) -> set[str]:
        """模型打过标签的视频（哪怕后来全被用户删了）。补标签时跳过这些。"""
        conn = self._connect()
        if conn is None:
            return set()
        try:
            with closing(conn):
                rows = conn.execute("SELECT DISTINCT video_id FROM tags WHERE source IN ('ai','rejected')").fetchall()
        except sqlite3.Error:
            return set()
        return {r["video_id"] for r in rows}

    # ---------- UP 主分组 ----------

    def get_uploader_groups(self) -> dict[str, str]:
        """所有有分组的 UP 主 -> 分组名。"""
        conn = self._connect()
        if conn is None:
            return {}
        try:
            with closing(conn):
                rows = conn.execute("SELECT uploader, group_name FROM uploader_groups").fetchall()
        except sqlite3.Error:
            return {}
        return {r["uploader"]: r["group_name"] for r in rows}

    def get_uploader_group(self, uploader: str) -> str | None:
        """获取单个 UP 主的分组名。"""
        conn = self._connect()
        if conn is None:
            return None
        try:
            with closing(conn):
                r = conn.execute("SELECT group_name FROM uploader_groups WHERE uploader = ?", (uploader,)).fetchone()
                return r["group_name"] if r else None
        except sqlite3.Error:
            return None

    def list_uploader_groups(self) -> list[str]:
        """列出所有不重复的分组名称（按名称排序）。"""
        conn = self._connect()
        if conn is None:
            return []
        try:
            with closing(conn):
                rows = conn.execute("SELECT DISTINCT group_name FROM uploader_groups ORDER BY group_name").fetchall()
        except sqlite3.Error:
            return []
        return [r["group_name"] for r in rows]

    def set_uploader_group(self, uploader: str, group_name: str | None) -> None:
        """设置或清除一个 UP 主的分组。group_name 为空字符串或 None 时删除。"""
        conn = self._connect()
        if conn is None:
            return
        uploader = uploader.strip()
        group_name = group_name.strip() if group_name else None
        try:
            with closing(conn):
                if not group_name:
                    conn.execute("DELETE FROM uploader_groups WHERE uploader = ?", (uploader,))
                else:
                    conn.execute(
                        "INSERT INTO uploader_groups (uploader, group_name, created_at)"
                        " VALUES (?, ?, ?) ON CONFLICT(uploader) DO UPDATE SET group_name=excluded.group_name",
                        (uploader, group_name, _now()),
                    )
                conn.commit()
        except sqlite3.Error as exc:
            log.warning("设置 UP 主分组失败：%s", exc)

    def rename_uploader_group(self, from_name: str, to_name: str) -> int:
        """将某个分组重命名为新名字。"""
        conn = self._connect()
        if conn is None:
            return 0
        from_name = from_name.strip()
        to_name = to_name.strip()
        if not from_name or not to_name or from_name == to_name:
            return 0
        try:
            with closing(conn):
                res = conn.execute("UPDATE uploader_groups SET group_name = ? WHERE group_name = ?", (to_name, from_name))
                conn.commit()
                return res.rowcount
        except sqlite3.Error as exc:
            log.warning("重命名 UP 主分组失败：%s", exc)
            return 0

    def delete_uploader_group(self, group_name: str) -> int:
        """删除某个分组（该组下的 UP 主自动恢复为未分组）。"""
        conn = self._connect()
        if conn is None:
            return 0
        group_name = group_name.strip()
        if not group_name:
            return 0
        try:
            with closing(conn):
                res = conn.execute("DELETE FROM uploader_groups WHERE group_name = ?", (group_name,))
                conn.commit()
                return res.rowcount
        except sqlite3.Error as exc:
            log.warning("删除 UP 主分组失败：%s", exc)
            return 0

    # ---------- 词表 ----------

    def term_scope(self, uploader: str | None) -> list[str]:
        """一个 UP 主的词表范围：自己 + 同分组的其他 UP 主。没分组就只有自己。

        没有 UP 主信息的视频用空串做 key，它们之间互相共享 —— 总比没有强。
        """
        me = (uploader or "").strip()
        if not me:
            return [""]
        group = self.get_uploader_group(me)
        if not group:
            return [me]
        conn = self._connect()
        if conn is None:
            return [me]
        try:
            with closing(conn):
                rows = conn.execute("SELECT uploader FROM uploader_groups WHERE group_name = ?", (group,)).fetchall()
        except sqlite3.Error:
            return [me]
        return sorted({me, *(r["uploader"] for r in rows)})

    def get_terms(self, uploader: str | None) -> list[TermEntry]:
        """范围内的词表。同一条 (src, dst) 多个 UP 主都有时合并计数；任一处否决过就算否决。"""
        scope = self.term_scope(uploader)
        conn = self._connect()
        if conn is None:
            return []
        try:
            with closing(conn):
                marks = ",".join("?" * len(scope))
                rows = conn.execute(
                    f"SELECT uploader, src, dst, why, hits, videos, state FROM terms WHERE uploader IN ({marks})",
                    scope,
                ).fetchall()
        except sqlite3.Error:
            return []
        merged: dict[tuple[str, str], TermEntry] = {}
        me = (uploader or "").strip()
        for r in rows:
            key = (r["src"], r["dst"])
            cur = merged.get(key)
            if cur is None:
                merged[key] = TermEntry(r["uploader"], r["src"], r["dst"], r["why"], r["hits"], r["videos"], r["state"])
                continue
            cur.hits += r["hits"]
            cur.videos += r["videos"]
            if r["state"] == "rejected":
                cur.state = "rejected"
            if r["uploader"] == me:
                cur.uploader = me
                cur.why = r["why"] or cur.why
        return sorted(merged.values(), key=lambda t: (-t.hits, t.src))

    def learn_terms(self, uploader: str | None, items: list[Any], video_id: str | None = None) -> int:
        """把一条视频最终生效的替换表记进词表。items 是 correction.Correction。

        命中数累加；同一条视频重跑纠错不重复算视频数。用户否决的写成 rejected，
        之后这个范围内不再自动套用；恢复了再改回 applied。
        """
        conn = self._connect()
        if conn is None or not items:
            return 0
        me = (uploader or "").strip()
        now = _now()
        try:
            with closing(conn):
                for c in items:
                    row = conn.execute(
                        "SELECT hits, videos, last_video FROM terms WHERE uploader = ? AND src = ? AND dst = ?",
                        (me, c.src, c.dst),
                    ).fetchone()
                    state = "applied" if c.applied else "rejected"
                    if row is None:
                        conn.execute(
                            "INSERT INTO terms (uploader, src, dst, why, hits, videos, last_video, state, updated_at)"
                            " VALUES (?,?,?,?,?,?,?,?,?)",
                            (me, c.src, c.dst, c.why or "", c.hits, 1, video_id, state, now),
                        )
                        continue
                    same_video = video_id is not None and row["last_video"] == video_id
                    conn.execute(
                        "UPDATE terms SET hits = ?, videos = ?, last_video = ?, state = ?, updated_at = ?"
                        " WHERE uploader = ? AND src = ? AND dst = ?",
                        (row["hits"] + (0 if same_video else c.hits),
                         row["videos"] + (0 if same_video else 1),
                         video_id, state, now, me, c.src, c.dst),
                    )
                conn.commit()
                return len(items)
        except sqlite3.Error as exc:
            log.warning("写词表失败：%s", exc)
            return 0

    def set_term_state(self, uploader: str | None, src: str, dst: str, state: str) -> None:
        """用户在某条视频上否决/恢复一条替换时同步到词表。没有这条就建一条（只记状态）。"""
        conn = self._connect()
        if conn is None:
            return
        me = (uploader or "").strip()
        try:
            with closing(conn):
                conn.execute(
                    "INSERT INTO terms (uploader, src, dst, why, hits, videos, state, updated_at)"
                    " VALUES (?,?,?,'',0,0,?,?)"
                    " ON CONFLICT(uploader, src, dst) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
                    (me, src, dst, state, _now()),
                )
                conn.commit()
        except sqlite3.Error as exc:
            log.warning("更新词表状态失败：%s", exc)

    def delete_term(self, uploader: str | None, src: str, dst: str) -> bool:
        conn = self._connect()
        if conn is None:
            return False
        try:
            with closing(conn):
                res = conn.execute("DELETE FROM terms WHERE uploader = ? AND src = ? AND dst = ?",
                                   ((uploader or "").strip(), src, dst))
                conn.commit()
                return res.rowcount > 0
        except sqlite3.Error:
            return False

    def hotwords(self, uploader: str | None, limit: int = 100) -> list[str]:
        """范围内生效的正确写法，命中多的在前。喂给 ASR 当热词。"""
        seen: list[str] = []
        for t in self.get_terms(uploader):
            if t.applied and t.dst not in seen:
                seen.append(t.dst)
            if len(seen) >= limit:
                break
        return seen

    # ---------- 回顾 ----------

    def get_digest(self, period: str, key: str) -> DigestEntry | None:
        conn = self._connect()
        if conn is None:
            return None
        try:
            with closing(conn):
                r = conn.execute(
                    "SELECT period, key, video_ids, content, provider, created_at FROM digests"
                    " WHERE period = ? AND key = ?", (period, key),
                ).fetchone()
        except sqlite3.Error:
            return None
        if r is None:
            return None
        try:
            ids = json.loads(r["video_ids"])
        except ValueError:
            ids = []
        return DigestEntry(r["period"], r["key"], ids, r["content"], r["provider"], r["created_at"])

    def put_digest(self, period: str, key: str, video_ids: list[str], content: str, provider: str) -> None:
        conn = self._connect()
        if conn is None:
            return
        try:
            with closing(conn):
                conn.execute(
                    "INSERT OR REPLACE INTO digests (period, key, video_ids, content, provider, created_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (period, key, json.dumps(video_ids), content, provider, _now()),
                )
                conn.commit()
        except sqlite3.Error as exc:
            log.warning("写回顾失败：%s", exc)

    def digests_for(self, period: str) -> dict[str, DigestEntry]:
        """某个粒度下所有生成过的回顾，key -> 记录。"""
        conn = self._connect()
        if conn is None:
            return {}
        try:
            with closing(conn):
                rows = conn.execute(
                    "SELECT period, key, video_ids, content, provider, created_at FROM digests WHERE period = ?",
                    (period,),
                ).fetchall()
        except sqlite3.Error:
            return {}
        out = {}
        for r in rows:
            try:
                ids = json.loads(r["video_ids"])
            except ValueError:
                ids = []
            out[r["key"]] = DigestEntry(r["period"], r["key"], ids, r["content"], r["provider"], r["created_at"])
        return out

    # ---------- 花费记账 ----------

    def add_usage(
        self, video_id: str, kind: str, detail: str | None, provider: str,
        input_tokens: int, output_tokens: int, calls: int, cost: float | None,
    ) -> None:
        """kind: summary | qa | uploader_qa。没读到用量的（比如假 provider）不记。"""
        if calls <= 0 and input_tokens <= 0:
            return
        conn = self._connect()
        if conn is None:
            return
        try:
            with closing(conn):
                conn.execute(
                    "INSERT INTO usage (video_id, kind, detail, provider, input_tokens, output_tokens,"
                    " calls, cost, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (video_id, kind, detail, provider, int(input_tokens), int(output_tokens),
                     int(calls), cost, _now()),
                )
                conn.commit()
        except sqlite3.Error as exc:
            log.warning("记账失败：%s", exc)

    def usage_totals(self, video_id: str | None = None, since: str | None = None) -> dict[str, Any]:
        """汇总：总 token、总花费、次数。可按视频或起始时间过滤。"""
        empty = {"input_tokens": 0, "output_tokens": 0, "calls": 0, "cost": 0.0}
        conn = self._connect()
        if conn is None:
            return empty
        where, params = [], []
        if video_id:
            where.append("video_id = ?")
            params.append(video_id)
        if since:
            where.append("created_at >= ?")
            params.append(since)
        sql = ("SELECT COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o,"
               " COALESCE(SUM(calls),0) c, COALESCE(SUM(cost),0) m FROM usage"
               + (" WHERE " + " AND ".join(where) if where else ""))
        try:
            with closing(conn):
                r = conn.execute(sql, params).fetchone()
        except sqlite3.Error:
            return empty
        return {"input_tokens": r["i"], "output_tokens": r["o"], "calls": r["c"], "cost": round(r["m"] or 0.0, 4)}

    def usage_by_video(self) -> dict[str, float]:
        """video_id -> 累计花费。库列表用。"""
        conn = self._connect()
        if conn is None:
            return {}
        try:
            with closing(conn):
                rows = conn.execute("SELECT video_id, COALESCE(SUM(cost),0) m FROM usage GROUP BY video_id").fetchall()
        except sqlite3.Error:
            return {}
        return {r["video_id"]: round(r["m"] or 0.0, 4) for r in rows}

    def usage_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        conn = self._connect()
        if conn is None:
            return []
        try:
            with closing(conn):
                rows = conn.execute(
                    "SELECT video_id, kind, detail, provider, input_tokens, output_tokens, calls, cost, created_at"
                    " FROM usage ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        except sqlite3.Error:
            return []
        return [dict(r) for r in rows]

    # ---------- 排队中的任务（服务重启后续跑） ----------

    def add_pending_job(self, job_id: str, kind: str, title: str, params: dict[str, Any]) -> None:
        conn = self._connect()
        if conn is None:
            return
        try:
            with closing(conn):
                conn.execute(
                    "INSERT OR REPLACE INTO pending_jobs (id, kind, title, params, created_at) VALUES (?,?,?,?,?)",
                    (job_id, kind, title, json.dumps(params, ensure_ascii=False), _now()),
                )
                conn.commit()
        except sqlite3.Error as exc:
            log.warning("记录待办任务失败：%s", exc)

    def remove_pending_job(self, job_id: str) -> None:
        conn = self._connect()
        if conn is None:
            return
        try:
            with closing(conn):
                conn.execute("DELETE FROM pending_jobs WHERE id = ?", (job_id,))
                conn.commit()
        except sqlite3.Error as exc:
            log.warning("删除待办任务失败：%s", exc)

    def pending_jobs(self) -> list[dict[str, Any]]:
        conn = self._connect()
        if conn is None:
            return []
        try:
            with closing(conn):
                rows = conn.execute(
                    "SELECT id, kind, title, params, created_at FROM pending_jobs ORDER BY created_at, rowid"
                ).fetchall()
        except sqlite3.Error:
            return []
        out = []
        for r in rows:
            try:
                params = json.loads(r["params"])
            except ValueError:
                continue
            out.append({"id": r["id"], "kind": r["kind"], "title": r["title"], "params": params,
                        "created_at": r["created_at"]})
        return out

    def update_source_meta(self, video_id: str, meta: dict[str, Any]) -> int:
        """给老记录补 UP 主等来源信息（v2 之前的行没有这几列）。返回更新行数。"""
        conn = self._connect()
        if conn is None:
            return 0
        try:
            with closing(conn):
                rows = conn.execute(
                    "SELECT key, payload FROM transcripts WHERE video_id = ?", (video_id,)
                ).fetchall()
                n = 0
                for r in rows:
                    payload = json.loads(r["payload"])
                    payload.setdefault("meta", {}).update(meta)
                    conn.execute(
                        "UPDATE transcripts SET uploader = ?, upload_date = ?, thumbnail = ?,"
                        " payload = ? WHERE key = ?",
                        (meta.get("uploader"), meta.get("upload_date"), meta.get("thumbnail"),
                         json.dumps(payload, ensure_ascii=False), r["key"]),
                    )
                    n += 1
                conn.commit()
                return n
        except (sqlite3.Error, ValueError) as exc:
            log.warning("更新缓存来源信息失败：%s", exc)
            return 0

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
                    conn.execute("DELETE FROM questions WHERE video_id = ?", (video_id,))
                    conn.execute("DELETE FROM tags WHERE video_id = ?", (video_id,))
                else:
                    t = conn.execute("DELETE FROM transcripts")
                    s = conn.execute("DELETE FROM summaries")
                    conn.execute("DELETE FROM questions")
                    conn.execute("DELETE FROM tags")
                    conn.execute("DELETE FROM digests")
                conn.commit()
                counts = (t.rowcount, s.rowcount)
                conn.execute("VACUUM")
                return counts
        except sqlite3.Error as exc:
            log.warning("清缓存失败：%s", exc)
            return (0, 0)
