"""命令行入口。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import click

from .config import Config, load_config
from .errors import VideoSummarizerError
from .models import SummaryOptions, Transcript
from .pipeline import PipelineResult, render_summary_markdown, run as run_pipeline
from .summarizer import get_provider as get_summarizer
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


def _load(config_path: Path | None, overrides: dict) -> Config:
    cfg = load_config(config_path)
    if overrides.get("model"):
        cfg.summarizer.model = overrides["model"]
    if overrides.get("base_url"):
        cfg.summarizer.base_url = overrides["base_url"]
    if overrides.get("asr_model"):
        cfg.asr.model = overrides["asr_model"]
    if overrides.get("device"):
        cfg.asr.device = overrides["device"]
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
@click.option("--model", default=None, help="临时覆盖总结模型")
@click.option("--base-url", default=None, help="临时覆盖 OpenAI 兼容接口地址")
@click.option("--asr-model", default=None, help="临时覆盖 whisper 模型，如 medium / large-v3")
@click.option("--device", type=click.Choice(["auto", "cuda", "cpu"]), default=None, help="ASR 设备")
@click.option("--output-dir", default=None, help="临时覆盖输出目录")
@click.option("--force", is_flag=True, help="忽略已有的转写和音频缓存，全部重跑")
@click.option("--force-asr", is_flag=True, help="即使有字幕也强制走语音识别")
@click.option("--no-summary", is_flag=True, help="只转写，不调用大模型")
@click.option("-y", "--yes", is_flag=True, help="跳过成本确认")
@_config_option
@_verbose_option
def run(url: str, summary_type, lang, extra, model, base_url, asr_model, device,
        output_dir, force, force_asr, no_summary, yes, config_path, verbose) -> None:
    """处理一个视频链接：URL -> 转写 -> 总结。"""
    _setup_logging(verbose)
    cfg = _load(config_path, {
        "model": model, "base_url": base_url, "asr_model": asr_model,
        "device": device, "output_dir": output_dir,
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
@click.option("--model", default=None, help="临时覆盖总结模型")
@click.option("--base-url", default=None, help="临时覆盖 OpenAI 兼容接口地址")
@click.option("-o", "--out", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help="总结输出路径（默认写到 transcript.json 同级的 summary.md）")
@click.option("-y", "--yes", is_flag=True, help="跳过成本确认")
@_config_option
@_verbose_option
def summarize(transcript_path: Path, summary_type, lang, extra, model, base_url,
              out, yes, config_path, verbose) -> None:
    """对已有的 transcript.json 重新总结，不重跑转写。"""
    _setup_logging(verbose)
    cfg = _load(config_path, {"model": model, "base_url": base_url})
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
@_config_option
def config(config_path) -> None:
    """打印当前生效的配置和密钥状态（不显示密钥内容）。"""
    cfg = _load(config_path, {})
    click.echo(f"配置文件      {cfg.source_path or '（未找到，使用默认值）'}")
    click.echo(f"输出目录      {cfg.output_dir}")
    click.echo("")
    click.echo(f"ASR           {cfg.asr.provider} / {cfg.asr.model} (device={cfg.asr.device})")
    click.echo(f"总结          {cfg.summarizer.provider} / {cfg.summarizer.model}")
    click.echo(f"接口地址      {cfg.summarizer.base_url or '(SDK 默认)'}")
    click.echo(f"上下文预算    {cfg.summarizer.max_context_tokens:,} tokens"
               f"（切块策略 {cfg.summarizer.chunk_strategy}）")
    has_key = bool(cfg.summarizer.api_key)
    click.echo(f"密钥          {cfg.summarizer.api_key_env} "
               f"{'已设置' if has_key else '未设置 —— 复制 .env.example 为 .env 并填入'}")


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
