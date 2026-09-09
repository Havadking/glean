"""预置的总结 prompt 模板（DESIGN.md 3.4）。

四种总结类型 + map-reduce 用的局部/汇总模板。加新模板只需往 TEMPLATES 里加一项。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import ConfigError

SYSTEM = (
    "你是一个专业的内容分析助手，擅长把视频/播客的口语转写整理成结构清晰、信息密度高的笔记。"
    "转写文本来自自动语音识别或字幕，可能有错别字、断句错误和口水话，请结合上下文理解真实意图，"
    "不要逐字复述。只依据转写内容作答，不要编造原文没有的信息；"
    "如果某处转写明显有误或听不清，可以标注为“(转写不清)”。"
)


@dataclass(frozen=True)
class PromptTemplate:
    key: str
    label: str
    instruction: str
    #  map-reduce 时对单块的要求；留空则复用 instruction
    map_instruction: str = ""


OVERALL = PromptTemplate(
    key="overall",
    label="整体摘要",
    instruction="""请输出一份整体摘要，使用 Markdown 格式：

## 一句话总结
用一句话说清这个视频在讲什么。

## 核心内容
分 3-8 个要点展开，每点一个小标题加两三句说明，覆盖主要论据和例子。

## 值得注意的细节
容易被忽略但有价值的信息、反直觉的观点、具体的数字或案例。

## 总体评价
内容的适用人群、深度、以及明显的局限或未展开之处。""",
    map_instruction="""这是一段长转写中的一部分。请提炼这一段的要点，用无序列表列出，
每条包含时间点和一句话说明，保留具体的数字、人名、结论。不要写开场白和结语。""",
)

BY_SPEAKER = PromptTemplate(
    key="by_speaker",
    label="分说话人摘要",
    instruction="""这是一段多人对话/播客。请输出分角色摘要，使用 Markdown 格式：

## 对话概览
参与者各是什么角色、整场在讨论什么。

## 各方观点
按说话人分小节，逐个总结其核心主张、论据和态度。

## 分歧与共识
双方（或多方）明确对立的点，以及达成一致的点。

## 结论
对话最后落到了什么地方。

如果转写没有说话人标签，就依据语气、称呼和话题转换推断角色，并注明这是推断。""",
    map_instruction="""这是一段长对话转写中的一部分。请按说话人提炼这一段各自说了什么，
用无序列表列出，每条注明说话人和时间点。不要写开场白和结语。""",
)

TIMELINE = PromptTemplate(
    key="timeline",
    label="时间轴大纲",
    instruction="""请按话题切分成章节，输出时间轴大纲（类似 YouTube chapters），使用 Markdown 格式：

## 章节
用列表逐条给出，格式为 `- [mm:ss] 章节标题 — 一句话说明这一段讲了什么`。
章节数量控制在 5-15 个，按话题实质变化切分，不要按固定时长机械切。
时间点必须来自转写中给出的时间戳，不要自己编。

## 主线
用三五句话说明这些章节串起来的整体脉络。""",
    map_instruction="""这是一段长转写中的一部分。请按话题切分这一段，
用 `- [mm:ss] 标题 — 说明` 的格式列出章节。时间点必须来自转写里的时间戳。
不要写开场白和结语。""",
)

KEY_POINTS = PromptTemplate(
    key="key_points",
    label="关键信息提取",
    instruction="""请提取关键信息，使用 Markdown 格式：

## 结论与主张
明确给出的结论、判断、建议，逐条列出。

## 关键数字与事实
出现的数字、日期、金额、比例、专有名词、引用来源，逐条列出并注明上下文。

## 行动项
可以直接执行的建议或步骤；如果内容里没有，写"无"。

## 存疑之处
说得含糊、缺少依据、或转写不清导致无法确认的地方。

不要为了凑数而编造，宁可某一节写"无"。""",
    map_instruction="""这是一段长转写中的一部分。请从这一段里抽取结论、关键数字与事实、行动项，
用无序列表列出，每条注明时间点。不要写开场白和结语。""",
)

TEMPLATES: dict[str, PromptTemplate] = {
    t.key: t for t in (OVERALL, BY_SPEAKER, TIMELINE, KEY_POINTS)
}


def get_template(key: str) -> PromptTemplate:
    template = TEMPLATES.get((key or "").strip().lower())
    if template is None:
        raise ConfigError(
            f"未知的总结类型: {key!r}，可选：{', '.join(TEMPLATES)}"
        )
    return template


def build_single_pass(
    template: PromptTemplate, header: str, body: str, language: str, extra: str | None
) -> str:
    return _assemble(
        header=header,
        instruction=template.instruction,
        body=body,
        language=language,
        extra=extra,
        body_label="转写全文",
    )


def build_map(
    template: PromptTemplate,
    header: str,
    body: str,
    language: str,
    extra: str | None,
    index: int,
    total: int,
) -> str:
    return _assemble(
        header=f"{header}\n本段：第 {index}/{total} 段",
        instruction=template.map_instruction or template.instruction,
        body=body,
        language=language,
        extra=extra,
        body_label="本段转写",
    )


def build_reduce(
    template: PromptTemplate, header: str, partials: list[str], language: str, extra: str | None
) -> str:
    joined = "\n\n".join(
        f"### 第 {i} 段要点\n{p}" for i, p in enumerate(partials, start=1)
    )
    return _assemble(
        header=header,
        instruction=(
            "下面是按顺序切分后逐段提炼的要点。请把它们整合成一份连贯的最终总结，"
            "去掉重复、按逻辑而非按分段组织内容。要求如下：\n\n"
            + template.instruction
        ),
        body=joined,
        language=language,
        extra=extra,
        body_label="各段要点",
    )


def _assemble(
    *, header: str, instruction: str, body: str, language: str, extra: str | None, body_label: str
) -> str:
    parts = [header, "", instruction]
    if extra:
        parts += ["", f"补充要求：{extra}"]
    parts += [
        "",
        f"输出语言：{language}。直接输出 Markdown 正文，不要用代码块包裹，不要写“好的”之类的开场白。",
        "",
        f"--- {body_label}开始 ---",
        body,
        f"--- {body_label}结束 ---",
    ]
    return "\n".join(parts)
