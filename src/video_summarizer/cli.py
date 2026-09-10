"""命令行入口。"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import click

from . import cache as cache_mod
from .config import Config, load_config
from .errors import VideoSummarizerError
from .models import SummaryOptions, Transcript
from .pipeline import PipelineResult, render_summary_markdown, run as run_pipeline
from .summarizer import AVAILABLE_PROVIDERS as SUMMARIZER_PROVIDERS, get_provider as get_summarizer
from .summarizer.base import CostEstimate
from .summarizer.prompts import TEMPLATES
from .summarizer.tokens import format_timestamp
from .ytdlp_base import probe

SUMMARY_TYPES = tuple(TEMPLATES)


def _setup_logging(verbose: bool) -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    if not verbose:
        # yt-dlp / httpx 的 INFO 日志太吵
        for noisy in ("httpx", "httpcore", "openai", "urllib3"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


# `--provider` 切换时同时带上的默认值。只改 provider 不改这些的话，
# 会拿着 DeepSeek 的 key 和 base_url 去调 Claude，报错还很莫名其妙。
_PROVIDER_DEFAULTS = {
    "claude": {"api_key_env": "ANTHROPIC_API_KEY", "base_url": None, "model": "claude-opus-5"},
    "ollama": {"api_key_env": "", "base_url": "http://localhost:11434", "model": None},
}


def _load(config_path: Path | None, overrides: dict) -> Config:
    cfg = load_config(config_path)

    provider = overrides.get("provider")
    if provider and provider.strip().lower() != cfg.summarizer.provider.strip().lower():
        cfg.summarizer.provider = provider
        for field, value in _PROVIDER_DEFAULTS.get(provider.strip().lower(), {}).items():
            if value is not None or field == "base_url":
                setattr(cfg.summarizer, field, value)

    if overrides.get("model"):
        cfg.summarizer.model = overrides["model"]
    if overrides.get("base_url"):
        cfg.summarizer.base_url = overrides["base_url"]
    if overrides.get("asr_model"):
        cfg.asr.model = overrides["asr_model"]
    if overrides.get("device"):
        cfg.asr.device = overrides["device"]
    if overrides.get("diarize") is not None:
        cfg.asr.diarize = overrides["diarize"]
    if overrides.get("output_dir"):
        cfg.output_dir = Path(overrides["output_dir"]).expanduser().resolve()
    return cfg


def _make_confirm(auto_yes: bool):
    def confirm(estimate: CostEstimate, transcript: Transcript) -> bool:
        click.echo("", err=True)
        click.echo(
            f"  转写长度   约 {estimate.transcript_tokens:,} tokens"
            f"（{len(transcript.segments)} 条分句）",
            err=True,
        )
        if estimate.is_chunked:
            click.echo(
                f"  处理策略   map-reduce，切成 {estimate.chunks} 块 + 1 次汇总，"
                f"共 {estimate.chunks + 1} 次请求",
                err=True,
            )
        else:
            click.echo("  处理策略   整篇一次性送入，1 次请求", err=True)
        click.echo(
            f"  预计输入   约 {estimate.estimated_input_tokens:,} tokens（估算值，按量计费）",
            err=True,
        )
        click.echo("", err=True)
        if auto_yes:
            return True
        return click.confirm("继续调用大模型？", default=True, err=True)

    return confirm


def _report(result: PipelineResult) -> None:
    click.echo("")
    click.echo(f"转写  {result.transcript_path}")
    if result.summary_path:
        click.echo(f"总结  {result.summary_path}")
    elif result.summary_skipped_reason:
        click.echo(f"总结  已跳过（{result.summary_skipped_reason}）")


_config_option = click.option(
    "--config", "config_path", type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="指定 config.yaml 路径（默认找当前目录和项目根目录）",
)
_verbose_option = click.option("-v", "--verbose", is_flag=True, help="打印调试日志")


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="video-summarizer", prog_name="vsum")
def main() -> None:
    """视频转写总结工具：给个链接，产出结构化转写和总结。"""


@main.command()
@click.argument("url")
@click.option(
    "--summary-type", type=click.Choice(SUMMARY_TYPES), default=None,
    help="总结类型（默认读 config.yaml）",
)
@click.option("--lang", default="zh", show_default=True, help="总结输出语言")
@click.option("--extra", default=None, help="附加到 prompt 的自定义要求")
@click.option("--provider", type=click.Choice(SUMMARIZER_PROVIDERS + ("anthropic",)), default=None,
              help="临时切换总结 provider（会一并套用该 provider 的默认 key 变量和接口地址）")
@click.option("--model", default=None, help="临时覆盖总结模型")
@click.option("--base-url", default=None, help="临时覆盖 OpenAI 兼容接口地址")
@click.option("--asr-model", default=None, help="临时覆盖 ASR 模型，如 sensevoice-small / large-v3")
@click.option("--device", type=click.Choice(["auto", "cuda", "cpu"]), default=None, help="ASR 设备")
@click.option("--diarize/--no-diarize", default=None,
              help="是否做说话人分离（默认 auto：只在选分说话人摘要时开）")
@click.option("--output-dir", default=None, help="临时覆盖输出目录")
@click.option("--force", is_flag=True, help="忽略已有的转写和音频缓存，全部重跑")
@click.option("--force-asr", is_flag=True, help="即使有字幕也强制走语音识别")
@click.option("--no-summary", is_flag=True, help="只转写，不调用大模型")
@click.option("--no-cache", is_flag=True, help="这次不读也不写缓存")
@click.option("-y", "--yes", is_flag=True, help="跳过成本确认")
@_config_option
@_verbose_option
def run(url: str, summary_type, lang, extra, provider, model, base_url, asr_model, device, diarize,
        output_dir, force, force_asr, no_summary, no_cache, yes, config_path, verbose) -> None:
    """处理一个视频链接：URL -> 转写 -> 总结。"""
    _setup_logging(verbose)
    cfg = _load(config_path, {
        "provider": provider, "model": model, "base_url": base_url, "asr_model": asr_model,
        "device": device, "output_dir": output_dir, "diarize": diarize,
    })
    options = SummaryOptions(
        summary_type=summary_type or cfg.summarizer.summary_type,
        language=lang,
        extra_instructions=extra,
    )
    result = run_pipeline(
        url, cfg,
        options=options,
        force=force,
        force_asr=force_asr,
        skip_summary=no_summary,
        use_cache=not no_cache,
        confirm=_make_confirm(yes),
    )
    _report(result)


@main.command()
@click.argument("url")
@_config_option
@_verbose_option
def inspect(url: str, config_path, verbose) -> None:
    """只探测：看看这个链接有没有人工字幕、时长多少。不下载任何东西。"""
    _setup_logging(verbose)
    cfg = _load(config_path, {})
    info = probe(url, cfg.download)

    click.echo(f"标题    {info.title}")
    click.echo(f"ID      {info.video_id}")
    click.echo(f"时长    {format_timestamp(info.duration_sec)}")
    click.echo(f"站点    {info.extractor}")
    click.echo(f"人工字幕 {', '.join(info.manual_subs) if info.manual_subs else '无'}")
    click.echo(f"自动字幕 {', '.join(info.auto_subs) if info.auto_subs else '无'}")
    click.echo("")
    if info.has_manual_subs:
        click.echo("=> 会走字幕路径，秒出转写。")
    else:
        click.echo("=> 没有人工字幕，会下载音频跑语音识别（耗时取决于视频长度和显卡）。")


@main.command()
@click.argument("transcript_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--summary-type", type=click.Choice(SUMMARY_TYPES), default=None,
    help="总结类型（默认读 config.yaml）",
)
@click.option("--lang", default="zh", show_default=True, help="总结输出语言")
@click.option("--extra", default=None, help="附加到 prompt 的自定义要求")
@click.option("--provider", type=click.Choice(SUMMARIZER_PROVIDERS + ("anthropic",)), default=None,
              help="临时切换总结 provider（会一并套用该 provider 的默认 key 变量和接口地址）")
@click.option("--model", default=None, help="临时覆盖总结模型")
@click.option("--base-url", default=None, help="临时覆盖 OpenAI 兼容接口地址")
@click.option("-o", "--out", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help="总结输出路径（默认写到 transcript.json 同级的 summary.md）")
@click.option("-y", "--yes", is_flag=True, help="跳过成本确认")
@_config_option
@_verbose_option
def summarize(transcript_path: Path, summary_type, lang, extra, provider, model, base_url,
              out, yes, config_path, verbose) -> None:
    """对已有的 transcript.json 重新总结，不重跑转写。"""
    _setup_logging(verbose)
    cfg = _load(config_path, {"provider": provider, "model": model, "base_url": base_url})
    transcript = Transcript.load(transcript_path)
    options = SummaryOptions(
        summary_type=summary_type or cfg.summarizer.summary_type,
        language=lang,
        extra_instructions=extra,
    )

    provider = get_summarizer(cfg.summarizer)
    estimate = provider.plan(transcript, options)
    if not _make_confirm(yes)(estimate, transcript):
        click.echo("已取消。")
        return

    summary = provider.summarize(transcript, options)
    target = out or transcript_path.parent / "summary.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        render_summary_markdown(summary, transcript, provider.describe(), options), encoding="utf-8"
    )
    click.echo("")
    click.echo(f"总结  {target}")


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="监听地址")
@click.option("--port", default=7860, show_default=True, type=int, help="监听端口")
@click.option("--share", is_flag=True, help="生成 Gradio 公网临时链接")
@click.option("--output-dir", default=None, help="临时覆盖输出目录")
@_config_option
@_verbose_option
def ui(host, port, share, output_dir, config_path, verbose) -> None:
    """启动 Web 界面。"""
    _setup_logging(verbose)
    cfg = _load(config_path, {"output_dir": output_dir})
    from .web.app import launch

    click.echo(f"界面地址 http://{host}:{port}")
    launch(cfg, host=host, port=port, share=share)


@main.group(invoke_without_command=True)
@_config_option
@click.pass_context
def cache(ctx, config_path) -> None:
    """查看和清理缓存。不带子命令时打印概况。"""
    cfg = _load(config_path, {})
    ctx.obj = cfg
    if ctx.invoked_subcommand is not None:
        return

    store = cache_mod.Cache(cfg.cache_db)
    info = store.stats()
    if not info.get("enabled"):
        click.echo("缓存未启用（config.yaml 里 cache_db 为空，或者库打不开）")
        return
    click.echo(f"缓存文件    {info['path']}")
    click.echo(f"占用        {info['size_bytes'] / 1024:,.0f} KB")
    click.echo(f"转写        {info['transcripts']} 条"
               f"（覆盖 {format_timestamp(info['cached_audio_sec'])} 音频）")
    click.echo(f"总结        {info['summaries']} 条")
    click.echo("")
    click.echo("`vsum cache list` 看明细，`vsum cache clear` 清理。")


@cache.command("list")
@click.option("-n", "--limit", default=20, show_default=True, help="最多列几条")
@click.pass_obj
def cache_list(cfg: Config, limit: int) -> None:
    """列出缓存里的转写和总结。"""
    store = cache_mod.Cache(cfg.cache_db)

    transcripts = store.list_transcripts(limit)
    click.echo(f"转写（{len(transcripts)} 条）")
    if not transcripts:
        click.echo("  （空）")
    for e in transcripts:
        marks = [e.source_type]
        if e.has_speakers:
            marks.append("带说话人")
        click.echo(
            f"  {e.key[:12]}  {format_timestamp(e.duration_sec):>8}  "
            f"{e.segment_count:>4} 句  [{'/'.join(marks)}]  {(e.title or e.video_id)[:40]}"
        )

    summaries = store.list_summaries(limit)
    click.echo("")
    click.echo(f"总结（{len(summaries)} 条）")
    if not summaries:
        click.echo("  （空）")
    for s in summaries:
        click.echo(
            f"  {s.key[:12]}  {s.summary_type:<11} {s.provider:<34} "
            f"{(s.title or s.video_id)[:30]}"
        )


@cache.command("clear")
@click.option("--video-id", default=None, help="只清这个视频的（默认清全部）")
@click.option("-y", "--yes", is_flag=True, help="不用确认")
@click.pass_obj
def cache_clear(cfg: Config, video_id: str | None, yes: bool) -> None:
    """清空缓存。产物文件不受影响，只是下次要重算。"""
    store = cache_mod.Cache(cfg.cache_db)
    scope = f"视频 {video_id} 的缓存" if video_id else "全部缓存"
    if not yes and not click.confirm(f"确定清掉{scope}？下次要重跑 ASR。", default=False):
        click.echo("已取消。")
        return
    transcripts, summaries = store.clear(video_id)
    click.echo(f"已清除 {transcripts} 条转写、{summaries} 条总结。")


@main.command()
@_config_option
def config(config_path) -> None:
    """打印当前生效的配置和密钥状态（不显示密钥内容）。"""
    cfg = _load(config_path, {})
    click.echo(f"配置文件      {cfg.source_path or '（未找到，使用默认值）'}")
    click.echo(f"输出目录      {cfg.output_dir}")
    click.echo("")
    click.echo(f"ASR           {cfg.asr.provider} / {cfg.asr.model} (device={cfg.asr.device})")
    fallback = f"{cfg.asr.fallback} / {cfg.asr.fallback_model}" if cfg.asr.fallback else "（未配置）"
    click.echo(f"ASR 兜底      {fallback}")
    click.echo(f"说话人分离    {cfg.asr.diarize}（auto = 只在选分说话人摘要时开）")
    click.echo(f"总结          {cfg.summarizer.provider} / {cfg.summarizer.model}")
    click.echo(f"接口地址      {cfg.summarizer.base_url or '(SDK 默认)'}")
    click.echo(f"上下文预算    {cfg.summarizer.max_context_tokens:,} tokens"
               f"（切块策略 {cfg.summarizer.chunk_strategy}）")
    if cfg.summarizer.provider.strip().lower() == "ollama":
        click.echo("密钥          本地模型，不需要")
    else:
        has_key = bool(cfg.summarizer.api_key)
        click.echo(f"密钥          {cfg.summarizer.api_key_env} "
                   f"{'已设置' if has_key else '未设置 —— 复制 .env.example 为 .env 并填入'}")

    # 模型权重动辄几 GB，落哪个盘由环境变量决定，而环境变量只有新开的终端才读得到。
    # 打出来，省得下到一半才发现进了系统盘。
    store = cache_mod.Cache(cfg.cache_db)
    s = store.stats()
    click.echo(f"缓存          " + (f"{s['transcripts']} 条转写 / {s['summaries']} 条总结  {cfg.cache_db}" if s.get("enabled") else "未启用"))

    click.echo("")
    click.echo("模型缓存位置（由环境变量决定，改完要重开终端）")
    for var, what, default in (
        ("MODELSCOPE_CACHE", "FunASR 权重", "~/.cache/modelscope"),
        ("HF_HOME", "whisper 权重", "~/.cache/huggingface"),
    ):
        value = os.environ.get(var)
        click.echo(f"  {var:<18}{what:<12}{value or f'未设置 -> {default}'}")


def cli_main() -> None:
    try:
        main(standalone_mode=False)
    except click.Abort:
        click.echo("已中断。", err=True)
        sys.exit(130)
    except click.ClickException as exc:
        exc.show()
        sys.exit(exc.exit_code)
    except VideoSummarizerError as exc:
        click.secho(f"错误：{exc}", fg="red", err=True)
        sys.exit(1)
    except KeyboardInterrupt:
        click.echo("已中断。", err=True)
        sys.exit(130)


if __name__ == "__main__":
    cli_main()
