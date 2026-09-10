"""串联四个模块：URL -> 字幕/ASR -> 结构化转写 -> LLM 总结 -> 落盘。"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import asr as asr_registry
from . import cache as cache_mod
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
    use_cache: bool = True,
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

    cache = cache_mod.Cache(cfg.cache_db if use_cache else None)
    cache_key = plan_transcript_key(info, cfg, force_asr=force_asr, diarize=diarize)

    transcript = _get_transcript(
        info, cfg, work_dir, transcript_path,
        force=force, force_asr=force_asr, diarize=diarize,
        cache=cache, cache_key=cache_key,
    )
    if wants_speakers and not transcript.speakers:
        log.warning(
            "选的是分说话人摘要，但这份转写没有说话人标签（%s），"
            "模型只能靠语气和称呼推断角色。",
            "官方字幕不区分说话人" if transcript.source_type == "subtitle" else "这一路的 ASR 没有分离能力",
        )
    # 记下指纹，下次缓存万一没了还能靠它认领这份产物
    transcript.meta["cache_key"] = cache_key
    transcript.save(transcript_path)
    cache.put_transcript(cache_key, transcript)
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
    summary_cache_key = cache_mod.summary_key(
        transcript_key=cache_key,
        provider_desc=provider.describe(),
        summary_type=options.summary_type,
        language=options.language,
        extra=options.extra_instructions,
    )

    # 同样的转写 + 同样的模型 + 同样的总结类型，没必要再花一次钱
    cached_summary = None if force else cache.get_summary(summary_cache_key)
    if cached_summary is not None:
        log.info("命中总结缓存（%s），跳过大模型调用", summary_cache_key[:12])
        summary = cached_summary
    else:
        estimate = provider.plan(transcript, options)
        result.estimate = estimate
        if confirm is not None and not confirm(estimate, transcript):
            result.summary_skipped_reason = "用户取消"
            return result

        log.info("调用 %s 生成总结 ...", provider.describe())
        summary = provider.summarize(transcript, options)
        cache.put_summary(
            summary_cache_key,
            transcript_key=cache_key,
            transcript=transcript,
            provider_desc=provider.describe(),
            summary_type=options.summary_type,
            language=options.language,
            content=summary,
        )

    summary_path = work_dir / "summary.md"
    summary_path.write_text(
        render_summary_markdown(summary, transcript, provider.describe(), options),
        encoding="utf-8",
    )
    result.summary = summary
    result.summary_path = summary_path
    log.info("总结已保存: %s", summary_path)
    return result


def plan_transcript_key(
    info: VideoInfo, cfg: Config, *, force_asr: bool, diarize: bool
) -> str:
    """算出这次会产出什么样的转写，据此得到缓存指纹。

    走字幕还是走 ASR 在探测阶段就能定，不用真跑一遍才知道。
    """
    picked = None if force_asr else fetcher.select_subtitle_language(info, cfg)
    if picked is not None:
        lang, is_auto = picked
        return cache_mod.transcript_key(
            video_id=info.video_id, extractor=info.extractor, source_type="subtitle",
            subtitle_language=lang, automatic_captions=is_auto,
        )
    return cache_mod.transcript_key(
        video_id=info.video_id, extractor=info.extractor, source_type="asr",
        asr_provider=cfg.asr.provider,
        asr_model=cfg.asr.diarize_model if diarize else cfg.asr.model,
        asr_language=cfg.asr.language, diarize=diarize,
    )


def _get_transcript(
    info: VideoInfo,
    cfg: Config,
    work_dir: Path,
    transcript_path: Path,
    *,
    force: bool,
    force_asr: bool,
    diarize: bool = False,
    cache: cache_mod.Cache | None = None,
    cache_key: str | None = None,
) -> Transcript:
    # 缓存按输入指纹索引：换了 ASR 模型、开了说话人分离，指纹就变了，
    # 不会拿到设置不符的旧结果。这是相比"看 transcript.json 在不在"的关键改进。
    # --no-cache 传进来的是个禁用状态的 Cache，这时连输出目录里的产物也不认领，
    # 免得"不走缓存"却还是拿到了上次的结果。音频文件仍然复用（那是 --force 管的）。
    if not force and cache_key and cache is not None and cache.enabled:
        cached = cache.get_transcript(cache_key)
        if cached is not None:
            log.info(
                "命中转写缓存（%s，%d 条分句），跳过%s",
                cache_key[:12], len(cached.segments),
                "字幕下载" if cached.source_type == "subtitle" else "语音识别",
            )
            return cached

        # 缓存里没有，但输出目录里躺着一份指纹相符的产物（缓存删了、或者从旧版本升上来）
        adopted = _adopt_existing(transcript_path, cache_key)
        if adopted is not None:
            return adopted

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


def _adopt_existing(transcript_path: Path, cache_key: str) -> Transcript | None:
    """认领输出目录里已有的 transcript.json —— 但只在指纹对得上时。

    产出时会把 cache_key 写进 meta，所以能判断这份转写是不是用当前设置跑出来的。
    没有这个字段的是旧版本留下的，宁可重跑也不冒用错设置的险。
    """
    if not transcript_path.is_file():
        return None
    try:
        existing = Transcript.load(transcript_path)
    except (OSError, ValueError, TypeError) as exc:
        log.debug("已有的 transcript.json 读不了，忽略：%s", exc)
        return None

    if existing.meta.get("cache_key") != cache_key:
        return None
    log.info("认领输出目录里已有的转写（%d 条分句），跳过重算", len(existing.segments))
    return existing


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
