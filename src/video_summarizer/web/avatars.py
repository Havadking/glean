"""UP 主头像：yt-dlp 的视频信息里没有作者头像，得按站点各自再问一次。

一位 UP 主只请求一次，图片存到 cache.sqlite 旁边的 avatars/ 目录里，以后直接从本地出。
拿不到的（抖音 cookie 过期、B 站限流、站点不认识）在内存里记一小时，别每次刷新页面都去打站点。
请求串行发——库页面一次要好几位 UP 主的头像，并发打 B 站最容易 412。
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from yt_dlp import YoutubeDL
from yt_dlp.networking import Request

from ..config import Config
from ..ytdlp_base import build_ydl_opts
from .library import LibraryEntry

log = logging.getLogger(__name__)

NEGATIVE_TTL_SEC = 3600
MAX_BYTES = 2 * 1024 * 1024

_lock = threading.Lock()
_failed_at: dict[str, float] = {}

_BV_SUFFIX_RE = re.compile(r"_p\d+$")   # 多 P 视频 yt-dlp 的 id 长 BV1xx_p2


def avatar_dir(cfg: Config) -> Path:
    return cfg.cache_db.parent / "avatars"


def cached(cfg: Config, name: str) -> Path | None:
    """本地已经有的头像文件；没有返回 None。"""
    stem = _stem(name)
    d = avatar_dir(cfg)
    if not d.is_dir():
        return None
    for p in d.glob(f"{stem}.*"):
        if p.is_file() and p.stat().st_size > 0:
            return p
    return None


def fetch(cfg: Config, name: str, entry: LibraryEntry) -> Path | None:
    """去站点拿一次头像并存到本地。拿不到返回 None，并在一小时内不再重试。"""
    with _lock:
        hit = cached(cfg, name)
        if hit is not None:
            return hit
        failed = _failed_at.get(name)
        if failed is not None and time.monotonic() - failed < NEGATIVE_TTL_SEC:
            return None
        try:
            path = _fetch_locked(cfg, name, entry)
        except Exception as exc:  # noqa: BLE001 —— 头像拿不到不算错，界面退回首字母
            log.info("拿不到 %s 的头像：%s", name, str(exc).splitlines()[0][:200])
            path = None
        if path is None:
            _failed_at[name] = time.monotonic()
        return path


def _fetch_locked(cfg: Config, name: str, entry: LibraryEntry) -> Path | None:
    site = _site(entry)
    if site is None:
        return None
    opts = build_ydl_opts(cfg.download, skip_download=True)
    with YoutubeDL(opts) as ydl:
        if site == "bilibili":
            url, referer = _bilibili_face(ydl, entry), "https://www.bilibili.com/"
        else:
            url, referer = _douyin_face(ydl, entry), "https://www.douyin.com/"
        if not url:
            return None
        with ydl.urlopen(Request(url, headers={"Referer": referer})) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            data = resp.read(MAX_BYTES + 1)
    if not data or len(data) > MAX_BYTES or not ctype.startswith("image/"):
        return None
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}.get(ctype)
    if not ext:
        return None
    d = avatar_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{_stem(name)}.{ext}"
    path.write_bytes(data)
    log.info("存下 %s 的头像（%s，%d 字节）", name, site, len(data))
    return path


def _bilibili_face(ydl: YoutubeDL, entry: LibraryEntry) -> str | None:
    """视频详情接口里带 owner.face。公开接口，不用 wbi 签名。"""
    bvid = _BV_SUFFIX_RE.sub("", entry.video_id)
    ie = ydl.get_info_extractor("BiliBili")
    data = ie._download_json(  # noqa: SLF001
        "https://api.bilibili.com/x/web-interface/view", bvid, query={"bvid": bvid},
        headers={"Referer": "https://www.bilibili.com/"}, note="拿 UP 主头像",
    )
    if not isinstance(data, dict) or data.get("code") != 0:
        return None
    owner = (data.get("data") or {}).get("owner") or {}
    return owner.get("face") or None


def _douyin_face(ydl: YoutubeDL, entry: LibraryEntry) -> str | None:
    """走 yt-dlp 抖音 extractor 用的同一个详情接口，cookie 一起复用；cookie 过期时这里也会 403。"""
    ie = ydl.get_info_extractor("Douyin")
    data = ie._download_json(  # noqa: SLF001
        "https://www.douyin.com/aweme/v1/web/aweme/detail/", entry.video_id,
        query={"aweme_id": entry.video_id}, note="拿作者头像",
    )
    author = ((data or {}).get("aweme_detail") or {}).get("author") or {}
    for key in ("avatar_larger", "avatar_medium", "avatar_thumb"):
        urls = (author.get(key) or {}).get("url_list") or []
        if urls:
            return urls[0]
    return None


def _site(entry: LibraryEntry) -> str | None:
    extractor = str(entry.meta.get("extractor") or "").lower()
    host = urlparse(entry.source_url or "").netloc.lower()
    if extractor.startswith("bilibili") or host.endswith("bilibili.com"):
        return "bilibili"
    if extractor.startswith("douyin") or host.endswith("douyin.com"):
        return "douyin"
    return None


def _stem(name: str) -> str:
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:16]
