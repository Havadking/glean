"""后台任务队列：界面把活交给它，自己去轮询或者订阅事件。

单 worker 线程串行跑 —— ASR 独占 GPU，LLM 调用也没必要并发。任务只存在内存里：
浏览器关了任务照跑，重新打开还能看到；服务进程重启就没了，这一点 v0.7 做批量时再改。

每个任务积累一串事件（阶段切换、进度、日志行、状态变化），带自增序号，
SSE 客户端断线重连时用序号续上，不会漏也不会重。
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

log = logging.getLogger(__name__)

# 任务超过这个数就把最老的"已结束"任务扔掉
MAX_KEPT_JOBS = 200

# ASR provider 的进度日志长这样："转写进度 62%（08:42 / 14:00）"
_PROGRESS_RE = re.compile(r"转写进度\s+(\d+)%")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class JobEvent:
    seq: int
    kind: str          # stage | progress | log | status
    data: dict[str, Any]
    ts: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "kind": self.kind, "ts": self.ts, **self.data}


@dataclass
class Job:
    id: str
    kind: str                  # process | summarize
    title: str
    params: dict[str, Any]
    status: str = "queued"     # queued | running | done | failed | cancelled
    stage: str | None = None
    stage_detail: str = ""
    progress: float | None = None
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    result: dict[str, Any] | None = None
    events: list[JobEvent] = field(default_factory=list)
    _subscribers: list[queue.Queue] = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def finished(self) -> bool:
        return self.status in ("done", "failed", "cancelled")

    def to_dict(self, with_events: bool = False) -> dict[str, Any]:
        d = {
            "id": self.id, "kind": self.kind, "title": self.title, "params": self.params,
            "status": self.status, "stage": self.stage, "stage_detail": self.stage_detail,
            "progress": self.progress, "created_at": self.created_at,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "error": self.error, "result": self.result,
        }
        if with_events:
            d["events"] = [e.to_dict() for e in self.events]
        return d

    # ---------- 事件 ----------

    def emit(self, kind: str, **data: Any) -> JobEvent:
        with self._lock:
            ev = JobEvent(seq=len(self.events) + 1, kind=kind, data=data)
            self.events.append(ev)
            subs = list(self._subscribers)
        for q in subs:
            q.put(ev)
        return ev

    def subscribe(self, after_seq: int = 0) -> queue.Queue:
        """订阅后续事件；after_seq 之后已经发生的先补发。"""
        q: queue.Queue = queue.Queue()
        with self._lock:
            for ev in self.events:
                if ev.seq > after_seq:
                    q.put(ev)
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)


class Reporter:
    """交给任务函数用的汇报口：阶段、进度、日志。"""

    def __init__(self, job: Job) -> None:
        self.job = job

    def stage(self, name: str, detail: str = "") -> None:
        self.job.stage = name
        self.job.stage_detail = detail
        self.job.progress = None
        self.job.emit("stage", stage=name, detail=detail)

    def progress(self, fraction: float) -> None:
        self.job.progress = max(0.0, min(1.0, fraction))
        self.job.emit("progress", progress=self.job.progress)

    def log(self, level: str, message: str) -> None:
        self.job.emit("log", level=level, message=message)


# 任务函数：拿 Reporter 干活，返回要塞进 job.result 的字典
JobFn = Callable[[Reporter], dict[str, Any]]


class _JobLogHandler(logging.Handler):
    """把 worker 线程上的日志转成任务事件。别的线程（比如 HTTP 请求）的日志不管。"""

    def __init__(self, manager: "JobManager") -> None:
        super().__init__(level=logging.INFO)
        self.manager = manager

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread != self.manager.worker_thread_id:
            return
        reporter = self.manager.current_reporter
        if reporter is None:
            return
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            return
        reporter.log(record.levelname.lower(), message)
        m = _PROGRESS_RE.search(message)
        if m:
            reporter.progress(int(m.group(1)) / 100.0)


class JobManager:
    def __init__(self, logger_name: str = "video_summarizer") -> None:
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._fns: dict[str, JobFn] = {}
        self._queue: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self.current_reporter: Reporter | None = None
        self._worker = threading.Thread(target=self._loop, name="vsum-jobs", daemon=True)
        self.worker_thread_id = 0
        self.logger_name = logger_name
        self._handler = _JobLogHandler(self)
        target = logging.getLogger(logger_name)
        target.addHandler(self._handler)
        # 进度和阶段说明都是 INFO 级别的日志，logger 门槛太高就什么都收不到
        if target.getEffectiveLevel() > logging.INFO:
            target.setLevel(logging.INFO)
        self._worker.start()

    # ---------- 提交 / 查询 ----------

    def submit(self, kind: str, title: str, params: dict[str, Any], fn: JobFn) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, title=title, params=params)
        with self._lock:
            self._jobs[job.id] = job
            self._fns[job.id] = fn
            self._trim()
        job.emit("status", status="queued")
        self._queue.put(job.id)
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        return list(self._jobs.values())[::-1]

    def cancel(self, job_id: str) -> bool:
        """只能取消还没开始的。正在跑的 ASR 没法安全打断。"""
        job = self._jobs.get(job_id)
        if job is None or job.status != "queued":
            return False
        job.status = "cancelled"
        job.finished_at = _now()
        job.emit("status", status="cancelled")
        return True

    def find_active(self, kind: str, **params: Any) -> Job | None:
        """同样的活已经在排队或在跑，就别重复提交。"""
        for job in self._jobs.values():
            if job.kind == kind and not job.finished and all(
                job.params.get(k) == v for k, v in params.items()
            ):
                return job
        return None

    # ---------- worker ----------

    def _loop(self) -> None:
        self.worker_thread_id = threading.get_ident()
        while True:
            job_id = self._queue.get()
            job = self._jobs.get(job_id)
            fn = self._fns.pop(job_id, None)
            if job is None or fn is None or job.status != "queued":
                continue
            self._run(job, fn)

    def _run(self, job: Job, fn: JobFn) -> None:
        reporter = Reporter(job)
        self.current_reporter = reporter
        job.status = "running"
        job.started_at = _now()
        job.emit("status", status="running")
        t0 = time.monotonic()
        try:
            job.result = fn(reporter)
            job.status = "done"
            job.stage = "done"
            job.progress = 1.0
        except Exception as exc:  # noqa: BLE001 —— 任务失败不能把 worker 带死
            job.status = "failed"
            job.error = str(exc) or exc.__class__.__name__
            log.error("任务失败：%s", job.error)
            log.debug("%s", traceback.format_exc())
        finally:
            self.current_reporter = None
            job.finished_at = _now()
            job.emit("status", status=job.status, error=job.error,
                     elapsed_sec=round(time.monotonic() - t0, 1), result=job.result)

    def _trim(self) -> None:
        while len(self._jobs) > MAX_KEPT_JOBS:
            for jid, job in self._jobs.items():
                if job.finished:
                    del self._jobs[jid]
                    self._fns.pop(jid, None)
                    break
            else:
                return
