"""思维导图：Markdown 大纲 -> 树 -> 可交互的图。

思维导图不是图片，是树。让大模型输出嵌套的 Markdown 大纲（标题 + 缩进列表），
这是它最不容易写崩的结构化格式；然后这里解析成树，交给 markmap-view 渲染。

刻意没用 markmap-lib 做 Markdown 解析：那个浏览器构建有 661KB，内置了
markdown-it、KaTeX、Prism 一堆我们用不上的东西。我们自己控制大纲格式，
解析十几行 Python 就够，还能测。只带 markmap-view（49KB）+ d3（273KB），
都打包在 static/ 里离线可用 —— 国内访问 CDN 时好时坏。

界面里的渲染在前端（frontend/src/components/Mindmap.tsx）；这里只负责解析和
CLI 导出的自包含 mindmap.html。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from html import escape
from pathlib import Path
from typing import Any

STATIC_DIR = Path(__file__).parent / "static"

_FENCE_RE = re.compile(r"^\s*```[\w-]*\s*\n(.*?)\n\s*```\s*$", re.S)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*?)\s*$")
_NUMBERED_RE = re.compile(r"^(\s*)\d+[.)]\s+(.*?)\s*$")
# 列表项里的加粗/行内代码留着没意义，导图节点越干净越好
_INLINE_MD_RE = re.compile(r"(\*\*|__|`)(.+?)\1")


@dataclass
class Node:
    content: str
    children: list["Node"] = field(default_factory=list)

    def to_markmap(self) -> dict[str, Any]:
        """markmap-view 吃的格式：{content, children}。"""
        return {"content": self.content, "children": [c.to_markmap() for c in self.children]}

    @property
    def size(self) -> int:
        return 1 + sum(c.size for c in self.children)

    @property
    def depth(self) -> int:
        return 1 + max((c.depth for c in self.children), default=0)


def strip_fence(text: str) -> str:
    """模型有时会把整段输出包在 ```markdown 里，剥掉。"""
    m = _FENCE_RE.match(text or "")
    return m.group(1) if m else (text or "")


def _clean(text: str) -> str:
    return _INLINE_MD_RE.sub(r"\2", text).strip()


def parse_outline(markdown: str, fallback_title: str = "思维导图") -> Node:
    """把 Markdown 大纲解析成树。

    规则很朴素，因为格式是我们在 prompt 里规定的：
    - `#` 的数量决定标题层级，第一个一级标题是根
    - 列表项挂在最近的标题下，缩进（每 2 空格或 1 tab 一级）决定嵌套
    - 标题之间的普通段落忽略 —— 导图里放不下长文本
    """
    root: Node | None = None
    # 栈里每项是 (层级, 节点)。标题的层级用 1..6，列表项用 100+缩进级，
    # 这样列表永远挂在标题下面，而不会反过来
    stack: list[tuple[int, Node]] = []

    def attach(level: int, node: Node) -> None:
        nonlocal root
        while stack and stack[-1][0] >= level:
            stack.pop()
        if stack:
            stack[-1][1].children.append(node)
        elif root is None:
            root = node
        else:
            # 第二个顶层节点：挂到根下，别丢
            root.children.append(node)
        stack.append((level, node))

    for raw in strip_fence(markdown).splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue

        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            text = _clean(m.group(2))
            if not text:
                continue
            if root is None and level > 1:
                # 模型没给一级标题，先造一个根
                attach(1, Node(fallback_title))
            attach(level, Node(text))
            continue

        m = _BULLET_RE.match(line) or _NUMBERED_RE.match(line)
        if m:
            indent = m.group(1).replace("\t", "  ")
            level = 100 + len(indent) // 2
            text = _clean(m.group(2))
            if not text:
                continue
            if root is None:
                attach(1, Node(fallback_title))
            attach(level, Node(text))
            continue
        # 其他行（段落、分隔线）忽略

    return root or Node(fallback_title)


# ---------- 渲染 ----------


@lru_cache(maxsize=1)
def _vendor_js() -> str:
    """d3 + markmap-view，拼成一段。只读一次。"""
    d3 = (STATIC_DIR / "d3.min.js").read_text(encoding="utf-8")
    view = (STATIC_DIR / "markmap-view.js").read_text(encoding="utf-8")
    return f"{d3}\n{view}"


def standalone_html(tree: Node, title: str, source_markdown: str = "") -> str:
    """自包含的 HTML 文件：JS 全部内联，双击就能在浏览器里打开，不需要联网。"""
    payload = json.dumps(tree.to_markmap(), ensure_ascii=False)
    safe_title = escape(title or "思维导图")
    source_block = ""
    if source_markdown:
        source_block = (
            "<details><summary>大纲源文件</summary>"
            f"<pre>{escape(source_markdown)}</pre></details>"
        )
    return f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{safe_title} · 思维导图</title>
<style>
  body{{margin:0;font-family:system-ui,-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
       color:#222;background:#fff}}
  header{{display:flex;align-items:center;gap:12px;padding:10px 16px;border-bottom:1px solid #e5e5e5}}
  header h1{{font-size:15px;font-weight:600;margin:0;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
  header button{{font-size:12px;padding:4px 10px;border:1px solid #ddd;border-radius:6px;background:#fff;cursor:pointer}}
  svg#mm{{width:100vw;height:calc(100vh - 48px);display:block}}
  details{{padding:8px 16px;font-size:13px;color:#666}}
  pre{{white-space:pre-wrap;font-size:12px;background:#fafafa;padding:10px;border-radius:6px}}
  @media (prefers-color-scheme: dark){{
    body{{color:#eee;background:#141414}} header{{border-color:#333}}
    header button{{background:#222;border-color:#444;color:#eee}} pre{{background:#1e1e1e}}
  }}
</style>
</head><body>
<header><h1>{safe_title}</h1>
<button onclick="mm.fit()">居中</button>
<button onclick="dl()">下载 SVG</button></header>
<svg id="mm"></svg>
{source_block}
<script>{_vendor_js()}</script>
<script>
var tree = {payload};
var mm = markmap.Markmap.create(document.getElementById('mm'), {{
  autoFit: true, duration: 300, maxWidth: 320, spacingVertical: 8, paddingX: 12
}}, tree);
function dl(){{
  var s = new XMLSerializer().serializeToString(document.getElementById('mm'));
  var a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([s], {{type: 'image/svg+xml'}}));
  a.download = {json.dumps((title or "mindmap") + ".svg", ensure_ascii=False)};
  a.click();
}}
</script>
</body></html>"""
