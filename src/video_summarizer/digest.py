"""回顾：一段时间（一周 / 一天）里收藏的视频，让模型做一次跨视频的综合。

不是定时任务。这个程序是本地服务，晚上八点没开就什么都不会发生，开着也没有推送渠道；
所以做成按需：打开回顾页时才生成，生成过的存进 cache.sqlite，没新视频就直接看上次的。

默认粒度是周。一天一两条视频，模型能做的只是复述总结，没有信息增量；
一周十来条才有东西可综合 —— 「本周 8 条，5 条护肤 3 条数码；两位 UP 主对某成分的说法相反」。

材料和「问 UP 主」一样，用每条视频的总结（优先「总体」），不是整篇转写。
时间按本地日期算：created_at 存的是 UTC，晚上十一点收藏的东西不该算到明天。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .summarizer.base import BaseSummarizer
from .summarizer.tokens import estimate_tokens

PERIODS = ("week", "day")

SYSTEM = (
    "你是用户的个人知识库助手。用户会给你 TA 在一段时间里收藏的所有视频的内容摘要"
    "（每条带编号、标题、UP 主、日期、标签），请写一份这段时间的回顾。\n\n"
    "回顾的价值在于**跨视频的综合**，不是逐条复述：\n"
    "1. 先用一两句话概括这段时间收藏的东西大体围绕什么（几个主题、各占多少）。\n"
    "2. 按主题分组，每组说清楚这些视频合起来讲了什么、有哪些具体可用的结论或数据。"
    "同一主题下不同视频说法不一致或互相补充的，明确指出来。\n"
    "3. 最后给出「值得回头看」：一两条最有价值的视频，说明为什么。\n\n"
    "规则：\n"
    "- **只依据给出的材料**，材料里没有的信息不要写，不要补充背景知识。\n"
    "- **每条结论都标注来自哪条视频**，用方括号加编号，如 【2】，放在句末；多条就标多个，如 【1】【3】。\n"
    "- 用 Markdown，`##` 做分组标题，能用列表就用列表。整体控制在 400 字以内，视频少就更短。\n"
    "- 不要复述规则，不要加开场白，不要用「本周」以外的时间称呼（材料会告诉你是哪段时间）。"
)

CITE_INDEX_RE = re.compile(r"【(\d{1,3})】")


@dataclass
class Material:
    index: int
    video_id: str
    title: str
    uploader: str | None
    date: str            # 本地日期 YYYY-MM-DD
    tags: list[str]
    kind: str            # 用的是哪种总结 / transcript
    text: str


# ---------- 时间 ----------


def local_date(iso: str) -> date:
    """UTC ISO 时间 → 本地日期。老产物只有文件修改时间（无时区），当本地时间处理。"""
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return date.today()
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.date()


def period_key(period: str, d: date) -> str:
    if period == "day":
        return d.isoformat()
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def period_range(period: str, key: str) -> tuple[date, date]:
    """key → (起, 止)，都含。周是 ISO 周，周一到周日。"""
    if period == "day":
        d = date.fromisoformat(key)
        return d, d
    m = re.fullmatch(r"(\d{4})-W(\d{2})", key)
    if not m:
        raise ValueError(f"不认识的周：{key!r}")
    start = date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
    return start, start + timedelta(days=6)


def shift_key(period: str, key: str, n: int) -> str:
    """往前 / 往后挪 n 个周期。"""
    start, _ = period_range(period, key)
    step = timedelta(days=7 if period == "week" else 1)
    return period_key(period, start + step * n)


def period_label(period: str, key: str) -> str:
    start, end = period_range(period, key)
    if period == "day":
        return f"{start.month} 月 {start.day} 日"
    if start.month == end.month:
        return f"{start.month} 月 {start.day} 日 – {end.day} 日"
    return f"{start.month} 月 {start.day} 日 – {end.month} 月 {end.day} 日"


# ---------- 生成 ----------


def build_prompt(period: str, key: str, materials: list[Material]) -> str:
    parts = [
        f"时间段：{period_label(period, key)}（{'这一周' if period == 'week' else '这一天'}）",
        f"收藏的视频数：{len(materials)}",
        "",
    ]
    for m in materials:
        head = f"=== 【{m.index}】{m.title}"
        if m.uploader:
            head += f" · {m.uploader}"
        head += f"（{m.date}"
        if m.tags:
            head += "，标签：" + "、".join(m.tags)
        head += "）==="
        parts.append(head)
        parts.append(m.text.strip())
        parts.append("")
    parts.append("请写这段时间的回顾：")
    return "\n".join(parts)


def plan_tokens(period: str, key: str, materials: list[Material]) -> int:
    return estimate_tokens(build_prompt(period, key, materials)) + estimate_tokens(SYSTEM)


def generate(provider: BaseSummarizer, period: str, key: str, materials: list[Material]) -> tuple[str, list[str]]:
    """返回 (回顾正文, 引用到的 video_id 列表)。"""
    if not materials:
        raise ValueError("这段时间没有可用的材料")
    text = provider.complete(SYSTEM, build_prompt(period, key, materials)).strip()
    # 有的模型会把换行写成字面量的反斜杠 n，渲染出来就是一整段
    text = text.replace("\\n", "\n")
    by_index = {m.index: m.video_id for m in materials}
    cited: list[str] = []
    for mm in CITE_INDEX_RE.finditer(text):
        vid = by_index.get(int(mm.group(1)))
        if vid and vid not in cited:
            cited.append(vid)
    return text, cited
