"""模块三的抽象层：ASR provider 接口。

v0.1 只有 whisper 一个实现；v0.2 起加 FunASR（默认）+ 说话人分离，
新增 provider 只需实现这个接口并在 registry 里注册，主流程不变。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import Segment


@dataclass
class ASRResult:
    """识别结果。支持说话人分离的 provider 负责填 Segment.speaker。"""

    segments: list[Segment]
    language: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class BaseASRProvider(ABC):
    """所有 ASR provider 的基类。"""

    name: str = "base"
    supports_diarization: bool = False
    # provider 支持的语言（ISO 639-1）。None 表示不设限，由 provider 自己判断。
    supported_languages: frozenset[str] | None = None

    @abstractmethod
    def transcribe(self, audio_path: Path) -> ASRResult:
        """把音频转成带时间轴的分句。"""

    def supports_language(self, language: str | None) -> bool:
        """语言是否在能力范围内。language 为 None（自动检测）时一律返回 True。"""
        if language is None or self.supported_languages is None:
            return True
        return language.split("-")[0].lower() in self.supported_languages

    def close(self) -> None:
        """释放显存等资源。默认无操作。"""
