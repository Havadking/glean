"""把 ASR 放到独立子进程跑，任务完就退出，内存整体还给系统。

为什么不在主进程跑：`import torch` 一下就是 1.4GB 提交内存，`import funasr` 再加 0.7GB，
CUDA context 再加 0.3GB —— 一个模型都没建，`vsum ui` 的进程就常驻 2GB 了。跑过一次任务后
torch 的缓存分配器、pinned memory、cuDNN/cuBLAS 工作区又留下几 GB，同一进程内基本收不回来
（实测跑完一条 5 分钟视频后提交内存 5.6GB）。放到子进程里，退出时操作系统一次性全收走。

协议很土但够用：父进程把请求（ASR 配置、音频路径、热词）写成一行 JSON 送到子进程 stdin，
子进程在 stdout 上逐行回 JSON —— 日志记录一条一条转发回来（父进程用同名 logger 重新打一遍，
所以文件日志、界面上的进度条都和原来一样），最后一行是结果或错误。

funasr / modelscope 会往 stdout 上 print 进度条之类的东西，混进协议流里就解析不了。
所以子进程一启动先把 fd 1 复制一份留给协议，再把 fd 2 覆盖到 fd 1 上：之后所有
print 和 C 层的输出都进 stderr，父进程原样继承（也就是还是进 ui.err.log）。

`warm_up()` 也走这里：起一个只 import 不干活的子进程。目的只是让操作系统把那 3GB DLL
读进文件缓存（冷启动慢主要是读盘 + 杀毒扫描），子进程退了缓存还在，下一个真任务照样秒过。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, TextIO

from ..config import ASRConfig
from ..errors import ASRError, ConfigError
from ..models import Segment
from .base import ASRResult

log = logging.getLogger(__name__)

# 设成 1 就回到主进程内跑 ASR（调试用，或者某些环境起不了子进程）
INPROCESS_ENV = "VSUM_ASR_INPROCESS"
PACKAGE_LOGGER = __name__.split(".")[0]

# 子进程报上来的异常类名 -> 父进程重新抛的类型。其余一律按 ASRError 抛
_ERROR_TYPES: dict[str, type[Exception]] = {
    "ASRError": ASRError,
    "ConfigError": ConfigError,
}


# ---------- 父进程侧 ----------


def transcribe(cfg: ASRConfig, audio_path: Path, *, diarize: bool = False,
               hotwords: list[str] | None = None, factory: str | None = None) -> ASRResult:
    """在子进程里跑完整的 ASR（含兜底），返回结果。阻塞到子进程退出。

    factory 是测试钩子："module:callable"，子进程用它代替 get_provider 构造 provider。
    """
    if os.environ.get(INPROCESS_ENV, "").strip() == "1":
        return _transcribe_inprocess(cfg, audio_path, diarize=diarize, hotwords=hotwords)

    request = {
        "cfg": asdict(cfg),
        "audio_path": str(audio_path),
        "diarize": diarize,
        "hotwords": list(hotwords or []),
        # 子进程照父进程的门槛放行日志：root 一份，本包一份（界面只把本包调到 INFO，root 可能更高）
        "log_levels": {
            "": logging.getLogger().getEffectiveLevel(),
            PACKAGE_LOGGER: logging.getLogger(PACKAGE_LOGGER).getEffectiveLevel(),
        },
        "factory": factory,
        "parent_pid": os.getpid(),
    }
    proc = _spawn()
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        proc.stdin.flush()
        final = _pump(proc.stdout)
    finally:
        _reap(proc)

    if final is None:
        raise ASRError(f"ASR 子进程没返回结果就退出了（退出码 {proc.returncode}），看 stderr 日志")
    if final.get("type") == "error":
        exc_type = _ERROR_TYPES.get(final.get("cls", ""), ASRError)
        raise exc_type(final.get("message") or "ASR 子进程报错")
    return _result_from_dict(final["result"])


def warm_up() -> None:
    """起一个子进程把 funasr/torch import 一遍就退出，给操作系统的文件缓存预热。"""
    if os.environ.get(INPROCESS_ENV, "").strip() == "1":
        from .funasr_provider import warm_up as inprocess_warm_up

        inprocess_warm_up()
        return

    started = time.monotonic()
    try:
        proc = _spawn("--warm-up")
    except OSError as exc:
        log.debug("ASR 预热子进程起不来：%s", exc)
        return
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.close()
    final = _pump(proc.stdout)
    _reap(proc)
    if final and final.get("type") == "result":
        log.info("ASR 运行时预热完成，耗时 %.1f 秒（cuda=%s）",
                 time.monotonic() - started, final["result"].get("cuda"))
    else:
        log.debug("ASR 预热子进程退出码 %s：%s", proc.returncode, final)


def _spawn(*args: str) -> subprocess.Popen[str]:
    env = dict(os.environ)
    # 管道两头都按 UTF-8 说话，别让 Windows 的代码页把中文日志弄坏
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return subprocess.Popen(
        [sys.executable, "-m", "video_summarizer.asr.worker", *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,   # 继承父进程的 stderr：模型下载进度条之类的照旧进日志文件
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


def _pump(stream: TextIO) -> dict[str, Any] | None:
    """逐行读子进程的协议输出：日志当场转发，遇到结果/错误就返回它。

    这个函数在调用方线程里跑（不另开线程），日志记录也就打在调用方线程上 ——
    界面的任务日志是按线程过滤的，换线程转发就收不到了。
    """
    final: dict[str, Any] | None = None
    for line in stream:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            log.debug("ASR 子进程输出了非协议行：%s", line[:200])
            continue
        kind = msg.get("type")
        if kind == "log":
            logging.getLogger(msg.get("name") or __name__).log(
                int(msg.get("level", logging.INFO)), "%s", msg.get("msg", ""),
            )
        elif kind in ("result", "error"):
            final = msg
            # 结果之后不会再有东西了，但还是读到 EOF，让子进程能正常退出
    return final


def _reap(proc: subprocess.Popen[str]) -> None:
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        log.warning("ASR 子进程没有按时退出，强制结束")
        if os.name == "nt":
            # venv 里的 python.exe 是个跳板，真正干活的解释器是它的子进程，得连树一起杀
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, check=False)
        proc.kill()
        proc.wait()


def _result_from_dict(raw: dict[str, Any]) -> ASRResult:
    return ASRResult(
        segments=[Segment(**s) for s in raw.get("segments", [])],
        language=raw.get("language"),
        meta=dict(raw.get("meta") or {}),
    )


def _transcribe_inprocess(cfg: ASRConfig, audio_path: Path, *, diarize: bool,
                          hotwords: list[str] | None) -> ASRResult:
    from . import get_provider

    provider = get_provider(cfg, diarize=diarize, hotwords=hotwords)
    try:
        return provider.transcribe(audio_path)
    finally:
        provider.close()


# ---------- 子进程侧 ----------


class _ForwardHandler(logging.Handler):
    """把子进程里的每条日志记录写成一行 JSON 送回父进程。"""

    def __init__(self, emit: Callable[[dict[str, Any]], None]) -> None:
        super().__init__()
        self._emit = emit

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            return
        self._emit({"type": "log", "name": record.name, "level": record.levelno, "msg": message})


def _watch_parent(parent_pid: int) -> None:
    """父进程一死就跟着退出，别留一个占着显卡的孤儿。

    不能用"阻塞读 stdin 等 EOF"来探测：Windows 上有一个线程卡在 stdin 的 ReadFile 里时，
    numpy 的扩展模块一加载就死锁（实测复现，别的阻塞方式都没事）。所以 Windows 上等父进程句柄，
    其他平台隔一会儿探一次。
    """
    try:
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            SYNCHRONIZE, INFINITE = 0x00100000, 0xFFFFFFFF
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, parent_pid)
            if not handle:
                return
            kernel32.WaitForSingleObject(handle, INFINITE)
        else:
            while True:
                time.sleep(2)
                os.kill(parent_pid, 0)
    except Exception:  # noqa: BLE001 —— 探测本身出问题就当父进程没了
        pass
    os._exit(1)


def serve(request: dict[str, Any], emit: Callable[[dict[str, Any]], None]) -> None:
    """按请求跑一次 ASR，结果或错误通过 emit 送出去。拆出来是为了能在进程内测协议。"""
    for name, level in (request.get("log_levels") or {"": logging.INFO}).items():
        logging.getLogger(name).setLevel(int(level))
    logging.getLogger().addHandler(_ForwardHandler(emit))
    try:
        cfg = ASRConfig(**request["cfg"])
        factory = request.get("factory")
        if factory:
            module_name, _, attr = factory.partition(":")
            import importlib

            get_provider = getattr(importlib.import_module(module_name), attr)
        else:
            from . import get_provider

        provider = get_provider(cfg, diarize=bool(request.get("diarize")),
                                hotwords=request.get("hotwords") or None)
        try:
            result = provider.transcribe(Path(request["audio_path"]))
        finally:
            provider.close()
        emit({"type": "result", "result": {
            "segments": [asdict(s) for s in result.segments],
            "language": result.language,
            "meta": result.meta,
        }})
    except Exception as exc:  # noqa: BLE001 —— 什么错都得报回去，父进程再决定怎么抛
        emit({"type": "error", "cls": exc.__class__.__name__,
              "message": str(exc) or exc.__class__.__name__})


def _serve_warm_up(emit: Callable[[dict[str, Any]], None]) -> None:
    try:
        import funasr  # noqa: F401
        import torch

        cuda = torch.cuda.is_available()
    except ImportError as exc:
        emit({"type": "error", "cls": "ImportError", "message": str(exc)})
        return
    emit({"type": "result", "result": {"cuda": cuda}})


def _ensure_stderr_writable() -> None:
    """继承来的 stderr 要是写不进去，就换成 devnull，别让一条进度条把整个 ASR 拖垮。

    典型场景：起 `vsum ui` 的那个进程（终端、IDE 预览）把 stderr 接成管道后自己先退了，
    服务本身还活着。Windows 上往没人读的管道写会报 [Errno 22] Invalid argument ——
    Python 的 logging 会吞掉这种错，但 modelscope 下载时的 tqdm 进度条不会，
    于是 AutoModel(...) 直接炸，被当成"FunASR 模型加载失败"切到 whisper 兜底，
    看起来就是"选了说话人分离结果还是没分"。

    空写（os.write(2, b"")）探测不出来，必须真写点东西，所以借启动这一行日志当探针。
    """
    marker = f"{time.strftime('%H:%M:%S')} INFO    ASR 子进程 {os.getpid()} 启动\n"
    try:
        os.write(2, marker.encode("utf-8"))
        return
    except OSError:
        pass
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 2)
    os.close(devnull)


def _main(argv: list[str]) -> int:
    _ensure_stderr_writable()
    # 协议流独占原来的 fd 1；之后任何往 stdout 的输出（print、进度条、C 层）都改道 stderr
    proto_fd = os.dup(1)
    os.dup2(2, 1)
    proto = os.fdopen(proto_fd, "w", encoding="utf-8", errors="replace", buffering=1)
    sys.stdout = sys.stderr
    lock = threading.Lock()

    def emit(msg: dict[str, Any]) -> None:
        with lock:
            proto.write(json.dumps(msg, ensure_ascii=False) + "\n")
            proto.flush()

    if "--warm-up" in argv:
        _serve_warm_up(emit)
        sys.stderr.flush()
        os._exit(0)

    line = sys.stdin.readline()
    if not line.strip():
        return 2
    request = json.loads(line)
    if request.get("parent_pid"):
        threading.Thread(target=_watch_parent, args=(int(request["parent_pid"]),),
                         name="parent-watch", daemon=True).start()
    serve(request, emit)
    # 结果已经送出去了，别等解释器慢吞吞地收拾 torch/CUDA（有时还会卡住），直接退
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
