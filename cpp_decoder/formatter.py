"""rich 终端渲染模块。

把 LLM 返回的 Markdown 报告切成三个板块
（【根本原因】/【错误代码位置】/【推荐修复方案】），
用 Panel + Markdown 高亮渲染：代码块自动获得语法着色，diff 自动红绿着色。
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

__all__ = ["SECTION_PATTERN", "SECTION_STYLES", "render_report", "split_sections"]

#: 匹配报告中的二级标题，例如 `## 【根本原因】`。
SECTION_PATTERN = re.compile(r"^##\s*【(.+?)】\s*$", re.MULTILINE)

#: 三个板块对应的（面板边框颜色, 图标）。
SECTION_STYLES: Dict[str, Tuple[str, str]] = {
    "根本原因": ("red", "🧨"),
    "错误代码位置": ("yellow", "📍"),
    "推荐修复方案": ("green", "🛠️"),
}


def split_sections(markdown: str) -> List[Tuple[str, str]]:
    """把 Markdown 报告按 ``## 【板块名】`` 切成 ``(板块名, 正文)`` 列表。

    - 保持原文顺序，正文去掉标题行本身；
    - 标题之前如果有游离文本，作为 ``("", 游离文本)`` 放在最前；
    - 完全没有板块标题时返回空列表（由调用方走兜底渲染）。
    """
    matches = list(SECTION_PATTERN.finditer(markdown))
    if not matches:
        return []
    sections: List[Tuple[str, str]] = []
    preamble = markdown[: matches[0].start()].strip()
    if preamble:
        sections.append(("", preamble))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        sections.append((match.group(1).strip(), markdown[match.end() : end].strip()))
    return sections


def render_report(markdown: str, console: Optional[Console] = None) -> None:
    """把诊断报告高亮渲染到终端。

    每个板块渲染成带彩色边框的 Panel，内部仍是 rich Markdown，
    因此 ```cpp / ```diff 代码块会自动语法高亮。
    当模型输出不符合三段式结构时，整篇按 Markdown 兜底渲染，保证不丢内容。
    """
    console = console or Console()
    console.print(Rule(title="🩺 C++ Template Error Decoder", style="cyan"))
    sections = split_sections(markdown)
    if not sections:
        console.print(Markdown(markdown))
        return
    for name, body in sections:
        if not name:
            console.print(Markdown(body))
            continue
        style, icon = SECTION_STYLES.get(name, ("cyan", "🔧"))
        console.print(
            Panel(
                Markdown(body),
                title=Text(f"{icon} {name}", style=f"bold {style}"),
                title_align="left",
                border_style=style,
                padding=(0, 1),
            )
        )
