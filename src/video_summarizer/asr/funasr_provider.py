"""FunASR 实现（DESIGN.md 3.3）。

v0.2 起的默认 ASR：中文内容的准确率和速度都比 whisper 好，模型也小得多
（SenseVoice-Small 234M 参数，对着 large-v3 的 1.55B）。
代价是只覆盖中英日韩粤，其他语言由 whisper 兜底（见 fallback.py）。

关于时间轴 —— 这里没有走 FunASR 的 `AutoModel(vad_model=...)` 一步到位写法，
因为实测那条路径只返回一整条拼接好的文本，keys 就 `['key', 'text']`，拿不到段边界，
而没有时间轴就做不了时间轴大纲、也没法和字幕来源的转写对齐。
所以拆成两步自己走：先用 FSMN-VAD 切出语音段拿到边界，再把切片批量送进识别模型。
顺带的好处是批量推理比逐段快得多（实测 100x 实时以上）。

另外 SenseVoice 是按 30 秒以内的短段训练的，整段长音频直接喂进去输出是乱的，
VAD 切分不是可选优化而是必需步骤。
"""

from __future__ import annotations

import logging
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

from ..config import ASRConfig
from ..errors import ASRError
from ..models import Segment
from .base import ASRResult, BaseASRProvider

log = logging.getLogger(__name__)

# SenseVoice 覆盖的语言。超出这个范围要走 whisper 兜底。
SENSEVOICE_LANGUAGES = frozenset({"zh", "en", "yue", "ja", "ko"})

# 配置里的短名 -> ModelScope 上的模型 id
MODEL_IDS = {
    "sensevoice-small": "iic/SenseVoiceSmall",
    "paraformer-zh": "iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
}
VAD_MODEL_ID = "fsmn-vad"

# 单个语音段的上限。SenseVoice 按 30 秒以内的短段训练，超了质量下降。
MAX_SEGMENT_MS = 30000
# 一批送多少段进模型。太大只是多占显存，收益已经平了。
BATCH_SIZE = 16
# 一次切片载入内存的段数上限，避免超长视频把整段音频复制一份进列表
CHUNK_GROUP = 128

EXPECTED_SAMPLE_RATE = 16000

# SenseVoice 的富文本标签：<|zh|><|EMO_UNKNOWN|><|Speech|><|withitn|>
_TAG_RE = re.compile(r"<\|([^|]*)\|>")


class FunASRProvider(BaseASRProvider):
    name = "funasr"
    supports_diarization = False  # v0.3 接 CAM++ 之后改成 True
    supported_languages = SENSEVOICE_LANGUAGES

    def __init__(self, cfg: ASRConfig) -> None:
        self.cfg = cfg
        self._asr = None
        self._vad = None
        self._device: str | None = None

    # ---------- 模型加载 ----------

    def _resolve_device(self) -> str:
        if self.cfg.device == "cpu":
            return "cpu"
        if self.cfg.device == "cuda":
            return "cuda:0"
        try:
            import torch

            return "cuda:0" if torch.cuda.is_available() else "cpu"
        except ImportError:
            return "cpu"

    def _load(self) -> None:
        if self._asr is not None:
            return

        try:
            from funasr import AutoModel
        except ImportError as exc:
            raise ASRError(
                "没装 funasr。跑 `uv sync --extra funasr` 装上，"
                "或者把 config.yaml 里 asr.provider 改成 whisper。"
            ) from exc

        model_id = MODEL_IDS.get(self.cfg.model.lower(), self.cfg.model)
        self._device = self._resolve_device()

        log.info("加载 FunASR 模型 %s（device=%s，首次会从 ModelScope 下载）", model_id, self._device)
        common: dict[str, Any] = {
            "device": self._device,
            "disable_update": True,  # 别每次启动都去查版本
            "disable_pbar": True,
        }
        try:
            # max_single_segment_time 是建模型时的配置项，不是 generate() 的参数，
            # 传错地方不会报错，只会静默用默认的 60000ms —— 那对 SenseVoice 太长了
            self._vad = AutoModel(
                model=VAD_MODEL_ID, max_single_segment_time=MAX_SEGMENT_MS, **common
            )
            self._asr = AutoModel(model=model_id, **common)
        except Exception as exc:  # noqa: BLE001 - funasr 抛的类型不固定
            raise ASRError(f"FunASR 模型加载失败: {exc}") from exc

    # ---------- 推理 ----------

    def transcribe(self, audio_path: Path) -> ASRResult:
        self._load()
        started = time.monotonic()

        audio, sample_rate = _read_audio(audio_path)
        spans = self._detect_speech(audio_path)
        if not spans:
            raise ASRError("FSMN-VAD 没检测到语音段，音频里可能没有人声")

        speech_sec = sum(end - start for start, end in spans) / 1000.0
        log.info("VAD 切出 %d 个语音段，共 %s", len(spans), _hms(speech_sec))

        segments: list[Segment] = []
        languages: Counter[str] = Counter()
        total_ms = spans[-1][1]
        next_report = 60_000.0

        for group_start in range(0, len(spans), CHUNK_GROUP):
            group = spans[group_start : group_start + CHUNK_GROUP]
            chunks = [
                audio[int(start * sample_rate / 1000) : int(end * sample_rate / 1000)]
                for start, end in group
            ]
            for (start_ms, end_ms), raw_text in zip(group, self._recognize(chunks, sample_rate)):
                text, lang = _strip_tags(raw_text)
                if lang:
                    languages[lang] += 1
                if text:
                    segments.append(
                        Segment(start=start_ms / 1000.0, end=end_ms / 1000.0, text=text)
                    )
                if end_ms >= next_report:
                    pct = f"{end_ms / total_ms * 100:5.1f}%" if total_ms else "  ?  "
                    log.info("转写进度 %s（%s / %s）", pct, _hms(end_ms / 1000), _hms(total_ms / 1000))
                    next_report = end_ms + 60_000.0

        if not segments:
            raise ASRError("FunASR 没识别出任何内容")

        elapsed = time.monotonic() - started
        log.info(
            "转写完成：%d 条分句，耗时 %s，实时率 %.1fx",
            len(segments), _hms(elapsed), (total_ms / 1000.0 / elapsed) if elapsed > 0 else 0.0,
        )

        return ASRResult(
            segments=segments,
            language=self.cfg.language or (languages.most_common(1)[0][0] if languages else None),
            meta={
                "asr_provider": self.name,
                "asr_model": self.cfg.model,
                "device": self._device,
                "vad_model": VAD_MODEL_ID,
                "speech_sec": round(speech_sec, 1),
                "detected_languages": dict(languages),
                "elapsed_sec": round(elapsed, 1),
            },
        )

    def _detect_speech(self, audio_path: Path) -> list[tuple[int, int]]:
        """跑 FSMN-VAD，拿到 [(起始毫秒, 结束毫秒), ...]。"""
        try:
            raw = self._vad.generate(input=str(audio_path))
        except Exception as exc:  # noqa: BLE001
            raise ASRError(f"FSMN-VAD 失败: {exc}") from exc

        spans: list[tuple[int, int]] = []
        for item in raw or []:
            for span in item.get("value") or []:
                if len(span) >= 2 and span[1] > span[0]:
                    spans.append((int(span[0]), int(span[1])))
        spans.sort()
        return spans

    def _recognize(self, chunks: list, sample_rate: int) -> list[str]:
        """批量识别一组音频切片，返回和输入等长的原始文本列表。"""
        if not chunks:
            return []
        try:
            out = self._asr.generate(
                input=chunks,
                fs=sample_rate,
                language=self.cfg.language or "auto",
                use_itn=True,  # 顺带做逆文本正则化，数字和标点会规范一些
                batch_size=BATCH_SIZE,
            )
        except Exception as exc:  # noqa: BLE001
            raise ASRError(f"FunASR 识别失败: {exc}") from exc

        texts = [item.get("text") or "" for item in out or []]
        if len(texts) != len(chunks):
            raise ASRError(
                f"FunASR 返回条数和输入段数对不上（{len(texts)} vs {len(chunks)}），时间轴无法对齐"
            )
        return texts

    def close(self) -> None:
        self._asr = None
        self._vad = None


def _read_audio(path: Path):
    """读成单声道 float32。管线上游产出的就是 16k 单声道，这里只做兜底。"""
    try:
        import soundfile as sf
    except ImportError as exc:
        raise ASRError("没装 soundfile，跑 `uv sync --extra funasr`") from exc

    try:
        audio, sample_rate = sf.read(str(path), dtype="float32", always_2d=False)
    except Exception as exc:  # noqa: BLE001
        raise ASRError(f"读取音频失败: {exc}") from exc

    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sample_rate != EXPECTED_SAMPLE_RATE:
        log.warning("音频采样率是 %d Hz，模型按 %d Hz 训练，识别质量可能受影响",
                    sample_rate, EXPECTED_SAMPLE_RATE)
    return audio, sample_rate


def _strip_tags(raw: str) -> tuple[str, str | None]:
    """剥掉 SenseVoice 的富文本标签，返回 (正文, 语言码)。

    不用官方的 rich_transcription_postprocess：那个会把标签转成 emoji（🎼😊），
    转写正文里不该混进这些东西。语言标签留下来给 Transcript.language 用。
    """
    tags = _TAG_RE.findall(raw or "")
    language = None
    for tag in tags:
        low = tag.lower()
        if low in SENSEVOICE_LANGUAGES:
            language = low
            break
    return _TAG_RE.sub("", raw or "").strip(), language


def _hms(seconds: float) -> str:
    seconds = int(max(0.0, seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
