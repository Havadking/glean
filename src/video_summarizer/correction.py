"""转写纠错：让模型给 ASR 转写出一张专有名词替换表，程序校验后叠加在原文上。

中文 ASR 的专名错几乎全是同音字（"语数科技"→"宇树科技"、"一焕方"→"幻方"），
模型靠上下文和世界知识纠这个是强项。但不让它重写全文：
- 重写全文的输出和输入一样长，贵一倍、慢几十倍，还会漏行、并句、改数字
- 替换表只有几百 token，每一处改动都能列出来、能否决

"只依据转写内容"是总结那边的铁律，这里恰恰要求模型用背景知识，所以幻觉方向反了：
模型会把它不认识但本来正确的名字"修"成它认识的名字。三层兜底：
prompt 约束、下面的校验规则（比 prompt 可靠）、界面上逐条否决。

原文永远不动。替换表存在 transcript.meta["corrections"] 里，读的时候再应用
（和 cleaning 一样是读时变换），时间轴和分句数都不变。
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from .models import Segment, Transcript
from .summarizer.base import BaseSummarizer
from .summarizer.tokens import chunk_segments, render_segments

log = logging.getLogger(__name__)

# prompt 或校验规则改了就 +1，缓存据此认出旧结果
PROMPT_VERSION = 1

SYSTEM = (
    "你是语音转写的校对员。转写来自自动语音识别，专有名词常被写成同音或近音的错字。"
    "你的任务是找出这些错字，输出一张替换表。规则：\n\n"
    "1. 只改人名、公司/机构名、产品名、专业术语、地名。不改口语、语法、语气词、断句。\n"
    "2. 只在读音相同或相近时才改（\"语数科技\"→\"宇树科技\"）。读音不同的一律不改。\n"
    "3. 数字、金额、日期、百分比一律不碰，即使看起来不对。\n"
    "4. 拿不准就不改。宁可漏改，不要改错。\n"
    "5. 只输出 JSON 数组：[{\"from\": \"原文\", \"to\": \"改后\", \"why\": \"一句话依据\"}]。"
    "from 必须是转写里原样出现的文字。没有要改的就输出 []。"
)

# 一张表最多留这么多条，多了按出现次数取
MAX_ITEMS = 60
# from / to 的长度差上限：改整句的直接丢
MAX_LEN_DIFF = 2
# 单字替换太容易误伤
MIN_SRC_LEN = 2
MAX_LEN = 30
# 已知术语喂给模型的上限
MAX_KNOWN = 80

STATE_APPLIED = "applied"
STATE_REJECTED = "rejected"

# 先按最外层的中括号抓（数组里是对象，本身不含中括号）；模型在前后废话里带了中括号就退回到不含嵌套的那种
_JSON_ARRAY_RES = (re.compile(r"\[.*\]", re.S), re.compile(r"\[[^\[\]]*\]", re.S))
# 替换不许带句末标点和换行：分段是按标点切的，动了会改变段落数
_FORBIDDEN_RE = re.compile(r"[\n\r。！？…；;.!?]")


@dataclass
class Correction:
    src: str
    dst: str
    why: str = ""
    hits: int = 0
    state: str = STATE_APPLIED

    @property
    def applied(self) -> bool:
        return self.state == STATE_APPLIED

    def to_dict(self) -> dict[str, Any]:
        return {"from": self.src, "to": self.dst, "why": self.why, "hits": self.hits, "state": self.state}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Correction":
        return cls(
            src=str(raw.get("from", "")), dst=str(raw.get("to", "")), why=str(raw.get("why", "") or ""),
            hits=int(raw.get("hits", 0) or 0),
            state=STATE_REJECTED if raw.get("state") == STATE_REJECTED else STATE_APPLIED,
        )


# ---------- prompt 与解析 ----------


def build_prompt(*, title: str | None, uploader: str | None, text: str, known: list[str]) -> str:
    parts = [f"视频标题：{title or '（无）'}"]
    if uploader:
        parts.append(f"UP 主：{uploader}")
    if known:
        parts.append("已知术语（转写里出现近音写法时优先对齐到这些）：" + "、".join(known[:MAX_KNOWN]))
    parts += ["", "=== 转写 ===", text.strip(), "=== 结束 ===", "", "请输出替换表 JSON 数组："]
    return "\n".join(parts)


def parse(text: str) -> list[dict[str, str]]:
    """模型输出 → [{from, to, why}]。抓第一个能解析成对象数组的 JSON 数组，抓不到就当空表。"""
    for pattern in _JSON_ARRAY_RES:
        for m in pattern.finditer(text or ""):
            try:
                arr = json.loads(m.group(0))
            except ValueError:
                continue
            if isinstance(arr, list) and any(isinstance(x, dict) for x in arr):
                return _items_from(arr)
    # 空数组也是合法回答（没有要改的）
    return []


def _items_from(arr: list) -> list[dict[str, str]]:
    out = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        src, dst = str(item.get("from", "")).strip(), str(item.get("to", "")).strip()
        if src and dst:
            out.append({"from": src, "to": dst, "why": str(item.get("why", "") or "").strip()})
    return out


# ---------- 校验 ----------


def validate(items: list[dict[str, str]], full_text: str) -> list[Correction]:
    """不过的一律丢。这是防幻觉的关键一层。"""
    # V5：同一个 from 多个 to，取出现最多的那个
    votes: dict[str, Counter] = defaultdict(Counter)
    whys: dict[tuple[str, str], str] = {}
    for it in items:
        src, dst = it["from"], it["to"]
        votes[src][dst] += 1
        whys.setdefault((src, dst), it.get("why", ""))

    picked: list[Correction] = []
    for src, counter in votes.items():
        dst, _n = counter.most_common(1)[0]
        # V4：得真的改了东西；单字替换不要
        if dst == src or len(src) < MIN_SRC_LEN or len(src) > MAX_LEN or len(dst) > MAX_LEN:
            continue
        # V2：数字交给人
        if any(ch.isdigit() for ch in src) or any(ch.isdigit() for ch in dst):
            continue
        # V3：长度差太大就是在改句子
        if abs(len(dst) - len(src)) > MAX_LEN_DIFF:
            continue
        if _FORBIDDEN_RE.search(src) or _FORBIDDEN_RE.search(dst):
            continue
        # V1：from 得原样出现
        hits = full_text.count(src)
        if hits == 0:
            continue
        picked.append(Correction(src=src, dst=dst, why=whys.get((src, dst), ""), hits=hits))

    # V6：一条的 from 出现在另一条的 to 里，会链式替换，丢掉前者
    picked = [c for c in picked if not any(c.src in other.dst for other in picked if other is not c)]

    picked.sort(key=lambda c: (-c.hits, c.src))
    return picked[:MAX_ITEMS]


# ---------- 应用 ----------


def apply_text(text: str, items: list[Correction]) -> str:
    """长的先换，"语数科技"和"语数"都在表里时不会互相干扰。"""
    if not text or not items:
        return text
    for c in sorted(items, key=lambda c: -len(c.src)):
        if c.applied and c.src in text:
            text = text.replace(c.src, c.dst)
    return text


def apply_segments(segments: list[Segment], items: list[Correction]) -> list[Segment]:
    """只改 text，时间轴和分句数原样。"""
    active = [c for c in items if c.applied]
    if not active:
        return segments
    return [replace(s, text=apply_text(s.text, active)) for s in segments]


def items_of(transcript: Transcript) -> list[Correction]:
    block = transcript.meta.get("corrections")
    if not isinstance(block, dict):
        return []
    return [Correction.from_dict(r) for r in block.get("items", []) if isinstance(r, dict)]


def applied_items(transcript: Transcript) -> list[Correction]:
    return [c for c in items_of(transcript) if c.applied]


def apply(transcript: Transcript) -> Transcript:
    """按 meta 里的替换表得到修正后的转写。没有表就原样返回（同一个对象）。"""
    active = applied_items(transcript)
    if not active:
        return transcript
    return replace(transcript, segments=apply_segments(transcript.segments, active),
                   meta={**transcript.meta, "corrected": True})


def fingerprint(transcript: Transcript) -> str:
    """生效的替换表拍成一个短串，掺进总结缓存的 key。没有替换表返回空串。"""
    active = applied_items(transcript)
    if not active:
        return ""
    return "|".join(f"{c.src}>{c.dst}" for c in sorted(active, key=lambda c: c.src))


def set_items(transcript: Transcript, items: list[Correction], *, provider_desc: str) -> Transcript:
    """把一张新表写进 meta。之前被否决过的同一条保持否决，不然重跑一次又冒出来。"""
    rejected = {(c.src, c.dst) for c in items_of(transcript) if not c.applied}
    merged = [replace(c, state=STATE_REJECTED) if (c.src, c.dst) in rejected else c for c in items]
    block = {
        "provider": provider_desc,
        "prompt_version": PROMPT_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "items": [c.to_dict() for c in merged],
    }
    return replace(transcript, meta={**transcript.meta, "corrections": block})


def set_state(transcript: Transcript, index: int, state: str) -> Transcript:
    """否决或恢复第 index 条。越界抛 IndexError。"""
    if state not in (STATE_APPLIED, STATE_REJECTED):
        raise ValueError(f"未知状态 {state!r}")
    block = transcript.meta.get("corrections")
    if not isinstance(block, dict):
        raise IndexError(index)
    items = list(block.get("items", []))
    items[index] = {**items[index], "state": state}
    return replace(transcript, meta={**transcript.meta, "corrections": {**block, "items": items}})


# ---------- 调模型 ----------


def suggest(provider: BaseSummarizer, transcript: Transcript, *, title: str | None,
            uploader: str | None, known: list[str] | None = None) -> list[Correction]:
    """让模型出表并校验。全文超预算就切块，每块独立出表，最后合并去重。"""
    if not transcript.segments:
        return []
    chunk_size = min(provider.cfg.chunk_tokens, provider._body_budget())
    chunks = chunk_segments(transcript.segments, chunk_size, with_time=False)
    raw: list[dict[str, str]] = []
    for i, chunk in enumerate(chunks, start=1):
        body = render_segments(chunk, with_time=False)
        if len(chunks) > 1:
            log.info("纠错：第 %d/%d 段", i, len(chunks))
        user = build_prompt(title=title, uploader=uploader, text=body, known=known or [])
        raw.extend(parse(provider.complete(SYSTEM, user)))
    return validate(raw, transcript.full_text)


def polish(provider: BaseSummarizer, transcript: Transcript, *, title: str | None,
           uploader: str | None, known: list[str] | None = None) -> Transcript:
    """跑一遍纠错并把表写进 meta。返回的仍是原文转写（表在 meta 里）。"""
    items = suggest(provider, transcript, title=title, uploader=uploader, known=known)
    log.info("纠错：模型给出 %d 条替换，%d 处命中", len(items), sum(c.hits for c in items))
    return set_items(transcript, items, provider_desc=provider.describe())
