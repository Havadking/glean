"""ASR provider 注册表。新增 provider 在这里加一行即可。"""

from __future__ import annotations

from dataclasses import replace

from ..config import ASRConfig
from ..errors import ConfigError
from .base import ASRResult, BaseASRProvider
from .fallback import FallbackASRProvider

__all__ = [
    "ASRResult",
    "BaseASRProvider",
    "FallbackASRProvider",
    "get_provider",
    "AVAILABLE_PROVIDERS",
]

AVAILABLE_PROVIDERS = ("funasr", "whisper")

# 各 provider 被当作兜底时用的默认模型（config 里没写 fallback_model 时用这个）
DEFAULT_MODELS = {
    "funasr": "sensevoice-small",
    "whisper": "large-v3",
}


def _build(name: str, cfg: ASRConfig, model: str) -> BaseASRProvider:
    name = (name or "").strip().lower()
    provider_cfg = replace(cfg, model=model)

    if name == "funasr":
        from .funasr_provider import FunASRProvider

        return FunASRProvider(provider_cfg)
    if name == "whisper":
        from .whisper_provider import WhisperProvider

        return WhisperProvider(provider_cfg)
    if name == "parakeet":
        raise ConfigError(
            "ASR provider `parakeet` 还没实现（设计文档里是可选加速项）。"
            f"可选：{', '.join(AVAILABLE_PROVIDERS)}"
        )
    raise ConfigError(
        f"未知的 ASR provider: {name!r}，可选：{', '.join(AVAILABLE_PROVIDERS)}"
    )


def get_provider(cfg: ASRConfig) -> BaseASRProvider:
    """按配置构造 provider；配了 fallback 就套一层兜底包装。"""
    primary = _build(cfg.provider, cfg, cfg.model)

    fallback_name = (cfg.fallback or "").strip().lower()
    if not fallback_name or fallback_name == (cfg.provider or "").strip().lower():
        return primary

    fallback_model = cfg.fallback_model or DEFAULT_MODELS.get(fallback_name)
    if not fallback_model:
        raise ConfigError(
            f"兜底 provider `{fallback_name}` 没有默认模型，"
            "请在 config.yaml 里补上 asr.fallback_model"
        )
    return FallbackASRProvider(primary, _build(fallback_name, cfg, fallback_model), cfg.language)
