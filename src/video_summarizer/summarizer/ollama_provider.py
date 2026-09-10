"""Ollama 本地模型实现（DESIGN.md 3.4 的 OllamaSummarizer）。

零成本、不出网，代价是本地模型的上下文窗口小，长转写基本都要走 map-reduce。

**最大的坑是 num_ctx**：Ollama 默认只给 4096 的上下文，超出的部分会被**静默丢弃**
——不报错、不警告，你只会发现总结莫名其妙漏掉了后半段内容。所以这里必须
显式按配置把 num_ctx 传下去。这也是没有直接复用 Ollama 的 OpenAI 兼容端点的原因：
那条路没法传这个参数。

注意 num_ctx 开大会显著吃显存，12GB 显存跑 8B 模型时 32K 上下文差不多是上限，
所以 config.yaml 里的 max_context_tokens 要按显存设，不能照抄云端模型的值。
"""

from __future__ import annotations

import logging

from ..config import SummarizerConfig
from ..errors import ConfigError, SummarizerError
from .base import BaseSummarizer

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://localhost:11434"
# 本地生成慢，长转写单次跑几分钟很正常
REQUEST_TIMEOUT_SEC = 1800.0


class OllamaSummarizer(BaseSummarizer):
    name = "ollama"

    def __init__(self, cfg: SummarizerConfig) -> None:
        super().__init__(cfg)
        self._client = None

    def describe(self) -> str:
        return f"ollama/{self.cfg.model}"

    @property
    def _base_url(self) -> str:
        url = (self.cfg.base_url or DEFAULT_BASE_URL).rstrip("/")
        # 配置里可能沿用了 OpenAI 兼容那套写法，末尾带 /v1；原生接口不需要
        if url.endswith("/v1"):
            url = url[: -len("/v1")]
        return url

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - 环境问题
            raise SummarizerError("没装 httpx，跑 `uv sync` 装上") from exc
        self._client = httpx.Client(timeout=REQUEST_TIMEOUT_SEC)
        return self._client

    def _complete(self, system: str, user: str) -> str:
        import httpx

        client = self._get_client()
        if not self.cfg.model:
            raise ConfigError("config.yaml 里 summarizer.model 没填，例如 qwen3:8b")

        payload = {
            "model": self.cfg.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "options": {
                # 不传这个就是默认 4096，超出部分被静默丢弃
                "num_ctx": self.cfg.max_context_tokens,
                "num_predict": self.cfg.max_output_tokens,
                "temperature": self.cfg.temperature,
            },
        }

        try:
            response = client.post(f"{self._base_url}/api/chat", json=payload)
        except httpx.ConnectError as exc:
            raise SummarizerError(
                f"连不上 Ollama（{self._base_url}）。确认它在跑：`ollama serve`；"
                f"如果装在别的机器上，改 config.yaml 里的 summarizer.base_url。\n原始错误：{exc}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise SummarizerError(
                f"Ollama 超过 {REQUEST_TIMEOUT_SEC:.0f} 秒没返回。"
                "本地模型跑长转写很慢，可以把 chunk_tokens 调小让每块短一些。"
            ) from exc

        if response.status_code == 404:
            raise ConfigError(
                f"Ollama 上没有模型 `{self.cfg.model}`，先拉下来：\n"
                f"    ollama pull {self.cfg.model}\n"
                f"（`ollama list` 可以看已有哪些）"
            )
        if response.status_code >= 400:
            raise SummarizerError(
                f"Ollama 返回 HTTP {response.status_code}: {response.text[:400]}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise SummarizerError(f"Ollama 返回的不是 JSON: {response.text[:200]}") from exc

        text = ((data.get("message") or {}).get("content") or "").strip()
        if not text:
            raise SummarizerError(
                f"Ollama 返回了空内容（done_reason={data.get('done_reason')}）"
            )

        prompt_tokens = data.get("prompt_eval_count")
        if prompt_tokens and prompt_tokens >= self.cfg.max_context_tokens:
            log.warning(
                "输入吃满了 num_ctx（%s / %s），超出的部分已经被 Ollama 丢掉，"
                "总结可能漏内容。把 summarizer.chunk_tokens 调小，或者加大 max_context_tokens。",
                prompt_tokens, self.cfg.max_context_tokens,
            )
        log.info(
            "本次调用 token：输入 %s / 输出 %s",
            prompt_tokens or "?", data.get("eval_count") or "?",
        )
        return text

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
