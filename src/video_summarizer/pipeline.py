"""串联四个模块：URL -> 字幕/ASR -> 结构化转写 -> LLM 总结 -> 落盘。"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import asr as asr_registry
from . import summarizer as summarizer_registry
from .audio import extractor
from .config import Config
from .errors import SubtitleNotFoundError
from .models import SummaryOptions, Transcript
from .subtitle import fetcher
from .summarizer.base import CostEstimate
from .summarizer.tokens import format_timestamp
from .ytdlp_base import VideoInfo, probe

log = logging.getLogger(__name__)

# 返回 False 表示用户不同意继续调用 LLM
ConfirmFn = Callable[[CostEstimate, "Transcript"], bool]


@dataclass
class PipelineResult:
    info: VideoInfo
    transcript: Transcript
    transcript_path: Path
    work_dir: Path
    summary: str | None = None
    summary_path: Path | None = None
    estimate: CostEstimate | None = None
    summary_skipped_reason: str | None = None


def run(
    url: str,
    cfg: Config,
    *,
    options: SummaryOptions,
    force: bool = False,
    force_asr: bool = False,
    skip_summary: bool = False,
    confirm: ConfirmFn | None = None,
) -> PipelineResult:
    log.info("探测视频信息 ...")
    info = probe(url, cfg.download)
    log.info(
        "《%s》 时长 %s 来源 %s",
        info.title, format_timestamp(info.duration_sec), info.extractor,
    )

    work_dir = cfg.output_dir / _slug(info)
    work_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = work_dir / "transcript.json"

    # 分角色总结要求转写带说话人标签，diarize=auto 时据此决定开不开
    wants_speakers = options.summary_type == "by_speaker"
    diarize = cfg.asr.wants_diarization(needed=wants_speakers)

    transcript = _get_transcript(
        info, cfg, work_dir, transcript_path,
        force=force, force_asr=force_asr, diarize=diarize,
    )
    if wants_speakers and not transcript.speakers:
        log.warning(
            "选的是分说话人摘要，但这份转写没有说话人标签"
            "（%s），模型只能靠语气和称呼推断角色。加 --diarize --force 可以重跑分离。",
            "官方字幕不区分说话人" if transcript.source_type == "subtitle" else "复用了旧转写或 ASR 未开分离",
        )
    transcript.save(transcript_path)
    log.info("转写已保存: %s（%d 条分句）", transcript_path, len(transcript.segments))

    result = PipelineResult(
        info=info,
        transcript=transcript,
        transcript_path=transcript_path,
        work_dir=work_dir,
    )

    if skip_summary:
        result.summary_skipped_reason = "按 --no-summary 跳过"
        return result

    provider = summarizer_registry.get_provider(cfg.summarizer)
    estimate = provider.plan(transcript, options)
    result.estimate = estimate

    if confirm is not None and not confirm(estimate, transcript):
        result.summary_skipped_reason = "用户取消"
        return result

    log.info("调用 %s 生成总结 ...", provider.describe())
    summary = provider.summarize(transcript, options)
    summary_path = work_dir / "summary.md"
    summary_path.write_text(
        render_summary_markdown(summary, transcript, provider.describe(), options),
        encoding="utf-8",
    )
    result.summary = summary
    result.summary_path = summary_path
    log.info("总结已保存: %s", summary_path)
    return result


def _get_transcript(
    info: VideoInfo,
    cfg: Config,
    work_dir: Path,
    transcript_path: Path,
    *,
    force: bool,
    force_asr: bool,
    diarize: bool = False,
) -> Transcript:
    if transcript_path.is_file() and not force:
        log.info("复用已有转写: %s（要重跑加 --force）", transcript_path.name)
        return Transcript.load(transcript_path)

    if not force_asr:
        try:
            return fetcher.fetch(info, cfg, workdir=work_dir / "subs")
        except SubtitleNotFoundError as exc:
            log.info("%s，转走语音识别", exc)

    log.info("提取音频 ...")
    audio_path = extractor.extract(info, cfg, work_dir / "audio", force=force)

    provider = asr_registry.get_provider(cfg.asr, diarize=diarize)
    log.info(
        "开始语音识别（provider=%s%s）...", provider.name, "，带说话人分离" if diarize else "",
    )
    try:
        asr_result = provider.transcribe(audio_path)
    finally:
        provider.close()

    return Transcript(
        source_url=info.url,
        source_type="asr",
        language=asr_result.language,
        duration_sec=info.duration_sec or (asr_result.segments[-1].end if asr_result.segments else 0.0),
        segments=asr_result.segments,
        title=info.title,
        video_id=info.video_id,
        meta={"extractor": info.extractor, **asr_result.meta},
    )


def render_summary_markdown(
    summary: str, transcript: Transcript, provider_desc: str, options: SummaryOptions
) -> str:
    """总结正文前加一段来源信息，便于追溯（DESIGN.md 5.2）。"""
    source_label = "官方字幕" if transcript.source_type == "subtitle" else "语音识别"
    if transcript.source_type == "asr":
        source_label += f"（{transcript.meta.get('asr_model', '?')}）"

    front = [
        f"# {transcript.title or '视频总结'}",
        "",
        f"- 来源：<{transcript.source_url}>",
        f"- 时长：{format_timestamp(transcript.duration_sec)}",
        f"- 转写方式：{source_label}",
        f"- 转写语言：{transcript.language or '未知'}",
        f"- 总结模型：{provider_desc}",
        f"- 总结类型：{options.summary_type}",
        f"- 生成时间：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %z')}",
        "",
        "---",
        "",
    ]
    return "\n".join(front) + summary.strip() + "\n"


_UNSAFE_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _slug(info: VideoInfo) -> str:
    """用 `标题-id` 做目录名，Windows 非法字符全换掉，长度截断。"""
    title = _UNSAFE_RE.sub("_", info.title or "untitled").strip(" .")
    title = re.sub(r"\s+", " ", title)[:60].strip(" .") or "untitled"
    return f"{title}-{info.video_id}"
