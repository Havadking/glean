"""ASR 子进程（asr/worker.py）：协议往返、错误映射、日志转发。

用打桩 provider（asr_stub.py），不加载任何模型，但确实起真实的子进程。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from video_summarizer.asr import worker
from video_summarizer.config import ASRConfig
from video_summarizer.errors import ASRError, ConfigError

STUB = "asr_stub:make_provider"
AUDIO = Path("测试.wav")  # 打桩 provider 不读它


@pytest.fixture(autouse=True)
def stub_on_path(monkeypatch):
    tests_dir = str(Path(__file__).parent)
    existing = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", tests_dir + (os.pathsep + existing if existing else ""))
    monkeypatch.delenv(worker.INPROCESS_ENV, raising=False)


# ---------- 走真实子进程 ----------


def test_roundtrip_result_and_forwarded_logs(caplog):
    caplog.set_level(logging.INFO, logger="video_summarizer")
    result = worker.transcribe(
        ASRConfig(provider="stub", model="ok"), AUDIO,
        diarize=True, hotwords=["宇树科技", "英伟达"], factory=STUB,
    )
    assert [s.text for s in result.segments] == ["来自 测试.wav"]
    assert result.segments[0].speaker == "Speaker_1"
    assert result.language == "zh"
    assert result.meta == {"asr_provider": "stub", "hotwords": 2, "diarize": True}
    # 子进程的日志用同名 logger 在父进程重新打了一遍，界面上的进度条靠这个
    assert any("转写进度  50%" in r.getMessage() and r.name == "video_summarizer.asr.stub"
               for r in caplog.records)


def test_asr_error_is_reraised_as_asr_error():
    with pytest.raises(ASRError, match="模拟失败"):
        worker.transcribe(ASRConfig(provider="stub", model="boom"), AUDIO, factory=STUB)


def test_unknown_exception_becomes_asr_error():
    with pytest.raises(ASRError, match="模型内部炸了"):
        worker.transcribe(ASRConfig(provider="stub", model="crash"), AUDIO, factory=STUB)


def test_config_error_from_real_registry():
    # 不打桩：走真实的 get_provider，未知 provider 在 import 任何模型库之前就报 ConfigError
    with pytest.raises(ConfigError, match="未知的 ASR provider"):
        worker.transcribe(ASRConfig(provider="nope", fallback=None), AUDIO)


def test_survives_dead_inherited_stderr(tmp_path):
    """起服务的那个进程先死了、stderr 管道没人读：子进程里的进度条不能把 ASR 拖垮。

    这里再套一层进程模拟"服务"：它的 stderr 是我们这边立刻关掉的管道，
    ASR 子进程从它那里继承这个死管道。结果写进文件里回来看。
    """
    import json
    import subprocess
    import sys

    out = tmp_path / "result.json"
    script = f"""
import json
from pathlib import Path
from video_summarizer.asr import worker
from video_summarizer.config import ASRConfig
r = worker.transcribe(ASRConfig(provider="stub", model="ok"), Path({str(AUDIO)!r}), factory={STUB!r})
Path({str(out)!r}).write_text(json.dumps([s.text for s in r.segments], ensure_ascii=False), encoding="utf-8")
"""
    proc = subprocess.Popen([sys.executable, "-c", script],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ.copy())
    assert proc.stdout is not None and proc.stderr is not None
    proc.stdout.close()
    proc.stderr.close()
    assert proc.wait(timeout=120) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == ["来自 测试.wav"]


# ---------- 进程内的协议逻辑 ----------


def test_serve_emits_result_then_nothing_else():
    sent: list[dict] = []
    worker.serve({
        "cfg": {"provider": "stub", "model": "ok"}, "audio_path": str(AUDIO),
        "diarize": False, "hotwords": [], "factory": STUB,
    }, sent.append)
    kinds = [m["type"] for m in sent]
    assert kinds[-1] == "result" and kinds.count("result") == 1
    assert sent[-1]["result"]["segments"][0]["text"] == "来自 测试.wav"


def test_inprocess_switch(monkeypatch):
    monkeypatch.setenv(worker.INPROCESS_ENV, "1")
    monkeypatch.setattr(worker, "_spawn", lambda *a: (_ for _ in ()).throw(AssertionError("不该起子进程")))
    with pytest.raises(ConfigError):
        worker.transcribe(ASRConfig(provider="nope", fallback=None), AUDIO)
