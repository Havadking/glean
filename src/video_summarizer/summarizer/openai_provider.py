"""OpenAI 兼容接口实现。

一份代码覆盖 OpenAI 官方和所有 OpenAI 兼容的国内厂商（DeepSeek / 通义 / Kimi / 智谱 等），
差别只在 config.yaml 里的 base_url + model + api_key_env。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..config import SummarizerConfig
from ..errors import ConfigError, SummarizerError
from .base import BaseSummarizer, ChatReply, ToolCall

log = logging.getLogger(__name__)

_MAX_RETRIES = 3
_BACKOFF_SEC = 4


class OpenAICompatibleSummarizer(BaseSummarizer):
    name = "openai"
    supports_tools = True

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

    def _create(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None):
        """发一次请求，带重试和记账。返回第一条 choice 的 message。"""
        client = self._get_client()
        last_error: Exception | None = None
        kwargs: dict[str, Any] = {
            "model": self.cfg.model, "messages": messages,
            "temperature": self.cfg.temperature, "max_tokens": self.cfg.max_output_tokens,
        }
        if tools:
            kwargs["tools"] = tools

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                response = client.chat.completions.create(**kwargs)
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
                cached = _cached_tokens(usage)
                log.info(
                    "本次调用 token：输入 %s（缓存命中 %s）/ 输出 %s",
                    getattr(usage, "prompt_tokens", "?"), cached,
                    getattr(usage, "completion_tokens", "?"),
                )
                self._record_usage(getattr(usage, "prompt_tokens", 0),
                                   getattr(usage, "completion_tokens", 0), cached)

            if not response.choices:
                raise SummarizerError("模型没有返回任何内容")
            return response.choices[0].message

        raise SummarizerError(f"调用 {self.describe()} 失败: {last_error}") from last_error

    def _complete(self, system: str, user: str) -> str:
        message = self._create([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ])
        content = (message.content or "").strip()
        if not content:
            raise SummarizerError("模型返回了空内容，可能触发了内容过滤或 max_tokens 太小")
        return content

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> ChatReply:
        message = self._create(messages, tools)
        content = (message.content or "").strip()
        calls: list[ToolCall] = []
        raw_calls: list[dict[str, Any]] = []
        for tc in getattr(message, "tool_calls", None) or []:
            fn = tc.function
            try:
                args = json.loads(fn.arguments or "{}")
            except ValueError:
                args = {}
            calls.append(ToolCall(id=tc.id, name=fn.name, arguments=args if isinstance(args, dict) else {}))
            raw_calls.append({"id": tc.id, "type": "function",
                              "function": {"name": fn.name, "arguments": fn.arguments or "{}"}})
        if not content and not calls:
            raise SummarizerError("模型返回了空内容，可能触发了内容过滤或 max_tokens 太小")
        msg: dict[str, Any] = {"role": "assistant", "content": content}
        if raw_calls:
            msg["tool_calls"] = raw_calls
        return ChatReply(content=content, tool_calls=calls, message=msg)


def _cached_tokens(usage: Any) -> int:
    """DeepSeek 报 prompt_cache_hit_tokens，OpenAI 报 prompt_tokens_details.cached_tokens。"""
    hit = getattr(usage, "prompt_cache_hit_tokens", None)
    if hit is None:
        details = getattr(usage, "prompt_tokens_details", None)
        hit = getattr(details, "cached_tokens", None) if details is not None else None
    try:
        return int(hit or 0)
    except (TypeError, ValueError):
        return 0


def _is_retryable(exc: Exception) -> bool:
    """限流、超时、5xx 值得重试；鉴权和参数错误重试也没用。"""
    name = type(exc).__name__
    if name in {"RateLimitError", "APITimeoutError", "APIConnectionError", "InternalServerError"}:
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and status >= 500
