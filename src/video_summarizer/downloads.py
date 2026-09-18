"""「存视频」：把整条视频合流成 mp4 落到本地，给随拾（local-video-browser）那边看。

拾光笺自己只留音频和转写，视频文件是给随拾的。两边靠文件系统松耦合：
mp4 旁边写一个精简的 `<同名>.suishi.json`（来源链接、作者、完整标题），随拾遍历目录时
顺手读它，就能显示来源徽标、跳回原链接、或者把这条视频送回拾光笺转写。

队列独立于 ASR 任务队列——下载不该排在半小时的转写后面——但相邻两次下载之间仍按
`batch_delay_sec` 隔开，B 站 412 是按 IP 算的。记录落 cache.sqlite 的 downloads 表，
服务重启时没跑完的重新入队，yt-dlp 自己会接着 .part 续传。
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .config import VIDEO_QUALITIES, Config, DownloadConfig
from .errors import DownloadError
from .ytdlp_base import VideoInfo, build_ydl_opts, download, probe

log = logging.getLogger(__name__)

SIDECAR_SUFFIX = ".suishi.json"
SIDECAR_SCHEMA = 1
# 进度事件最多这么密，随拾那边一条 SSE 连接看全部任务，别刷屏
PROGRESS_MIN_INTERVAL_SEC = 0.5
MAX_KEPT_JOBS = 200
MAX_KEPT_EVENTS = 500

# 文件名：标题截到 80 字节（Windows 路径有 260 的坎，作者目录再吃一层），id 放方括号里方便随拾按 id 找文件
OUTTMPL = "%(extractor_key)s/%(channel,uploader,uploader_id|未知作者).40B/%(title).80B [%(id)s].%(ext)s"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _local_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------- 纯函数：格式选择、sidecar ----------


def normalize_quality(value: Any, default: Any = 1080) -> str:
    """接受 1080 / "1080" / "1080p" / "best"；不认识的退回默认值。"""
    s = str(value if value is not None else default).strip().lower().rstrip("p")
    return s if s in VIDEO_QUALITIES else normalize_quality(default, "best")


def format_selector(quality: str, prefer_h264: bool = True) -> str:
    """yt-dlp 的 -f 表达式。

    优先 mp4 视频轨 + m4a 音频轨（合流后 Chrome 原生能播），其次任何能合流的组合，
    再兜底单流。prefer_h264 时先试 avc1 编码，B 站高码率常是 HEVC/AV1，浏览器不一定解得开。
    """
    h = f"[height<={quality}]" if quality != "best" else ""
    parts: list[str] = []
    if prefer_h264:
        parts.append(f"bestvideo{h}[vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]")
        parts.append(f"bestvideo{h}[vcodec^=avc1]+bestaudio")
    parts.append(f"bestvideo{h}[ext=mp4]+bestaudio[ext=m4a]")
    parts.append(f"bestvideo{h}+bestaudio")
    parts.append(f"best{h}")
    parts.append("best")
    return "/".join(parts)


def sidecar_path_for(media: Path) -> Path:
    """`标题 [id].mp4` -> `标题 [id].suishi.json`。只换最后一个后缀，标题里的点不受影响。"""
    return media.with_name(media.stem + SIDECAR_SUFFIX)


def available_heights(info: VideoInfo) -> list[int]:
    """探测结果里有哪些视频高度可选，从高到低。没 formats 字段就空。"""
    heights: set[int] = set()
    for f in info.raw.get("formats") or []:
        if not isinstance(f, dict):
            continue
        if f.get("vcodec") in (None, "none"):
            continue
        h = f.get("height")
        if isinstance(h, (int, float)) and h > 0:
            heights.add(int(h))
    return sorted(heights, reverse=True)


def build_sidecar(info: VideoInfo, *, quality: str, file_path: Path) -> dict[str, Any]:
    raw = info.raw or {}
    desc = (raw.get("description") or "").strip()
    uploader_url = raw.get("channel_url") or raw.get("uploader_url")
    if not uploader_url and "bilibili" in (info.extractor or "").lower() and raw.get("uploader_id"):
        uploader_url = f"https://space.bilibili.com/{raw['uploader_id']}"
    return {
        "schema": SIDECAR_SCHEMA,
        "site": info.extractor,
        "video_id": info.video_id,
        "url": info.url,
        "title": info.title,
        "uploader": info.uploader,
        "uploader_url": uploader_url,
        "upload_date": info.upload_date,
        "duration_sec": info.duration_sec,
        "description": desc[:500],
        "thumbnail_url": info.thumbnail,
        "quality": quality,
        "file_name": file_path.name,
        "downloaded_at": _local_now(),
        "downloaded_by": f"glean {__version__}",
    }


def write_sidecar(media: Path, data: dict[str, Any]) -> Path:
    p = sidecar_path_for(media)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def overall_progress(hook: dict[str, Any]) -> float | None:
    """yt-dlp 的进度回调 -> 整条任务的 0~1。

    视频轨 + 音频轨是两次独立下载，各自从 0 跑到 1；这里按 requested_formats 里各轨的
    体积加权拼成一条。体积不全就按轨数平均。算不出来返回 None。
    """
    if hook.get("status") != "downloading":
        return None
    total = hook.get("total_bytes") or hook.get("total_bytes_estimate")
    done = hook.get("downloaded_bytes")
    if not total or done is None:
        return None
    frac = max(0.0, min(1.0, done / total))
    info = hook.get("info_dict") or {}
    fmts = info.get("requested_formats")
    if not isinstance(fmts, list) or len(fmts) < 2:
        return frac
    cur = info.get("format_id")
    idx = next((i for i, f in enumerate(fmts) if isinstance(f, dict) and f.get("format_id") == cur), None)
    if idx is None:
        return frac
    sizes = [(f.get("filesize") or f.get("filesize_approx") or 0) if isinstance(f, dict) else 0 for f in fmts]
    if any(s <= 0 for s in sizes):
        sizes = [1.0] * len(fmts)
    weight_total = float(sum(sizes))
    before = float(sum(sizes[:idx]))
    return max(0.0, min(1.0, (before + frac * sizes[idx]) / weight_total))


# ---------- 任务 ----------


STATUSES = ("queued", "downloading", "merging", "done", "failed", "cancelled")


@dataclass
class DownloadJob:
    id: str
    url: str
    video_id: str
    title: str
    quality: str
    extractor: str = "unknown"
    uploader: str | None = None
    thumbnail: str | None = None
    duration_sec: float = 0.0
    upload_date: str | None = None
    status: str = "queued"
    progress: float = 0.0
    speed: str | None = None
    eta_sec: int | None = None
    note: str | None = None            # 给界面看的一句话，比如"正在用本机浏览器取签名"
    file_path: str | None = None
    sidecar_path: str | None = None
    error: str | None = None
    then_summarize: str | None = None  # 下完自动送去转写的总结类型；"" = 只转写；None = 不送
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None

    @property
    def finished(self) -> bool:
        return self.status in ("done", "failed", "cancelled")

    @property
    def active(self) -> bool:
        return self.status in ("queued", "downloading", "merging")

    def try_heal(self, video_dir: Path | None = None, *, force: bool = False) -> bool:
        """如果记录里的 file_path 物理文件不存在，但在同级目录或 video_dir 中能通过
        .suishi.json 的 video_id / url 匹配到重命名后的新文件，则自动修复路径和标题。
        """
        if self.file_path and Path(self.file_path).is_file():
            return True

        # 节流：针对找不到文件的任务，非强制时每秒最多扫描一次，避免频繁磁盘 IO
        now = time.monotonic()
        last_check = getattr(self, "_last_heal_check", 0.0)
        if not force and now - last_check < 1.0:
            return False
        self._last_heal_check = now

        dirs_to_check: list[Path] = []
        if self.file_path:
            parent = Path(self.file_path).parent
            if parent.is_dir():
                dirs_to_check.append(parent)
        if video_dir and video_dir.is_dir() and video_dir not in dirs_to_check:
            dirs_to_check.append(video_dir)

        candidate_sidecars: list[Path] = []
        for d in dirs_to_check:
            try:
                candidate_sidecars.extend(d.glob(f"*{SIDECAR_SUFFIX}"))
            except OSError:
                pass
        if not candidate_sidecars and video_dir and video_dir.is_dir():
            try:
                candidate_sidecars.extend(video_dir.rglob(f"*{SIDECAR_SUFFIX}"))
            except OSError:
                pass

        vid = self.video_id
        url = self.url
        for s_path in candidate_sidecars:
            try:
                text = s_path.read_text(encoding="utf-8")
                if vid and vid not in text and url not in text:
                    continue
                meta = json.loads(text)
            except Exception:
                continue

            if not isinstance(meta, dict):
                continue

            if (vid and meta.get("video_id") == vid) or (url and meta.get("url") == url):
                stem = s_path.name[: -len(SIDECAR_SUFFIX)]
                parent = s_path.parent
                orig_suffix = Path(self.file_path).suffix if self.file_path else ".mp4"
                preferred = parent / f"{stem}{orig_suffix}"
                media_path: Path | None = preferred if preferred.is_file() else None
                if media_path is None:
                    for ext in (".mp4", ".mkv", ".webm", ".mov", ".flv", ".avi"):
                        cand = parent / f"{stem}{ext}"
                        if cand.is_file():
                            media_path = cand
                            break
                if media_path is None:
                    try:
                        for cand in parent.glob(f"{stem}.*"):
                            if cand.is_file() and not cand.name.endswith(SIDECAR_SUFFIX):
                                media_path = cand
                                break
                    except OSError:
                        pass

                if media_path is not None:
                    self.file_path = str(media_path)
                    self.sidecar_path = str(s_path)
                    if meta.get("title"):
                        self.title = meta["title"]
                    return True

        return False

    def to_dict(self, video_dir: Path | None = None) -> dict[str, Any]:
        d = asdict(self)
        exists = bool(self.file_path and Path(self.file_path).is_file())
        if not exists and self.status == "done":
            if self.try_heal(video_dir):
                exists = True
                d["file_path"] = self.file_path
                d["sidecar_path"] = self.sidecar_path
                d["title"] = self.title
        d["file_exists"] = exists
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DownloadJob":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class DownloadEvent:
    seq: int
    job: dict[str, Any]
    ts: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "ts": self.ts, "job": self.job}


class DownloadManager:
    """单 worker 串行下载；所有任务的变化汇成一条事件流。

    `get_config` 每次任务重新读配置（改了目录、清晰度不用重启）；`throttle` / `touched`
    是 State 上的那两个，和转写任务共用"上次碰站点的时间"。
    """

    def __init__(
        self,
        get_config: Callable[[], Config],
        *,
        throttle: Callable[[float], None] | None = None,
        touched: Callable[[], None] | None = None,
        on_done: Callable[[DownloadJob], None] | None = None,
        persist: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._get_config = get_config
        self._throttle = throttle or (lambda _d: None)
        self._touched = touched or (lambda: None)
        self._on_done = on_done
        self._persist = persist or (lambda _row: None)
        self._jobs: OrderedDict[str, DownloadJob] = OrderedDict()
        self._infos: dict[str, VideoInfo] = {}
        self._queue: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._events: list[DownloadEvent] = []
        self._subscribers: list[queue.Queue] = []
        self._seq = 0
        self._worker = threading.Thread(target=self._loop, name="vsum-downloads", daemon=True)
        self._worker.start()

    # ---------- 提交 / 查询 ----------

    def submit(
        self,
        info: VideoInfo | None,
        *,
        url: str,
        quality: str,
        force: bool = False,
        then_summarize: str | None = None,
        existing: dict[str, Any] | None = None,
        restore: dict[str, Any] | None = None,
    ) -> tuple[DownloadJob, bool]:
        """入队。返回 (任务, 是否重复)。

        `existing` 是库里同一 video_id 的旧记录：文件还在且没 force 就不重下；
        `restore` 是服务重启时从库里捞出来的没跑完的行，沿用它的 id。
        """
        if restore is not None:
            job = DownloadJob.from_dict(restore)
            job.status, job.progress, job.speed, job.eta_sec, job.note, job.error = "queued", 0.0, None, None, None, None
            job.started_at = job.finished_at = None
        else:
            video_id = info.video_id if info else ""
            with self._lock:
                for j in self._jobs.values():
                    if j.active and (j.url == url or (video_id and j.video_id == video_id)):
                        return j, True
            if existing and not force and existing.get("status") == "done":
                fp = existing.get("file_path")
                if fp and Path(fp).is_file():
                    job = DownloadJob.from_dict(existing)
                    with self._lock:
                        self._jobs.setdefault(job.id, job)
                    return self._jobs[job.id], True
            job = DownloadJob(
                id=uuid.uuid4().hex[:12], url=url, quality=quality,
                video_id=video_id or url, title=info.title if info else url,
                extractor=info.extractor if info else "unknown",
                uploader=info.uploader if info else None,
                thumbnail=info.thumbnail if info else None,
                duration_sec=info.duration_sec if info else 0.0,
                upload_date=info.upload_date if info else None,
                then_summarize=then_summarize,
            )
        with self._lock:
            self._jobs[job.id] = job
            if info is not None:
                self._infos[job.id] = info
            self._trim()
        self._changed(job)
        self._queue.put(job.id)
        return job, False

    def get(self, job_id: str) -> DownloadJob | None:
        return self._jobs.get(job_id)

    def heal_and_persist(self, job: DownloadJob, video_dir: Path | None = None, *, force: bool = False) -> bool:
        """尝试自愈任务的文件路径。如果路径或元数据修复，触发变更通知并持久化。"""
        if job.try_heal(video_dir, force=force):
            self._changed(job, persist=True)
            return True
        return False

    def to_dict(self, job: DownloadJob, video_dir: Path | None = None) -> dict[str, Any]:
        """导出任务字典。如果文件不存在且自愈成功，自动触发持久化与广播。"""
        exists = bool(job.file_path and Path(job.file_path).is_file())
        if not exists and job.status == "done":
            if self.heal_and_persist(job, video_dir):
                exists = True
        d = job.to_dict(video_dir=None)
        d["file_exists"] = exists
        return d

    def list(self) -> list[DownloadJob]:
        """进行中的在前（按排队先后），其余按创建时间倒序。"""
        jobs = list(self._jobs.values())
        active = sorted((j for j in jobs if j.active), key=lambda j: j.created_at)
        rest = sorted((j for j in jobs if not j.active), key=lambda j: j.created_at, reverse=True)
        return active + rest

    def find_by_video(self, video_id: str) -> DownloadJob | None:
        for j in self._jobs.values():
            if j.video_id == video_id:
                return j
        return None

    def cancel(self, job_id: str) -> bool:
        """只能取消还没开始的；正在下的让它下完。"""
        job = self._jobs.get(job_id)
        if job is None or job.status != "queued":
            return False
        job.status = "cancelled"
        job.finished_at = _now()
        self._changed(job)
        return True

    def forget(self, job_id: str) -> bool:
        """从内存里删掉一条已结束的记录（库里的由调用方删）。"""
        job = self._jobs.get(job_id)
        if job is None or not job.finished:
            return False
        with self._lock:
            self._jobs.pop(job_id, None)
            self._infos.pop(job_id, None)
        return True

    def load(self, rows: list[dict[str, Any]]) -> None:
        """启动时把库里的历史记录塞进内存（已结束的只展示，没跑完的重新入队）。"""
        for row in rows[::-1]:
            try:
                job = DownloadJob.from_dict(row)
            except TypeError:
                continue
            if job.active:
                self.submit(None, url=job.url, quality=job.quality, restore=row)
            else:
                with self._lock:
                    self._jobs[job.id] = job

    # ---------- 事件流 ----------

    def subscribe(self, after_seq: int = 0) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            for ev in self._events:
                if ev.seq > after_seq:
                    q.put(ev)
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def _changed(self, job: DownloadJob, *, persist: bool = True) -> None:
        """广播一次快照。进度事件很密，不必每次都落库，状态变化才写。"""
        snapshot = job.to_dict()
        with self._lock:
            self._seq += 1
            ev = DownloadEvent(seq=self._seq, job=snapshot)
            self._events.append(ev)
            if len(self._events) > MAX_KEPT_EVENTS:
                del self._events[: len(self._events) - MAX_KEPT_EVENTS]
            subs = list(self._subscribers)
        for q in subs:
            q.put(ev)
        if not persist:
            return
        try:
            self._persist(snapshot)
        except Exception as exc:  # noqa: BLE001 —— 记录失败不影响下载
            log.warning("下载记录没写进库：%s", exc)

    def _trim(self) -> None:
        while len(self._jobs) > MAX_KEPT_JOBS:
            for jid, job in self._jobs.items():
                if job.finished:
                    del self._jobs[jid]
                    self._infos.pop(jid, None)
                    break
            else:
                return

    # ---------- worker ----------

    def _loop(self) -> None:
        while True:
            job_id = self._queue.get()
            job = self._jobs.get(job_id)
            if job is None or job.status != "queued":
                continue
            self._run(job)

    def _run(self, job: DownloadJob) -> None:
        cfg = self._get_config()
        job.status = "downloading"
        job.started_at = _now()
        job.note = "准备中"
        self._changed(job)
        try:
            if not cfg.download.video_dir:
                raise DownloadError("config.yaml 里没有配置 download.video_dir，不知道该把视频存到哪")
            video_dir = Path(cfg.download.video_dir)
            video_dir.mkdir(parents=True, exist_ok=True)

            self._throttle(cfg.download.batch_delay_sec)
            self._touched()
            info = self._infos.get(job.id)
            if info is None:
                job.note = "正在探测链接"
                self._changed(job)
                info = probe(job.url, cfg.download)
                job.video_id, job.title, job.extractor = info.video_id, info.title, info.extractor
                job.uploader, job.thumbnail = info.uploader, info.thumbnail
                job.duration_sec, job.upload_date = info.duration_sec, info.upload_date

            job.note = None
            final = self._download(job, info, cfg.download, video_dir)
            job.status = "merging"
            job.progress = 1.0
            job.speed, job.eta_sec = None, None
            self._changed(job)

            sidecar = write_sidecar(final, build_sidecar(info, quality=job.quality, file_path=final))
            job.file_path, job.sidecar_path = str(final), str(sidecar)
            job.status = "done"
        except Exception as exc:  # noqa: BLE001 —— 一条失败不能把 worker 带死
            job.status = "failed"
            job.error = _friendly_error(exc)
            log.error("存视频失败：%s", job.error)
            log.debug("%s", traceback.format_exc())
        finally:
            job.finished_at = _now()
            job.note = None
            self._changed(job)
        if job.status == "done" and self._on_done is not None:
            try:
                self._on_done(job)
            except Exception as exc:  # noqa: BLE001
                log.warning("下载完成后的回调出错：%s", exc)

    def _download(self, job: DownloadJob, info: VideoInfo, dcfg: DownloadConfig, video_dir: Path) -> Path:
        finished_files: list[str] = []
        final_from_pp: list[str] = []
        last_emit = [0.0]

        def _progress(d: dict[str, Any]) -> None:
            st = d.get("status")
            if st == "finished" and d.get("filename"):
                finished_files.append(d["filename"])
                return
            if st != "downloading":
                return
            frac = overall_progress(d)
            now = time.monotonic()
            if now - last_emit[0] < PROGRESS_MIN_INTERVAL_SEC:
                return
            last_emit[0] = now
            if frac is not None:
                job.progress = frac
            job.speed = (d.get("_speed_str") or "").strip() or None
            eta = d.get("eta")
            job.eta_sec = int(eta) if isinstance(eta, (int, float)) else None
            self._changed(job, persist=False)

        def _postprocess(d: dict[str, Any]) -> None:
            if d.get("status") == "started" and d.get("postprocessor") == "Merger":
                job.status = "merging"
                job.speed, job.eta_sec = None, None
                self._changed(job)
            if d.get("status") == "finished":
                fp = (d.get("info_dict") or {}).get("filepath")
                if fp:
                    final_from_pp.append(fp)

        opts = build_ydl_opts(
            dcfg,
            format=format_selector(job.quality, dcfg.prefer_h264),
            merge_output_format="mp4",
            outtmpl={"default": str(video_dir / OUTTMPL)},
            windowsfilenames=True,
            noplaylist=True,
            continuedl=True,
            progress_hooks=[_progress],
            postprocessor_hooks=[_postprocess],
        )
        download(info, opts, dcfg)

        for candidate in reversed(final_from_pp + finished_files):
            p = Path(candidate)
            if p.is_file() and p.suffix.lower() != ".part":
                return p
        # 极少数 extractor 不触发回调，按文件名里的 [id] 找
        for p in video_dir.rglob(f"*[[]{info.video_id}[]].*"):
            if p.is_file() and p.suffix.lower() not in (".part", ".json"):
                return p
        raise DownloadError("yt-dlp 说下完了，但在目录里找不到文件")


def _friendly_error(exc: Exception) -> str:
    msg = str(exc) or exc.__class__.__name__
    first = msg.strip().splitlines()[0] if msg.strip() else msg
    low = first.lower()
    if "412" in first or "precondition failed" in low:
        return "B 站限流（412），过几分钟再试；带登录态 cookie 会宽松很多"
    if "ffmpeg" in low and ("not found" in low or "not installed" in low or "找不到" in first):
        return "本机没有 ffmpeg，合流不了。装好 ffmpeg 并加进 PATH 后重试"
    if "fresh cookies" in low or "403" in first:
        return f"站点拒绝了请求（{first[:120]}）"
    return first[:300]
