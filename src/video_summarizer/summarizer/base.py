"""模块四的抽象层：LLM 总结 provider 接口（DESIGN.md 3.4）。

长文本策略在基类里实现（子类只管把一次请求发出去），
这样"整篇塞进去还是 map-reduce"的判断对所有 provider 一致，
新增 provider 只需实现 `_complete()`。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..config import SummarizerConfig
from ..errors import SummarizerError
from ..models import Transcript, SummaryOptions
from . import prompts, tokens

log = logging.getLogger(__name__)

# 给 system prompt、指令模板和模型输出留的余量
_PROMPT_OVERHEAD_TOKENS = 1500


@dataclass
class CostEstimate:
    """执行前给用户看的量级估算（DESIGN.md 第 7 节坑 4）。"""

    transcript_tokens: int
    chunks: int
    strategy: str  # "single" | "map-reduce"
    estimated_input_tokens: int

    @property
    def is_chunked(self) -> bool:
        return self.strategy == "map-reduce"


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatReply:
    """chat() 的一轮回复。tool_calls 非空时 content 可能是空的。"""

    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    # 原样追加回 messages 的那条 assistant 消息（带 tool_calls 时必须带上，否则下一轮接口报错）
    message: dict[str, Any] = field(default_factory=dict)


class BaseSummarizer(ABC):
    """所有总结 provider 的基类。"""

    name: str = "base"
    # 能不能走工具调用（function calling）。不能的 provider 调 chat() 时工具被忽略
    supports_tools: bool = False

    def __init__(self, cfg: SummarizerConfig) -> None:
        self.cfg = cfg
        # 本实例累计的真实用量（provider 从响应里读到的），用来记账
        self.usage_input = 0
        self.usage_output = 0
        self.usage_calls = 0
        # 输入里命中服务端前缀缓存的部分（DeepSeek / OpenAI 会报）。只用来展示，不参与记账
        self.usage_cached = 0

    # ---------- 子类只需实现这个 ----------

    @abstractmethod
    def _complete(self, system: str, user: str) -> str:
        """发一次请求，返回模型输出的纯文本。"""

    # ---------- 以下为共用逻辑 ----------

    def describe(self) -> str:
        return f"{self.name}/{self.cfg.model}"

    def complete(self, system: str, user: str) -> str:
        """一次裸调用。问答这类不走总结模板的功能用它。"""
        return self._complete(system, user)

    def complete_json(self, system: str, user: str) -> str:
        """要求输出 JSON 的裸调用。支持 JSON 模式的 provider 覆盖它（保证语法合法，但截断照样会坏）。"""
        return self._complete(system, user)

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> ChatReply:
        """多轮消息（OpenAI 格式）。默认实现把对话压成一条 user 走 _complete，不支持工具。

        支持工具的 provider 覆盖它；工具定义用 OpenAI 的 {"type": "function", "function": {...}}。
        """
        system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
        parts = []
        for m in messages:
            role = m.get("role")
            if role == "user":
                parts.append(m.get("content") or "")
            elif role == "assistant" and m.get("content"):
                parts.append(f"（你之前的回答）\n{m['content']}")
            elif role == "tool":
                parts.append(f"（检索结果）\n{m.get('content') or ''}")
        text = self._complete(system, "\n\n".join(parts))
        return ChatReply(content=text, message={"role": "assistant", "content": text})

    def _record_usage(self, input_tokens: Any, output_tokens: Any, cached_tokens: Any = 0) -> None:
        """provider 拿到响应后调一下。读不到用量的（"?"）不计。"""
        try:
            self.usage_input += int(input_tokens or 0)
            self.usage_output += int(output_tokens or 0)
        except (TypeError, ValueError):
            return
        self.usage_calls += 1
        try:
            self.usage_cached += int(cached_tokens or 0)
        except (TypeError, ValueError):
            pass

    def take_cached(self) -> int:
        """取走并清零累计的缓存命中 token。"""
        n, self.usage_cached = self.usage_cached, 0
        return n

    def take_usage(self) -> tuple[int, int, int]:
        """取走并清零累计用量：(输入, 输出, 调用次数)。"""
        u = (self.usage_input, self.usage_output, self.usage_calls)
        self.usage_input = self.usage_output = self.usage_calls = 0
        return u

    def close(self) -> None:
        """释放连接等资源。默认无操作。"""

    def _body_budget(self) -> int:
        """单次请求里留给转写正文的 token 预算。"""
        budget = (
            self.cfg.max_context_tokens
            - self.cfg.max_output_tokens
            - _PROMPT_OVERHEAD_TOKENS
        )
        if budget <= 0:
            raise SummarizerError(
                f"上下文预算算下来是 {budget}，检查 config.yaml 里的 "
                "summarizer.max_context_tokens 和 max_output_tokens"
            )
        return budget

    def plan(self, transcript: Transcript, options: SummaryOptions) -> CostEstimate:
        """决定走单次还是 map-reduce，并给出 token 量级。不发请求。"""
        with_time = options.summary_type == "timeline" or bool(transcript.speakers)
        body = tokens.render_segments(transcript.segments, with_time=with_time)
        body_tokens = tokens.estimate_tokens(body)
        budget = self._body_budget()

        strategy = self.cfg.chunk_strategy
        if strategy == "never":
            chunked = False
        elif strategy == "always":
            chunked = True
        elif strategy == "auto":
            chunked = body_tokens > budget
        else:
            raise SummarizerError(
                f"未知的 chunk_strategy: {strategy!r}，可选：auto | always | never"
            )

        if not chunked:
            return CostEstimate(
                transcript_tokens=body_tokens,
                chunks=1,
                strategy="single",
                estimated_input_tokens=body_tokens + _PROMPT_OVERHEAD_TOKENS,
            )

        chunk_size = min(self.cfg.chunk_tokens, budget)
        chunks = tokens.chunk_segments(transcript.segments, chunk_size, with_time=with_time)
        # map 阶段每块一次请求，reduce 阶段再一次，reduce 的输入约等于各块输出之和
        reduce_input = len(chunks) * min(self.cfg.max_output_tokens, 1200)
        return CostEstimate(
            transcript_tokens=body_tokens,
            chunks=len(chunks),
            strategy="map-reduce",
            estimated_input_tokens=(
                body_tokens + len(chunks) * _PROMPT_OVERHEAD_TOKENS + reduce_input
            ),
        )

    def summarize(self, transcript: Transcript, options: SummaryOptions) -> str:
        if not transcript.segments:
            raise SummarizerError("转写是空的，没东西可总结")

        template = prompts.get_template(options.summary_type)
        header = _build_header(transcript)
        with_time = options.summary_type == "timeline" or bool(transcript.speakers)
        estimate = self.plan(transcript, options)

        if estimate.strategy == "single":
            log.info("单次总结（约 %d tokens 输入）", estimate.estimated_input_tokens)
            body = tokens.render_segments(transcript.segments, with_time=with_time)
            return self._complete(
                prompts.SYSTEM,
                prompts.build_single_pass(
                    template, header, body, options.language, options.extra_instructions
                ),
            )

        chunk_size = min(self.cfg.chunk_tokens, self._body_budget())
        chunks = tokens.chunk_segments(transcript.segments, chunk_size, with_time=with_time)
        log.info("转写过长，走 map-reduce：%d 块 + 1 次汇总", len(chunks))

        partials: list[str] = []
        for i, chunk in enumerate(chunks, start=1):
            log.info("总结第 %d/%d 块 ...", i, len(chunks))
            partials.append(
                self._complete(
                    prompts.SYSTEM,
                    prompts.build_map(
                        template,
                        header,
                        tokens.render_segments(chunk, with_time=with_time),
                        options.language,
                        options.extra_instructions,
                        index=i,
                        total=len(chunks),
                    ),
                )
            )

        log.info("汇总各块要点 ...")
        return self._complete(
            prompts.SYSTEM,
            prompts.build_reduce(
                template, header, partials, options.language, options.extra_instructions
            ),
        )


def _build_header(transcript: Transcript) -> str:
    lines = [f"视频标题：{transcript.title or '（未知）'}"]
    if transcript.duration_sec:
        lines.append(f"时长：{tokens.format_timestamp(transcript.duration_sec)}")
    lines.append(f"来源：{transcript.source_url}")
    lines.append(
        "转写来源：官方字幕" if transcript.source_type == "subtitle" else "转写来源：语音识别（可能有识别错误）"
    )
    speakers = transcript.speakers
    if speakers:
        lines.append(f"说话人标签：{', '.join(speakers)}")
    return "\n".join(lines)
