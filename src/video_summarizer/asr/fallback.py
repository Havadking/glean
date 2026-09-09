"""兜底包装：主 provider 不行就换备用的（DESIGN.md 6 节 `asr.fallback`）。

三种情况会切到备用 provider：

1. 配置里指定了语言，而主 provider 不支持（FunASR 只覆盖中日英韩粤，
   whisper 覆盖近百种语言，这是文档里给 fallback 的原始理由）
2. 主 provider 抛错（模型下载失败、显存不够……）
3. 主 provider 跑完没识别出任何内容

对调用方来说它就是一个普通的 provider，pipeline 不用知道兜底这回事。
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..errors import ASRError
from .base import ASRResult, BaseASRProvider

log = logging.getLogger(__name__)


class FallbackASRProvider(BaseASRProvider):
    def __init__(
        self,
        primary: BaseASRProvider,
        fallback: BaseASRProvider,
        language: str | None = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.language = language

    @property
    def name(self) -> str:
        return f"{self.primary.name}+{self.fallback.name}"

    @property
    def supports_diarization(self) -> bool:
        return self.primary.supports_diarization

    def supports_language(self, language: str | None) -> bool:
        return self.primary.supports_language(language) or self.fallback.supports_language(language)

    def transcribe(self, audio_path: Path) -> ASRResult:
        if not self.primary.supports_language(self.language):
            log.info(
                "%s 不支持语言 %s，直接用兜底 provider %s",
                self.primary.name, self.language, self.fallback.name,
            )
            return self._run_fallback(audio_path, reason=f"主 provider 不支持 {self.language}")

        try:
            result = self.primary.transcribe(audio_path)
        except ASRError as exc:
            log.warning("%s 失败，切到兜底 provider %s：%s", self.primary.name, self.fallback.name, exc)
            self.primary.close()
            return self._run_fallback(audio_path, reason=str(exc))

        if not result.segments:
            log.warning("%s 没识别出内容，切到兜底 provider %s", self.primary.name, self.fallback.name)
            self.primary.close()
            return self._run_fallback(audio_path, reason="主 provider 结果为空")

        return result

    def _run_fallback(self, audio_path: Path, reason: str) -> ASRResult:
        result = self.fallback.transcribe(audio_path)
        result.meta["fallback_from"] = self.primary.name
        result.meta["fallback_reason"] = reason
        return result

    def close(self) -> None:
        self.primary.close()
        self.fallback.close()
