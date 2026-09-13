"""模块一：字幕获取。

只认 yt-dlp 里 "Available subtitles"（人工上传）那部分；
"Available automatic captions"（自动生成/自动翻译）默认忽略，准确率不够用来做总结。
B 站这类只有 danmaku 的情况在 ytdlp_base 里已经被过滤，会判定为"无字幕"。
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from yt_dlp.utils import DownloadError as YTDLPDownloadError

from ..config import Config
from ..errors import DownloadError, SubtitleNotFoundError
from ..models import Transcript
from ..ytdlp_base import VideoInfo, build_ydl_opts, download, pick_language
from . import vtt

log = logging.getLogger(__name__)

_PARSEABLE_SUFFIXES = (".vtt", ".srt")


def select_subtitle_language(info: VideoInfo, cfg: Config) -> tuple[str, bool] | None:
    """返回 (语言, 是否为自动字幕)；没有可用字幕返回 None。"""
    lang = pick_language(info.manual_subs, cfg.subtitle.preferred_languages)
    if lang:
        return lang, False
    if cfg.subtitle.accept_auto_captions:
        lang = pick_language(info.auto_subs, cfg.subtitle.preferred_languages)
        if lang:
            return lang, True
    return None


def fetch(info: VideoInfo, cfg: Config, workdir: Path | None = None) -> Transcript:
    """下载并解析字幕，产出结构化转写。没有人工字幕时抛 SubtitleNotFoundError。"""
    picked = select_subtitle_language(info, cfg)
    if picked is None:
        raise SubtitleNotFoundError(
            f"没有可用的人工字幕（自动字幕 {len(info.auto_subs)} 种，已按配置忽略）"
        )
    lang, is_auto = picked

    tmp = tempfile.TemporaryDirectory(dir=workdir) if workdir is None else None
    target_dir = Path(tmp.name) if tmp else Path(workdir)
    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        path = _download(info, cfg, lang, is_auto, target_dir)
        segments = vtt.parse_file(path)
    finally:
        if tmp is not None:
            tmp.cleanup()

    if not segments:
        raise SubtitleNotFoundError(f"字幕文件解析后是空的（语言 {lang}）")

    duration = info.duration_sec or (segments[-1].end if segments else 0.0)
    log.info("字幕命中：语言 %s，%d 条分句", lang, len(segments))

    return Transcript(
        source_url=info.url,
        source_type="subtitle",
        language=lang,
        duration_sec=duration,
        segments=segments,
        title=info.title,
        video_id=info.video_id,
        meta={
            "subtitle_language": lang,
            "automatic_captions": is_auto,
            **info.source_meta,
        },
    )


def _download(
    info: VideoInfo, cfg: Config, lang: str, is_auto: bool, target_dir: Path
) -> Path:
    opts = build_ydl_opts(
        cfg.download,
        skip_download=True,
        writesubtitles=not is_auto,
        writeautomaticsub=is_auto,
        subtitleslangs=[lang],
        subtitlesformat="vtt/srt/best",
        outtmpl={"default": str(target_dir / "%(id)s.%(ext)s")},
        postprocessors=[{"key": "FFmpegSubtitlesConvertor", "format": "vtt"}],
    )
    try:
        download(info, opts)
    except YTDLPDownloadError as exc:
        raise DownloadError(f"下载字幕失败: {exc}") from exc

    for suffix in _PARSEABLE_SUFFIXES:
        found = sorted(target_dir.glob(f"*{suffix}"))
        if found:
            return found[0]

    leftovers = ", ".join(p.name for p in sorted(target_dir.iterdir())) or "（空目录）"
    raise DownloadError(f"下载后没找到 vtt/srt 字幕文件，目录里只有: {leftovers}")
