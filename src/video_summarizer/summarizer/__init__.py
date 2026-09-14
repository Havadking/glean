"""总结 provider 注册表。新增 provider 在这里加一行即可。"""

from __future__ import annotations

from ..config import SummarizerConfig
from ..errors import ConfigError
from .base import BaseSummarizer, CostEstimate

__all__ = ["BaseSummarizer", "CostEstimate", "get_provider", "AVAILABLE_PROVIDERS"]

AVAILABLE_PROVIDERS = ("openai", "claude", "ollama")

# 别名：文档和习惯上的叫法都认
_ALIASES = {
    "anthropic": "claude",
    "deepseek": "openai",   # 以及其他一切 OpenAI 兼容接口
    "qwen": "openai",
    "dashscope": "openai",
    "moonshot": "openai",
    "kimi": "openai",
    "zhipu": "openai",
}


def get_provider(cfg: SummarizerConfig) -> BaseSummarizer:
    raw = (cfg.provider or "").strip()
    name = raw.split("#")[0].strip().lower()
    name = _ALIASES.get(name, name)

    if name == "openai":
        from .openai_provider import OpenAICompatibleSummarizer

        return OpenAICompatibleSummarizer(cfg)
    if name == "claude":
        from .claude_provider import ClaudeSummarizer

        return ClaudeSummarizer(cfg)
    if name == "ollama":
        from .ollama_provider import OllamaSummarizer

        return OllamaSummarizer(cfg)

    raise ConfigError(
        f"未知的总结 provider: {cfg.provider!r}，可选：{', '.join(AVAILABLE_PROVIDERS)}"
        f"（国内厂商的 OpenAI 兼容接口都走 openai，改 base_url 即可）"
    )
