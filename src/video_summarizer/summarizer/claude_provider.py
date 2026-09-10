"""Anthropic 原生接口实现（DESIGN.md 3.4 的 ClaudeSummarizer）。

为什么不复用 OpenAI 兼容那条路：Claude 的上下文窗口大得多（当前一代 1M），
整篇长转写基本不用切块，总结更连贯；而且原生接口才能用 effort 这类参数控制成本。

三个和 OpenAI 那边不一样、写错了会直接 400 或者静默截断的地方，都在下面注释里说明。
"""

from __future__ import annotations

import logging

from ..config import SummarizerConfig
from ..errors import ConfigError, SummarizerError
from .base import BaseSummarizer

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5"

# 带思考的模型里，思考消耗的 token 也算进 max_tokens。留太小的话
# 思考还没结束预算就用光了，正文会被截断。
MIN_SANE_MAX_TOKENS = 8192


class ClaudeSummarizer(BaseSummarizer):
    name = "claude"

    def __init__(self, cfg: SummarizerConfig) -> None:
        super().__init__(cfg)
        self._client = None

    def describe(self) -> str:
        return f"anthropic/{self.cfg.model}"

    def _get_client(self):
        if self._client is not None:
            return self._client

        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - 环境问题
            raise SummarizerError("没装 anthropic 包，跑 `uv sync` 装上") from exc

        api_key = self.cfg.api_key
        if not api_key:
            raise ConfigError(
                f"环境变量 {self.cfg.api_key_env} 是空的。"
                f"把 .env.example 复制成 .env 并填入 key（config.yaml 里 "
                f"summarizer.api_key_env 决定读哪个变量）。"
            )

        kwargs = {"api_key": api_key, "timeout": 900.0, "max_retries": 3}
        if self.cfg.base_url:
            kwargs["base_url"] = self.cfg.base_url  # 走代理或自建中转时才需要
        self._client = anthropic.Anthropic(**kwargs)
        return self._client

    def _complete(self, system: str, user: str) -> str:
        import anthropic

        client = self._get_client()

        if self.cfg.max_output_tokens < MIN_SANE_MAX_TOKENS:
            log.warning(
                "max_output_tokens 只有 %d。Claude 当前一代默认开着思考，"
                "思考的 token 也算进这个额度，容易还没写完就被截断，建议调到 16000 以上。",
                self.cfg.max_output_tokens,
            )

        params = {
            "model": self.cfg.model or DEFAULT_MODEL,
            "max_tokens": self.cfg.max_output_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        # effort 控制思考深度和整体花费，是 Claude 这边最直接的成本旋钮。
        # 注意它在 output_config 里面，不是顶层参数。
        if self.cfg.effort:
            params["output_config"] = {"effort": self.cfg.effort}

        # 刻意不传 temperature：当前一代 Claude 已经移除采样参数，传了直接 400。
        # 配置里的 temperature 只对 OpenAI 兼容和 Ollama 那两条路生效。

        try:
            # 用流式而不是一次性返回：转写可能有十几万 token，
            # 非流式请求容易撞上 HTTP 超时。get_final_message() 会等到收完再给完整结果。
            with client.messages.stream(**params) as stream:
                message = stream.get_final_message()
        except anthropic.AuthenticationError as exc:
            raise ConfigError(f"{self.cfg.api_key_env} 无效或已失效: {exc}") from exc
        except anthropic.PermissionDeniedError as exc:
            raise ConfigError(f"这个 key 没有调用 {self.cfg.model} 的权限: {exc}") from exc
        except anthropic.NotFoundError as exc:
            raise ConfigError(
                f"模型名 `{self.cfg.model}` 不存在: {exc}\n"
                f"当前一代可选：claude-opus-5 / claude-sonnet-5 / claude-haiku-4-5"
            ) from exc
        except anthropic.BadRequestError as exc:
            raise SummarizerError(f"请求被拒绝（参数或内容问题）: {exc}") from exc
        except anthropic.RateLimitError as exc:
            raise SummarizerError(f"触发限流，SDK 重试后仍然失败: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise SummarizerError(f"调用 {self.describe()} 失败（HTTP {exc.status_code}）: {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise SummarizerError(f"连不上 Anthropic 接口: {exc}") from exc

        return self._extract_text(message)

    def _extract_text(self, message) -> str:
        stop_reason = getattr(message, "stop_reason", None)

        if stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) or "未说明"
            raise SummarizerError(
                f"模型拒绝了这次请求（类别：{category}）。"
                "转写内容可能触发了安全策略，可以换个总结类型或换个 provider 试试。"
            )

        # content 里可能混着 thinking 块，只取 text
        text = "\n".join(
            block.text for block in (message.content or []) if getattr(block, "type", None) == "text"
        ).strip()

        if stop_reason == "max_tokens":
            log.warning(
                "输出被 max_tokens=%d 截断了。Claude 的思考也占这个额度，"
                "把 config.yaml 里 summarizer.max_output_tokens 调大，或者把 effort 调低。",
                self.cfg.max_output_tokens,
            )

        if not text:
            raise SummarizerError(
                f"模型没有返回正文（stop_reason={stop_reason}）。"
                "如果 max_output_tokens 偏小，可能全被思考消耗掉了。"
            )

        usage = getattr(message, "usage", None)
        if usage is not None:
            log.info(
                "本次调用 token：输入 %s / 输出 %s",
                getattr(usage, "input_tokens", "?"),
                getattr(usage, "output_tokens", "?"),
            )
        return text
