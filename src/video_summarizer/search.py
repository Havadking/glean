"""全库全文搜索：SQLite FTS5，零 LLM 成本。

索引什么：转写按**阅读视图的段落**索引（命中直接定位到用户看到的那一段），
总结按行索引（命中跳到对应类型）。

中文怎么切：不用 FTS5 自带的 trigram —— 它对少于三个字的词无能为力，而
"甘油""蜂花"这种两字产品名恰恰是最常搜的。这里把 CJK 文本**按字切开**存进
索引列（"防晒棒" -> "防 晒 棒"），查询时把词拼成短语 `"防 晒"`，相邻匹配。
一个字也能搜，命中精确，代价是索引大一点，对个人库无所谓。拉丁文按空格分词，
unicode61 tokenizer 自带大小写折叠。

索引和缓存放同一个 sqlite 文件，任何失败都降级成"没结果"，不影响主流程。
"""

from __future__ import annotations

import logging
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import correction
from .models import Transcript
from .web.reading import _is_cjk, to_paragraphs

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
    tok,
    video_id UNINDEXED,
    kind UNINDEXED,
    ref UNINDEXED,
    start UNINDEXED,
    text UNINDEXED,
    tokenize = 'unicode61'
);
"""

# 一次最多返回这么多条命中
MAX_HITS = 200
# 摘录：命中前后各留这么多字符
SNIPPET_CONTEXT = 40

_WS_RE = re.compile(r"\s+")
_QUOTE_RE = re.compile(r'"')


def tokenize(text: str) -> str:
    """把文本变成可索引的形式：CJK 逐字分开，其他按原样保留（由 unicode61 再切）。"""
    out: list[str] = []
    prev_cjk = False
    for ch in text:
        if ch.isspace():
            out.append(" ")
            prev_cjk = False
            continue
        cjk = _is_cjk(ch)
        if cjk or prev_cjk:
            out.append(" ")
        out.append(ch)
        prev_cjk = cjk
    return _WS_RE.sub(" ", "".join(out)).strip()


def build_query(query: str) -> str | None:
    """用户输入 -> FTS5 MATCH 表达式。每个空格分开的词是一个短语，词之间 AND。"""
    phrases = []
    for word in query.split():
        tok = tokenize(word)
        if not tok:
            continue
        # 标点在 unicode61 里是分隔符，去掉免得短语匹配失败
        parts = [_QUOTE_RE.sub("", t) for t in tok.split(" ") if any(c.isalnum() for c in t)]
        if not parts:
            continue
        phrase = '"' + " ".join(parts) + '"'
        # 拉丁词做前缀匹配：搜 elephant 能命中 elephants，也方便边打边搜
        if not _is_cjk(parts[-1][-1]):
            phrase += "*"
        phrases.append(phrase)
    return " ".join(phrases) if phrases else None


@dataclass
class Hit:
    video_id: str
    kind: str            # transcript | summary
    ref: str             # transcript: 段落序号；summary: 总结类型
    start: float         # transcript: 段落开始秒；summary: 行号
    text: str
    snippet: str

    def to_dict(self) -> dict[str, Any]:
        return {"video_id": self.video_id, "kind": self.kind, "ref": self.ref,
                "start": self.start, "snippet": self.snippet}


def make_snippet(text: str, query: str, context: int = SNIPPET_CONTEXT) -> str:
    """围绕第一个命中的词截一段。找不到（比如命中的是别的词）就截开头。"""
    words = [w for w in query.split() if w]
    lower = text.lower()
    pos = -1
    hit_len = 0
    for w in words:
        i = lower.find(w.lower())
        if i >= 0 and (pos < 0 or i < pos):
            pos, hit_len = i, len(w)
    if pos < 0:
        return text[: context * 2] + ("…" if len(text) > context * 2 else "")
    a = max(0, pos - context)
    b = min(len(text), pos + hit_len + context)
    return ("…" if a > 0 else "") + text[a:b] + ("…" if b < len(text) else "")


class SearchIndex:
    def __init__(self, path: Path | None) -> None:
        self.path = Path(path) if path else None
        self._ready = False

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
                conn.commit()
                self._ready = True
            return conn
        except sqlite3.Error as exc:
            log.warning("打不开搜索索引 %s：%s", self.path, exc)
            self.path = None
            return None

    # ---------- 写 ----------

    def index_video(
        self, video_id: str, transcript: Transcript | None,
        summaries: Iterable[tuple[str, str]] = (),
    ) -> int:
        """重建一个视频的全部索引行。summaries 是 (类型, 正文) 列表。返回写入行数。

        transcript 传原文（带纠错表的那份）：摘录显示修正后的，索引列原文和修正文都进 ——
        用户记得哪个写法就搜哪个。纠错不动标点和时间轴，所以段落编号和阅读视图一致。
        """
        rows: list[tuple[str, str, str, str, float, str]] = []
        if transcript is not None:
            fixes = correction.applied_items(transcript)
            for i, p in enumerate(to_paragraphs(transcript.segments)):
                if not p.text.strip():
                    continue
                shown = correction.apply_text(p.text, fixes)
                tok = tokenize(shown) if shown == p.text else tokenize(shown) + " " + tokenize(p.text)
                rows.append((tok, video_id, "transcript", str(i), p.start, shown))
        for summary_type, content in summaries:
            for line_no, line in enumerate(content.splitlines()):
                clean = line.strip().lstrip("#-*>0123456789. ").strip()
                if len(clean) < 2:
                    continue
                rows.append((tokenize(clean), video_id, "summary", summary_type, float(line_no), clean))

        conn = self._connect()
        if conn is None:
            return 0
        try:
            with closing(conn):
                conn.execute("DELETE FROM search_fts WHERE video_id = ?", (video_id,))
                conn.executemany(
                    "INSERT INTO search_fts (tok, video_id, kind, ref, start, text) VALUES (?,?,?,?,?,?)",
                    rows,
                )
                conn.commit()
        except sqlite3.Error as exc:
            log.warning("写搜索索引失败：%s", exc)
            return 0
        return len(rows)

    def remove_video(self, video_id: str) -> None:
        conn = self._connect()
        if conn is None:
            return
        try:
            with closing(conn):
                conn.execute("DELETE FROM search_fts WHERE video_id = ?", (video_id,))
                conn.commit()
        except sqlite3.Error as exc:
            log.warning("删搜索索引失败：%s", exc)

    def clear(self) -> None:
        conn = self._connect()
        if conn is None:
            return
        try:
            with closing(conn):
                conn.execute("DELETE FROM search_fts")
                conn.commit()
        except sqlite3.Error as exc:
            log.warning("清搜索索引失败：%s", exc)

    # ---------- 读 ----------

    def count(self) -> int:
        conn = self._connect()
        if conn is None:
            return 0
        try:
            with closing(conn):
                return conn.execute("SELECT COUNT(*) FROM search_fts").fetchone()[0]
        except sqlite3.Error:
            return 0

    def indexed_videos(self) -> set[str]:
        conn = self._connect()
        if conn is None:
            return set()
        try:
            with closing(conn):
                return {r[0] for r in conn.execute("SELECT DISTINCT video_id FROM search_fts")}
        except sqlite3.Error:
            return set()

    def search(self, query: str, *, limit: int = MAX_HITS, video_id: str | None = None) -> list[Hit]:
        match = build_query(query)
        conn = self._connect()
        if match is None or conn is None:
            return []
        sql = ("SELECT video_id, kind, ref, start, text FROM search_fts WHERE search_fts MATCH ?"
               + (" AND video_id = ?" if video_id else "")
               + " ORDER BY video_id, kind DESC, start LIMIT ?")  # transcript 排在 summary 前
        params: tuple[Any, ...] = (match, video_id, limit) if video_id else (match, limit)
        try:
            with closing(conn):
                rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            log.warning("搜索失败：%s", exc)
            return []
        return [
            Hit(video_id=r["video_id"], kind=r["kind"], ref=r["ref"], start=r["start"],
                text=r["text"], snippet=make_snippet(r["text"], query))
            for r in rows
        ]


# ---------- 和库同步 ----------


def sync_index(cfg, *, force: bool = False) -> tuple[int, int]:
    """把库里的视频灌进索引。默认只补缺的；force 全部重建。返回 (处理的视频数, 行数)。"""
    from .web.library import load_library, load_summaries, load_transcript

    index = SearchIndex(cfg.cache_db)
    if not index.enabled:
        return (0, 0)
    have = set() if force else index.indexed_videos()
    if force:
        index.clear()
    videos = rows = 0
    for entry in load_library(cfg):
        if entry.video_id in have:
            continue
        n = index.index_video(entry.video_id, load_transcript(cfg, entry, raw=True),
                              [(k, v.content) for k, v in load_summaries(cfg, entry).items()])
        videos += 1
        rows += n
    return (videos, rows)


def index_one(cfg, video_id: str) -> int:
    """任务跑完后更新这一个视频。找不到就从索引里删掉。"""
    from .web.library import find_entry, load_summaries, load_transcript

    index = SearchIndex(cfg.cache_db)
    entry = find_entry(cfg, video_id)
    if entry is None:
        index.remove_video(video_id)
        return 0
    return index.index_video(video_id, load_transcript(cfg, entry, raw=True),
                             [(k, v.content) for k, v in load_summaries(cfg, entry).items()])
