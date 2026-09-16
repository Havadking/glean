"""ASR provider 注册表。新增 provider 在这里加一行即可。"""

from __future__ import annotations

import logging
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
    "describe",
    "AVAILABLE_PROVIDERS",
]

log = logging.getLogger(__name__)

AVAILABLE_PROVIDERS = ("funasr", "whisper")

# 各 provider 被当作兜底时用的默认模型（config 里没写 fallback_model 时用这个）
DEFAULT_MODELS = {
    "funasr": "fun-asr-nano",
    "whisper": "large-v3",
}


def _build(name: str, cfg: ASRConfig, model: str, diarize: bool = False,
           hotwords: list[str] | None = None) -> BaseASRProvider:
    name = (name or "").strip().lower()
    provider_cfg = replace(cfg, model=model)

    if name == "funasr":
        from .funasr_provider import FunASRProvider

        return FunASRProvider(provider_cfg, diarize=diarize, hotwords=hotwords)
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


def get_provider(cfg: ASRConfig, diarize: bool = False,
                 hotwords: list[str] | None = None) -> BaseASRProvider:
    """按配置构造 provider；配了 fallback 就套一层兜底包装。

    diarize=True 时要求 provider 输出说话人标签。目前只有 FunASR 支持，
    whisper 没有这个能力，所以兜底那一路仍然是无标签的转写 —— 总比没有强。
    hotwords 是希望模型优先写出的专名（来自词表）；不支持热词的 provider 忽略。
    """
    primary = _build(cfg.provider, cfg, cfg.model, diarize=diarize, hotwords=hotwords)
    if diarize and not primary.supports_diarization:
        raise ConfigError(
            f"ASR provider `{cfg.provider}` 不支持说话人分离，"
            "把 config.yaml 里 asr.provider 改成 funasr，或者关掉 asr.diarize"
        )

    fallback_name = (cfg.fallback or "").strip().lower()
    if not fallback_name or fallback_name == (cfg.provider or "").strip().lower():
        return primary

    fallback_model = cfg.fallback_model or DEFAULT_MODELS.get(fallback_name)
    if not fallback_model:
        raise ConfigError(
            f"兜底 provider `{fallback_name}` 没有默认模型，"
            "请在 config.yaml 里补上 asr.fallback_model"
        )
    fallback = _build(fallback_name, cfg, fallback_model, hotwords=hotwords)
    if diarize and not fallback.supports_diarization:
        log.info("兜底 provider %s 不支持说话人分离，真走到兜底时转写不会带说话人标签", fallback_name)
    return FallbackASRProvider(primary, fallback, cfg.language)


def describe(cfg: ASRConfig) -> str:
    """日志里用的 provider 名，和 get_provider 返回对象的 .name 一致，但不构造 provider。"""
    primary = (cfg.provider or "").strip().lower()
    fallback = (cfg.fallback or "").strip().lower()
    return f"{primary}+{fallback}" if fallback and fallback != primary else primary
