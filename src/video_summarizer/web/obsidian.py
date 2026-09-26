"""存到 Obsidian：把一条视频的总结、问答、转写写成库里的一篇笔记。

直接写文件，不走 obsidian:// URI（长文本塞不进 URL，也没法更新已有笔记）或第三方插件——
库就在本机，Obsidian 会自己发现新文件，obsidian-git 顺手就备份了。

笔记用 frontmatter 里的 `vsum_id` 认人：同一条视频再存一次是**原地更新**，
哪怕笔记已经被挪到别的文件夹、改了名。更新时 `## 我的笔记` 往下的内容原样保留，
frontmatter 里用户会改的字段（status、tags、created……）也不覆盖。
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

import yaml

from ..cache import Cache
from ..config import Config
from ..errors import VideoSummarizerError
from ..summarizer.prompts import TEMPLATES
from ..summarizer.tokens import format_timestamp
from . import library as library_mod
from . import thumbs as thumbs_mod
from .reading import to_paragraphs

log = logging.getLogger(__name__)

MY_NOTES_HEADING = "## 我的笔记"
# 这些字段每次导出都以拾光笺为准；其余字段（status、type、created、用户自己加的）更新时保留原值
_MANAGED_KEYS = ("source", "url", "author", "published", "platform", "duration", "vsum_id")
# 找旧笔记时只读文件开头这么多字节，frontmatter 一定在这里面
_HEAD_BYTES = 4096
_VSUM_ID_RE = re.compile(r"""^vsum_id:\s*["']?(.+?)["']?\s*$""", re.MULTILINE)
# Windows 文件名不许的字符 + Obsidian 链接里有特殊含义的 # ^ [ ] |
_BAD_FILENAME_RE = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f]')
_MAX_FILENAME = 80
# [03:27] / [1:02:03]，后面已经跟着 ( 的是链接，不再处理
_TIMESTAMP_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?::(\d{2}))?\](?!\()")
_HEADING_RE = re.compile(r"^(#{1,6})(\s)", re.MULTILINE)
_THUMB_EXT = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}

ThumbFetcher = Callable[[str], tuple[bytes, str]]


class ObsidianError(VideoSummarizerError):
    """库路径没配、不存在之类的预期内失败。"""


@dataclass
class ExportResult:
    path: Path
    rel: str             # 相对库根目录，正斜杠
    action: str          # created | updated

    @property
    def uri(self) -> str:
        return note_uri(self.path)


# ---------- 小工具 ----------


def note_uri(path: Path) -> str:
    """obsidian://open?path=<绝对路径>，不用知道库名。"""
    return "obsidian://open?path=" + quote(str(path).replace("\\", "/"), safe="/:")


def sanitize_filename(title: str, fallback: str) -> str:
    name = _BAD_FILENAME_RE.sub(" ", title or "")
    name = re.sub(r"\s+", " ", name).strip().lstrip(".")
    if len(name) > _MAX_FILENAME:
        name = name[:_MAX_FILENAME].rstrip()
    name = name.rstrip(". ")
    return name or _BAD_FILENAME_RE.sub("_", fallback) or "video"


def clean_tag(tag: str) -> str | None:
    """Obsidian 标签不能有空格和大多数标点，纯数字也不算标签。"""
    t = re.sub(r"\s+", "-", (tag or "").strip().lstrip("#"))
    t = re.sub(r"[^\w\-/]", "", t)
    t = t.strip("-/")
    if not t or t.isdigit():
        return None
    return t


def timestamp_url(source_url: str, seconds: float) -> str | None:
    """跳到原视频某一秒的链接。只认 B 站和 YouTube，别的站点没有可靠的时间参数。"""
    if not source_url:
        return None
    parsed = urlparse(source_url)
    host = (parsed.hostname or "").lower()
    secs = int(max(0.0, seconds))
    if host.endswith("bilibili.com") or host == "b23.tv":
        value = str(secs)
    elif host.endswith("youtube.com") or host == "youtu.be":
        value = f"{secs}s"
    else:
        return None
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k != "t"]
    query.append(("t", value))
    return urlunparse(parsed._replace(query=urlencode(query)))


def link_timestamps(text: str, source_url: str) -> str:
    """把正文里的 [mm:ss] 换成能跳到原视频那一秒的链接。"""
    if timestamp_url(source_url, 0) is None:
        return text

    def repl(m: re.Match[str]) -> str:
        a, b, c = m.group(1), m.group(2), m.group(3)
        secs = int(a) * 3600 + int(b) * 60 + int(c) if c else int(a) * 60 + int(b)
        return f"[{m.group(0)[1:-1]}]({timestamp_url(source_url, secs)})"

    return _TIMESTAMP_RE.sub(repl, text)


def demote_headings(text: str, min_level: int = 3) -> str:
    """总结里的 # / ## 放到笔记的 ## 小节下面会乱层级，整体往下挪，最高一级变成 min_level。"""
    levels = [len(m.group(1)) for m in _HEADING_RE.finditer(_strip_fences(text))]
    if not levels:
        return text
    shift = max(0, min_level - min(levels))
    if not shift:
        return text
    out, in_fence = [], False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence:
            m = _HEADING_RE.match(line)
            if m:
                line = "#" * min(6, len(m.group(1)) + shift) + line[len(m.group(1)):]
        out.append(line)
    return "\n".join(out)


def _strip_fences(text: str) -> str:
    return re.sub(r"```.*?```", "", text, flags=re.DOTALL)


def split_note(text: str) -> tuple[dict[str, Any], str]:
    """拆成 (frontmatter, 正文)。frontmatter 坏了就当没有，正文照样返回。"""
    text = text.lstrip("﻿").replace("\r\n", "\n")
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            try:
                fm = yaml.safe_load(text[4:end]) or {}
            except yaml.YAMLError:
                fm = {}
            body = text[end + 4:]
            return (fm if isinstance(fm, dict) else {}), body.lstrip("\n")
    return {}, text


def _my_notes(body: str) -> str | None:
    """正文里 `## 我的笔记` 起往后的部分（含标题行），没有返回 None。"""
    m = re.search(rf"^{re.escape(MY_NOTES_HEADING)}\s*$", body, re.MULTILINE)
    return body[m.start():].rstrip() + "\n" if m else None


def _read_head(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            return f.read(_HEAD_BYTES)
    except OSError:
        return ""


def _note_id(path: Path) -> str | None:
    head = _read_head(path)
    if not head.lstrip("﻿").startswith("---"):
        return None
    end = head.find("\n---", 3)
    m = _VSUM_ID_RE.search(head[: end if end != -1 else len(head)])
    return m.group(1) if m else None


def find_note(vault: Path, video_id: str, hint: str | None = None) -> Path | None:
    """按 vsum_id 找已经存过的笔记。先看上次记下的位置，不对再扫全库（跳过 . 开头的目录）。"""
    if hint:
        p = vault / hint
        if p.is_file() and _note_id(p) == video_id:
            return p
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if name.endswith(".md"):
                p = Path(root) / name
                if _note_id(p) == video_id:
                    return p
    return None


def _rel(vault: Path, path: Path) -> str:
    return path.relative_to(vault).as_posix()


def vault_dir(cfg: Config) -> Path:
    if not cfg.obsidian.enabled:
        raise ObsidianError("还没配置 Obsidian 库路径（设置页 → Obsidian）")
    vault = Path(str(cfg.obsidian.vault_path))
    if not vault.is_dir():
        raise ObsidianError(f"Obsidian 库目录不存在：{vault}")
    return vault


# ---------- 渲染 ----------


def _platform(entry: library_mod.LibraryEntry) -> str | None:
    host = (urlparse(entry.source_url or "").hostname or "").lower()
    for key, name in (("bilibili", "bilibili"), ("b23.tv", "bilibili"), ("youtu", "youtube"),
                      ("douyin", "douyin"), ("iesdouyin", "douyin")):
        if key in host:
            return name
    ext = entry.meta.get("extractor")
    return str(ext).lower() if ext else None


def _published(entry: library_mod.LibraryEntry) -> date | None:
    try:
        return datetime.strptime(entry.upload_date or "", "%Y%m%d").date()
    except ValueError:
        return None


def build_frontmatter(entry: library_mod.LibraryEntry, tags: list[str],
                      existing: dict[str, Any] | None = None, today: date | None = None) -> dict[str, Any]:
    """新笔记的字段顺序跟 Web Clipper 的剪藏一致；已有笔记在原字段上合并。"""
    generated: dict[str, Any] = {
        "type": "inbox",
        "status": "pending",
        "source": "video-summarizer",
        "url": entry.source_url or None,
        "created": today or date.today(),
        "author": f"[[{entry.uploader}]]" if entry.uploader else None,
        "published": _published(entry),
        "platform": _platform(entry),
        "duration": format_timestamp(entry.duration_sec) if entry.duration_sec else None,
        "vsum_id": entry.video_id,
        "tags": ["clippings", "video", *tags],
    }
    if not existing:
        return generated
    merged = dict(existing)
    for k in _MANAGED_KEYS:
        merged[k] = generated[k]
    old_tags = existing.get("tags") or []
    if isinstance(old_tags, str):
        old_tags = [old_tags]
    seen: list[str] = []
    for t in [*old_tags, *generated["tags"]]:
        if t and t not in seen:
            seen.append(t)
    merged["tags"] = seen
    return merged


def dump_frontmatter(fm: dict[str, Any]) -> str:
    return "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, width=10**6) + "---\n"


def render_body(
    cfg: Config,
    entry: library_mod.LibraryEntry,
    include: list[str],
    *,
    thumb_embed: str | None = None,
    app_url: str | None = None,
) -> str:
    """拾光笺负责的那部分正文（到 `## 我的笔记` 之前为止）。"""
    url = entry.source_url or ""
    parts: list[str] = []
    if thumb_embed:
        parts.append(thumb_embed)

    info = ["> [!info] 视频信息"]
    if entry.remark and entry.remark.strip() != entry.title:
        info.append(f"> - 原标题：{entry.title}")
    if entry.uploader:
        info.append(f"> - 作者：{entry.uploader}")
    meta_line = []
    if entry.duration_sec:
        meta_line.append(f"时长 {format_timestamp(entry.duration_sec)}")
    meta_line.append(entry.source_label)
    info.append("> - " + " · ".join(meta_line))
    links = []
    if url:
        links.append(f"[原视频]({url})")
    if app_url:
        links.append(f"[在拾光笺中打开]({app_url.rstrip('/')}/video/{quote(entry.video_id)})")
    if links:
        info.append("> - " + " · ".join(links))
    parts.append("\n".join(info))

    if "summaries" in include:
        sums = library_mod.load_summaries(cfg, entry)
        for key in TEMPLATES:
            s = sums.get(key)
            if s is None:
                continue
            content = link_timestamps(demote_headings(s.content.strip()), url)
            parts.append(f"## {s.label}\n\n{content}")

    if "qa" in include:
        qs = Cache(cfg.cache_db).questions_for_video(entry.video_id)
        if qs:
            items = [f"### {q.question.strip()}\n\n{link_timestamps(demote_headings(q.answer.strip(), 4), url)}"
                     for q in qs]
            parts.append("## 问答\n\n" + "\n\n".join(items))

    if "transcript" in include:
        transcript = library_mod.load_transcript(cfg, entry)
        if transcript and transcript.segments:
            paras = to_paragraphs(transcript.segments)
            lines = []
            for p in paras:
                ts = format_timestamp(p.start)
                link = timestamp_url(url, p.start)
                stamp = f"**[{ts}]({link})**" if link else f"**{ts}**"
                speaker = f"{p.speaker}：" if p.speaker else ""
                lines.append(f"{stamp} {speaker}{p.text.strip()}")
            if cfg.obsidian.transcript_collapsed:
                quoted = "\n>\n".join("> " + ln.replace("\n", "\n> ") for ln in lines)
                parts.append(f"## 转写全文\n\n> [!quote]- 展开（{len(paras)} 段）\n{quoted}")
            else:
                parts.append("## 转写全文\n\n" + "\n\n".join(lines))

    return "\n\n".join(parts) + "\n"


def _save_thumbnail(vault: Path, cfg: Config, entry: library_mod.LibraryEntry,
                    fetch: ThumbFetcher) -> str | None:
    """封面存进附件目录，返回 ![[...]] 嵌入。抖音封面链接会过期、B 站防盗链，所以不直接引用远程地址。"""
    att_dir = vault / (cfg.obsidian.attachment_folder or "")
    stem = "vsum-" + sanitize_filename(entry.video_id, "video")
    if att_dir.is_dir():
        for p in att_dir.glob(stem + ".*"):
            return f"![[{_rel(vault, p)}]]"
    if not entry.thumbnail:
        return None
    try:
        data, ctype = fetch(entry.thumbnail)
    except Exception as exc:  # noqa: BLE001 —— 封面拿不到不影响存笔记
        log.info("封面没存下来（%s）：%s", entry.thumbnail, exc)
        return None
    ext = _THUMB_EXT.get(ctype.split(";")[0].strip().lower(), "jpg")
    att_dir.mkdir(parents=True, exist_ok=True)
    path = att_dir / f"{stem}.{ext}"
    path.write_bytes(data)
    return f"![[{_rel(vault, path)}]]"


# ---------- 入口 ----------


def export(
    cfg: Config,
    entry: library_mod.LibraryEntry,
    include: list[str] | None = None,
    *,
    app_url: str | None = None,
    fetch_thumb: ThumbFetcher | None = None,
    today: date | None = None,
) -> ExportResult:
    vault = vault_dir(cfg)
    include = list(cfg.obsidian.include if include is None else include)
    cache = Cache(cfg.cache_db)
    fetch = fetch_thumb or (lambda u: thumbs_mod.fetch(u, cfg.download.user_agent))

    recorded = cache.get_obsidian_export(entry.video_id)
    path = find_note(vault, entry.video_id, recorded[0] if recorded else None)

    existing_fm: dict[str, Any] | None = None
    my_notes: str | None = None
    if path is not None:
        existing_fm, old_body = split_note(path.read_text(encoding="utf-8"))
        my_notes = _my_notes(old_body)
        action = "updated"
    else:
        folder = vault / (cfg.obsidian.folder or "")
        name = sanitize_filename(entry.display_title, entry.video_id)
        path = folder / f"{name}.md"
        if path.exists():  # 同名但不是这条视频的笔记，别覆盖人家
            path = folder / f"{name} ({sanitize_filename(entry.video_id, 'video')}).md"
        action = "created"

    tags = [t for t in (clean_tag(e.tag) for e in cache.tags_for_video(entry.video_id)
                        if e.source != "rejected") if t]
    fm = build_frontmatter(entry, tags, existing_fm, today)
    body = render_body(cfg, entry, include, thumb_embed=_save_thumbnail(vault, cfg, entry, fetch),
                       app_url=app_url)
    text = dump_frontmatter(fm) + body + "\n" + (my_notes or MY_NOTES_HEADING + "\n")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    rel = _rel(vault, path)
    cache.set_obsidian_export(entry.video_id, rel)
    return ExportResult(path=path, rel=rel, action=action)


def status(cfg: Config, video_id: str) -> dict[str, Any] | None:
    """详情页用：这条视频存过没有、现在在库里哪。记下的位置失效了就扫一遍库重新找。"""
    if not cfg.obsidian.enabled:
        return None
    vault = Path(str(cfg.obsidian.vault_path))
    if not vault.is_dir():
        return None
    cache = Cache(cfg.cache_db)
    recorded = cache.get_obsidian_export(video_id)
    if recorded is None:
        return None
    rel, exported_at = recorded
    path = vault / rel
    if not (path.is_file() and _note_id(path) == video_id):
        path = find_note(vault, video_id)
        if path is None:
            cache.set_obsidian_export(video_id, None)
            return None
        rel = _rel(vault, path)
        cache.set_obsidian_export(video_id, rel)
    return {"path": rel, "exported_at": exported_at, "uri": note_uri(path)}
