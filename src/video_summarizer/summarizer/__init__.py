"""总结 provider 注册表。新增 provider 在这里加一行即可。"""

from __future__ import annotations

from ..config import SummarizerConfig
from ..errors import ConfigError
from .base import BaseSummarizer, CostEstimate

__all__ = ["BaseSummarizer", "CostEstimate", "get_provider", "AVAILABLE_PROVIDERS"]

# v0.4 会补 "claude"（Anthropic 原生接口）和 "ollama"（本地模型）
AVAILABLE_PROVIDERS = ("openai",)


def get_provider(cfg: SummarizerConfig) -> BaseSummarizer:
    name = (cfg.provider or "").strip().lower()
    if name == "openai":
        from .openai_provider import OpenAICompatibleSummarizer

        return OpenAICompatibleSummarizer(cfg)
    if name in {"claude", "anthropic", "ollama"}:
        raise ConfigError(
            f"总结 provider `{name}` 还没实现（路线图 v0.4）。"
            "Ollama 本身提供 OpenAI 兼容接口，可以先用 provider: openai + "
            "base_url: http://localhost:11434/v1 顶上。"
        )
    raise ConfigError(
        f"未知的总结 provider: {cfg.provider!r}，可选：{', '.join(AVAILABLE_PROVIDERS)}"
    )
