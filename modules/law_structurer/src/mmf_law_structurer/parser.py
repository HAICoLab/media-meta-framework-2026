from __future__ import annotations

import re
from dataclasses import dataclass, field

CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
LEVELS = ["law", "article", "paragraph", "item", "subitem"]

RE_CHAPTER = re.compile(r"^제\s*(\d+)\s*장\s*(.*)$")
RE_SECTION = re.compile(r"^제\s*(\d+)\s*절\s*(.*)$")
RE_ARTICLE = re.compile(r"^(제\s*\d+\s*조(?:의\s*\d+)?)\s*(?:\(([^)]*)\))?\s*(.*)$")
RE_PARAGRAPH = re.compile(rf"^([{CIRCLED}])\s*(.*)$")
RE_ITEM = re.compile(r"^(\d+)\.\s*(.*)$")
RE_SUBITEM = re.compile(r"^([가-하])\.\s*(.*)$")
RE_ADDENDA = re.compile(r"^부\s*칙")


@dataclass
class LawNode:
    level: str
    number: str
    heading: str | None = None
    text: str = ""
    chapter: str | None = None
    section: str | None = None
    children: list[LawNode] = field(default_factory=list)

    def walk(self, path: tuple[str, ...] = ()):
        """(node, path, parent_path) 순서로 깊이 우선 순회."""
        my_path = (*path, self.number)
        yield self, my_path, path or None
        for child in self.children:
            yield from child.walk(my_path)


def parse_law_text(raw: str) -> LawNode:
    lines = [ln.strip() for ln in raw.splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        raise ValueError("빈 법령 텍스트")

    root = LawNode(level="law", number=lines[0])
    stack: list[LawNode] = [root]
    chapter = section = None

    def push(node: LawNode) -> None:
        depth = LEVELS.index(node.level)
        while len(stack) > depth:
            stack.pop()
        node.chapter, node.section = chapter, section
        stack[-1].children.append(node)
        stack.append(node)

    def push_paragraph_or_text(rest: str) -> None:
        if m := RE_PARAGRAPH.match(rest):
            push(LawNode(level="paragraph", number=m.group(1), text=m.group(2)))
        elif rest:
            stack[-1].text = f"{stack[-1].text} {rest}".strip()

    for line in lines[1:]:
        if RE_ADDENDA.match(line):
            break  # TODO: 부칙 별도 처리
        if m := RE_CHAPTER.match(line):
            chapter, section = line, None
            continue
        if m := RE_SECTION.match(line):
            section = line
            continue
        if m := RE_ARTICLE.match(line):
            number = re.sub(r"\s+", "", m.group(1))
            push(LawNode(level="article", number=number, heading=m.group(2)))
            push_paragraph_or_text(m.group(3).strip())
            continue
        if len(stack) > 1 and (m := RE_PARAGRAPH.match(line)):
            push(LawNode(level="paragraph", number=m.group(1), text=m.group(2)))
            continue
        if len(stack) > 1 and (m := RE_ITEM.match(line)):
            push(LawNode(level="item", number=f"{m.group(1)}.", text=m.group(2)))
            continue
        if len(stack) > 2 and (m := RE_SUBITEM.match(line)):
            push(LawNode(level="subitem", number=f"{m.group(1)}.", text=m.group(2)))
            continue
        stack[-1].text = f"{stack[-1].text} {line}".strip()  # 이어지는 줄

    return root
