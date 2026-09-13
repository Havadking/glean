"""OpenAI 兼容接口实现。

一份代码覆盖 OpenAI 官方和所有 OpenAI 兼容的国内厂商（DeepSeek / 通义 / Kimi / 智谱 等），
差别只在 config.yaml 里的 base_url + model + api_key_env。
"""

from __future__ import annotations

import logging
import time

from ..config import SummarizerConfig
from ..errors import ConfigError, SummarizerError
from .base import BaseSummarizer

log = logging.getLogger(__name__)

_MAX_RETRIES = 3
_BACKOFF_SEC = 4


class OpenAICompatibleSummarizer(BaseSummarizer):
    name = "openai"

    def __init__(self, cfg: SummarizerConfig) -> None:
        super().__init__(cfg)
        self._client = None

    def describe(self) -> str:
        host = (self.cfg.base_url or "api.openai.com").split("//")[-1].split("/")[0]
        return f"{host}/{self.cfg.model}"

    def _get_client(self):
        if self._client is not None:
            return self._client

        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - 环境问题
            raise SummarizerError("没装 openai 包，先跑 `uv sync`") from exc

        api_key = self.cfg.api_key
        if not api_key:
            raise ConfigError(
                f"环境变量 {self.cfg.api_key_env} 是空的。"
                f"把 .env.example 复制成 .env 并填入 key（config.yaml 里 "
                f"summarizer.api_key_env 决定读哪个变量）。"
            )

        self._client = OpenAI(
            api_key=api_key,
            base_url=self.cfg.base_url or None,
            timeout=600.0,
            max_retries=0,  # 重试逻辑自己控，好打日志
        )
        return self._client

    def _complete(self, system: str, user: str) -> str:
        client = self._get_client()
        last_error: Exception | None = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                response = client.chat.completions.create(
                    model=self.cfg.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    temperature=self.cfg.temperature,
                    max_tokens=self.cfg.max_output_tokens,
                )
            except Exception as exc:  # noqa: BLE001 - SDK 异常类型较多，统一兜住
                last_error = exc
                if attempt == _MAX_RETRIES or not _is_retryable(exc):
                    break
                wait = _BACKOFF_SEC * attempt
                log.warning("请求失败（第 %d/%d 次），%ds 后重试：%s",
                            attempt, _MAX_RETRIES, wait, exc)
                time.sleep(wait)
                continue

            usage = getattr(response, "usage", None)
            if usage is not None:
                log.info(
                    "本次调用 token：输入 %s / 输出 %s",
                    getattr(usage, "prompt_tokens", "?"),
                    getattr(usage, "completion_tokens", "?"),
                )
                self._record_usage(getattr(usage, "prompt_tokens", 0), getattr(usage, "completion_tokens", 0))

            if not response.choices:
                raise SummarizerError("模型没有返回任何内容")
            content = (response.choices[0].message.content or "").strip()
            if not content:
                raise SummarizerError("模型返回了空内容，可能触发了内容过滤或 max_tokens 太小")
            return content

        raise SummarizerError(f"调用 {self.describe()} 失败: {last_error}") from last_error


def _is_retryable(exc: Exception) -> bool:
    """限流、超时、5xx 值得重试；鉴权和参数错误重试也没用。"""
    name = type(exc).__name__
    if name in {"RateLimitError", "APITimeoutError", "APIConnectionError", "InternalServerError"}:
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and status >= 500
