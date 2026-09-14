"""合集 / UP 主空间：把一个列表链接展开成一页视频，供批量勾选。

两条路：
- **B 站个人空间**（space.bilibili.com/<uid>/video）直接调 yt-dlp 内部用的那个空间接口
  （`x/space/wbi/arc/search`，yt-dlp 负责 wbi 签名）。yt-dlp 自己在 flat 模式下只吐 BV 号，
  没标题没时长，勾选起来没法看；这个接口一页 30 条带标题、时长、封面、发布时间，
  还支持关键词 —— 一个 1500 条投稿的搬运号里找"护肤"两个字，靠的就是它。
- **其他列表**（B 站合集、YouTube 播放列表……）走 yt-dlp 的 flat 提取，按 playlist_items 分页。

每页一个请求，不预取。B 站 412 是按 IP 限流，列表页也算。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError as YTDLPDownloadError, ExtractorError

from .config import DownloadConfig
from .errors import DownloadError
from . import douyin
from .ytdlp_base import (
    VideoInfo, _is_rate_limited, build_ydl_opts, video_info_from_dict, wants_douyin_browser,
)

T = TypeVar("T")

log = logging.getLogger(__name__)

PAGE_SIZE = 30
_SPACE_RE = re.compile(r"space\.bilibili\.com/(\d+)(?:/upload)?(?:/video)?/?(?:[?#]|$)")
_LENGTH_RE = re.compile(r"^(?:(\d+):)?(\d+):(\d+)$")


@dataclass
class ListedVideo:
    video_id: str
    url: str
    title: str
    duration_sec: float | None = None
    thumbnail: str | None = None
    uploader: str | None = None
    upload_date: str | None = None   # YYYYMMDD

    def to_dict(self) -> dict[str, Any]:
        return {
            "video_id": self.video_id, "url": self.url, "title": self.title,
            "duration_sec": self.duration_sec, "thumbnail": self.thumbnail,
            "uploader": self.uploader, "upload_date": self.upload_date,
        }


@dataclass
class ListPage:
    kind: str                 # space | playlist
    title: str
    url: str
    page: int
    page_size: int
    total: int | None         # 不知道就 None
    entries: list[ListedVideo] = field(default_factory=list)
    keyword: str = ""

    @property
    def has_more(self) -> bool:
        if self.total is None:
            return len(self.entries) >= self.page_size
        return self.page * self.page_size < self.total

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "title": self.title, "url": self.url, "page": self.page,
            "page_size": self.page_size, "total": self.total, "has_more": self.has_more,
            "keyword": self.keyword, "entries": [e.to_dict() for e in self.entries],
        }


def _parse_length(s: str | None) -> float | None:
    m = _LENGTH_RE.match((s or "").strip())
    if not m:
        return None
    h, mm, ss = m.groups()
    return (int(h) if h else 0) * 3600 + int(mm) * 60 + int(ss)


def _yyyymmdd(ts: Any) -> str | None:
    from datetime import datetime, timezone

    try:
        return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y%m%d")
    except (TypeError, ValueError, OSError):
        return None


# 空间接口的限流比视频页紧得多：实测连着请求几次就 412，一两分钟才放开。
# 8 秒重试基本白等，等 20 秒再试一次，还不行就直接告诉用户等一会。
LIST_BACKOFF_SEC = (20,)

RATE_LIMIT_HINT = (
    "被 B 站限流了（412）。这个接口一两分钟内只能请求几次，等一两分钟再试；"
    "config.yaml 里配置 download.cookies_file（浏览器导出的登录态）会宽松很多。"
)


def _with_backoff(what: str, fn: Callable[[], T], waits: tuple[int, ...] = LIST_BACKOFF_SEC) -> T:
    """被限流（412/429）就退避重试。"""
    for attempt in range(len(waits) + 1):
        try:
            return fn()
        except (YTDLPDownloadError, ExtractorError) as exc:
            if not _is_rate_limited(exc):
                raise DownloadError(f"{what}：{exc}") from exc
            if attempt == len(waits):
                raise DownloadError(f"{what}：{RATE_LIMIT_HINT}") from exc
            wait = waits[attempt]
            log.warning("%s被站点限流，%ds 后重试（%d/%d）", what, wait, attempt + 1, len(waits))
            time.sleep(wait)
    raise AssertionError("unreachable")


def space_id(url: str) -> str | None:
    m = _SPACE_RE.search(url)
    return m.group(1) if m else None


def _bilibili_space(mid: str, cfg: DownloadConfig, page: int, keyword: str) -> ListPage:
    from yt_dlp.extractor.bilibili import BilibiliSpaceVideoIE

    page_url = f"https://space.bilibili.com/{mid}/video"
    with YoutubeDL(build_ydl_opts(cfg, skip_download=True)) as ydl:
        ie = BilibiliSpaceVideoIE(ydl)
        query = {
            "keyword": keyword, "mid": mid, "order": "pubdate", "order_avoided": "true",
            "platform": "web", "pn": page, "ps": PAGE_SIZE, "tid": 0,
            "web_location": "333.1387", "special_type": "", "index": 0,
            **ie._dm_params,  # noqa: SLF001 —— 和 yt-dlp 自己发的请求一模一样
        }
        resp = _with_backoff("读取空间投稿列表", lambda: ie._download_json(  # noqa: SLF001
            "https://api.bilibili.com/x/space/wbi/arc/search", mid,
            query=ie._sign_wbi(query, mid),  # noqa: SLF001
            headers={"Referer": page_url, "Origin": "https://space.bilibili.com",
                     "Accept-Language": "en,zh-CN;q=0.9,zh;q=0.8"},
            note=f"读取空间第 {page} 页",
        ))
    if not isinstance(resp, dict) or resp.get("code") != 0:
        code = resp.get("code") if isinstance(resp, dict) else "?"
        hint = RATE_LIMIT_HINT if code in (-401, -352, 412) else (resp.get("message") if isinstance(resp, dict) else "")
        raise DownloadError(f"空间接口返回错误（{code}）：{hint}")

    data = resp.get("data") or {}
    vlist = ((data.get("list") or {}).get("vlist")) or []
    entries = []
    author = None
    for v in vlist:
        if not v.get("bvid"):
            continue
        author = author or v.get("author")
        entries.append(ListedVideo(
            video_id=v["bvid"], url=f"https://www.bilibili.com/video/{v['bvid']}",
            title=v.get("title") or v["bvid"], duration_sec=_parse_length(v.get("length")),
            thumbnail=v.get("pic"), uploader=v.get("author"), upload_date=_yyyymmdd(v.get("created")),
        ))
    total = ((data.get("page") or {}).get("count"))
    title = f"{author} 的投稿" if author else f"空间 {mid}"
    if keyword:
        title += f"（搜「{keyword}」）"
    return ListPage(kind="space", title=title, url=page_url, page=page, page_size=PAGE_SIZE,
                    total=int(total) if isinstance(total, int) else None, entries=entries, keyword=keyword)


def _flat_extract(url: str, cfg: DownloadConfig, page: int) -> dict[str, Any]:
    start = (page - 1) * PAGE_SIZE + 1
    end = page * PAGE_SIZE
    opts = build_ydl_opts(cfg, skip_download=True, extract_flat="in_playlist",
                          playlist_items=f"{start}:{end}")

    def fetch():
        try:
            with YoutubeDL(opts) as ydl:
                return ydl.sanitize_info(ydl.extract_info(url, download=False))
        except YTDLPDownloadError as exc:
            if not wants_douyin_browser(url, cfg, exc):
                raise
            # 抖音没有列表，这里只会是单个视频；详情接口被风控拦下就换本机浏览器去拿
            log.info("yt-dlp 打不通抖音详情接口，改用本机浏览器：%s", str(exc).splitlines()[0][:120])
            return douyin.probe_via_browser(url, cfg)

    info = _with_backoff("读取列表", fetch)
    if not info:
        raise DownloadError(f"yt-dlp 没有返回任何信息: {url}")
    return info


_BV_PART_RE = re.compile(r"/video/(BV[0-9A-Za-z]{10})/?\?(?:.*&)?p=(\d+)")


def _flat_entry_id(page_url: str) -> str | None:
    """flat 条目没给 id 时从链接推。只认 B 站多 P 视频（每一 P 是 BVxxx?p=N）：
    yt-dlp 完整探测它时给的 id 是 BVxxx_pN，这里保持一致，库里的记录才对得上。
    其他站点乱编 id 和库里对不上，还是跳过。"""
    m = _BV_PART_RE.search(page_url)
    return f"{m.group(1)}_p{m.group(2)}" if m else None


def _page_from_flat(info: dict[str, Any], url: str, page: int) -> ListPage:
    entries = []
    for e in info.get("entries") or []:
        if not e:
            continue
        page_url = e.get("url") or e.get("webpage_url") or ""
        if not page_url:
            continue
        # B 站多 P 视频 flat 出来的条目只有 url，id / 标题都是空的
        vid = str(e.get("id") or "") or _flat_entry_id(page_url)
        if not vid:
            continue
        m = _BV_PART_RE.search(page_url)
        fallback_title = f"{info.get('title')} · P{m.group(2)}" if m and info.get("title") else vid
        entries.append(ListedVideo(
            video_id=vid, url=page_url, title=e.get("title") or fallback_title,
            duration_sec=float(e["duration"]) if e.get("duration") else None,
            thumbnail=e.get("thumbnail") or next((t.get("url") for t in (e.get("thumbnails") or []) if t.get("url")), None),
            uploader=e.get("uploader") or e.get("channel") or info.get("uploader") or info.get("channel"),
            upload_date=e.get("upload_date"),
        ))
    total = info.get("playlist_count")
    return ListPage(kind="playlist", title=info.get("title") or url, url=info.get("webpage_url") or url,
                    page=page, page_size=PAGE_SIZE, total=int(total) if isinstance(total, int) else None,
                    entries=entries)


def probe_any(url: str, cfg: DownloadConfig, *, page: int = 1, keyword: str = "") -> ListPage | VideoInfo:
    """一次请求分清楚是列表还是单个视频。

    单个视频的 flat 提取就是完整提取，直接转成 VideoInfo，不用再探测一次 ——
    对 B 站来说少一个请求就少一分被 412 的风险。
    """
    page = max(1, int(page))
    mid = space_id(url)
    if mid:
        return _bilibili_space(mid, cfg, page, keyword.strip())
    info = _flat_extract(url, cfg, page)
    if info.get("_type") == "playlist":
        return _page_from_flat(info, url, page)
    return video_info_from_dict(info, url)


def probe_listing(url: str, cfg: DownloadConfig, *, page: int = 1, keyword: str = "") -> ListPage | None:
    """只要列表：链接是单个视频就返回 None。"""
    result = probe_any(url, cfg, page=page, keyword=keyword)
    return result if isinstance(result, ListPage) else None
