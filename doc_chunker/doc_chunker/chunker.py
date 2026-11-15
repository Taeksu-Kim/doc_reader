"""Chunker: ParsedDoc -> Units, cut along the document's own structure.

Rules (in order):
1. A whole file that fits ``max_tokens`` is one unit (most small business files).
2. Otherwise build the heading tree. A section that fits is one unit; consecutive small sibling
   sections are packed together up to ``max_tokens`` (no 50-token crumbs).
3. A section without sub-headings that is still too big is cut at block boundaries; a single
   oversized text block is cut at sentence / line boundaries; an oversized table is cut by rows
   with the header row repeated in every part. A table row is never split.
Every unit starts with a breadcrumb line (document > section path, location) and the document's
front matter (the short text before the first section: dates, version, status, attendees), so a
unit found by search on its own still says *which* version / date it belongs to.

Token counting is pluggable (``count=``): the default estimate is calibrated on Korean text for the
Qwen tokenizer; pass a real tokenizer's length function for another model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from doc_chunker.model import Unit, est_tokens
from doc_parser import Block, ParsedDoc

TokenCount = Callable[[str], int]

DEFAULT_MAX_TOKENS = 3000
PREAMBLE_TOKENS = 160       # front matter repeated in every unit is capped at this


@dataclass
class _Node:
    heading: Block | None
    level: int
    body: list[Block] = field(default_factory=list)
    children: list["_Node"] = field(default_factory=list)

    def blocks(self) -> list[Block]:
        out = ([self.heading] if self.heading else []) + self.body
        for c in self.children:
            out += c.blocks()
        return out

    def tokens(self, count: TokenCount) -> int:
        return sum(count(b.render()) for b in self.blocks())


def _tree(blocks: list[Block]) -> _Node:
    root = _Node(None, 0)
    stack = [root]
    for b in blocks:
        if b.type == "heading":
            while len(stack) > 1 and stack[-1].level >= b.level:
                stack.pop()
            node = _Node(b, b.level)
            stack[-1].children.append(node)
            stack.append(node)
        else:
            stack[-1].body.append(b)
    return root


def _cut_block(b: Block, max_tokens: int, count: TokenCount) -> list[Block]:
    """Split one oversized block into blocks that fit."""
    if count(b.render()) <= max_tokens:
        return [b]
    if b.type == "table":
        header, rows = b.rows[0], b.rows[1:]
        out, cur = [], []
        budget = max_tokens - count(Block("table", b.loc, rows=[header]).render())
        size = 0
        for r in rows:
            t = count("| " + " | ".join(r) + " |")
            if cur and size + t > budget:
                out.append(Block("table", b.loc, rows=[header] + cur))
                cur, size = [], 0
            cur.append(r)
            size += t
        if cur:
            out.append(Block("table", b.loc, rows=[header] + cur))
        return out
    pieces = re.split(r"(?<=[.!?。다요])\s+|\n", b.text)
    out, cur = [], ""
    for p in pieces:
        if cur and count(cur + " " + p) > max_tokens:
            out.append(Block(b.type, b.loc, text=cur.strip(), level=b.level))
            cur = ""
        while count(p) > max_tokens:              # no sentence boundary at all: hard cut
            n = max(int(len(p) * max_tokens / max(count(p), 1)), 1)
            out.append(Block(b.type, b.loc, text=p[:n]))
            p = p[n:]
        cur += (" " if cur else "") + p
    if cur.strip():
        out.append(Block(b.type, b.loc, text=cur.strip(), level=b.level))
    return out


def _pack(blocks: list[Block], max_tokens: int, count: TokenCount) -> list[list[Block]]:
    parts: list[list[Block]] = []
    cur: list[Block] = []
    size = 0
    for b in blocks:
        for piece in _cut_block(b, max_tokens, count):
            t = count(piece.render())
            if cur and size + t > max_tokens:
                parts.append(cur)
                cur, size = [], 0
            cur.append(piece)
            size += t
    if cur:
        parts.append(cur)
    return parts


def _preamble(blocks: list[Block]) -> list[Block]:
    """Front matter: up to two leading text blocks (after an optional title heading) before the
    first section heading. A document without sections only lends its first block."""
    out: list[Block] = []
    has_sections = any(b.type == "heading" for b in blocks[1:])
    for k, b in enumerate(blocks):
        if b.type == "heading":
            if k == 0:
                continue
            break
        if b.type == "text":
            out.append(b)
        if len(out) == (2 if has_sections else 1):
            break
    return out


def chunk(doc: ParsedDoc, max_tokens: int = DEFAULT_MAX_TOKENS, count: TokenCount = est_tokens) -> list[Unit]:
    """Units of at most ``max_tokens`` (as measured by ``count``), header lines included."""
    groups: list[tuple[list[str], list[Block], str]] = []   # (heading path, blocks, part label)
    root = _tree(doc.blocks)
    pre_blocks = _preamble(doc.blocks)
    pre = " / ".join(x.strip() for b in pre_blocks for x in b.text.splitlines() if x.strip())
    cap = min(PREAMBLE_TOKENS, max_tokens // 4)
    if count(pre) > cap:
        pre = pre[:max(int(len(pre) * cap / count(pre)), 1)] + "…"
    name = Path(doc.path).name
    # room for the breadcrumb + front matter lines added on top of every unit
    longest_path = max((count(b.text) for b in doc.blocks if b.type == "heading"), default=0) * 3
    overhead = (count(f"> {name} · {doc.title} · p.000–p.000 · 부분 00/00\n> 문서 정보: ")
                + longest_path + (count(pre) if pre else 0) + 10)
    max_tokens = max(max_tokens - overhead, 100)

    def emit(node: _Node, path: list[str]) -> None:
        here = path + ([node.heading.text] if node.heading else [])
        if node.tokens(count) <= max_tokens:
            if node.blocks():
                groups.append((path, node.blocks(), ""))
            return
        head = ([node.heading] if node.heading else [])
        pending: list[Block] = head + node.body
        if count("\n".join(b.render() for b in pending)) > max_tokens:
            parts = _pack(pending, max_tokens, count)
            for k, part in enumerate(parts, 1):
                groups.append((here, part, f"{k}/{len(parts)}"))
            pending = []
        size = sum(count(b.render()) for b in pending)
        for child in node.children:
            ct = child.tokens(count)
            if ct > max_tokens:
                if pending and any(b.type != "heading" for b in pending):
                    groups.append((here, pending, ""))
                pending, size = [], 0
                emit(child, here)
                continue
            if pending and size + ct > max_tokens:
                groups.append((here, pending, ""))
                pending, size = [], 0
            pending += child.blocks()
            size += ct
        if pending and any(b.type != "heading" for b in pending):
            groups.append((here, pending, ""))

    emit(root, [])
    units = []
    first_pre = pre_blocks[0] if pre_blocks else None
    for n, (path, blocks, part) in enumerate(groups, 1):
        locs = [b.loc.label() for b in blocks if b.loc.label()]
        start, end = (locs[0], locs[-1]) if locs else ("", "")
        crumb = " › ".join(([doc.title] if not path or path[0] != doc.title else []) + path)
        where = start if start == end else f"{start}–{end}"
        header = f"> {name} · {crumb}" + (f" · {where}" if where else "") + (f" · 부분 {part}" if part else "")
        if pre and not any(b is first_pre for b in blocks):
            header += f"\n> 문서 정보: {pre}"
        text = header + "\n\n" + "\n\n".join(b.render() for b in blocks if b.render())
        units.append(Unit(id=f"{name}#{n}", path=doc.path, kind=doc.kind, heading_path=path,
                          loc_start=start, loc_end=end, text=text, tokens=count(text), part=part))
    return units

