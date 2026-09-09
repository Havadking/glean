"""yt-dlp 的共用封装。

字幕模块和音频模块都靠 yt-dlp 做站点适配，公共的选项构造和探测逻辑放这里，
避免两处各写一份 UA / cookie / 重试策略。

关于风控：B 站对同一 IP 的请求频率敏感，超了就返回 412 Precondition Failed，
和 UA 无关（裸 yt-dlp 同样会中）。对策是**少发请求**：探测阶段拿到的 info 直接复用给下载，
以及失败后退避重试。`download.user_agent` 默认不覆盖——yt-dlp 自己按站点设的 header
是经过测试的，自定义 UA 只在正常路径失败后作为最后一根稻草。
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError as YTDLPDownloadError

from .config import DownloadConfig
from .errors import DownloadError

log = logging.getLogger(__name__)

# 这些不是真的字幕轨：B 站弹幕、YouTube 直播聊天记录
PSEUDO_SUBTITLE_LANGS = {"danmaku", "live_chat", "rechat"}


@dataclass
class VideoInfo:
    """探测结果：够不够判断走字幕还是走 ASR。"""

    url: str
    video_id: str
    title: str
    duration_sec: float
    extractor: str
    manual_subs: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    auto_subs: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # 探测阶段 yt-dlp 返回的完整信息，后续下载直接复用，少发一轮请求
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def has_manual_subs(self) -> bool:
        return bool(self.manual_subs)


def build_ydl_opts(
    cfg: DownloadConfig, *, override_headers: dict[str, str] | None = None, **extra: Any
) -> dict[str, Any]:
    """构造 yt-dlp 选项。默认不动 header，交给 yt-dlp 的站点适配。"""
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 30,
        "ignoreerrors": False,
    }
    if override_headers:
        opts["http_headers"] = dict(override_headers)
    if cfg.cookies_from_browser:
        opts["cookiesfrombrowser"] = (cfg.cookies_from_browser,)
    opts.update(extra)
    return opts


def _clean_subs(raw: dict[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
    """剔除弹幕、直播聊天这类伪字幕轨。"""
    if not raw:
        return {}
    return {
        lang: formats
        for lang, formats in raw.items()
        if lang not in PSEUDO_SUBTITLE_LANGS and formats
    }


PROBE_RETRIES = 3
PROBE_BACKOFF_SEC = 8


def probe(url: str, cfg: DownloadConfig) -> VideoInfo:
    """只取元信息，不下载任何媒体。等价于 `yt-dlp --list-subs` 但结构化。

    被站点限流（B 站 412、YouTube 429）时退避重试几次再放弃。
    """
    opts = build_ydl_opts(cfg, skip_download=True)
    info = None
    last_error: Exception | None = None

    for attempt in range(1, PROBE_RETRIES + 1):
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.sanitize_info(ydl.extract_info(url, download=False))
            break
        except YTDLPDownloadError as exc:
            last_error = exc
            if attempt == PROBE_RETRIES or not _is_rate_limited(exc):
                raise DownloadError(_probe_error_message(exc)) from exc
            wait = PROBE_BACKOFF_SEC * attempt
            log.warning("探测被站点限流，%ds 后重试（%d/%d）", wait, attempt, PROBE_RETRIES)
            time.sleep(wait)

    if info is None:
        raise DownloadError(f"yt-dlp 没有返回任何信息: {url}（最后一次错误：{last_error}）")

    # 播放列表 / 合集：只取第一个条目
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise DownloadError(f"这个链接是个空播放列表: {url}")
        if len(entries) > 1:
            log.warning("链接是播放列表，共 %d 个条目，只处理第一个", len(entries))
        info = entries[0]

    return VideoInfo(
        url=info.get("webpage_url") or url,
        video_id=str(info.get("id") or "unknown"),
        title=info.get("title") or "untitled",
        duration_sec=float(info.get("duration") or 0.0),
        extractor=info.get("extractor_key") or info.get("extractor") or "unknown",
        manual_subs=_clean_subs(info.get("subtitles")),
        auto_subs=_clean_subs(info.get("automatic_captions")),
        raw=info,
    )


def download(info: VideoInfo, opts: dict[str, Any]) -> None:
    """执行下载。

    优先用探测阶段拿到的 info 直接下载（等价于 `--load-info-json`），
    这样不用再请求一次网页，能明显降低被站点风控拦住的概率。
    info 不可用时退回按 URL 重新解析。
    """
    if info.raw:
        with tempfile.TemporaryDirectory() as tmp:
            info_file = Path(tmp) / "info.json"
            try:
                info_file.write_text(json.dumps(info.raw), encoding="utf-8")
            except (TypeError, ValueError):
                log.debug("info 无法序列化，退回按 URL 下载")
            else:
                with YoutubeDL(opts) as ydl:
                    ydl.download_with_info_file(str(info_file))
                return

    with YoutubeDL(opts) as ydl:
        ydl.download([info.url])


def pick_language(
    available: dict[str, list[dict[str, Any]]], preferred: list[str]
) -> str | None:
    """按偏好顺序选字幕语言。

    先精确匹配，再做前缀匹配（`zh` 命中 `zh-Hans` / `zh-CN`），都没有就取第一个。
    """
    if not available:
        return None
    for want in preferred:
        if want in available:
            return want
    lowered = {lang.lower(): lang for lang in available}
    for want in preferred:
        w = want.lower()
        for lang_lower, lang in lowered.items():
            if lang_lower == w or lang_lower.startswith(w + "-") or w.startswith(lang_lower + "-"):
                return lang
    return next(iter(available))


_RATE_LIMIT_RE = re.compile(r"HTTP Error (?:412|429|403)\b")


def _is_rate_limited(exc: Exception) -> bool:
    return bool(_RATE_LIMIT_RE.search(str(exc)))


def _probe_error_message(exc: Exception) -> str:
    text = str(exc)
    if "412" in text:
        return "\n".join([
            f"探测视频信息失败: {text}",
            "412 是 B 站按 IP 的频率风控，和账号、UA 都无关。等几分钟再试；"
            "如果一直中，在 config.yaml 里把 download.cookies_from_browser "
            "设成 chrome/edge，用登录态请求会宽松很多。",
        ])
    return f"探测视频信息失败: {text}"
