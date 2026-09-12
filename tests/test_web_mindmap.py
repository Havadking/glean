"""思维导图：大纲解析和导出。"""

from __future__ import annotations

import json

import pytest

from video_summarizer.summarizer.prompts import TEMPLATES
from video_summarizer.web import mindmap

OUTLINE = """# 冬季男生护肤
## 补水
- 喷雾
  - 买喷头细的
  - 手掌按压，不要拍
- 一天两次
## 锁水
- 甘油
  - 十几块一瓶
  - 滴 4-5 滴
## 误区
- 不要天天敷面膜
- 洗面奶一天一次
"""


def test_parses_headings_and_nested_bullets():
    tree = mindmap.parse_outline(OUTLINE)
    assert tree.content == "冬季男生护肤"
    assert [c.content for c in tree.children] == ["补水", "锁水", "误区"]
    spray = tree.children[0].children[0]
    assert spray.content == "喷雾"
    assert [c.content for c in spray.children] == ["买喷头细的", "手掌按压，不要拍"]
    assert tree.size == 13
    assert tree.depth == 4


def test_strips_code_fence_the_model_sometimes_adds():
    fenced = "```markdown\n" + OUTLINE + "```"
    assert mindmap.parse_outline(fenced).content == "冬季男生护肤"


def test_missing_root_heading_gets_fallback():
    tree = mindmap.parse_outline("## 只有二级\n- 要点", fallback_title="某视频")
    assert tree.content == "某视频"
    assert tree.children[0].content == "只有二级"


def test_second_top_level_heading_attaches_to_root():
    tree = mindmap.parse_outline("# 根\n## a\n# 另一个一级\n## b")
    assert [c.content for c in tree.children] == ["a", "另一个一级"]


def test_bullets_without_any_heading():
    tree = mindmap.parse_outline("- a\n  - b\n- c", fallback_title="根")
    assert tree.content == "根"
    assert [c.content for c in tree.children] == ["a", "c"]
    assert tree.children[0].children[0].content == "b"


def test_inline_markdown_is_stripped_from_nodes():
    tree = mindmap.parse_outline("# 根\n- **加粗** 和 `代码`")
    assert tree.children[0].content == "加粗 和 代码"


def test_numbered_lists_and_tabs_work():
    tree = mindmap.parse_outline("# 根\n1. 一\n\t- 一的子\n2. 二")
    assert [c.content for c in tree.children] == ["一", "二"]
    assert tree.children[0].children[0].content == "一的子"


def test_prose_between_headings_is_ignored():
    tree = mindmap.parse_outline("# 根\n这是一段解释文字。\n## 分支\n- 点")
    assert [c.content for c in tree.children] == ["分支"]


def test_empty_input_yields_fallback_root():
    tree = mindmap.parse_outline("", fallback_title="空")
    assert tree.content == "空" and tree.children == []


def test_markmap_json_shape():
    data = mindmap.parse_outline(OUTLINE).to_markmap()
    assert set(data) == {"content", "children"}
    assert all(set(c) == {"content", "children"} for c in data["children"])


# ---------- 渲染 ----------


def test_widget_embeds_tree_and_escapes_it():
    html = mindmap.widget_html(mindmap.parse_outline('# 根<script>\n- "引号"'))
    assert 'class="vs-mm"' in html and "<svg" in html
    # 树在 data 属性里，标签和引号都得转义，否则会破坏属性或注入
    assert "<script>" not in html.split("data-tree")[1].split("</div>")[0]
    assert "&quot;" in html


def test_widget_placeholder_when_empty():
    assert "还没有思维导图" in mindmap.widget_html(None)


def test_standalone_html_is_self_contained():
    tree = mindmap.parse_outline(OUTLINE)
    html = mindmap.standalone_html(tree, "护肤", OUTLINE)
    assert html.startswith("<!doctype html>")
    # 库要内联，不能有外链 —— 离线双击就得能开
    assert "<script src=" not in html
    assert "d3" in html and "markmap" in html
    assert "Markmap.create" in html
    # 树和源大纲都在
    assert "冬季男生护肤" in html
    assert "<details>" in html and "大纲源文件" in html
    # 标题要转义
    assert "<title>护肤 · 思维导图</title>" in html


def test_standalone_title_is_escaped():
    html = mindmap.standalone_html(mindmap.parse_outline("# x"), '<b>"t"</b>')
    assert "<b>" not in html.split("<title>")[1].split("</title>")[0]


def test_vendor_js_files_are_present_and_look_right():
    d3 = (mindmap.STATIC_DIR / "d3.min.js").read_text(encoding="utf-8")
    view = (mindmap.STATIC_DIR / "markmap-view.js").read_text(encoding="utf-8")
    assert len(d3) > 100_000, "d3 应该是完整的压缩版"
    assert "this.markmap" in view, "markmap-view 应该是挂到全局的 IIFE 构建"


def test_mindmap_is_a_registered_summary_type():
    assert "mindmap" in TEMPLATES
    assert TEMPLATES["mindmap"].label == "思维导图"
    # 格式要求得在 prompt 里，程序靠它解析
    assert "两个空格" in TEMPLATES["mindmap"].instruction
