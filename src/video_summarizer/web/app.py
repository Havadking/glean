"""Gradio 界面组装。

两个页面（Gradio 6 的 Blocks.route 是真多页，不是 tab 伪装）：
- 处理：贴链接 -> 转写 -> 总结
- 历史：所有处理过的视频，以视频名为标题的卡片列表

两个页面都要展示"转写 + 总结"，所以那部分做成 _Workspace，两边各建一份实例。
Gradio 的组件不能跨 route 复用，但构造逻辑可以。

界面只做编排：分段聚合在 reading.py，历史数据在 library.py，HTML 在 render.py，
真正的业务逻辑全在 pipeline / provider 里，这里一行都不重写。
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from ..cache import Cache, summary_key
from ..config import Config, load_config
from ..errors import VideoSummarizerError
from ..models import SummaryOptions, Transcript
from ..pipeline import plan_transcript_key, run as run_pipeline, write_summary_files
from ..summarizer import get_provider as get_summarizer
from ..summarizer.prompts import TEMPLATES
from ..summarizer.tokens import format_timestamp
from . import library, mindmap, reading, render

log = logging.getLogger(__name__)

# 每种总结类型配一句"什么时候用它"。原来只有四个光秃秃的中文标签，
# 用户得自己猜「关键信息提取」和「整体摘要」差在哪。
TYPE_HINTS = {
    "overall": "讲了什么、有什么值得注意",
    "timeline": "按话题分章节，方便跳着看",
    "key_points": "数字、结论、可执行的步骤",
    "by_speaker": "多人对话，谁说了什么",
    "mindmap": "一张图看结构，可折叠缩放",
}

# 思维导图组件：Gradio 更新组件时只替换 innerHTML，不会重新执行脚本，
# 所以挂一个 MutationObserver，内容一变就重新渲染。
_MINDMAP_JS_ON_LOAD = """
var render = function () { if (window.__vsRenderMindmaps) window.__vsRenderMindmaps(element); };
new MutationObserver(render).observe(element, { childList: true, subtree: true });
render();
"""
TYPE_CHOICES = [
    (f"{t.label} · {TYPE_HINTS.get(key, '')}".rstrip(" ·"), key)
    for key, t in TEMPLATES.items()
]


def _require_gradio():
    try:
        import gradio as gr
    except ImportError as exc:  # pragma: no cover - 环境问题
        raise VideoSummarizerError(
            "没装 gradio。跑 `uv sync --extra ui` 装上（想要 GPU 就 "
            "`uv sync --extra ui --extra cuda`）。"
        ) from exc
    return gr


# ---------- 后台任务的日志转发 ----------


class _QueueHandler(logging.Handler):
    def __init__(self, sink: queue.Queue) -> None:
        super().__init__()
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.sink.put_nowait(self.format(record))
        except queue.Full:  # pragma: no cover
            pass


def _run_streaming(work: Callable[[], Any]) -> Iterator[tuple[str, Any, Exception | None]]:
    """在后台线程跑 work，边跑边把累积的日志 yield 出来。

    产出 (日志文本, 结果, 异常)。中间若干次结果和异常都是 None，最后一次才有值。
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
        except Exception as exc:  # noqa: BLE001 - 任何失败都要送回界面
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


# ---------- 小工具 ----------


def _error_md(exc: Exception) -> str:
    if isinstance(exc, VideoSummarizerError):
        return f"**出错了**\n\n```\n{exc}\n```"
    return f"**出错了**\n\n```\n{type(exc).__name__}: {exc}\n```\n\n完整堆栈见运行日志。"


def _transcript_brief(t: Transcript) -> str:
    source = "官方字幕" if t.source_type == "subtitle" else "语音识别"
    if t.source_type == "asr" and t.meta.get("asr_model"):
        source += f" {t.meta['asr_model']}"
    bits = [format_timestamp(t.duration_sec), source, t.language or "未知语言",
            f"{len(t.segments)} 句"]
    if t.speakers:
        bits.append(f"{len(t.speakers)} 位说话人")
    return f"### {t.title or '（无标题）'}\n{' · '.join(bits)}"


def _summary_provenance(provider_desc: str, summary_type: str, created: str = "") -> str:
    label = TEMPLATES[summary_type].label if summary_type in TEMPLATES else summary_type
    bits = [provider_desc, label]
    if created:
        bits.append(created[:16].replace("T", " "))
    return " · ".join(bits)


@dataclass
class _Loaded:
    """当前工作区里装着的东西。"""

    transcript: Transcript | None = None
    path: Path | None = None
    cache_key: str | None = None
    paragraphs: list = field(default_factory=list)


def build_app(cfg: Config):
    gr = _require_gradio()

    with gr.Blocks(title="视频转写总结", fill_width=True) as app:
        gr.Navbar(main_page_name="处理")
        _build_process_page(gr, cfg)

    # Gradio 6 的 route 是真多页，各自是独立的 Blocks —— 组件不能跨页共用，
    # 所以历史页要自己建一份工作区，页面加载时也要自己拉一次数据。
    with app.route("历史", "/library") as library_page:
        _build_library_page(gr, cfg, library_page)

    return app


# ---------- 转写 + 总结 的共用视图 ----------


def _build_workspace(gr, cfg: Config, loaded_state):
    """建一份"转写 + 总结"视图，返回组件和一个装载函数。

    两个页面各建一份 —— Gradio 组件不能跨 route 共用，但这段构造逻辑可以。
    """
    with gr.Tabs():
        with gr.Tab("转写"):
            brief = gr.Markdown("还没有转写。")
            with gr.Row():
                # 不给足宽度的话 Gradio 会把两个选项挤成竖排，看着像列表不像开关
                view_mode = gr.Radio(
                    ["阅读", "精确"], value="阅读", label=None, container=False,
                    scale=0, min_width=190,
                )
                search = gr.Textbox(
                    placeholder="搜索转写内容", label=None, container=False,
                    lines=1, max_lines=1, scale=3,
                )
            reading_html = gr.HTML(render.paragraphs_html([]))
            precise_table = gr.Dataframe(
                headers=["开始", "结束", "说话人", "文本"],
                datatype=["str", "str", "str", "str"],
                column_count=(4, "fixed"),
                column_widths=["10%", "10%", "14%", "66%"],
                wrap=True, interactive=False, max_height=620,
                label=None, visible=False,
            )
            transcript_file = gr.File(label="transcript.json", visible=False)

        with gr.Tab("总结"):
            summary_type = gr.Radio(
                choices=TYPE_CHOICES, value=cfg.summarizer.summary_type,
                label="想要哪种总结",
            )
            with gr.Row():
                lang = gr.Textbox(value="zh", label="输出语言", scale=1)
                extra = gr.Textbox(
                    label="附加要求（可选）",
                    placeholder="例如：重点关注技术细节，忽略寒暄", scale=4,
                )
            with gr.Row():
                status = gr.Markdown("先获取转写。")
                generate_btn = gr.Button("生成总结", variant="primary", scale=0,
                                         interactive=False)
            mindmap_html = gr.HTML(
                mindmap.widget_html(None), head=mindmap.head_html(),
                js_on_load=_MINDMAP_JS_ON_LOAD, visible=False, padding=False,
            )
            summary_md = gr.Markdown()
            provenance = gr.Markdown()
            with gr.Row():
                summary_file = gr.File(label="summary.md", visible=False)
                mindmap_file = gr.File(label="mindmap.html（双击可开）", visible=False)

    # ---------- 视图切换与搜索 ----------

    def on_view_change(mode, loaded: _Loaded, query):
        show_reading = mode == "阅读"
        rows = render.transcript_rows(loaded.transcript.segments) if loaded.transcript else []
        return (
            gr.update(visible=show_reading,
                      value=render.paragraphs_html(loaded.paragraphs, query)),
            gr.update(visible=not show_reading, value=rows),
        )

    view_mode.change(on_view_change, [view_mode, loaded_state, search],
                     [reading_html, precise_table])
    search.change(on_view_change, [view_mode, loaded_state, search],
                  [reading_html, precise_table])

    # ---------- 选了总结类型：先查缓存 ----------

    def show_summary(text: str, chosen: str, loaded: _Loaded, provider_desc: str):
        """把一份总结正文摆到界面上。思维导图类型渲染成图，其余渲染 Markdown。

        返回 (summary_md, provenance, summary_file, mindmap_html, mindmap_file)。
        """
        work_dir = loaded.path.parent if loaded.path else None
        summary_path = work_dir / "summary.md" if work_dir else None
        summary_file = gr.update(value=str(summary_path),
                                 visible=bool(summary_path and summary_path.is_file()))
        provenance = _summary_provenance(provider_desc, chosen)

        if chosen != "mindmap":
            return (text, provenance, summary_file,
                    gr.update(visible=False), gr.update(visible=False))

        title = loaded.transcript.title if loaded.transcript else ""
        tree = mindmap.parse_outline(text, fallback_title=title or "思维导图")
        html_path = work_dir / "mindmap.html" if work_dir else None
        return (
            # 导图下面附上大纲源文件，折叠着，想改的人能看到原文
            f"<details><summary>大纲源文件（{tree.size} 个节点）</summary>\n\n"
            f"```markdown\n{mindmap.strip_fence(text)}\n```\n\n</details>",
            provenance,
            summary_file,
            gr.update(value=mindmap.widget_html(tree, title), visible=True),
            gr.update(value=str(html_path),
                      visible=bool(html_path and html_path.is_file())),
        )

    def _hidden_summary():
        return ("", "", gr.update(visible=False), gr.update(visible=False),
                gr.update(visible=False))

    def on_type_change(chosen, language, extra_text, loaded: _Loaded):
        if loaded.transcript is None:
            return ("先获取转写。", gr.update(interactive=False), *_hidden_summary())

        run_cfg = load_config(cfg.source_path)
        options = SummaryOptions(summary_type=chosen, language=(language or "zh").strip(),
                                 extra_instructions=(extra_text or "").strip() or None)
        try:
            provider = get_summarizer(run_cfg.summarizer)
        except VideoSummarizerError as exc:
            return (f"无法使用总结模型：{exc}", gr.update(interactive=False),
                    *_hidden_summary())

        cached = None
        if loaded.cache_key:
            key = summary_key(
                transcript_key=loaded.cache_key, provider_desc=provider.describe(),
                summary_type=chosen, language=options.language,
                extra=options.extra_instructions,
            )
            cached = Cache(run_cfg.cache_db).get_summary(key)

        if cached is not None:
            return (
                "**已有缓存，不花钱** — 换个类型或改附加要求会重新生成。",
                gr.update(interactive=True, value="重新生成"),
                *show_summary(cached, chosen, loaded, provider.describe()),
            )

        try:
            estimate = provider.plan(loaded.transcript, options)
        except VideoSummarizerError as exc:
            return (f"无法估算：{exc}", gr.update(interactive=False), *_hidden_summary())

        strategy = (f"切成 {estimate.chunks} 块 + 1 次汇总，共 {estimate.chunks + 1} 次请求"
                    if estimate.is_chunked else "1 次请求")
        return (
            f"约 {estimate.estimated_input_tokens:,} tokens · {strategy} · "
            f"`{provider.describe()}`",
            gr.update(interactive=True, value="生成总结"),
            *_hidden_summary(),
        )

    type_inputs = [summary_type, lang, extra, loaded_state]
    type_outputs = [status, generate_btn, summary_md, provenance, summary_file,
                    mindmap_html, mindmap_file]
    summary_type.change(on_type_change, type_inputs, type_outputs)
    lang.change(on_type_change, type_inputs, type_outputs)
    extra.change(on_type_change, type_inputs, type_outputs)

    handles = {
        "brief": brief, "view_mode": view_mode, "search": search,
        "reading_html": reading_html, "precise_table": precise_table,
        "transcript_file": transcript_file, "summary_type": summary_type,
        "lang": lang, "extra": extra, "status": status,
        "generate_btn": generate_btn, "summary_md": summary_md,
        "provenance": provenance, "summary_file": summary_file,
        "mindmap_html": mindmap_html, "mindmap_file": mindmap_file,
    }
    handles["on_type_change"] = on_type_change
    handles["show_summary"] = show_summary
    handles["hidden_summary"] = _hidden_summary
    # 总结相关的输出组件，按 on_type_change / on_generate 的返回顺序
    handles["summary_outputs"] = [status, generate_btn, summary_md, provenance,
                                  summary_file, mindmap_html, mindmap_file]
    return handles


def _load_updates(gr, transcript: Transcript, path: Path | None, cache_key: str | None):
    """把一份转写装进工作区，返回给 Gradio 的更新元组。"""
    paragraphs = reading.to_paragraphs(transcript.segments)
    loaded = _Loaded(transcript=transcript, path=path, cache_key=cache_key,
                     paragraphs=paragraphs)
    return loaded, (
        _transcript_brief(transcript),
        gr.update(value=render.paragraphs_html(paragraphs), visible=True),
        gr.update(value=render.transcript_rows(transcript.segments), visible=False),
        gr.update(value=str(path), visible=path is not None),
        "阅读",
    )


def _wire_generate(gr, cfg: Config, h: dict, loaded_state, log_box):
    """接上"生成总结"按钮。命中缓存的情况在 on_type_change 里已经处理掉了。"""

    hidden = h["hidden_summary"]
    n_hidden = len(hidden())

    def on_generate(loaded: _Loaded, chosen, language, extra_text):
        if loaded.transcript is None:
            yield ("", "先获取转写。", gr.update(), *hidden())
            return

        run_cfg = load_config(cfg.source_path)
        options = SummaryOptions(summary_type=chosen, language=(language or "zh").strip(),
                                 extra_instructions=(extra_text or "").strip() or None)
        provider = get_summarizer(run_cfg.summarizer)
        cache = Cache(run_cfg.cache_db)
        key = summary_key(
            transcript_key=loaded.cache_key or "", provider_desc=provider.describe(),
            summary_type=chosen, language=options.language,
            extra=options.extra_instructions,
        ) if loaded.cache_key else None

        def work():
            text = provider.summarize(loaded.transcript, options)
            if key:
                cache.put_summary(key, transcript_key=loaded.cache_key,
                                  transcript=loaded.transcript,
                                  provider_desc=provider.describe(),
                                  summary_type=chosen, language=options.language,
                                  content=text)
            if loaded.path is not None:
                write_summary_files(text, loaded.transcript, provider.describe(),
                                    options, loaded.path.parent)
            return text

        done = None
        for logs, value, error in _run_streaming(work):
            if error is not None:
                yield (logs, _error_md(error), gr.update(), *hidden())
                return
            if value is None:
                yield (logs, gr.update(), gr.update(), *([gr.update()] * n_hidden))
                continue
            done = (logs, value)

        logs, text = done
        yield (
            logs,
            "**已生成** — 结果已存进缓存，下次同样的设置不再花钱。",
            gr.update(value="重新生成"),
            *h["show_summary"](text, chosen, loaded, provider.describe()),
        )

    h["generate_btn"].click(
        on_generate,
        [loaded_state, h["summary_type"], h["lang"], h["extra"]],
        [log_box, *h["summary_outputs"]],
    )


# ---------- 页面一：处理 ----------


def _build_process_page(gr, cfg: Config) -> None:
    loaded_state = gr.State(_Loaded())

    gr.Markdown(
        "贴一个视频链接（YouTube / Bilibili / 其他 yt-dlp 支持的站点）。"
        "有人工字幕就直接用，没有才下载音频跑语音识别。"
    )
    with gr.Row():
        url_box = gr.Textbox(
            label=None, container=False, lines=1, max_lines=1, scale=5,
            placeholder="https://www.bilibili.com/video/BV... 或 https://www.youtube.com/watch?v=...",
        )
        fetch_btn = gr.Button("获取转写", variant="primary", scale=0)

    with gr.Accordion("高级选项", open=False):
        with gr.Row():
            asr_model_box = gr.Textbox(label="ASR 模型", value=cfg.asr.model,
                                       info="funasr: sensevoice-small；whisper: large-v3")
            device_box = gr.Radio(["auto", "cuda", "cpu"], value=cfg.asr.device,
                                  label="ASR 设备")
            diarize_box = gr.Radio(["auto", "true", "false"],
                                   value=str(cfg.asr.diarize).lower(),
                                   label="说话人分离",
                                   info="auto = 选了分说话人摘要才开")
        with gr.Row():
            force_asr_box = gr.Checkbox(label="有字幕也强制走语音识别", value=False)
            force_box = gr.Checkbox(label="忽略缓存全部重跑", value=False)

    log_box = gr.Textbox(label="运行日志", lines=6, max_lines=6,
                         interactive=False, autoscroll=True)

    h = _build_workspace(gr, cfg, loaded_state)
    _wire_generate(gr, cfg, h, loaded_state, log_box)

    def on_fetch(url, asr_model, device, diarize, force_asr, force, chosen, language, extra_text):
        blank = (gr.update(), gr.update(), gr.update(), gr.update(), gr.update())
        url = (url or "").strip()
        if not url:
            yield ("", "请先填一个视频链接。", *blank[1:], _Loaded(),
                   "先获取转写。", gr.update(interactive=False))
            return

        run_cfg = load_config(cfg.source_path)
        run_cfg.output_dir = cfg.output_dir
        run_cfg.asr.model = (asr_model or "").strip() or run_cfg.asr.model
        run_cfg.asr.device = device
        run_cfg.asr.diarize = diarize

        options = SummaryOptions(summary_type=chosen)
        work = lambda: run_pipeline(  # noqa: E731
            url, run_cfg, options=options, force=bool(force),
            force_asr=bool(force_asr), skip_summary=True,
        )

        result = None
        for logs, value, error in _run_streaming(work):
            if error is not None:
                yield (logs, _error_md(error), *blank[1:], _Loaded(),
                       "转写失败。", gr.update(interactive=False))
                return
            if value is None:
                yield (logs, *blank, gr.update(), gr.update(), gr.update())
                continue
            result = (logs, value)

        logs, pipeline_result = result
        cache_key = plan_transcript_key(
            pipeline_result.info, run_cfg,
            force_asr=bool(force_asr),
            diarize=run_cfg.asr.wants_diarization(needed=chosen == "by_speaker"),
        )
        loaded, updates = _load_updates(gr, pipeline_result.transcript,
                                        pipeline_result.transcript_path, cache_key)
        status, btn, *_ = h["on_type_change"](chosen, language, extra_text, loaded)
        yield (logs, *updates, loaded, status, btn)

    fetch_inputs = [url_box, asr_model_box, device_box, diarize_box,
                    force_asr_box, force_box, h["summary_type"], h["lang"], h["extra"]]
    fetch_outputs = [log_box, h["brief"], h["reading_html"], h["precise_table"],
                     h["transcript_file"], h["view_mode"], loaded_state,
                     h["status"], h["generate_btn"]]
    fetch_btn.click(on_fetch, fetch_inputs, fetch_outputs)
    url_box.submit(on_fetch, fetch_inputs, fetch_outputs)


# ---------- 页面二：历史 ----------


def _build_library_page(gr, cfg: Config, page) -> None:
    loaded_state = gr.State(_Loaded())
    entries_state = gr.State([])

    gr.Navbar(main_page_name="处理")
    gr.Markdown("处理过的视频都在这儿。点「打开」把转写和总结装进下面的视图。")
    with gr.Row():
        search_box = gr.Textbox(label=None, container=False, lines=1, max_lines=1,
                                placeholder="按标题搜索", scale=4)
        sort_box = gr.Dropdown(["最近处理", "时长最长", "标题"], value="最近处理",
                               label=None, container=False, scale=1)
        refresh_btn = gr.Button("刷新", scale=0)


    # 卡片列表必须排在工作区前面 —— 列表是这个页面的主角。
    # gr.render 按声明位置渲染，但函数体要到触发时才跑，所以这里先声明、
    # 后面再把工作区的组件填进 h；等真正渲染时它已经建好了。
    h: dict = {}

    def load_entries():
        return library.load_library(cfg)

    def filtered(entries, query, sort):
        q = (query or "").strip().lower()
        items = [e for e in entries if not q or q in (e.title or "").lower()]
        if sort == "时长最长":
            items.sort(key=lambda e: e.duration_sec, reverse=True)
        elif sort == "标题":
            items.sort(key=lambda e: (e.title or "").lower())
        return items

    @gr.render(inputs=[entries_state, search_box, sort_box],
               triggers=[entries_state.change, search_box.change, sort_box.change])
    def render_cards(entries, query, sort):
        items = filtered(entries, query, sort)
        # 计数要跟着筛选走 —— 放在外面的话搜索之后还显示总数，会误导
        total = format_timestamp(sum(e.duration_sec for e in items))
        summaries = sum(len(e.summaries) for e in items)
        suffix = f"（共 {len(entries)} 个）" if len(items) != len(entries) else ""
        gr.Markdown(f"{len(items)} 个视频{suffix} · 共 {total} · {summaries} 份总结")

        if not items:
            gr.Markdown("_没有匹配的记录。换个关键词，或者去「处理」页跑一个新视频。_")
            return
        for entry in items:
            with gr.Row(equal_height=True):
                gr.HTML(render.card_html(entry), padding=False)
                open_btn = gr.Button("打开", scale=0, min_width=72)

            def _open(_entry=entry):
                if _entry.transcript_path is None or not _entry.transcript_path.is_file():
                    return (_Loaded(), "这条记录只在缓存里，产物文件已经不在了。",
                            gr.update(), gr.update(), gr.update(), gr.update())
                transcript = Transcript.load(_entry.transcript_path)
                cache_key = transcript.meta.get("cache_key")
                loaded, updates = _load_updates(gr, transcript,
                                                _entry.transcript_path, cache_key)
                return (loaded, *updates)

            open_btn.click(
                _open, None,
                [loaded_state, h["brief"], h["reading_html"], h["precise_table"],
                 h["transcript_file"], h["view_mode"]],
            ).then(
                h["on_type_change"],
                [h["summary_type"], h["lang"], h["extra"], loaded_state],
                h["summary_outputs"],
            )

    log_box = gr.Textbox(label="运行日志", lines=4, max_lines=4,
                         interactive=False, autoscroll=True)
    h.update(_build_workspace(gr, cfg, loaded_state))
    _wire_generate(gr, cfg, h, loaded_state, log_box)

    refresh_btn.click(load_entries, None, entries_state)
    page.load(load_entries, None, entries_state)


def launch(cfg: Config, host: str = "127.0.0.1", port: int = 7860,
           share: bool = False) -> None:
    gr = _require_gradio()

    # Gradio 提供下载时会把文件复制一份到临时目录，默认落在 C 盘；跟着输出目录走
    os.environ.setdefault("GRADIO_TEMP_DIR", str(cfg.output_dir / ".gradio_tmp"))

    build_app(cfg).queue().launch(
        server_name=host, server_port=port, share=share, inbrowser=True,
        theme=gr.themes.Soft(),
    )
