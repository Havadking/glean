"""Gradio 界面（DESIGN.md 路线图 v0.5）。

流程刻意做成两步：先出转写和成本估算，再由用户点一下才真正调大模型。
和 CLI 的确认提示是同一个道理 —— 长视频的总结按量计费，不该点一下就悄悄花钱。

界面只做编排，所有实际逻辑都复用 pipeline / provider，不在这里重写一遍。
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import traceback
from pathlib import Path
from typing import Any, Callable, Iterator

from .config import Config, load_config
from .errors import VideoSummarizerError
from .models import SummaryOptions, Transcript
from .pipeline import render_summary_markdown, run as run_pipeline
from .summarizer import get_provider as get_summarizer
from .summarizer.base import CostEstimate
from .summarizer.prompts import TEMPLATES
from .summarizer.tokens import format_timestamp

log = logging.getLogger(__name__)

SUMMARY_CHOICES = [(t.label, key) for key, t in TEMPLATES.items()]


def _require_gradio():
    try:
        import gradio as gr
    except ImportError as exc:  # pragma: no cover - 环境问题
        raise VideoSummarizerError(
            "没装 gradio。跑 `uv sync --extra ui` 装上（想要 GPU 就 "
            "`uv sync --extra ui --extra cuda`）。"
        ) from exc
    return gr


# ---------- 日志转发：把后台线程的日志实时喂给界面 ----------


class _QueueHandler(logging.Handler):
    def __init__(self, sink: queue.Queue) -> None:
        super().__init__()
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.sink.put_nowait(self.format(record))
        except queue.Full:  # pragma: no cover - 队列没设上限
            pass


def _run_streaming(work: Callable[[], Any]) -> Iterator[tuple[str, Any, Exception | None]]:
    """在后台线程跑 work，边跑边把累积的日志 yield 出来。

    产出 (日志文本, 结果, 异常)。前面若干次结果和异常都是 None，最后一次才有值。
    """
    sink: queue.Queue = queue.Queue()
    handler = _QueueHandler(sink)
    handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))

    root = logging.getLogger("video_summarizer")
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            box["result"] = work()
        except Exception as exc:  # noqa: BLE001 - 要把任何失败都送回界面
            box["error"] = exc
            if not isinstance(exc, VideoSummarizerError):
                log.error("未预期的错误:\n%s", traceback.format_exc())

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()

    lines: list[str] = []
    try:
        while thread.is_alive() or not sink.empty():
            try:
                lines.append(sink.get(timeout=0.3))
            except queue.Empty:
                pass
            yield "\n".join(lines), None, None
        thread.join()
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)

    yield "\n".join(lines), box.get("result"), box.get("error")


# ---------- 数据 -> 界面 ----------


def _transcript_rows(transcript: Transcript) -> list[list[str]]:
    return [
        [format_timestamp(s.start), s.speaker or "-", s.text]
        for s in transcript.segments
    ]


def _transcript_brief(transcript: Transcript, path: Path) -> str:
    source = "官方字幕" if transcript.source_type == "subtitle" else "语音识别"
    if transcript.source_type == "asr":
        source += f"（{transcript.meta.get('asr_model', '?')} / {transcript.meta.get('device', '?')}）"
    speakers = transcript.speakers
    lines = [
        f"### {transcript.title or '（无标题）'}",
        "",
        f"- 时长：{format_timestamp(transcript.duration_sec)}　分句：{len(transcript.segments)} 条",
        f"- 转写方式：{source}　语言：{transcript.language or '未知'}",
        f"- 存放位置：`{path}`",
    ]
    if speakers:
        lines.append(f"- 说话人：{'、'.join(speakers)}")
    return "\n".join(lines)


def _estimate_brief(estimate: CostEstimate, provider_desc: str) -> str:
    if estimate.is_chunked:
        strategy = (
            f"map-reduce，切成 {estimate.chunks} 块 + 1 次汇总，"
            f"共 **{estimate.chunks + 1} 次**请求"
        )
    else:
        strategy = "整篇一次性送入，**1 次**请求"
    return "\n".join([
        f"- 转写长度：约 **{estimate.transcript_tokens:,}** tokens",
        f"- 处理策略：{strategy}",
        f"- 预计输入：约 **{estimate.estimated_input_tokens:,}** tokens（估算值，按量计费）",
        f"- 使用模型：`{provider_desc}`",
    ])


def _error_text(exc: Exception) -> str:
    if isinstance(exc, VideoSummarizerError):
        return f"### 出错了\n\n```\n{exc}\n```"
    return f"### 出错了\n\n```\n{type(exc).__name__}: {exc}\n```\n\n完整堆栈见运行日志。"


def _list_history(cfg: Config) -> list[str]:
    """扫描输出目录里已有的转写，按修改时间倒序。"""
    if not cfg.output_dir.is_dir():
        return []
    found = list(cfg.output_dir.glob("*/transcript.json"))
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [str(p) for p in found]


# ---------- 界面 ----------


def build_app(cfg: Config):
    gr = _require_gradio()

    with gr.Blocks(title="视频转写总结") as app:
        transcript_state = gr.State(None)
        path_state = gr.State(None)

        gr.Markdown(
            "# 视频转写总结\n"
            "贴一个视频链接（YouTube / Bilibili / 其他 yt-dlp 支持的站点）。"
            "有人工字幕就直接用，没有才下载音频跑语音识别。"
        )

        with gr.Row():
            url_box = gr.Textbox(
                label="视频链接",
                placeholder="https://www.bilibili.com/video/BV... 或 https://www.youtube.com/watch?v=...",
                lines=1,
                max_lines=1,
                scale=5,
            )
            fetch_btn = gr.Button("① 获取转写", variant="primary", scale=1)

        with gr.Accordion("高级选项", open=False):
            with gr.Row():
                asr_model_box = gr.Textbox(
                    label="ASR 模型", value=cfg.asr.model,
                    info="funasr: sensevoice-small / paraformer-zh；whisper: tiny / small / medium / large-v3",
                )
                device_box = gr.Radio(
                    ["auto", "cuda", "cpu"], value=cfg.asr.device, label="ASR 设备",
                )
            with gr.Row():
                force_asr_box = gr.Checkbox(label="有字幕也强制走语音识别", value=False)
                force_box = gr.Checkbox(label="忽略缓存全部重跑", value=False)

        log_box = gr.Textbox(
            label="运行日志", lines=8, max_lines=8, interactive=False, autoscroll=True,
        )

        with gr.Tabs():
            with gr.Tab("转写"):
                transcript_info = gr.Markdown("还没有转写。上面填个链接，点「获取转写」。")
                transcript_table = gr.Dataframe(
                    headers=["时间", "说话人", "文本"],
                    datatype=["str", "str", "str"],
                    column_count=(3, "fixed"),
                    column_widths=["12%", "14%", "74%"],
                    wrap=True,
                    interactive=False,  # 只读，别把它当成可编辑表格 / CSV 导入口
                    max_height=520,
                    label=None,
                )
                transcript_file = gr.File(label="transcript.json", visible=False)

            with gr.Tab("总结"):
                with gr.Row():
                    summary_type_box = gr.Radio(
                        choices=SUMMARY_CHOICES,
                        value=cfg.summarizer.summary_type,
                        label="总结类型",
                    )
                    lang_box = gr.Textbox(label="输出语言", value="zh", scale=0)
                extra_box = gr.Textbox(
                    label="附加要求（可选）", placeholder="例如：重点关注技术细节，忽略寒暄",
                )
                estimate_md = gr.Markdown("先获取转写，这里会显示预计消耗。")
                summarize_btn = gr.Button("② 生成总结", variant="primary", interactive=False)
                summary_md = gr.Markdown()
                summary_file = gr.File(label="summary.md", visible=False)

            with gr.Tab("历史"):
                gr.Markdown(f"扫描 `{cfg.output_dir}` 下已有的转写，可以直接换个角度重新总结。")
                history_box = gr.Dropdown(
                    choices=_list_history(cfg), label="已有转写", value=None
                )
                with gr.Row():
                    refresh_btn = gr.Button("刷新列表")
                    load_btn = gr.Button("载入", variant="primary")

        # ---------- 回调 ----------

        def on_fetch(url, asr_model, device, force_asr, force, summary_type, progress=gr.Progress()):
            url = (url or "").strip()
            if not url:
                yield ("", "请先填一个视频链接。", [], gr.update(visible=False),
                       "先获取转写，这里会显示预计消耗。", gr.update(interactive=False), None, None)
                return

            run_cfg = load_config(cfg.source_path)
            run_cfg.output_dir = cfg.output_dir
            run_cfg.asr.model = (asr_model or "").strip() or run_cfg.asr.model
            run_cfg.asr.device = device

            options = SummaryOptions(summary_type=summary_type)
            work = lambda: run_pipeline(  # noqa: E731 - 只是给线程包一层
                url, run_cfg,
                options=options,
                force=bool(force),
                force_asr=bool(force_asr),
                skip_summary=True,
            )

            result = None
            for logs, value, error in _run_streaming(work):
                if error is not None:
                    yield (logs, _error_text(error), [], gr.update(visible=False),
                           "转写失败，没有可估算的内容。", gr.update(interactive=False), None, None)
                    return
                if value is None:
                    yield (logs, gr.update(), gr.update(), gr.update(),
                           gr.update(), gr.update(), gr.update(), gr.update())
                    continue
                result = (logs, value)

            logs, pipeline_result = result
            transcript = pipeline_result.transcript
            path = pipeline_result.transcript_path

            try:
                provider = get_summarizer(run_cfg.summarizer)
                estimate = provider.plan(transcript, options)
                estimate_text = _estimate_brief(estimate, provider.describe())
                can_summarize = True
            except VideoSummarizerError as exc:
                estimate_text = f"无法估算：{exc}"
                can_summarize = False

            yield (
                logs,
                _transcript_brief(transcript, path),
                _transcript_rows(transcript),
                gr.update(value=str(path), visible=True),
                estimate_text,
                gr.update(interactive=can_summarize),
                transcript,
                str(path),
            )

        def on_summarize(transcript, path, summary_type, lang, extra):
            if transcript is None:
                yield "", "还没有转写，先做第一步。", gr.update(visible=False)
                return

            run_cfg = load_config(cfg.source_path)
            options = SummaryOptions(
                summary_type=summary_type,
                language=(lang or "zh").strip(),
                extra_instructions=(extra or "").strip() or None,
            )
            provider = get_summarizer(run_cfg.summarizer)

            def work():
                text = provider.summarize(transcript, options)
                target = Path(path).parent / "summary.md"
                target.write_text(
                    render_summary_markdown(text, transcript, provider.describe(), options),
                    encoding="utf-8",
                )
                return text, target

            done = None
            for logs, value, error in _run_streaming(work):
                if error is not None:
                    yield logs, _error_text(error), gr.update(visible=False)
                    return
                if value is None:
                    yield logs, gr.update(), gr.update()
                    continue
                done = (logs, value)

            logs, (text, target) = done
            yield logs, text, gr.update(value=str(target), visible=True)

        def on_load_history(path, summary_type):
            if not path:
                return (gr.update(), gr.update(), gr.update(), gr.update(),
                        gr.update(interactive=False), None, None)
            transcript = Transcript.load(Path(path))
            try:
                provider = get_summarizer(cfg.summarizer)
                estimate = _estimate_brief(
                    provider.plan(transcript, SummaryOptions(summary_type=summary_type)),
                    provider.describe(),
                )
                can_summarize = True
            except VideoSummarizerError as exc:
                estimate, can_summarize = f"无法估算：{exc}", False
            return (
                _transcript_brief(transcript, Path(path)),
                _transcript_rows(transcript),
                gr.update(value=path, visible=True),
                estimate,
                gr.update(interactive=can_summarize),
                transcript,
                path,
            )

        fetch_btn.click(
            on_fetch,
            inputs=[url_box, asr_model_box, device_box, force_asr_box, force_box, summary_type_box],
            outputs=[log_box, transcript_info, transcript_table, transcript_file,
                     estimate_md, summarize_btn, transcript_state, path_state],
        )
        url_box.submit(
            on_fetch,
            inputs=[url_box, asr_model_box, device_box, force_asr_box, force_box, summary_type_box],
            outputs=[log_box, transcript_info, transcript_table, transcript_file,
                     estimate_md, summarize_btn, transcript_state, path_state],
        )
        summarize_btn.click(
            on_summarize,
            inputs=[transcript_state, path_state, summary_type_box, lang_box, extra_box],
            outputs=[log_box, summary_md, summary_file],
        )
        refresh_btn.click(
            lambda: gr.update(choices=_list_history(cfg)), outputs=[history_box]
        )
        load_btn.click(
            on_load_history,
            inputs=[history_box, summary_type_box],
            outputs=[transcript_info, transcript_table, transcript_file, estimate_md,
                     summarize_btn, transcript_state, path_state],
        )

    return app


def launch(cfg: Config, host: str = "127.0.0.1", port: int = 7860, share: bool = False) -> None:
    gr = _require_gradio()

    # Gradio 提供下载时会把文件复制一份到临时目录，默认落在 C 盘；跟着输出目录走
    os.environ.setdefault("GRADIO_TEMP_DIR", str(cfg.output_dir / ".gradio_tmp"))
    build_app(cfg).queue().launch(
        server_name=host,
        server_port=port,
        share=share,
        inbrowser=True,
        theme=gr.themes.Soft(),
    )
