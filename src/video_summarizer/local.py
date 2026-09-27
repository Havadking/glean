"""本地视频：直播录播之类，文件在硬盘上，不经过 yt-dlp。

**只记路径不拷文件** —— 录播动辄几个 GB，流水线只从里面抽一份 16k wav 去识别。

**身份按内容认，不按路径。** video_id = local- + 「大小 + 头尾各 4MB」的哈希：文件改名、挪目录后
再加一次还是同一条，缓存和已有总结照样命中，也不会重复入库。算一次只读 8MB，几 GB 的文件也是毫秒级。

产出的 VideoInfo 和 yt-dlp 探测的长得一样（extractor="local"、url 是文件路径），后面的转写、
纠错、总结、标签整条链路不用区分来源。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from .cache import Cache
from .config import Config
from .errors import DownloadError
from .ytdlp_base import VideoInfo

log = logging.getLogger(__name__)

EXTRACTOR = "local"

MEDIA_EXTS = {
    ".mp4", ".flv", ".mkv", ".ts", ".mov", ".webm", ".avi", ".m4v", ".wmv", ".m2ts",
    ".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus",
}

_CHUNK = 4 * 1024 * 1024
_WIN_PATH_RE = re.compile(r"^(?:[a-zA-Z]:[\\/]|\\\\)")
# 录播工具的文件名里常带日期：20240512、2024-05-12、2024_05_12、2024.05.12
_DATE_RE = re.compile(r"(?<!\d)(20\d{2})[-_.年]?(0[1-9]|1[0-2])[-_.月]?(0[1-9]|[12]\d|3[01])(?!\d)")


def is_local(text: str) -> bool:
    """这段输入是不是本地文件路径（而不是链接）。"""
    t = (text or "").strip().strip('"')
    return t.lower().startswith("file://") or bool(_WIN_PATH_RE.match(t)) or t.startswith("/")


def to_path(text: str) -> Path:
    """用户贴进来的东西 -> Path。资源管理器「复制文件地址」会带引号，file:// 链接要解码。"""
    t = (text or "").strip().strip('"').strip()
    if t.lower().startswith("file://"):
        parsed = urlparse(t)
        t = unquote(parsed.path)
        if re.match(r"^/[a-zA-Z]:", t):  # file:///E:/x.mp4 -> /E:/x.mp4
            t = t[1:]
    return Path(t)


def check_file(path: Path) -> Path:
    if not path.is_file():
        raise DownloadError(f"找不到这个文件：{path}")
    if path.suffix.lower() not in MEDIA_EXTS:
        raise DownloadError(f"不像是音视频文件（{path.suffix or '没有扩展名'}）：{path.name}")
    return path.resolve()


def fingerprint(path: Path) -> str:
    size = path.stat().st_size
    h = hashlib.sha1(str(size).encode())
    with path.open("rb") as f:
        h.update(f.read(_CHUNK))
        if size > 2 * _CHUNK:
            f.seek(-_CHUNK, 2)
            h.update(f.read(_CHUNK))
    return f"local-{h.hexdigest()[:16]}"


def media_info(path: Path) -> dict:
    """ffprobe 读时长和有没有视频轨。没装 ffprobe 或读失败就返回空，时长留给 ASR 结果补。"""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        log.warning("找不到 ffprobe，读不了时长")
        return {}
    cmd = [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        log.warning("ffprobe 读不了 %s：%s", path.name, proc.stderr.strip()[:200])
        return {}
    try:
        raw = json.loads(proc.stdout or "{}")
    except ValueError:
        return {}
    streams = raw.get("streams") or []
    return {
        "duration": float((raw.get("format") or {}).get("duration") or 0.0),
        "has_video": any(s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
                         for s in streams),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
    }


def guess_date(path: Path) -> str:
    """文件名里认得出日期就用它，否则用文件修改时间（录播结束的时间）。YYYYMMDD。"""
    m = _DATE_RE.search(path.stem)
    if m:
        return "".join(m.groups())
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y%m%d")


def thumb_path(cfg: Config, video_id: str) -> Path:
    return cfg.cache_db.parent / "local_thumbs" / f"{video_id}.jpg"


def thumb_url(video_id: str) -> str:
    return f"/api/local/{video_id}/thumb"


def make_thumb(path: Path, target: Path, duration: float) -> bool:
    """截 10% 处的一帧当封面。开头常是黑屏或片头，10% 一般已经进正题了。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    at = f"{max(0.0, duration * 0.1):.2f}"
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-ss", at, "-i", str(path),
           "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "4", str(target)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not target.is_file():
        log.info("截封面失败（纯音频文件没有画面是正常的）：%s", proc.stderr.strip()[:200])
        return False
    return True


def probe(text: str, cfg: Config, *, with_thumb: bool = True) -> VideoInfo:
    """本地文件 -> VideoInfo。之前加过的文件沿用当时填的标题、主播和日期。"""
    path = check_file(to_path(text))
    video_id = fingerprint(path)
    cache = Cache(cfg.cache_db)
    known = cache.get_local_source(video_id)
    meta = cache.get_video_meta(video_id)
    mi = media_info(path)
    duration = mi.get("duration") or 0.0

    thumbnail = None
    if with_thumb and mi.get("has_video", True):
        tp = thumb_path(cfg, video_id)
        if tp.is_file() or make_thumb(path, tp, duration):
            thumbnail = thumb_url(video_id)

    # 文件挪过地方：顺手把新位置记上
    if known and known["path"] != str(path):
        cache.put_local_source(video_id, path=str(path), size=path.stat().st_size)

    return VideoInfo(
        url=str(path),
        video_id=video_id,
        title=(known or {}).get("title") or path.stem,
        duration_sec=duration,
        extractor=EXTRACTOR,
        uploader=meta.get("uploader"),
        upload_date=meta.get("upload_date") or guess_date(path),
        thumbnail=thumbnail,
    )


def locate(cfg: Config, video_id: str, source_url: str | None = None) -> Path | None:
    """这条本地视频的原文件现在在哪。找不到（挪走、删了）返回 None。"""
    rec = Cache(cfg.cache_db).get_local_source(video_id)
    for cand in ((rec or {}).get("path"), source_url):
        if cand and Path(cand).is_file():
            return Path(cand)
    return None


def pick_files(multiple: bool = False) -> list[str]:
    """弹出系统的文件选择框。服务跑在本机，这样拿得到真实路径（浏览器的 <input type=file> 拿不到）。"""
    import tkinter
    from tkinter import filedialog

    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        types = [("音视频", " ".join(f"*{e}" for e in sorted(MEDIA_EXTS))), ("所有文件", "*.*")]
        if multiple:
            picked = filedialog.askopenfilenames(parent=root, title="选择录播文件", filetypes=types)
            return [str(Path(p)) for p in picked]
        one = filedialog.askopenfilename(parent=root, title="选择录播文件", filetypes=types)
        return [str(Path(one))] if one else []
    finally:
        root.destroy()
