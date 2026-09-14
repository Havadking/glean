"""给视频打标签：让模型从总结里提炼几个主题词，跨 UP 主找东西用。

库里已经有两个找东西的维度 —— 按 UP 主分组、全文搜索。标签补的是第三个：
主题。搜「甘油」能找到提到甘油的段落，但回答不了「我收藏过哪些护肤视频」。

最大的坑是词表漂移：放任模型自由发挥，会得到「护肤 / 护肤品 / 皮肤护理 / 美妆」
四个意思差不多的标签，筛选就废了。所以每次都把库里已有的标签喂给模型，
要求优先复用、只在确实没有合适的时候才新建，一条限 3–5 个。

输入用总结而不是整篇转写（和「问 UP 主」一个思路），一次几乎不花钱。
"""

from __future__ import annotations

import json
import re

from .summarizer.base import BaseSummarizer

SYSTEM = (
    "你是一个内容归档助手，负责给视频打主题标签，方便日后按主题检索。\n\n"
    "规则：\n"
    "1. 输出 3 到 5 个标签，**只输出一个 JSON 字符串数组**，不要任何别的文字。\n"
    "2. 标签是主题词，不是句子：2 到 6 个字的中文名词或短语（英文专有名词可以保留原文）。"
    "比如「护肤」「成分分析」「Python」「职场」「历史」。\n"
    "3. **优先从「已有标签」里选**。已有标签里有意思相近的，就用已有的那个，不要另造一个近义词。"
    "只有内容确实不属于任何已有标签时才新建。\n"
    "4. 从宽到窄：至少一个宽泛的领域标签（如「护肤」「编程」「投资」），再加一两个更具体的（如「防晒」「Rust」）。\n"
    "5. 不要用「视频」「总结」「教程」「分享」这类什么内容都能套的词，不要用 UP 主名字。"
)

MAX_TAGS = 5
MAX_TAG_LEN = 20
# 词表太长就只给最常用的
MAX_VOCAB = 80

_JSON_ARRAY_RE = re.compile(r"\[[^\[\]]*\]", re.S)
_SPLIT_RE = re.compile(r"[,，、\n;；|]+")


def normalize(tag: str) -> str:
    """去掉前后空白和 # 号、把连续空白压成一个；拉丁字母统一小写。空的返回空串。"""
    t = tag.strip().lstrip("#＃").strip()
    t = re.sub(r"\s+", " ", t)
    t = t.strip("\"'“”‘’「」[]")
    if not t or len(t) > MAX_TAG_LEN:
        return ""
    return t.lower() if t.isascii() else t


def dedupe(tags: list[str]) -> list[str]:
    out: list[str] = []
    for t in tags:
        n = normalize(t)
        if n and n not in out:
            out.append(n)
    return out


def parse_tags(text: str) -> list[str]:
    """模型输出 → 标签列表。首选 JSON 数组；模型没守规矩就按逗号 / 换行切。"""
    m = _JSON_ARRAY_RE.search(text)
    if m:
        try:
            arr = json.loads(m.group(0))
            if isinstance(arr, list):
                return dedupe([str(x) for x in arr])[:MAX_TAGS]
        except ValueError:
            pass
    parts = [p.strip(" -*·") for p in _SPLIT_RE.split(text)]
    return dedupe(parts)[:MAX_TAGS]


def build_prompt(*, title: str, uploader: str | None, kind: str, text: str, vocab: list[str]) -> str:
    parts = [f"视频标题：{title}"]
    if uploader:
        parts.append(f"UP 主：{uploader}")
    parts.append("")
    if vocab:
        parts.append("已有标签（优先复用）：" + "、".join(vocab[:MAX_VOCAB]))
    else:
        parts.append("已有标签：（库里还没有标签，这是第一条）")
    parts.append("")
    label = "转写开头" if kind == "transcript" else "内容总结"
    parts.append(f"=== {label} ===")
    parts.append(text.strip())
    parts.append("=== 结束 ===")
    parts.append("")
    parts.append("请输出标签 JSON 数组：")
    return "\n".join(parts)


def suggest(provider: BaseSummarizer, *, title: str, uploader: str | None, kind: str, text: str,
            vocab: list[str]) -> list[str]:
    user = build_prompt(title=title, uploader=uploader, kind=kind, text=text, vocab=vocab)
    return parse_tags(provider.complete(SYSTEM, user))
