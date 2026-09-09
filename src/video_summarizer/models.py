"""模块间传递的标准化中间数据结构（见 DESIGN.md 第 5 节）。

四个模块彼此解耦，只认这里定义的结构，任何一个模块的实现都可以单独替换。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


@dataclass
class Segment:
    """一条转写分句。"""

    start: float
    end: float
    text: str
    speaker: str | None = None

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class Transcript:
    """结构化转写。字幕来源和 ASR 来源产出同一个结构。"""

    source_url: str
    source_type: str  # "subtitle" | "asr"
    language: str | None
    duration_sec: float
    segments: list[Segment] = field(default_factory=list)
    title: str | None = None
    video_id: str | None = None
    # 附加信息：字幕语言、ASR 模型名等，便于追溯
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def full_text(self) -> str:
        return "\n".join(s.text for s in self.segments if s.text.strip())

    @property
    def speakers(self) -> list[str]:
        seen: list[str] = []
        for s in self.segments:
            if s.speaker and s.speaker not in seen:
                seen.append(s.speaker)
        return seen

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    @classmethod
    def load(cls, path: Path) -> "Transcript":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        segments = [Segment(**s) for s in raw.pop("segments", [])]
        return cls(segments=segments, **raw)


@dataclass
class SummaryOptions:
    """总结层的运行时选项。"""

    summary_type: str = "overall"  # overall | by_speaker | timeline | key_points
    language: str = "zh"  # 总结输出语言
    extra_instructions: str | None = None
