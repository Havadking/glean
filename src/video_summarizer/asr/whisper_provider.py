"""faster-whisper 实现。

v0.1 的默认 ASR，v0.2 起降级为兜底（覆盖 FunASR 不支持的小语种）。
不做说话人分离 —— whisper 本身没有这个能力，要接 pyannote 再按时间轴对齐，留给 v0.3。
"""

from __future__ import annotations

import logging
import os
import sysconfig
import time
from pathlib import Path

from ..config import ASRConfig
from ..errors import ASRError
from ..models import Segment
from .base import ASRResult, BaseASRProvider

log = logging.getLogger(__name__)

# os.add_dll_directory 返回的句柄一旦释放，目录就从搜索路径里摘掉了，得留着引用
_dll_handles: list[object] = []
_dll_dirs_added = False

# 这类报错说明 GPU 路径不可用，值得退回 CPU 重试一次
_CUDA_ERROR_HINTS = ("cublas", "cudnn", "cuda", "gpu", "device")


def _add_cuda_dll_dirs() -> None:
    """把 pip 装的 nvidia-* 运行库目录加进 Windows 的 DLL 搜索路径。

    Linux 上这些包会写 RPATH，Windows 上不会，ctranslate2 因此找不到 cublas64_12.dll。
    PyTorch 自己会做这件事，但我们没装 torch，得手动来。

    两件事都要做：`os.add_dll_directory` 管 Python 自己加载扩展模块，
    但 ctranslate2 是在运行到第一次矩阵运算时用裸 LoadLibrary 拉 cublas 的，
    那条路径只认 PATH。只加前者会出现"模型能加载、一算就报找不到 DLL"。
    """
    global _dll_dirs_added
    if _dll_dirs_added or os.name != "nt":
        return
    _dll_dirs_added = True

    nvidia_root = Path(sysconfig.get_paths()["purelib"]) / "nvidia"
    if not nvidia_root.is_dir():
        return

    found = [d for d in sorted(nvidia_root.glob("*/bin")) if any(d.glob("*.dll"))]
    if not found:
        return
    for bin_dir in found:
        _dll_handles.append(os.add_dll_directory(str(bin_dir)))
    os.environ["PATH"] = os.pathsep.join(
        [str(d) for d in found] + [os.environ.get("PATH", "")]
    )
    log.debug("加入 CUDA DLL 搜索路径: %s", ", ".join(str(d) for d in found))


class _EmptyResult(Exception):
    """内部信号：这一趟没识别出任何内容，交给 _run 决定要不要换策略重试。"""


class WhisperProvider(BaseASRProvider):
    name = "whisper"
    supports_diarization = False

    def __init__(self, cfg: ASRConfig) -> None:
        self.cfg = cfg
        self._model = None
        self._device: str | None = None
        self._compute_type: str | None = None

    # ---------- 模型加载 ----------

    def _load(self, plans: list[tuple[str, str]] | None = None):
        if self._model is not None:
            return self._model

        _add_cuda_dll_dirs()
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # pragma: no cover - 环境问题
            raise ASRError("没装 faster-whisper，先跑 `uv sync`") from exc

        plans = plans or self._device_plans()
        last_error: Exception | None = None
        for device, compute_type in plans:
            try:
                log.info(
                    "加载 whisper 模型 %s（device=%s, compute_type=%s，首次会下载权重）",
                    self.cfg.model, device, compute_type,
                )
                model = WhisperModel(self.cfg.model, device=device, compute_type=compute_type)
            except Exception as exc:  # noqa: BLE001 - ctranslate2 抛的类型不固定
                last_error = exc
                log.warning("device=%s 加载失败：%s", device, exc)
                continue
            self._model, self._device, self._compute_type = model, device, compute_type
            return model

        raise ASRError(
            f"whisper 模型加载失败（已尝试 {[d for d, _ in plans]}）: {last_error}\n"
            "GPU 路径需要 CUDA 运行库，跑 `uv sync --extra cuda` 装上；"
            "或把 config.yaml 里 asr.device 改成 cpu。"
        )

    def _device_plans(self) -> list[tuple[str, str]]:
        """返回按优先级排列的 (device, compute_type)。auto 时 GPU 失败自动退 CPU。"""
        compute = self.cfg.compute_type

        def for_device(device: str) -> str:
            if compute != "auto":
                return compute
            return "float16" if device == "cuda" else "int8"

        if self.cfg.device == "cpu":
            return [("cpu", for_device("cpu"))]
        if self.cfg.device == "cuda":
            return [("cuda", for_device("cuda"))]
        return [("cuda", for_device("cuda")), ("cpu", for_device("cpu"))]

    # ---------- 推理 ----------

    def transcribe(self, audio_path: Path) -> ASRResult:
        try:
            return self._run(audio_path)
        except ASRError as exc:
            # 模型能加载不代表能算：cuBLAS/cuDNN 缺失要到第一次矩阵运算才暴露
            if self._device != "cuda" or self.cfg.device == "cuda":
                raise
            if not any(h in str(exc).lower() for h in _CUDA_ERROR_HINTS):
                raise
            log.warning("GPU 推理失败，退回 CPU 重试一次：%s", exc)
            self.close()
            self._load([("cpu", "int8" if self.cfg.compute_type == "auto" else self.cfg.compute_type)])
            return self._run(audio_path)

    def _run(self, audio_path: Path) -> ASRResult:
        """跑一遍；如果 VAD 把音频全滤没了，关掉 VAD 再来一次。

        纯音乐、强背景音的内容 VAD 容易整段误判成非人声，这时候直接失败对用户没帮助。
        """
        try:
            return self._transcribe_once(audio_path, vad_filter=self.cfg.vad_filter)
        except _EmptyResult:
            if not self.cfg.vad_filter:
                raise ASRError("转写结果是空的，音频里可能没有人声") from None
            log.warning("VAD 把整段音频都判成了非人声（纯音乐/强背景音常见），关掉 VAD 重试")
            try:
                return self._transcribe_once(audio_path, vad_filter=False)
            except _EmptyResult:
                raise ASRError("转写结果是空的（关掉 VAD 后依然如此），音频里可能确实没有人声") from None

    def _transcribe_once(self, audio_path: Path, vad_filter: bool) -> ASRResult:
        model = self._load()
        started = time.monotonic()

        try:
            raw_segments, info = model.transcribe(
                str(audio_path),
                language=self.cfg.language,
                beam_size=self.cfg.beam_size,
                vad_filter=vad_filter,
            )
            total = float(getattr(info, "duration", 0.0) or 0.0)
            segments: list[Segment] = []
            next_report = 60.0

            # faster-whisper 是惰性生成器，真正的识别发生在这个循环里
            for seg in raw_segments:
                text = (seg.text or "").strip()
                if text:
                    segments.append(Segment(start=float(seg.start), end=float(seg.end), text=text))
                if seg.end >= next_report:
                    pct = f"{seg.end / total * 100:5.1f}%" if total else "  ?  "
                    log.info("转写进度 %s（%s / %s）", pct, _hms(seg.end), _hms(total))
                    next_report = seg.end + 60.0
        except Exception as exc:  # noqa: BLE001
            raise ASRError(f"whisper 转写失败: {exc}") from exc

        if not segments:
            raise _EmptyResult()

        elapsed = time.monotonic() - started
        log.info(
            "转写完成：%d 条分句，耗时 %s，实时率 %.1fx",
            len(segments), _hms(elapsed), (total / elapsed) if elapsed > 0 else 0.0,
        )

        return ASRResult(
            segments=segments,
            language=getattr(info, "language", None) or self.cfg.language,
            meta={
                "asr_provider": self.name,
                "asr_model": self.cfg.model,
                "device": self._device,
                "compute_type": self._compute_type,
                "vad_filter": vad_filter,
                "language_probability": getattr(info, "language_probability", None),
                "elapsed_sec": round(elapsed, 1),
            },
        )

    def close(self) -> None:
        self._model = None


def _hms(seconds: float) -> str:
    seconds = int(max(0.0, seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
