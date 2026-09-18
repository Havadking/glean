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
import threading
import time
from pathlib import Path
from typing import Any

from .. import __version__
from ..cache import Cache, summary_key
from ..config import (
    Config,
    load_config,
    mask_api_key,
    update_asr_config,
    update_env_key,
    update_pricing,
    update_summarizer_config,
)
from ..errors import VideoSummarizerError
from ..models import SummaryOptions
from ..pipeline import plan_transcript_key, run as run_pipeline, write_summary_files
from .. import cleaning
from .. import correction as correction_mod
from .. import downloads as downloads_mod
from .. import digest as digest_mod
from .. import tagging as tagging_mod
from .. import listing as listing_mod
from .. import qa as qa_mod
from .. import search as search_mod
from ..subtitle.fetcher import select_subtitle_language
from ..summarizer import get_provider as get_summarizer
from ..summarizer.base import CostEstimate
from ..summarizer.prompts import TEMPLATES
from ..audio import extractor as audio_extractor
from ..ytdlp_base import VideoInfo, extract_url, probe
from urllib.parse import urlparse
from urllib.request import Request as UrlRequest, urlopen
from . import avatars as avatars_mod
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
    {"value": "fun-asr-nano", "label": "Fun-ASR-Nano", "note": "准，专名少错，吃词表热词"},
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
        # 存视频的队列，create_app 里装上（它的完成回调要用到 _submit_process）
        self.downloads: Any = None
        # url -> VideoInfo。探测过的直接复用给任务，少发一次请求（B 站 412 的主要来源）
        self._probed: dict[str, VideoInfo] = {}
        # 搜索索引是 v0.7 加的，老库第一次启动补建；平时靠任务完成时增量更新
        try:
            videos, rows = search_mod.sync_index(self.fresh_config())
            if videos:
                log.info("搜索索引补建：%d 个视频，%d 行", videos, rows)
        except Exception as exc:  # noqa: BLE001 —— 索引坏了不能拦住界面启动
            log.warning("搜索索引补建失败：%s", exc)
        # 上一个视频碰站点的时间，批量时据此隔开
        self._last_site_touch = 0.0
        self._touch_lock = threading.Lock()
        # 列表页缓存：(url, page, keyword) -> (时间, 页)。空间接口限流很紧，翻回上一页不该再打一次
        self._listings: dict[tuple[str, int, str], tuple[float, Any]] = {}
        self._warm_up_asr()

    def _warm_up_asr(self) -> None:
        """后台起个子进程把 funasr/torch import 一遍，让磁盘缓存热起来。冷启动这步要一分多钟，别让第一个任务扛。"""
        if (self.cfg.asr.provider or "").strip().lower() != "funasr":
            return
        from ..asr.worker import warm_up

        threading.Thread(target=warm_up, name="vsum-asr-warmup", daemon=True).start()

    LISTING_TTL_SEC = 600

    def listing_cached(self, url: str, page: int, keyword: str):
        hit = self._listings.get((url, page, keyword))
        if hit and time.monotonic() - hit[0] < self.LISTING_TTL_SEC:
            return hit[1]
        return None

    def remember_listing(self, url: str, page: int, keyword: str, page_obj) -> None:
        if len(self._listings) > 100:
            self._listings.pop(next(iter(self._listings)))
        self._listings[(url, page, keyword)] = (time.monotonic(), page_obj)

    def throttle(self, delay: float) -> None:
        """在 worker 线程里调：离上次碰站点不够 delay 秒就等。"""
        with self._touch_lock:
            wait = self._last_site_touch + delay - time.monotonic()
        if wait > 0:
            log.info("隔 %.0f 秒再碰站点（batch_delay_sec）", wait)
            time.sleep(wait)

    def touched(self) -> None:
        with self._touch_lock:
            self._last_site_touch = time.monotonic()

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
        "remark": e.remark,
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
        "has_audio": library_mod.audio_path(e) is not None,
        "summaries": [
            {"type": s.summary_type, "label": s.label, "provider": s.provider,
             "created_at": s.created_at}
            for s in e.summaries
        ],
    }


def _record_usage(cfg: Config, provider, *, video_id: str, kind: str, detail: str | None) -> dict[str, Any]:
    """把 provider 刚才真实用掉的 token 记进账本，返回这一笔。"""
    i, o, n = provider.take_usage()
    cost = _money(cfg, i, o) if n else None
    Cache(cfg.cache_db).add_usage(video_id, kind, detail, provider.describe(), i, o, n, cost)
    return {"input_tokens": i, "output_tokens": o, "calls": n, "cost": cost, "currency": cfg.summarizer.currency}


def _record_usage_raw(cfg: Config, usage: tuple[int, int, int] | None, provider_desc: str | None,
                      *, video_id: str, kind: str) -> None:
    """流水线里已经取走的用量（比如纠错那一步）记进账本。"""
    if not usage or not provider_desc:
        return
    i, o, n = usage
    Cache(cfg.cache_db).add_usage(video_id, kind, None, provider_desc, i, o, n, _money(cfg, i, o) if n else None)


def _corrections_dict(transcript) -> dict[str, Any] | None:
    """meta 里的纠错表给界面看的形状。没跑过纠错返回 None。"""
    block = transcript.meta.get("corrections") if transcript else None
    if not isinstance(block, dict):
        return None
    items = correction_mod.items_of(transcript)
    return {
        "provider": block.get("provider"),
        "created_at": block.get("created_at"),
        "items": [{"index": i, **c.to_dict()} for i, c in enumerate(items)],
        "applied_hits": sum(c.hits for c in items if c.applied),
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


def _summaries_dict(cfg: Config, entry: library_mod.LibraryEntry) -> dict[str, dict[str, Any]]:
    """每种类型最新的一份总结，带正文；思维导图附带解析好的树。"""
    out: dict[str, dict[str, Any]] = {}
    for key, s in library_mod.load_summaries(cfg, entry).items():
        out[key] = {"type": key, "label": s.label, "provider": s.provider,
                    "created_at": s.created_at, "content": s.content, "source": s.source}
    if "mindmap" in out:
        tree = mindmap_mod.parse_outline(out["mindmap"]["content"], fallback_title=entry.title)
        out["mindmap"]["tree"] = tree.to_markmap()
        out["mindmap"]["nodes"] = tree.size
        out["mindmap"]["depth"] = tree.depth
    return out


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _in_period(period: str, key: str, entry: library_mod.LibraryEntry) -> bool:
    start, end = digest_mod.period_range(period, key)
    return start <= digest_mod.local_date(entry.created_at) <= end


def _tags_list(entries) -> list[dict[str, Any]]:
    return [{"tag": t.tag, "source": t.source} for t in entries]


def _tag_video(c: Config, entry: library_mod.LibraryEntry, provider=None) -> list[str]:
    """让模型给一条视频打标签并写进库。返回这次写进去的 AI 标签。没材料返回空。"""
    mat = library_mod.material_text(c, entry)
    if mat is None:
        return []
    kind, text = mat
    cache = Cache(c.cache_db)
    provider = provider or get_summarizer(c.summarizer)
    vocab = [t for t, _n in cache.tag_counts()]
    tags = tagging_mod.suggest(provider, title=entry.title, uploader=entry.uploader, kind=kind, text=text, vocab=vocab)
    written = cache.set_ai_tags(entry.video_id, tags)
    _record_usage(c, provider, video_id=entry.video_id, kind="tags", detail=None)
    return written


# ---------- 应用 ----------


def create_app(cfg: Config):
    _require_fastapi()
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel

    state = State(cfg)
    app = FastAPI(title=APP_NAME, version=__version__, docs_url="/api/docs", redoc_url=None)
    if cfg.web.cors_origins:
        # 给随拾这类本机页面跨域调接口。来源在 load_config 里已经限定只能是 localhost
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware, allow_origins=list(cfg.web.cors_origins),
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"], allow_headers=["*"],
        )

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
            "correct_terms_default": c.summarizer.correct_terms,
            "provider": provider_desc,
            "model": c.summarizer.name or c.summarizer.model,
            "currency": c.summarizer.currency,
            "priced": c.summarizer.price_input_per_m is not None,
            "output_dir": str(c.output_dir),
        }

    # ----- 模型与价格配置 -----

    class PricingBody(BaseModel):
        price_input_per_m: float | None = None
        price_output_per_m: float | None = None
        currency: str = "¥"

    class LlmConfigBody(BaseModel):
        name: str | None = None
        provider: str | None = None
        model: str | None = None
        base_url: str | None = None
        api_key_env: str | None = None
        api_key: str | None = None
        temperature: float | None = None
        max_context_tokens: int | None = None
        max_output_tokens: int | None = None
        price_input_per_m: float | None = None
        price_output_per_m: float | None = None
        currency: str | None = "¥"

    class AsrConfigBody(BaseModel):
        provider: str | None = None
        model: str | None = None
        device: str | None = None
        diarize: str | bool | None = None

    class TestLlmBody(BaseModel):
        provider: str | None = None
        model: str | None = None
        base_url: str | None = None
        api_key: str | None = None
        api_key_env: str | None = None

    @app.get("/api/config/pricing")
    def get_pricing():
        c = state.fresh_config()
        try:
            provider_desc = get_summarizer(c.summarizer).describe()
        except VideoSummarizerError as exc:
            provider_desc = f"未配置（{exc}）"
        return {
            "price_input_per_m": c.summarizer.price_input_per_m,
            "price_output_per_m": c.summarizer.price_output_per_m,
            "currency": c.summarizer.currency,
            "priced": c.summarizer.price_input_per_m is not None,
            "provider": provider_desc,
            "model": c.summarizer.model,
        }

    @app.post("/api/config/pricing")
    def set_pricing(body: PricingBody):
        if body.price_input_per_m is not None and body.price_input_per_m < 0:
            raise HTTPException(400, "输入单价不能为负数")
        if body.price_output_per_m is not None and body.price_output_per_m < 0:
            raise HTTPException(400, "输出单价不能为负数")
        c = state.fresh_config()
        currency = body.currency.strip() or "¥"
        update_pricing(
            c.source_path,
            price_input_per_m=body.price_input_per_m,
            price_output_per_m=body.price_output_per_m,
            currency=currency,
        )
        state.cfg.summarizer.price_input_per_m = body.price_input_per_m
        state.cfg.summarizer.price_output_per_m = body.price_output_per_m
        state.cfg.summarizer.currency = currency
        return get_pricing()

    @app.get("/api/config/llm")
    def get_llm_config():
        c = state.fresh_config()
        raw_key = c.summarizer.api_key or ""
        try:
            provider_desc = get_summarizer(c.summarizer).describe()
        except Exception as exc:
            provider_desc = f"未配置（{exc}）"

        return {
            "name": c.summarizer.name or c.summarizer.model,
            "provider": c.summarizer.provider,
            "model": c.summarizer.model,
            "base_url": c.summarizer.base_url,
            "api_key_env": c.summarizer.api_key_env,
            "has_api_key": bool(raw_key),
            "masked_api_key": mask_api_key(raw_key),
            "temperature": c.summarizer.temperature,
            "max_context_tokens": c.summarizer.max_context_tokens,
            "max_output_tokens": c.summarizer.max_output_tokens,
            "price_input_per_m": c.summarizer.price_input_per_m,
            "price_output_per_m": c.summarizer.price_output_per_m,
            "currency": c.summarizer.currency,
            "provider_desc": provider_desc,
        }

    @app.post("/api/config/llm")
    def set_llm_config(body: LlmConfigBody):
        c = state.fresh_config()
        api_key_env = body.api_key_env.strip() if body.api_key_env else c.summarizer.api_key_env
        if body.api_key and body.api_key.strip():
            env_file = (c.source_path.parent if c.source_path else Path(".")) / ".env"
            update_env_key(env_file, api_key_env, body.api_key.strip())

        update_summarizer_config(
            c.source_path,
            name=body.name,
            provider=body.provider,
            model=body.model,
            base_url=body.base_url,
            api_key_env=api_key_env,
            temperature=body.temperature,
            max_context_tokens=body.max_context_tokens,
            max_output_tokens=body.max_output_tokens,
            price_input_per_m=body.price_input_per_m,
            price_output_per_m=body.price_output_per_m,
            currency=body.currency,
        )
        refreshed = state.fresh_config()
        state.cfg.summarizer = refreshed.summarizer
        return get_llm_config()

    @app.get("/api/config/asr")
    def get_asr_config():
        c = state.fresh_config()
        return {
            "provider": c.asr.provider,
            "model": c.asr.model,
            "device": c.asr.device,
            "diarize": c.asr.diarize,
        }

    @app.post("/api/config/asr")
    def set_asr_config(body: AsrConfigBody):
        c = state.fresh_config()
        update_asr_config(
            c.source_path,
            provider=body.provider,
            model=body.model,
            device=body.device,
            diarize=body.diarize,
        )
        refreshed = state.fresh_config()
        state.cfg.asr = refreshed.asr
        return get_asr_config()

    @app.post("/api/config/test-llm")
    def test_llm(body: TestLlmBody | None = None):
        c = state.fresh_config()
        t0 = time.monotonic()
        try:
            target_cfg = c.summarizer
            if body and (body.provider or body.model or body.base_url or body.api_key):
                from ..config import SummarizerConfig
                env_key = body.api_key_env or c.summarizer.api_key_env
                if body.api_key and body.api_key.strip():
                    os.environ[env_key] = body.api_key.strip()
                target_cfg = SummarizerConfig(
                    provider=body.provider or c.summarizer.provider,
                    model=body.model or c.summarizer.model,
                    base_url=body.base_url if body.base_url is not None else c.summarizer.base_url,
                    api_key_env=env_key,
                    temperature=0.3,
                )

            provider = get_summarizer(target_cfg)
            reply = provider.complete("请回复pong", "ping")
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            return {"ok": True, "latency_ms": elapsed_ms, "model": target_cfg.model, "reply": reply.strip()}
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            return {"ok": False, "latency_ms": elapsed_ms, "error": str(exc)}

    # ----- 花费 -----

    @app.get("/api/usage")
    def usage(limit: int = 50):
        from datetime import datetime, timezone

        c = state.fresh_config()
        cache = Cache(c.cache_db)
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
        return {
            "currency": c.summarizer.currency,
            "priced": c.summarizer.price_input_per_m is not None,
            "total": cache.usage_totals(),
            "month": cache.usage_totals(since=month_start),
            "recent": cache.usage_recent(limit=max(1, min(limit, 500))),
        }

    # ----- 存储 -----

    @app.get("/api/storage")
    def storage():
        return library_mod.storage_report(state.fresh_config())

    @app.post("/api/storage/clear-audio")
    def clear_audio():
        n, freed = library_mod.clear_audio(state.fresh_config())
        return {"dirs": n, "freed_bytes": freed}

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
        cache = Cache(c.cache_db)
        costs = cache.usage_by_video()
        tags = cache.tags_by_video()
        up_groups = cache.get_uploader_groups()
        for g in groups:
            g["group"] = up_groups.get(g["uploader"]) if g["uploader"] else None
            for e in g["entries"]:
                e["cost"] = costs.get(e["video_id"])
                e["tags"] = _tags_list(tags.get(e["video_id"], []))
        totals = cache.usage_totals()
        # 本周回顾的状态，侧栏据此画小红点
        week_key = digest_mod.period_key("week", digest_mod.local_date(_now_iso()))
        week_ids = {e.video_id for e in entries if _in_period("week", week_key, e)}
        week_digest = cache.get_digest("week", week_key) if week_ids else None
        return {
            "stats": {
                "videos": len(entries),
                "duration_sec": sum(e.duration_sec for e in entries),
                "summaries": n_summaries,
                "mindmaps": n_mindmaps,
                "cost": totals["cost"],
                "calls": totals["calls"],
                "currency": c.summarizer.currency,
            },
            "groups": groups,
            "uploader_groups": cache.list_uploader_groups(),
            "review": {
                "period": "week", "key": week_key, "videos": len(week_ids),
                "generated": week_digest is not None,
                "stale": week_digest is not None and set(week_digest.video_ids) != week_ids,
            },
        }

    @app.get("/api/videos/{video_id}")
    def video(video_id: str, clean: bool = False, raw: bool = False):
        """raw=1 看没应用纠错表的原文。"""
        c = state.fresh_config()
        entry = library_mod.find_entry(c, video_id)
        if entry is None:
            raise HTTPException(404, "没有这个视频")
        transcript = library_mod.load_transcript(c, entry, raw=raw)
        segments = transcript.segments if transcript else []
        paragraphs = to_paragraphs(cleaning.clean_segments(segments) if clean else segments)
        return {
            **_entry_dict(entry),
            "cleaned": clean,
            "raw": raw,
            "corrections": _corrections_dict(transcript),
            "clean_ratio": round(cleaning.removed_ratio(segments), 4) if segments else 0.0,
            "usage": Cache(c.cache_db).usage_totals(video_id),
            "meta": transcript.meta if transcript else entry.meta,
            "paragraphs": [
                {"start": p.start, "end": p.end, "text": p.text, "speaker": p.speaker}
                for p in paragraphs
            ],
            "summaries": _summaries_dict(c, entry),
            "tags": _tags_list(Cache(c.cache_db).tags_for_video(video_id)),
        }

    @app.get("/api/videos/{video_id}/audio")
    def video_audio(video_id: str):
        """本地音频，给详情页的播放器用。FileResponse 支持 Range，拖进度条不用整段下。"""
        c = state.fresh_config()
        entry = library_mod.find_entry(c, video_id)
        path = library_mod.audio_path(entry) if entry else None
        if path is None:
            raise HTTPException(404, "这条视频没有本地音频")
        return FileResponse(path, media_type="audio/wav",
                            headers={"Cache-Control": "private, max-age=86400"})

    @app.post("/api/videos/{video_id}/audio")
    def fetch_audio(video_id: str):
        """字幕路径的视频没下过音频、或者清理过音频：单独下一份，进任务队列。"""
        c = state.fresh_config()
        entry = library_mod.find_entry(c, video_id)
        if entry is None:
            raise HTTPException(404, "没有这个视频")
        if library_mod.audio_path(entry) is not None:
            return {"job": None, "duplicate": False, "cached": True}
        dup = state.jobs.find_active("audio", video_id=video_id)
        if dup is not None:
            return {"job": dup.to_dict(), "duplicate": True, "cached": False}
        url = entry.source_url
        if not url:
            raise HTTPException(400, "这条视频没记录来源链接，下不了音频")
        work_dir = entry.work_dir or (c.output_dir / f"{entry.title[:60]}-{entry.video_id}")

        def work(rep: Reporter) -> dict[str, Any]:
            info = state.probed(url)
            if info is None:
                state.throttle(c.download.batch_delay_sec)
                rep.stage("probe", "探测视频")
                info = probe(url, c.download)
                state.remember_probe(url, info)
            state.touched()
            rep.stage("download", "提取音频")
            path = audio_extractor.extract(info, c, work_dir / "audio")
            return {"video_id": video_id, "path": str(path), "bytes": path.stat().st_size}

        job = state.jobs.submit("audio", f"下载音频 · {entry.title}", {"video_id": video_id, "url": url}, work)
        return {"job": job.to_dict(), "duplicate": False, "cached": False}

    @app.get("/api/videos/{video_id}/estimate")
    def estimate(video_id: str, type: str = "overall", language: str = "zh"):
        c = state.fresh_config()
        if type not in TEMPLATES:
            raise HTTPException(400, f"不认识的总结类型 {type}")
        entry = library_mod.find_entry(c, video_id)
        transcript = library_mod.load_transcript(c, entry) if entry else None
        if transcript is None:
            raise HTTPException(404, "没有这个视频的转写")
        provider = get_summarizer(c.summarizer)
        if c.summarizer.clean_transcript:
            transcript = cleaning.clean_transcript(transcript)
        est = provider.plan(transcript, SummaryOptions(summary_type=type, language=language))
        return {"provider": provider.describe(), **_estimate_dict(c, est)}

    class RemarkBody(BaseModel):
        remark: str | None = None

    @app.post("/api/videos/{video_id}/remark")
    def update_remark(video_id: str, body: RemarkBody):
        c = state.fresh_config()
        if library_mod.find_entry(c, video_id) is None:
            raise HTTPException(404, "没有这个视频")
        clean = library_mod.set_video_remark(c, video_id, body.remark)
        return {"video_id": video_id, "remark": clean}

    @app.delete("/api/videos/{video_id}")
    def delete_video(video_id: str):
        c = state.fresh_config()
        entry = library_mod.find_entry(c, video_id)
        if entry is None:
            raise HTTPException(404, "没有这个视频")
        t, s = Cache(c.cache_db).clear(video_id)
        search_mod.SearchIndex(c.cache_db).remove_video(video_id)
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
        entry = library_mod.find_entry(c, video_id)
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
        page: int = 1
        keyword: str = ""

    def _listing_dict(c: Config, page: listing_mod.ListPage) -> dict[str, Any]:
        known = {e.video_id: e for e in library_mod.load_library(c)}
        active = {j.params.get("url") for j in state.jobs.list() if j.kind == "process" and not j.finished}
        d = page.to_dict()
        for e in d["entries"]:
            e["upload_date"] = _fmt_date(e["upload_date"])
            hit = known.get(e["video_id"])
            e["in_library"] = hit is not None
            e["summaries_done"] = [s.summary_type for s in hit.summaries] if hit else []
            e["queued"] = e["url"] in active
        return {**d, "kind": "list", "list_kind": d["kind"]}

    @app.post("/api/probe")
    def probe_url(body: ProbeBody):
        """贴什么都行：单个视频出探测卡，合集 / UP 主空间出一页列表。"""
        c = state.fresh_config()
        url = extract_url(body.url)
        if not url:
            raise HTTPException(400, "先填一个视频链接")
        info = state.probed(url)
        if info is None:
            keyword = body.keyword.strip()
            cached_page = state.listing_cached(url, body.page, keyword)
            if cached_page is not None:
                return _listing_dict(c, cached_page)
            state.touched()
            result = listing_mod.probe_any(url, c.download, page=body.page, keyword=keyword)
            if isinstance(result, listing_mod.ListPage):
                state.remember_listing(url, body.page, keyword, result)
                return _listing_dict(c, result)
            info = result
            state.remember_probe(url, info)

        picked = select_subtitle_language(info, c)
        diarize = c.asr.wants_diarization(needed=False)
        key = plan_transcript_key(info, c, force_asr=False, diarize=diarize)
        cache = Cache(c.cache_db)
        cached = cache.get_transcript(key) is not None
        existing = library_mod.find_entry(c, info.video_id)

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
            "kind": "video",
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
        correct_terms: bool | None = None   # None = 按 config

    def _submit_process(url: str, *, summary_type: str | None, asr_model: str | None,
                        diarize: str | bool | None, force: bool, force_asr: bool,
                        title: str | None = None, batch: bool = False, persisted_id: str | None = None,
                        correct_terms: bool | None = None):
        dup = state.jobs.find_active("process", url=url)
        if dup is not None:
            return dup, True

        c = state.fresh_config()
        if asr_model == "whisper":
            c.asr.provider, c.asr.model = "whisper", "large-v3"
        elif asr_model:
            c.asr.model = asr_model
        if diarize is not None:
            c.asr.diarize = diarize
        if correct_terms is not None:
            c.summarizer.correct_terms = correct_terms
        info = state.probed(url)
        job_title = title or (info.title if info else url)
        params = {"url": url, "summary_type": summary_type, "asr_model": asr_model,
                  "diarize": diarize, "force": force, "force_asr": force_asr, "title": job_title,
                  "batch": batch, "correct_terms": correct_terms}
        cache = Cache(c.cache_db)

        def work(rep: Reporter) -> dict[str, Any]:
            try:
                # 批量时相邻两个视频之间隔开一点，探测和下载各算一次碰站点
                if batch or info is None:
                    state.throttle(c.download.batch_delay_sec)
                state.touched()
                options = SummaryOptions(summary_type=summary_type or c.summarizer.summary_type)
                result = run_pipeline(
                    url, c, options=options, force=force, force_asr=force_asr,
                    skip_summary=summary_type is None, on_stage=rep.stage, info=info,
                )
                search_mod.index_one(c, result.info.video_id)
                _record_usage_raw(c, result.correction_usage, result.correction_provider_desc,
                                  video_id=result.info.video_id, kind="polish")
                if result.summary_provider is not None:
                    _record_usage(c, result.summary_provider, video_id=result.info.video_id,
                                  kind="summary", detail=summary_type)
                tags = _auto_tag(c, result.info.video_id, rep, provider=result.summary_provider)
                return {
                    "tags": tags,
                    "video_id": result.info.video_id,
                    "title": result.info.title,
                    "segments": len(result.transcript.segments),
                    "summary_type": summary_type,
                    "summary_done": result.summary is not None,
                    "summary_skipped": result.summary_skipped_reason,
                }
            finally:
                cache.remove_pending_job(rep.job.id)

        job = state.jobs.submit("process", job_title, params, work)
        # 记到库里：服务重启时排队的还能续上
        cache.add_pending_job(job.id, "process", job_title, params)
        if persisted_id and persisted_id != job.id:
            cache.remove_pending_job(persisted_id)
        return job, False

    def _resume_pending() -> None:
        c = state.fresh_config()
        pending = Cache(c.cache_db).pending_jobs()
        if not pending:
            return
        log.info("上次还有 %d 个任务没跑完，继续排队", len(pending))
        for p in pending:
            prm = p["params"]
            if not prm.get("url"):
                Cache(c.cache_db).remove_pending_job(p["id"])
                continue
            _submit_process(
                prm["url"], summary_type=prm.get("summary_type"), asr_model=prm.get("asr_model"),
                diarize=prm.get("diarize"), force=bool(prm.get("force")), force_asr=bool(prm.get("force_asr")),
                title=prm.get("title") or p["title"], batch=True, persisted_id=p["id"],
                correct_terms=prm.get("correct_terms"),
            )

    _resume_pending()

    @app.post("/api/jobs")
    def create_job(body: ProcessBody):
        url = extract_url(body.url)
        if not url:
            raise HTTPException(400, "先填一个视频链接")
        summary_type = (body.summary_type or "").strip() or None
        if summary_type and summary_type not in TEMPLATES:
            raise HTTPException(400, f"不认识的总结类型 {summary_type}")
        job, dup = _submit_process(url, summary_type=summary_type, asr_model=body.asr_model,
                                   diarize=body.diarize, force=body.force, force_asr=body.force_asr,
                                   correct_terms=body.correct_terms)
        return {"job": job.to_dict(), "duplicate": dup}

    class BatchBody(BaseModel):
        items: list[dict[str, Any]]      # {url, title?}
        asr_model: str | None = None
        diarize: str | bool | None = None
        summary_type: str | None = None
        correct_terms: bool | None = None

    @app.post("/api/jobs/batch")
    def create_batch(body: BatchBody):
        """一批视频排队。串行跑、相邻隔 batch_delay_sec，排队的记进库，重启续跑。"""
        summary_type = (body.summary_type or "").strip() or None
        if summary_type and summary_type not in TEMPLATES:
            raise HTTPException(400, f"不认识的总结类型 {summary_type}")
        urls = [(extract_url(str(it.get("url") or "")), str(it.get("title") or "").strip() or None)
                for it in body.items]
        urls = [(u, t) for u, t in urls if u]
        if not urls:
            raise HTTPException(400, "没有可处理的链接")
        if len(urls) > 200:
            raise HTTPException(400, "一次最多 200 个")
        jobs, dups = [], 0
        for url, title in urls:
            job, dup = _submit_process(url, summary_type=summary_type, asr_model=body.asr_model,
                                       diarize=body.diarize, force=False, force_asr=False,
                                       title=title, batch=True, correct_terms=body.correct_terms)
            jobs.append(job.to_dict())
            dups += int(dup)
        return {"jobs": jobs, "queued": len(jobs) - dups, "duplicates": dups}

    @app.delete("/api/jobs")
    def cancel_queued():
        """清空还没开始的。正在跑的那个跑完为止。"""
        n = 0
        c = state.fresh_config()
        cache = Cache(c.cache_db)
        for j in state.jobs.list():
            if j.status == "queued" and state.jobs.cancel(j.id):
                cache.remove_pending_job(j.id)
                n += 1
        return {"cancelled": n}

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
        entry = library_mod.find_entry(c, video_id)
        transcript = library_mod.load_transcript(c, entry) if entry else None
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
            corrections=correction_mod.fingerprint(transcript),
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
        # load_transcript 给的已经是应用了纠错表的；清洗再叠在上面
        model_input = cleaning.clean_transcript(transcript) if c.summarizer.clean_transcript else transcript

        def work(rep: Reporter) -> dict[str, Any]:
            rep.stage("summarize", f"调用 {provider.describe()}")
            est = provider.plan(model_input, options)
            text = provider.summarize(model_input, options)
            if key:
                cache.put_summary(key, transcript_key=entry.cache_key, transcript=transcript,
                                  provider_desc=provider.describe(), summary_type=body.type,
                                  language=options.language, content=text)
            write_summary_files(text, transcript, provider.describe(), options, work_dir)
            search_mod.index_one(c, video_id)
            used = _record_usage(c, provider, video_id=video_id, kind="summary", detail=body.type)
            _auto_tag(c, video_id, rep, provider=provider)
            return {"video_id": video_id, "type": body.type, "content": text,
                    "provider": provider.describe(), "estimate": _estimate_dict(c, est), "used": used}

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
        ok = state.jobs.cancel(job_id)
        if ok:
            Cache(state.fresh_config().cache_db).remove_pending_job(job_id)
        return {"cancelled": ok, "job": job.to_dict()}

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

    # ----- 存视频（给随拾用） -----

    def _persist_download(row: dict[str, Any]) -> None:
        Cache(state.fresh_config().cache_db).upsert_download(row)

    def _after_download(job: downloads_mod.DownloadJob) -> None:
        """下载完成后顺手送去转写（随拾点了「下载并发送拾光笺」）。"""
        if job.then_summarize is None:
            return
        _submit_process(job.url, summary_type=job.then_summarize or None, asr_model=None, diarize=None,
                        force=False, force_asr=False, title=job.title, batch=True)

    state.downloads = downloads_mod.DownloadManager(
        state.fresh_config, throttle=state.throttle, touched=state.touched,
        on_done=_after_download, persist=_persist_download,
    )
    state.downloads.load(Cache(cfg.cache_db).downloads())

    def _downloads_enabled(c: Config) -> None:
        if not c.download.video_dir:
            raise HTTPException(404, "拾光笺没有开启存视频功能：在 config.yaml 的 download.video_dir 填一个目录")

    def _download_config_dict(c: Config) -> dict[str, Any]:
        vd = Path(c.download.video_dir) if c.download.video_dir else None
        dir_ok = False
        if vd is not None:
            try:
                vd.mkdir(parents=True, exist_ok=True)
                dir_ok = vd.is_dir() and os.access(vd, os.W_OK)
            except OSError:
                dir_ok = False
        return {
            "enabled": vd is not None,
            "video_dir": str(vd) if vd else None,
            "dir_name": vd.name if vd else None,
            "dir_ok": dir_ok,
            "quality": downloads_mod.normalize_quality(c.download.video_quality),
            "qualities": list(downloads_mod.VIDEO_QUALITIES),
            "prefer_h264": bool(c.download.prefer_h264),
            "ffmpeg": shutil.which("ffmpeg") is not None,
            "app": APP_NAME,
            "version": __version__,
        }

    @app.get("/api/downloads/config")
    def download_config():
        return _download_config_dict(state.fresh_config())

    class DownloadProbeBody(BaseModel):
        url: str

    @app.post("/api/downloads/probe")
    def download_probe(body: DownloadProbeBody):
        """探测一条视频能不能存、有哪些清晰度、是不是已经存过 / 转写过。合集链接返回 kind=list。"""
        c = state.fresh_config()
        _downloads_enabled(c)
        url = extract_url(body.url)
        if not url:
            raise HTTPException(400, "先填一个视频链接")
        info = state.probed(url)
        if info is None:
            state.touched()
            result = listing_mod.probe_any(url, c.download)
            if isinstance(result, listing_mod.ListPage):
                return {"kind": "list", "list_kind": result.kind, "title": result.title, "total": result.total,
                        "url": url}
            info = result
            state.remember_probe(url, info)

        cache = Cache(c.cache_db)
        existing = cache.find_download(info.video_id)
        in_mem = state.downloads.find_by_video(info.video_id)
        file_ok = bool(existing and existing.get("file_path") and Path(existing["file_path"]).is_file())
        library_hit = library_mod.find_entry(c, info.video_id)
        heights = downloads_mod.available_heights(info)
        return {
            "kind": "video",
            "url": info.url,
            "video_id": info.video_id,
            "title": info.title,
            "uploader": info.uploader,
            "upload_date": _fmt_date(info.upload_date),
            "thumbnail": info.thumbnail,
            "duration_sec": info.duration_sec,
            "extractor": info.extractor,
            "heights": heights,
            "max_height": heights[0] if heights else None,
            "already_downloaded": file_ok,
            "download": existing if file_ok else None,
            "downloading": in_mem.to_dict() if in_mem and in_mem.active else None,
            "in_library": library_hit is not None,
            "summaries_done": [s.summary_type for s in library_hit.summaries] if library_hit else [],
        }

    class DownloadBody(BaseModel):
        url: str
        quality: str | int | None = None
        force: bool = False
        # None = 下完不管；"" = 下完只转写；"overall" 等 = 下完转写并出这种总结
        then_summarize: str | None = None

    @app.post("/api/downloads")
    def create_download(body: DownloadBody):
        c = state.fresh_config()
        _downloads_enabled(c)
        url = extract_url(body.url)
        if not url:
            raise HTTPException(400, "先填一个视频链接")
        if body.then_summarize and body.then_summarize not in TEMPLATES:
            raise HTTPException(400, f"不认识的总结类型 {body.then_summarize}")
        quality = downloads_mod.normalize_quality(body.quality, c.download.video_quality)
        info = state.probed(url)
        existing = Cache(c.cache_db).find_download(info.video_id) if info else None
        job, dup = state.downloads.submit(info, url=url, quality=quality, force=body.force,
                                          then_summarize=body.then_summarize, existing=existing)
        return {"job": job.to_dict(), "duplicate": dup}

    @app.get("/api/downloads")
    def list_downloads(limit: int = 200):
        jobs = state.downloads.list()[: max(1, min(int(limit), 500))]
        return {"jobs": [j.to_dict() for j in jobs], "config": _download_config_dict(state.fresh_config())}

    @app.get("/api/downloads/events")
    async def download_events(request: Request, after: int = 0, once: bool = False):
        """一条 SSE 看全部下载任务：每个事件是某个任务的完整快照。断线带 Last-Event-ID 续。

        once=1 只补发积压的事件就断开，给不想长连接的客户端轮询用。
        """
        last = request.headers.get("last-event-id")
        if last and last.isdigit():
            after = max(after, int(last))
        q = state.downloads.subscribe(after_seq=after)
        loop = asyncio.get_running_loop()

        async def gen():
            try:
                yield ": connected\n\n"
                while True:
                    if await request.is_disconnected():
                        return
                    if once and q.empty():
                        return
                    try:
                        ev = await loop.run_in_executor(None, q.get, True, 15)
                    except Exception:  # noqa: BLE001 —— queue.Empty
                        yield ": keepalive\n\n"
                        continue
                    payload = json.dumps(ev.to_dict(), ensure_ascii=False)
                    yield f"id: {ev.seq}\nevent: job\ndata: {payload}\n\n"
            finally:
                state.downloads.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    _THUMB_HOSTS = ("hdslb.com", "bilivideo.com", "biliimg.com", "douyinpic.com", "douyinstatic.com",
                    "byteimg.com", "bytedance.com", "ytimg.com", "googleusercontent.com")
    _THUMB_REFERERS = {"hdslb.com": "https://www.bilibili.com/", "bilivideo.com": "https://www.bilibili.com/",
                       "biliimg.com": "https://www.bilibili.com/", "douyinpic.com": "https://www.douyin.com/",
                       "douyinstatic.com": "https://www.douyin.com/", "byteimg.com": "https://www.douyin.com/"}

    @app.get("/api/downloads/thumb")
    def download_thumb(url: str):
        """封面图代理：抖音的封面带 Referer 校验，随拾页面直接 <img> 拿不到。只放行几个站点的图片 CDN。"""
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        suffix = next((h for h in _THUMB_HOSTS if host == h or host.endswith("." + h)), None)
        if parsed.scheme not in ("http", "https") or suffix is None:
            raise HTTPException(400, "不代理这个地址")
        headers = {"User-Agent": state.cfg.download.user_agent}
        if suffix in _THUMB_REFERERS:
            headers["Referer"] = _THUMB_REFERERS[suffix]
        try:
            with urlopen(UrlRequest(url, headers=headers), timeout=15) as resp:  # noqa: S310 —— 域名已白名单
                ctype = resp.headers.get("Content-Type", "image/jpeg")
                data = resp.read(3 * 1024 * 1024)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"拿不到封面：{exc}") from exc
        if not ctype.startswith("image/"):
            raise HTTPException(502, "对方返回的不是图片")
        from fastapi.responses import Response
        return Response(content=data, media_type=ctype, headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/api/downloads/{job_id}")
    def get_download(job_id: str):
        job = state.downloads.get(job_id)
        if job is None:
            raise HTTPException(404, "没有这个下载任务")
        return job.to_dict()

    @app.delete("/api/downloads/{job_id}")
    def delete_download(job_id: str):
        """排队中的取消；已结束的只删记录，不删文件。"""
        job = state.downloads.get(job_id)
        if job is None:
            raise HTTPException(404, "没有这个下载任务")
        if job.status == "queued":
            state.downloads.cancel(job_id)
            return {"cancelled": True, "job": job.to_dict()}
        if not job.finished:
            raise HTTPException(409, "正在下载的任务没法中途取消，等它下完")
        state.downloads.forget(job_id)
        Cache(state.fresh_config().cache_db).remove_download(job_id)
        return {"removed": True}

    @app.post("/api/downloads/{job_id}/open")
    def open_download(job_id: str):
        job = state.downloads.get(job_id)
        if job is None or not job.file_path or not Path(job.file_path).is_file():
            raise HTTPException(404, "文件不在了")
        path = Path(job.file_path)
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", str(path)])  # noqa: S603, S607
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(path)])  # noqa: S603, S607
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])  # noqa: S603, S607
        return {"ok": True}

    # ----- 纠专有名词 -----

    def _corrections_response(c: Config, entry: library_mod.LibraryEntry) -> dict[str, Any]:
        t = library_mod.load_transcript(c, entry, raw=True)
        return {"video_id": entry.video_id, "corrections": _corrections_dict(t)}

    @app.post("/api/videos/{video_id}/corrections")
    def run_corrections(video_id: str):
        """给一条已有的视频（重新）出替换表。进任务队列，用量按 polish 记账。"""
        c = state.fresh_config()
        entry = library_mod.find_entry(c, video_id)
        transcript = library_mod.load_transcript(c, entry, raw=True) if entry else None
        if entry is None or transcript is None:
            raise HTTPException(404, "没有这个视频的转写")
        if transcript.source_type != "asr":
            raise HTTPException(400, "官方字幕是人工的，不用纠错")
        if c.summarizer.provider == "ollama":
            raise HTTPException(400, "本地小模型纠专有名词不靠谱，换一个在线模型再试")
        dup = state.jobs.find_active("polish", video_id=video_id)
        if dup is not None:
            return {"job": dup.to_dict(), "duplicate": True}
        provider = get_summarizer(c.summarizer)

        def work(rep: Reporter) -> dict[str, Any]:
            rep.stage("polish", f"调用 {provider.describe()} 纠专有名词")
            cache = Cache(c.cache_db)
            fixed = correction_mod.polish(provider, transcript, title=entry.title, uploader=entry.uploader,
                                          terms=cache.get_terms(entry.uploader))
            library_mod.save_transcript(c, entry, fixed)
            cache.learn_terms(entry.uploader, correction_mod.items_of(fixed), video_id=video_id)
            search_mod.index_one(c, video_id)
            used = _record_usage(c, provider, video_id=video_id, kind="polish", detail=None)
            return {**_corrections_response(c, entry), "used": used}

        job = state.jobs.submit("polish", f"纠专有名词 · {entry.title}", {"video_id": video_id}, work)
        return {"job": job.to_dict(), "duplicate": False}

    class CorrectionStateBody(BaseModel):
        state: str      # applied | rejected

    @app.patch("/api/videos/{video_id}/corrections/{index}")
    def set_correction_state(video_id: str, index: int, body: CorrectionStateBody):
        """否决或恢复一条替换。即时生效：阅读视图、搜索索引、总结缓存 key 都跟着变。"""
        c = state.fresh_config()
        entry = library_mod.find_entry(c, video_id)
        transcript = library_mod.load_transcript(c, entry, raw=True) if entry else None
        if entry is None or transcript is None:
            raise HTTPException(404, "没有这个视频的转写")
        try:
            fixed = correction_mod.set_state(transcript, index, body.state)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except IndexError:
            raise HTTPException(404, "没有这一条")
        library_mod.save_transcript(c, entry, fixed)
        # 否决/恢复同步到词表：这个 UP 主（及同组）之后的视频跟着变
        item = correction_mod.items_of(fixed)[index]
        Cache(c.cache_db).set_term_state(entry.uploader, item.src, item.dst, item.state)
        search_mod.index_one(c, video_id)
        return _corrections_response(c, entry)

    # ----- 词表 -----

    @app.get("/api/terms")
    def list_terms(uploader: str = ""):
        """某个 UP 主（含同组）攒下的专名词表。不传 uploader 是"没有 UP 主信息"那一堆的。"""
        c = state.fresh_config()
        cache = Cache(c.cache_db)
        return {
            "uploader": uploader,
            "scope": cache.term_scope(uploader),
            "terms": [t.to_dict() for t in cache.get_terms(uploader)],
        }

    class TermBody(BaseModel):
        uploader: str = ""
        src: str
        dst: str

    @app.delete("/api/terms")
    def delete_term(body: TermBody):
        c = state.fresh_config()
        return {"deleted": Cache(c.cache_db).delete_term(body.uploader, body.src, body.dst)}

    # ----- 问视频 -----

    @app.get("/api/videos/{video_id}/questions")
    def list_questions(video_id: str):
        c = state.fresh_config()
        entry = library_mod.find_entry(c, video_id)
        transcript = library_mod.load_transcript(c, entry) if entry else None
        if entry is None or transcript is None:
            raise HTTPException(404, "没有这个视频的转写")
        try:
            provider = get_summarizer(c.summarizer)
            p = qa_mod.plan(provider, transcript)
            estimate = {**p, "cost": _money(c, p["input_tokens"], 600), "currency": c.summarizer.currency,
                        "provider": provider.describe()}
        except VideoSummarizerError as exc:
            estimate = {"error": str(exc)}
        return {
            "estimate": estimate,
            "questions": [
                {"id": q.id, "question": q.question, "answer": q.answer, "provider": q.provider,
                 "citations": q.citations, "created_at": q.created_at}
                for q in Cache(c.cache_db).questions_for_video(video_id)
            ],
        }

    class AskBody(BaseModel):
        question: str
        # 前端把最近几轮传回来，服务端不维护会话
        history: list[dict[str, str]] = []

    @app.post("/api/videos/{video_id}/ask")
    def ask_video(video_id: str, body: AskBody):
        """同步调用，几秒到十几秒。不进任务队列：不该排在一个 20 分钟的 ASR 后面。"""
        question = body.question.strip()
        if not question:
            raise HTTPException(400, "先写个问题")
        c = state.fresh_config()
        entry = library_mod.find_entry(c, video_id)
        transcript = library_mod.load_transcript(c, entry) if entry else None
        if entry is None or transcript is None:
            raise HTTPException(404, "没有这个视频的转写")
        provider = get_summarizer(c.summarizer)
        history = [qa_mod.Turn(h.get("question", ""), h.get("answer", "")) for h in body.history
                   if h.get("question") and h.get("answer")]
        if c.summarizer.clean_transcript:
            transcript = cleaning.clean_transcript(transcript)
        answer = qa_mod.ask(provider, transcript, question, history)
        qid = Cache(c.cache_db).add_question(video_id, answer.question, answer.answer,
                                             answer.provider, answer.citations)
        used = _record_usage(c, provider, video_id=video_id, kind="qa", detail=question[:200])
        return {"id": qid, **answer.to_dict(), "used": used,
                "cost": used["cost"] if used["calls"] else _money(c, answer.input_tokens, 600),
                "currency": c.summarizer.currency}

    @app.delete("/api/questions/{question_id}")
    def delete_question(question_id: int):
        c = state.fresh_config()
        return {"deleted": Cache(c.cache_db).delete_question(question_id)}

    # ----- 问 UP 主（跨视频） -----

    def _uploader_materials(c: Config, uploader: str) -> tuple[list[qa_mod.Material], list[dict[str, Any]]]:
        entries = [e for e in library_mod.load_library(c) if e.uploader == uploader]
        # 按发布日期升序，模型才好说"先说 X 后来改口 Y"
        entries.sort(key=lambda e: (e.upload_date or "", e.created_at))
        materials, videos = [], []
        for i, e in enumerate(entries, 1):
            mat = library_mod.material_text(c, e)
            if mat is None:
                continue
            kind, text = mat
            materials.append(qa_mod.Material(index=i, video_id=e.video_id, title=e.title,
                                             date=_fmt_date(e.upload_date), kind=kind, text=text))
            videos.append({"index": i, "video_id": e.video_id, "title": e.title, "remark": e.remark,
                           "upload_date": _fmt_date(e.upload_date),
                           "duration_sec": e.duration_sec, "material": kind,
                           "tokens": qa_mod.estimate_tokens(text)})
        return materials, videos

    @app.get("/api/uploaders/groups")
    def uploader_groups():
        c = state.fresh_config()
        cache = Cache(c.cache_db)
        return {
            "groups": cache.list_uploader_groups(),
            "mapping": cache.get_uploader_groups(),
        }

    class RenameGroupBody(BaseModel):
        from_name: str
        to_name: str

    @app.post("/api/uploaders/groups/rename")
    def rename_uploader_group_endpoint(body: RenameGroupBody):
        c = state.fresh_config()
        cache = Cache(c.cache_db)
        n = cache.rename_uploader_group(body.from_name, body.to_name)
        return {"renamed": n, "groups": cache.list_uploader_groups()}

    @app.delete("/api/uploaders/groups/{name}")
    def delete_uploader_group_endpoint(name: str):
        c = state.fresh_config()
        cache = Cache(c.cache_db)
        n = cache.delete_uploader_group(name)
        return {"deleted": n, "groups": cache.list_uploader_groups()}

    class UploaderGroupBody(BaseModel):
        group: str | None = None

    @app.put("/api/uploaders/{name}/group")
    def set_uploader_group_endpoint(name: str, body: UploaderGroupBody):
        c = state.fresh_config()
        cache = Cache(c.cache_db)
        cache.set_uploader_group(name, body.group)
        return {
            "uploader": name,
            "group": cache.get_uploader_group(name),
            "groups": cache.list_uploader_groups(),
        }

    @app.get("/api/uploaders/{name}")
    def uploader_info(name: str):
        c = state.fresh_config()
        materials, videos = _uploader_materials(c, name)
        if not videos and not any(e.uploader == name for e in library_mod.load_library(c)):
            raise HTTPException(404, "库里没有这位创作者")
        total = sum(v["tokens"] for v in videos) + 400
        try:
            provider_desc = get_summarizer(c.summarizer).describe()
        except VideoSummarizerError as exc:
            provider_desc = f"未配置（{exc}）"
        cache = Cache(c.cache_db)
        qs = cache.questions_for_video(f"uploader:{name}")
        group = cache.get_uploader_group(name)
        return {
            "uploader": name,
            "group": group,
            "videos": videos,
            "no_summary": sum(1 for v in videos if v["material"] == "transcript"),
            "estimate": {"input_tokens": total, "cost": _money(c, total, 800), "currency": c.summarizer.currency,
                         "provider": provider_desc},
            "questions": [
                {"id": q.id, "question": q.question, "answer": q.answer, "provider": q.provider,
                 "citations": q.citations, "created_at": q.created_at}
                for q in qs
            ],
        }

    @app.get("/api/uploaders/{name}/avatar")
    def uploader_avatar(name: str):
        """头像图片。本地有就直接出；没有就去站点拿一次存下来；拿不到 404，前端退回首字母。"""
        c = state.fresh_config()
        path = avatars_mod.cached(c, name)
        if path is None:
            entries = [e for e in library_mod.load_library(c) if e.uploader == name]
            if not entries:
                raise HTTPException(404, "库里没有这位创作者")
            path = avatars_mod.fetch(c, name, entries)
        if path is None:
            raise HTTPException(404, "拿不到头像")
        return FileResponse(path, headers={"Cache-Control": "private, max-age=604800"})

    @app.post("/api/uploaders/{name}/ask")
    def uploader_ask(name: str, body: AskBody):
        question = body.question.strip()
        if not question:
            raise HTTPException(400, "先写个问题")
        c = state.fresh_config()
        materials, _videos = _uploader_materials(c, name)
        if not materials:
            raise HTTPException(404, "这位创作者还没有可用的材料")
        provider = get_summarizer(c.summarizer)
        history = [qa_mod.Turn(h.get("question", ""), h.get("answer", "")) for h in body.history
                   if h.get("question") and h.get("answer")]
        answer = qa_mod.ask_uploader(provider, name, materials, question, history)
        key = f"uploader:{name}"
        qid = Cache(c.cache_db).add_question(key, answer.question, answer.answer, answer.provider, answer.citations)
        used = _record_usage(c, provider, video_id=key, kind="uploader_qa", detail=question[:200])
        return {"id": qid, **answer.to_dict(), "used": used,
                "cost": used["cost"] if used["calls"] else _money(c, answer.input_tokens, 800),
                "currency": c.summarizer.currency}

    # ----- 标签 -----

    def _auto_tag(c: Config, video_id: str, rep: Reporter, provider=None) -> list[str]:
        """任务收尾时顺手打标签。只给还没打过的视频打；失败不能拖垮主任务。"""
        if not c.summarizer.auto_tags or video_id in Cache(c.cache_db).videos_with_ai_tags():
            return []
        entry = library_mod.find_entry(c, video_id)
        if entry is None:
            return []
        try:
            rep.stage("tags", "生成标签")
            return _tag_video(c, entry, provider=provider)
        except Exception as exc:  # noqa: BLE001 —— 标签是附赠的，出错只记日志
            rep.log("warning", f"打标签失败：{exc}")
            return []

    @app.get("/api/tags")
    def list_tags():
        c = state.fresh_config()
        cache = Cache(c.cache_db)
        tagged = cache.videos_with_ai_tags()
        untagged = [e.video_id for e in library_mod.load_library(c) if e.video_id not in tagged]
        return {"tags": [{"tag": t, "count": n} for t, n in cache.tag_counts()], "untagged": len(untagged)}

    class TagBody(BaseModel):
        tag: str

    @app.post("/api/videos/{video_id}/tags")
    def add_tag(video_id: str, body: TagBody):
        tag = tagging_mod.normalize(body.tag)
        if not tag:
            raise HTTPException(400, "标签不能为空，也别超过 20 个字")
        c = state.fresh_config()
        if library_mod.find_entry(c, video_id) is None:
            raise HTTPException(404, "没有这个视频")
        cache = Cache(c.cache_db)
        cache.add_tag(video_id, tag, "user")
        return {"tags": _tags_list(cache.tags_for_video(video_id))}

    @app.delete("/api/videos/{video_id}/tags/{tag}")
    def remove_tag(video_id: str, tag: str):
        c = state.fresh_config()
        cache = Cache(c.cache_db)
        removed = cache.remove_tag(video_id, tag)
        return {"removed": removed, "tags": _tags_list(cache.tags_for_video(video_id))}

    @app.post("/api/videos/{video_id}/tags/generate")
    def generate_tags(video_id: str):
        """同步调用，一两秒。会替换上次 AI 打的，用户加的和删过的不动。"""
        c = state.fresh_config()
        entry = library_mod.find_entry(c, video_id)
        if entry is None:
            raise HTTPException(404, "没有这个视频")
        provider = get_summarizer(c.summarizer)
        written = _tag_video(c, entry, provider=provider)
        cache = Cache(c.cache_db)
        return {"generated": written, "tags": _tags_list(cache.tags_for_video(video_id)),
                "provider": provider.describe()}

    @app.post("/api/tags/backfill")
    def backfill_tags():
        """给库里还没打过标签的视频挨个打一遍。进任务队列，一条一次调用。"""
        c = state.fresh_config()
        dup = state.jobs.find_active("tags")
        if dup is not None:
            return {"job": dup.to_dict(), "duplicate": True, "count": dup.params.get("count", 0)}
        tagged = Cache(c.cache_db).videos_with_ai_tags()
        todo = [e for e in library_mod.load_library(c) if e.video_id not in tagged]
        if not todo:
            return {"job": None, "duplicate": False, "count": 0}
        provider = get_summarizer(c.summarizer)

        def work(rep: Reporter) -> dict[str, Any]:
            done, failed = 0, 0
            for i, e in enumerate(todo):
                if rep.job.status == "cancelled":
                    break
                rep.stage("tags", f"{i + 1}/{len(todo)} {e.title}")
                rep.progress(i / len(todo))
                try:
                    _tag_video(c, e, provider=provider)
                    done += 1
                except Exception as exc:  # noqa: BLE001 —— 一条失败继续下一条
                    failed += 1
                    rep.log("warning", f"「{e.title}」打标签失败：{exc}")
            return {"tagged": done, "failed": failed}

        job = state.jobs.submit("tags", f"补标签 · {len(todo)} 条视频", {"count": len(todo)}, work)
        return {"job": job.to_dict(), "duplicate": False, "count": len(todo)}

    # ----- 回顾 -----

    def _review_materials(c: Config, period: str, key: str) -> tuple[list[digest_mod.Material], list[dict[str, Any]]]:
        entries = [e for e in library_mod.load_library(c) if _in_period(period, key, e)]
        entries.sort(key=lambda e: e.created_at)
        tags = Cache(c.cache_db).tags_by_video()
        materials, videos = [], []
        for i, e in enumerate(entries, 1):
            vtags = [t.tag for t in tags.get(e.video_id, [])]
            mat = library_mod.material_text(c, e)
            kind, text = mat if mat else ("none", "")
            if mat:
                materials.append(digest_mod.Material(
                    index=i, video_id=e.video_id, title=e.title, uploader=e.uploader,
                    date=digest_mod.local_date(e.created_at).isoformat(), tags=vtags, kind=kind, text=text))
            videos.append({"index": i, "video_id": e.video_id, "title": e.title, "remark": e.remark,
                           "uploader": e.uploader, "thumbnail": e.thumbnail,
                           "duration_sec": e.duration_sec, "created_at": e.created_at,
                           "tags": vtags, "material": kind, "tokens": qa_mod.estimate_tokens(text)})
        return materials, videos

    def _digest_dict(d, current_ids: set[str]) -> dict[str, Any]:
        return {"content": d.content, "provider": d.provider, "created_at": d.created_at,
                "video_ids": d.video_ids, "stale": set(d.video_ids) != current_ids,
                "new_count": len(current_ids - set(d.video_ids))}

    @app.get("/api/review")
    def review(period: str = "week", key: str = ""):
        if period not in digest_mod.PERIODS:
            raise HTTPException(400, "period 只能是 week 或 day")
        c = state.fresh_config()
        today = digest_mod.local_date(_now_iso())
        current = digest_mod.period_key(period, today)
        key = key or current
        try:
            start, end = digest_mod.period_range(period, key)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        cache = Cache(c.cache_db)
        materials, videos = _review_materials(c, period, key)
        ids = {v["video_id"] for v in videos}
        d = cache.get_digest(period, key)
        tokens = digest_mod.plan_tokens(period, key, materials) if materials else 0
        try:
            provider_desc = get_summarizer(c.summarizer).describe()
        except VideoSummarizerError as exc:
            provider_desc = f"未配置（{exc}）"
        # 有视频的周期列表（含当前周期），前端据此翻页
        counts: dict[str, int] = {current: 0}
        for e in library_mod.load_library(c):
            k = digest_mod.period_key(period, digest_mod.local_date(e.created_at))
            counts[k] = counts.get(k, 0) + 1
        generated = cache.digests_for(period)
        periods = [{"key": k, "label": digest_mod.period_label(period, k), "videos": n, "generated": k in generated}
                   for k, n in sorted(counts.items(), reverse=True)]
        return {
            "period": period, "key": key, "current": current, "label": digest_mod.period_label(period, key),
            "range": {"start": start.isoformat(), "end": end.isoformat()},
            "periods": periods,
            "videos": videos,
            "no_material": sum(1 for v in videos if v["material"] == "none"),
            "digest": _digest_dict(d, ids) if d else None,
            "estimate": {"input_tokens": tokens, "cost": _money(c, tokens, 800) if materials else None,
                         "currency": c.summarizer.currency, "provider": provider_desc},
        }

    class ReviewBody(BaseModel):
        period: str = "week"
        key: str = ""
        force: bool = False

    @app.post("/api/review/generate")
    def review_generate(body: ReviewBody):
        """同步调用，几秒。没新视频且已有一份时直接返回旧的，除非 force。"""
        if body.period not in digest_mod.PERIODS:
            raise HTTPException(400, "period 只能是 week 或 day")
        c = state.fresh_config()
        key = body.key or digest_mod.period_key(body.period, digest_mod.local_date(_now_iso()))
        cache = Cache(c.cache_db)
        materials, videos = _review_materials(c, body.period, key)
        ids = {v["video_id"] for v in videos}
        existing = cache.get_digest(body.period, key)
        if existing and not body.force and set(existing.video_ids) == ids:
            return {"digest": _digest_dict(existing, ids), "cached": True}
        if not materials:
            raise HTTPException(404, "这段时间没有可用的材料")
        provider = get_summarizer(c.summarizer)
        content, _cited = digest_mod.generate(provider, body.period, key, materials)
        cache.put_digest(body.period, key, sorted(ids), content, provider.describe())
        used = _record_usage(c, provider, video_id=f"review:{body.period}:{key}", kind="review", detail=key)
        d = cache.get_digest(body.period, key)
        return {"digest": _digest_dict(d, ids), "cached": False, "used": used}

    # ----- 搜索 -----

    @app.get("/api/search")
    def search(q: str = "", limit: int = 200):
        """全库搜转写和总结。零 LLM 成本。结果按视频分组，每组带命中的段落和总结行。"""
        c = state.fresh_config()
        q = q.strip()
        if not q:
            return {"query": q, "videos": [], "transcript_hits": 0, "summary_hits": 0}
        hits = search_mod.SearchIndex(c.cache_db).search(q, limit=max(1, min(limit, 1000)))
        by_video: dict[str, list[search_mod.Hit]] = {}
        for h in hits:
            by_video.setdefault(h.video_id, []).append(h)
        entries = {e.video_id: e for e in library_mod.load_library(c)}
        videos = []
        for vid, group in by_video.items():
            entry = entries.get(vid)
            if entry is None:
                continue
            videos.append({
                "video_id": vid, "title": entry.title, "remark": entry.remark, "uploader": entry.uploader,
                "thumbnail": entry.thumbnail, "duration_sec": entry.duration_sec,
                "hits": [h.to_dict() for h in group],
            })
        # 命中多的视频排前面
        videos.sort(key=lambda v: len(v["hits"]), reverse=True)
        return {
            "query": q, "videos": videos,
            "transcript_hits": sum(1 for h in hits if h.kind == "transcript"),
            "summary_hits": sum(1 for h in hits if h.kind == "summary"),
        }

    @app.post("/api/search/reindex")
    def reindex():
        videos, rows = search_mod.sync_index(state.fresh_config(), force=True)
        return {"videos": videos, "rows": rows}

    # ----- 静态前端 -----

    if (DIST_DIR / "index.html").is_file():
        app.mount("/assets", StaticFiles(directory=DIST_DIR / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            candidate = DIST_DIR / path
            if path and candidate.is_file() and ".." not in path:
                return FileResponse(candidate)
            # index.html 不能让浏览器缓存，否则前端更新后还会引用旧的 assets
            return FileResponse(DIST_DIR / "index.html", headers={"Cache-Control": "no-cache"})
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
