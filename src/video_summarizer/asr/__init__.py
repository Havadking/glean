"""ASR provider 注册表。新增 provider 在这里加一行即可。"""

from __future__ import annotations

from ..config import ASRConfig
from ..errors import ConfigError
from .base import ASRResult, BaseASRProvider

__all__ = ["ASRResult", "BaseASRProvider", "get_provider", "AVAILABLE_PROVIDERS"]

# v0.2 会加 "funasr"，v0.2+ 可选 "parakeet"
AVAILABLE_PROVIDERS = ("whisper",)


def get_provider(cfg: ASRConfig) -> BaseASRProvider:
    name = (cfg.provider or "").strip().lower()
    if name == "whisper":
        from .whisper_provider import WhisperProvider

        return WhisperProvider(cfg)
    if name in {"funasr", "parakeet"}:
        raise ConfigError(
            f"ASR provider `{name}` 还没实现（路线图 v0.2+），"
            "先把 config.yaml 里 asr.provider 改成 whisper"
        )
    raise ConfigError(
        f"未知的 ASR provider: {cfg.provider!r}，可选：{', '.join(AVAILABLE_PROVIDERS)}"
    )
