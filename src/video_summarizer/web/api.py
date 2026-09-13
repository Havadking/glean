"""拾光笺的 HTTP 接口：FastAPI + SSE。前端是 web/dist 里的静态文件。

设计原则（DESIGN.md 第 9 节）：
- 任何要花钱的动作，调用前先能拿到估算（/probe、/estimate）
- 能靠缓存解决的不问用户：已有的总结直接返回，不重新生成
- 长活（转写、总结）都进任务队列，客户端订阅 /jobs/{id}/events
"""

# 注意：这个文件不能用 `from __future__ import annotations` —— 请求体模型定义在
# create_app 里面，字符串注解会让 FastAPI 找不到类，把 body 当成 query 参数。

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .. import __version__
from ..cache import Cache, summary_key
from ..config import Config, load_config
from ..errors import VideoSummarizerError
from ..models import SummaryOptions, Transcript
from ..pipeline import plan_transcript_key, run as run_pipeline, write_summary_files
from ..subtitle.fetcher import select_subtitle_language
from ..summarizer import get_provider as get_summarizer
from ..summarizer.base import CostEstimate
from ..summarizer.prompts import TEMPLATES
from ..ytdlp_base import VideoInfo, probe
from . import library as library_mod
from . import mindmap as mindmap_mod
from .jobs import JobManager, Reporter
from .reading import to_paragraphs

log = logging.getLogger(__name__)

APP_NAME = "拾光笺"
DIST_DIR = Path(__file__).parent / "dist"

TYPE_HINTS = {
    "overall": "讲了什么、有什么值得注意",
    "timeline": "按话题分章节，方便跳着看",
    "key_points": "数字、结论、可执行的步骤",
    "by_speaker": "多人对话，谁说了什么",
    "mindmap": "一张图看结构，可折叠缩放",
}

ASR_CHOICES = [
    {"value": "sensevoice-small", "label": "SenseVoice-Small", "note": "快，中英日韩"},
    {"value": "paraformer-zh", "label": "Paraformer-zh", "note": "分说话人，仅中文"},
    {"value": "whisper", "label": "Whisper large-v3", "note": "兜底，多语种"},
]

# 没有转写之前估 token 用：中文口语约 3 token/秒
TOKENS_PER_SEC = 3.2
# 每次请求的输出大致这么多，估钱用
OUTPUT_TOKENS_PER_CALL = 1200


def _require_fastapi():
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError as exc:
        raise VideoSummarizerError(
            "界面需要 fastapi 和 uvicorn，请先执行 `uv sync --extra ui`"
        ) from exc


# ---------- 服务状态 ----------


class State:
    """一个进程一份：配置、任务队列、探测结果缓存。"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.jobs = JobManager()
        # url -> VideoInfo。探测过的直接复用给任务，少发一次请求（B 站 412 的主要来源）
        self._probed: dict[str, VideoInfo] = {}

    def fresh_config(self) -> Config:
        """每次任务重新读 config.yaml，改了配置不用重启。output_dir 跟命令行给的。"""
        cfg = load_config(self.cfg.source_path)
        cfg.output_dir = self.cfg.output_dir
        return cfg

    def remember_probe(self, url: str, info: VideoInfo) -> None:
        if len(self._probed) > 50:
            self._probed.pop(next(iter(self._probed)))
        self._probed[url] = info

    def probed(self, url: str) -> VideoInfo | None:
        return self._probed.get(url)


# ---------- 序列化 ----------


def _fmt_date(yyyymmdd: str | None) -> str | None:
    if not yyyymmdd or len(yyyymmdd) != 8:
        return yyyymmdd
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"


def _entry_dict(e: library_mod.LibraryEntry) -> dict[str, Any]:
    return {
        "video_id": e.video_id,
        "title": e.title,
        "source_url": e.source_url,
        "extractor": e.meta.get("extractor"),
        "uploader": e.uploader,
        "upload_date": _fmt_date(e.upload_date),
        "thumbnail": e.thumbnail,
        "duration_sec": e.duration_sec,
        "source_type": e.source_type,
        "source_label": e.source_label,
        "asr_model": e.asr_model,
        "diarized": bool(e.meta.get("diarization")),
        "language": e.language,
        "segment_count": e.segment_count,
        "speaker_count": e.speaker_count,
        "created_at": e.created_at,
        "work_dir": str(e.work_dir) if e.work_dir else None,
        "summaries": [
            {"type": s.summary_type, "label": s.label, "provider": s.provider,
             "created_at": s.created_at}
            for s in e.summaries
        ],
    }


def _money(cfg: Config, input_tokens: int, output_tokens: int) -> float | None:
    p_in, p_out = cfg.summarizer.price_input_per_m, cfg.summarizer.price_output_per_m
    if p_in is None and p_out is None:
        return None
    return round(input_tokens * (p_in or 0) / 1e6 + output_tokens * (p_out or 0) / 1e6, 4)


def _estimate_dict(cfg: Config, est: CostEstimate) -> dict[str, Any]:
    calls = est.chunks + 1 if est.is_chunked else 1
    out_tokens = OUTPUT_TOKENS_PER_CALL * calls
    return {
        "transcript_tokens": est.transcript_tokens,
        "input_tokens": est.estimated_input_tokens,
        "output_tokens": out_tokens,
        "calls": calls,
        "strategy": est.strategy,
        "cost": _money(cfg, est.estimated_input_tokens, out_tokens),
        "currency": cfg.summarizer.currency,
    }


def _load_transcript(cfg: Config, entry: library_mod.LibraryEntry) -> Transcript | None:
    cache = Cache(cfg.cache_db)
    if entry.cache_key:
        t = cache.get_transcript(entry.cache_key)
        if t is not None:
            return t
    if entry.transcript_path and entry.transcript_path.is_file():
        try:
            return Transcript.load(entry.transcript_path)
        except (OSError, ValueError, TypeError) as exc:
            log.warning("读不了 %s：%s", entry.transcript_path, exc)
    return None


def _find_entry(cfg: Config, video_id: str) -> library_mod.LibraryEntry | None:
    for e in library_mod.load_library(cfg):
        if e.video_id == video_id:
            return e
    return None


def _summaries_dict(cfg: Config, entry: library_mod.LibraryEntry) -> dict[str, dict[str, Any]]:
    """每种类型最新的一份总结，带正文。缓存优先，其次目录里的 summary.md。"""
    out: dict[str, dict[str, Any]] = {}
    cache = Cache(cfg.cache_db)
    for s in cache.summaries_for_video(entry.video_id):
        if s.summary_type in out or not s.content:
            continue
        out[s.summary_type] = {
            "type": s.summary_type, "label": TEMPLATES[s.summary_type].label
            if s.summary_type in TEMPLATES else s.summary_type,
            "provider": s.provider, "created_at": s.created_at, "content": s.content,
            "source": "cache",
        }
    for ref in entry.summaries:
        if ref.path is None or ref.summary_type in out or ref.summary_type == "unknown":
            continue
        try:
            text = ref.path.read_text(encoding="utf-8")
        except OSError:
            continue
        body = text.split("\n---\n", 1)[1] if "\n---\n" in text else text
        out[ref.summary_type] = {
            "type": ref.summary_type, "label": ref.label, "provider": ref.provider,
            "created_at": ref.created_at, "content": body.strip(), "source": "file",
        }
    if "mindmap" in out:
        tree = mindmap_mod.parse_outline(out["mindmap"]["content"], fallback_title=entry.title)
        out["mindmap"]["tree"] = tree.to_markmap()
        out["mindmap"]["nodes"] = tree.size
        out["mindmap"]["depth"] = tree.depth
    return out


# ---------- 应用 ----------


def create_app(cfg: Config):
    _require_fastapi()
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel

    state = State(cfg)
    app = FastAPI(title=APP_NAME, version=__version__, docs_url="/api/docs", redoc_url=None)

    @app.exception_handler(VideoSummarizerError)
    async def _domain_error(_req: Request, exc: VideoSummarizerError):
        return JSONResponse(status_code=400, content={"error": str(exc)})

    # ----- 元信息 -----

    @app.get("/api/meta")
    def meta():
        c = state.fresh_config()
        try:
            provider_desc = get_summarizer(c.summarizer).describe()
        except VideoSummarizerError as exc:
            provider_desc = f"未配置（{exc}）"
        return {
            "app": APP_NAME, "version": __version__,
            "summary_types": [
                {"key": k, "label": t.label, "hint": TYPE_HINTS.get(k, "")}
                for k, t in TEMPLATES.items()
            ],
            "default_summary_type": c.summarizer.summary_type,
            "asr_choices": ASR_CHOICES,
            "asr_default": c.asr.model,
            "diarize_default": c.asr.diarize,
            "provider": provider_desc,
            "currency": c.summarizer.currency,
            "priced": c.summarizer.price_input_per_m is not None,
            "output_dir": str(c.output_dir),
        }

    # ----- 库 -----

    @app.get("/api/library")
    def library():
        c = state.fresh_config()
        entries = library_mod.load_library(c)
        groups = [
            {"uploader": name, "entries": [_entry_dict(e) for e in items]}
            for name, items in library_mod.group_by_uploader(entries)
        ]
        n_summaries = sum(len(e.summaries) for e in entries)
        n_mindmaps = sum(1 for e in entries for s in e.summaries if s.summary_type == "mindmap")
        return {
            "stats": {
                "videos": len(entries),
                "duration_sec": sum(e.duration_sec for e in entries),
                "summaries": n_summaries,
                "mindmaps": n_mindmaps,
            },
            "groups": groups,
        }

    @app.get("/api/videos/{video_id}")
    def video(video_id: str):
        c = state.fresh_config()
        entry = _find_entry(c, video_id)
        if entry is None:
            raise HTTPException(404, "没有这个视频")
        transcript = _load_transcript(c, entry)
        paragraphs = to_paragraphs(transcript.segments) if transcript else []
        return {
            **_entry_dict(entry),
            "meta": transcript.meta if transcript else entry.meta,
            "paragraphs": [
                {"start": p.start, "end": p.end, "text": p.text, "speaker": p.speaker}
                for p in paragraphs
            ],
            "summaries": _summaries_dict(c, entry),
        }

    @app.get("/api/videos/{video_id}/estimate")
    def estimate(video_id: str, type: str = "overall", language: str = "zh"):
        c = state.fresh_config()
        if type not in TEMPLATES:
            raise HTTPException(400, f"不认识的总结类型 {type}")
        entry = _find_entry(c, video_id)
        transcript = _load_transcript(c, entry) if entry else None
        if transcript is None:
            raise HTTPException(404, "没有这个视频的转写")
        provider = get_summarizer(c.summarizer)
        est = provider.plan(transcript, SummaryOptions(summary_type=type, language=language))
        return {"provider": provider.describe(), **_estimate_dict(c, est)}

    @app.delete("/api/videos/{video_id}")
    def delete_video(video_id: str):
        c = state.fresh_config()
        entry = _find_entry(c, video_id)
        if entry is None:
            raise HTTPException(404, "没有这个视频")
        t, s = Cache(c.cache_db).clear(video_id)
        removed_dir = False
        if entry.work_dir and entry.work_dir.is_dir():
            # 只删我们自己产出的目录：里面必须有 transcript.json，且在 output_dir 之下
            try:
                entry.work_dir.resolve().relative_to(c.output_dir.resolve())
                shutil.rmtree(entry.work_dir)
                removed_dir = True
            except (ValueError, OSError) as exc:
                log.warning("没删产物目录 %s：%s", entry.work_dir, exc)
        return {"transcripts": t, "summaries": s, "removed_dir": removed_dir}

    @app.post("/api/videos/{video_id}/open")
    def open_folder(video_id: str):
        c = state.fresh_config()
        entry = _find_entry(c, video_id)
        if entry is None or not entry.work_dir or not entry.work_dir.is_dir():
            raise HTTPException(404, "没有产物目录")
        path = str(entry.work_dir)
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])  # noqa: S603, S607
        else:
            subprocess.Popen(["xdg-open", path])  # noqa: S603, S607
        return {"ok": True}

    # ----- 探测 -----

    class ProbeBody(BaseModel):
        url: str

    @app.post("/api/probe")
    def probe_url(body: ProbeBody):
        c = state.fresh_config()
        url = body.url.strip()
        if not url:
            raise HTTPException(400, "先填一个视频链接")
        info = state.probed(url) or probe(url, c.download)
        state.remember_probe(url, info)

        picked = select_subtitle_language(info, c)
        diarize = c.asr.wants_diarization(needed=False)
        key = plan_transcript_key(info, c, force_asr=False, diarize=diarize)
        cache = Cache(c.cache_db)
        cached = cache.get_transcript(key) is not None
        existing = _find_entry(c, info.video_id)

        est_tokens = int(info.duration_sec * TOKENS_PER_SEC)
        return {
            "url": info.url,
            "video_id": info.video_id,
            "title": info.title,
            "uploader": info.uploader,
            "upload_date": _fmt_date(info.upload_date),
            "thumbnail": info.thumbnail,
            "duration_sec": info.duration_sec,
            "extractor": info.extractor,
            "subtitle": {"language": picked[0], "auto": picked[1]} if picked else None,
            "transcript_cached": cached,
            "already_in_library": existing is not None,
            "summaries_done": [s.summary_type for s in existing.summaries] if existing else [],
            "estimate": {
                # 没转写之前只能按时长估
                "input_tokens": est_tokens,
                "output_tokens": OUTPUT_TOKENS_PER_CALL,
                "cost": _money(c, est_tokens, OUTPUT_TOKENS_PER_CALL),
                "currency": c.summarizer.currency,
                # SenseVoice 实测约 100 倍速；字幕路径几乎不花时间
                "asr_sec": 0 if picked or cached else max(5, int(info.duration_sec / 100) + 5),
            },
        }

    # ----- 任务 -----

    class ProcessBody(BaseModel):
        url: str
        asr_model: str | None = None
        diarize: str | bool | None = None
        summary_type: str | None = None     # None / "" = 只转写
        force: bool = False
        force_asr: bool = False

    @app.post("/api/jobs")
    def create_job(body: ProcessBody):
        url = body.url.strip()
        if not url:
            raise HTTPException(400, "先填一个视频链接")
        summary_type = (body.summary_type or "").strip() or None
        if summary_type and summary_type not in TEMPLATES:
            raise HTTPException(400, f"不认识的总结类型 {summary_type}")

        dup = state.jobs.find_active("process", url=url)
        if dup is not None:
            return {"job": dup.to_dict(), "duplicate": True}

        c = state.fresh_config()
        if body.asr_model == "whisper":
            c.asr.provider, c.asr.model = "whisper", "large-v3"
        elif body.asr_model:
            c.asr.model = body.asr_model
        if body.diarize is not None:
            c.asr.diarize = body.diarize
        info = state.probed(url)
        title = info.title if info else url

        def work(rep: Reporter) -> dict[str, Any]:
            options = SummaryOptions(summary_type=summary_type or c.summarizer.summary_type)
            result = run_pipeline(
                url, c, options=options, force=body.force, force_asr=body.force_asr,
                skip_summary=summary_type is None, on_stage=rep.stage, info=info,
            )
            return {
                "video_id": result.info.video_id,
                "title": result.info.title,
                "segments": len(result.transcript.segments),
                "summary_type": summary_type,
                "summary_done": result.summary is not None,
                "summary_skipped": result.summary_skipped_reason,
            }

        job = state.jobs.submit("process", title, {"url": url, "summary_type": summary_type,
                                                    "asr_model": body.asr_model}, work)
        return {"job": job.to_dict(), "duplicate": False}

    class SummarizeBody(BaseModel):
        type: str
        language: str = "zh"
        extra: str | None = None
        force: bool = False

    @app.post("/api/videos/{video_id}/summaries")
    def create_summary(video_id: str, body: SummarizeBody):
        if body.type not in TEMPLATES:
            raise HTTPException(400, f"不认识的总结类型 {body.type}")
        c = state.fresh_config()
        entry = _find_entry(c, video_id)
        transcript = _load_transcript(c, entry) if entry else None
        if entry is None or transcript is None:
            raise HTTPException(404, "没有这个视频的转写")

        dup = state.jobs.find_active("summarize", video_id=video_id, type=body.type)
        if dup is not None:
            return {"job": dup.to_dict(), "duplicate": True}

        options = SummaryOptions(summary_type=body.type, language=body.language,
                                 extra_instructions=(body.extra or "").strip() or None)
        provider = get_summarizer(c.summarizer)
        cache = Cache(c.cache_db)
        key = summary_key(
            transcript_key=entry.cache_key or "", provider_desc=provider.describe(),
            summary_type=body.type, language=options.language, extra=options.extra_instructions,
        ) if entry.cache_key else None

        # 已经有一份且没要求重做：不进队列，直接给
        if key and not body.force:
            cached = cache.get_summary(key)
            if cached is not None:
                if entry.work_dir:
                    write_summary_files(cached, transcript, provider.describe(), options, entry.work_dir)
                return {"job": None, "duplicate": False, "cached": True,
                        "summary": {"type": body.type, "content": cached,
                                    "provider": provider.describe()}}

        work_dir = entry.work_dir or (c.output_dir / f"{entry.title[:60]}-{entry.video_id}")

        def work(rep: Reporter) -> dict[str, Any]:
            rep.stage("summarize", f"调用 {provider.describe()}")
            est = provider.plan(transcript, options)
            text = provider.summarize(transcript, options)
            if key:
                cache.put_summary(key, transcript_key=entry.cache_key, transcript=transcript,
                                  provider_desc=provider.describe(), summary_type=body.type,
                                  language=options.language, content=text)
            write_summary_files(text, transcript, provider.describe(), options, work_dir)
            return {"video_id": video_id, "type": body.type, "content": text,
                    "provider": provider.describe(), **_estimate_dict(c, est)}

        job = state.jobs.submit("summarize", f"{TEMPLATES[body.type].label} · {entry.title}",
                                {"video_id": video_id, "type": body.type}, work)
        return {"job": job.to_dict(), "duplicate": False, "cached": False}

    @app.get("/api/jobs")
    def list_jobs():
        return {"jobs": [j.to_dict() for j in state.jobs.list()]}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        job = state.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "没有这个任务")
        return job.to_dict(with_events=True)

    @app.delete("/api/jobs/{job_id}")
    def cancel_job(job_id: str):
        job = state.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "没有这个任务")
        return {"cancelled": state.jobs.cancel(job_id), "job": job.to_dict()}

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(job_id: str, request: Request, after: int = 0):
        """SSE。事件的 id 就是序号，断线后浏览器会带 Last-Event-ID 回来续。"""
        job = state.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "没有这个任务")
        last = request.headers.get("last-event-id")
        if last and last.isdigit():
            after = max(after, int(last))
        q = job.subscribe(after_seq=after)
        loop = asyncio.get_running_loop()

        async def gen():
            try:
                while True:
                    if await request.is_disconnected():
                        return
                    if job.finished and q.empty():
                        return  # 已经结束、也没有欠着的事件，别让客户端干等
                    try:
                        ev = await loop.run_in_executor(None, q.get, True, 15)
                    except Exception:  # noqa: BLE001 —— queue.Empty
                        yield ": keepalive\n\n"
                        continue
                    payload = json.dumps(ev.to_dict(), ensure_ascii=False)
                    yield f"id: {ev.seq}\nevent: {ev.kind}\ndata: {payload}\n\n"
                    if ev.kind == "status" and ev.data.get("status") in ("done", "failed", "cancelled"):
                        return
            finally:
                job.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ----- 静态前端 -----

    if (DIST_DIR / "index.html").is_file():
        app.mount("/assets", StaticFiles(directory=DIST_DIR / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            candidate = DIST_DIR / path
            if path and candidate.is_file() and ".." not in path:
                return FileResponse(candidate)
            return FileResponse(DIST_DIR / "index.html")
    else:
        @app.get("/", include_in_schema=False)
        def no_frontend():
            return HTMLResponse(
                "<p style='font:15px system-ui;padding:40px'>前端还没构建："
                "在 <code>frontend/</code> 里执行 <code>npm install && npm run build</code>，"
                "产物会放到 <code>web/dist/</code>。接口文档在 <a href='/api/docs'>/api/docs</a>。</p>"
            )

    return app


def launch(cfg: Config, host: str = "127.0.0.1", port: int = 7860, inbrowser: bool = True) -> None:
    _require_fastapi()
    import uvicorn

    app = create_app(cfg)
    if inbrowser:
        import threading
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(f"http://{host}:{port}")).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
