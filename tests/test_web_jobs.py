"""后台任务队列：顺序、事件、日志捕获、失败不拖垮 worker。"""

from __future__ import annotations

import logging
import threading
import time

import pytest

from video_summarizer.web.jobs import JobManager, Reporter


def _wait(job, timeout=5.0):
    t0 = time.monotonic()
    while not job.finished and time.monotonic() - t0 < timeout:
        time.sleep(0.01)
    assert job.finished, f"任务没结束：{job.status}"
    return job


@pytest.fixture
def manager():
    # 每个测试自己的 logger 名，免得 handler 串到别的测试
    return JobManager(logger_name=f"test.jobs.{id(object())}")


def test_runs_in_order_and_records_result(manager):
    order = []

    def fn(tag):
        def work(rep: Reporter):
            order.append(tag)
            return {"tag": tag}
        return work

    a = manager.submit("process", "a", {"url": "a"}, fn("a"))
    b = manager.submit("process", "b", {"url": "b"}, fn("b"))
    _wait(a); _wait(b)
    assert order == ["a", "b"]
    assert a.status == "done" and a.result == {"tag": "a"}
    assert a.started_at and a.finished_at


def test_events_have_increasing_seq_and_terminal_status(manager):
    def work(rep: Reporter):
        rep.stage("probe", "探测")
        rep.progress(0.5)
        rep.log("info", "hello")
        return {}

    job = _wait(manager.submit("process", "x", {}, work))
    seqs = [e.seq for e in job.events]
    assert seqs == list(range(1, len(seqs) + 1))
    kinds = [e.kind for e in job.events]
    assert kinds[0] == "status" and kinds[-1] == "status"
    assert "stage" in kinds and "progress" in kinds and "log" in kinds
    assert job.events[-1].data["status"] == "done"
    assert job.progress == 1.0 and job.stage == "done"


def test_failure_marks_job_and_worker_keeps_going(manager):
    def bad(rep):
        raise RuntimeError("炸了")

    def good(rep):
        return {"ok": True}

    a = manager.submit("process", "bad", {}, bad)
    b = manager.submit("process", "good", {}, good)
    _wait(a); _wait(b)
    assert a.status == "failed" and a.error == "炸了"
    assert b.status == "done"


def test_cancel_only_works_while_queued(manager):
    gate = threading.Event()

    def slow(rep):
        gate.wait(5)
        return {}

    a = manager.submit("process", "slow", {}, slow)
    b = manager.submit("process", "later", {}, lambda rep: {})
    time.sleep(0.05)
    assert a.status == "running"
    assert manager.cancel(a.id) is False
    assert manager.cancel(b.id) is True
    assert b.status == "cancelled"
    gate.set()
    _wait(a)
    # 取消的那个不会被跑
    assert b.result is None and b.finished_at


def test_subscribe_replays_history_then_streams(manager):
    gate = threading.Event()

    def work(rep):
        rep.log("info", "first")
        gate.wait(5)
        rep.log("info", "second")
        return {}

    job = manager.submit("process", "s", {}, work)
    time.sleep(0.05)
    q = job.subscribe(after_seq=0)
    replayed = [q.get(timeout=1) for _ in range(3)]  # queued, running, first
    assert [e.kind for e in replayed] == ["status", "status", "log"]
    gate.set()
    later = q.get(timeout=2)
    assert later.kind == "log" and later.data["message"] == "second"
    job.unsubscribe(q)


def test_worker_logs_become_events_and_progress_is_parsed(manager):
    logger = logging.getLogger(manager.logger_name)

    def work(rep):
        logger.info("转写进度 42%（00:10 / 00:24）")
        logger.warning("小心")
        return {}

    job = _wait(manager.submit("process", "log", {}, work))
    logs = [e for e in job.events if e.kind == "log"]
    assert any("转写进度" in e.data["message"] for e in logs)
    assert any(e.data["level"] == "warning" for e in logs)
    progress = [e for e in job.events if e.kind == "progress"]
    assert progress and abs(progress[0].data["progress"] - 0.42) < 1e-9


def test_logs_from_other_threads_are_ignored(manager):
    gate = threading.Event()

    def work(rep):
        gate.wait(5)
        return {}

    job = manager.submit("process", "x", {}, work)
    time.sleep(0.05)
    logging.getLogger(manager.logger_name).info("来自请求线程的日志")
    gate.set()
    _wait(job)
    assert not any(e.kind == "log" for e in job.events)


def test_find_active_dedups_by_params(manager):
    gate = threading.Event()
    job = manager.submit("process", "x", {"url": "u"}, lambda rep: (gate.wait(5), {})[1])
    time.sleep(0.02)
    assert manager.find_active("process", url="u") is job
    assert manager.find_active("process", url="other") is None
    gate.set()
    _wait(job)
    assert manager.find_active("process", url="u") is None


def test_to_dict_shapes():
    manager = JobManager(logger_name="test.jobs.shape")
    job = _wait(manager.submit("summarize", "t", {"video_id": "v"}, lambda rep: {"n": 1}))
    d = job.to_dict()
    assert {"id", "kind", "title", "params", "status", "stage", "progress", "result", "error"} <= set(d)
    assert "events" not in d
    assert d["result"] == {"n": 1}
    assert job.to_dict(with_events=True)["events"][0]["seq"] == 1
