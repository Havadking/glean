"""模块二：音频提取。

yt-dlp 拿最佳音轨，再用 ffmpeg 转成 16kHz 单声道 wav —— ASR 模型内部都按这个规格重采样，
提前转好既避免二次有损压缩，也把 2 小时视频的音频从 GB 级压到百 MB 级。

重试策略：先按 yt-dlp 的默认行为走（它自己会处理各站点的 UA 和 Referer），
失败了退避重试，最后一次才换上配置里的浏览器 UA + Referer。
B 站的 412 是按 IP 的频率风控，换 UA 没用，退避等待才是对策。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from yt_dlp.utils import DownloadError as YTDLPDownloadError

from ..config import Config
from ..errors import DownloadError
from ..ytdlp_base import VideoInfo, build_ydl_opts, download

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
_BACKOFF_SEC = 5


def extract(info: VideoInfo, cfg: Config, workdir: Path, force: bool = False) -> Path:
    """下载音轨并转成 16k 单声道 wav，返回 wav 路径。已存在则直接复用。"""
    workdir.mkdir(parents=True, exist_ok=True)
    wav_path = workdir / f"{info.video_id}.wav"

    if wav_path.is_file() and wav_path.stat().st_size > 0 and not force:
        log.info("复用已有音频: %s", wav_path.name)
        return wav_path

    source = _download_audio(info, cfg, workdir)
    _to_wav(source, wav_path)
    if source != wav_path:
        source.unlink(missing_ok=True)
    return wav_path


def _download_audio(info: VideoInfo, cfg: Config, workdir: Path) -> Path:
    attempts: list[dict[str, str] | None] = [None, None]
    if cfg.download.user_agent:
        headers = {"User-Agent": cfg.download.user_agent}
        origin = _origin(info.url)
        if origin:
            headers["Referer"] = origin
        attempts.append(headers)

    last_error: Exception | None = None
    for i, headers in enumerate(attempts, start=1):
        try:
            return _run_ydl(info, cfg, workdir, headers)
        except (YTDLPDownloadError, DownloadError) as exc:
            last_error = exc
            if i == len(attempts):
                break
            wait = _BACKOFF_SEC * i
            hint = "换自定义 UA + Referer " if attempts[i] else ""
            log.warning("音频下载失败，%ds 后%s重试（%d/%d）：%s",
                        wait, hint, i, len(attempts), exc)
            time.sleep(wait)

    raise DownloadError(
        f"下载音频失败: {last_error}\n"
        "如果是 B 站的 412 / 403，多半是触发了风控或需要登录，"
        "可以在 config.yaml 里把 download.cookies_from_browser 设成 chrome/edge 试试。"
    )


def _run_ydl(
    info: VideoInfo, cfg: Config, workdir: Path, headers: dict[str, str] | None
) -> Path:
    downloaded: list[str] = []

    def _hook(status: dict) -> None:
        if status.get("status") == "finished" and status.get("filename"):
            downloaded.append(status["filename"])

    opts = build_ydl_opts(
        cfg.download,
        override_headers=headers,
        format="bestaudio/best",
        outtmpl={"default": str(workdir / "%(id)s.%(ext)s")},
        progress_hooks=[_hook],
    )
    download(info, opts, cfg.download)

    if downloaded:
        return Path(downloaded[-1])

    # 极少数 extractor 不触发 hook，退回到按 id 找文件
    files = [p for p in workdir.iterdir() if p.is_file() and p.stem == info.video_id]
    if files:
        return max(files, key=lambda p: p.stat().st_mtime)
    raise DownloadError("yt-dlp 声称下载成功，但没找到输出文件")


def _to_wav(source: Path, target: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise DownloadError("找不到 ffmpeg，请先安装并加入 PATH")

    log.info("转码为 %d Hz 单声道 wav ...", SAMPLE_RATE)
    cmd = [
        ffmpeg,
        "-y",
        "-loglevel", "error",
        "-i", str(source),
        "-vn",
        "-ac", "1",
        "-ar", str(SAMPLE_RATE),
        "-c:a", "pcm_s16le",
        str(target),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not target.is_file():
        raise DownloadError(f"ffmpeg 转码失败:\n{proc.stderr.strip()}")


def _origin(url: str) -> str | None:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}/"
