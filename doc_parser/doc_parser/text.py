"""Plain text / Markdown: headings from markdown, numbered titles, [안건], 제n조; pipe tables."""

from __future__ import annotations

import re
from pathlib import Path

from doc_parser.model import Block, Loc
from doc_parser._common import _trim_table


_MD_H = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")
_NUM_H = re.compile(r"^((?:\d+\.)+\d*|\d+\.)\s+(\S.*)$")          # "1. 개요", "2.3 일정"
_BRACKET_H = re.compile(r"^\[([^\]]{1,30})\]\s*(.*)$")             # "[안건 1] 3분기 실적"
_ARTICLE_H = re.compile(r"^(제\s*\d+\s*조(?:\s*\([^)]*\))?)\s*(.*)$")  # "제3조 (국내 숙박비) ..."
_ROMAN_H = re.compile(r"^([IVXⅠ-Ⅹ]+)\.\s+(\S.*)$")


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp949"):        # Korean Windows files are often cp949
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _looks_like_heading(rest: str) -> bool:
    rest = rest.strip()
    return 0 < len(rest) <= 40 and not rest.endswith((".", ":", "。")) and "→" not in rest


def parse_text(path: Path) -> tuple[str, list[Block], list[str]]:
    lines = _read_text(path).splitlines()
    blocks: list[Block] = []
    title = ""
    para: list[str] = []
    para_start = 0
    is_md = path.suffix.lower() in (".md", ".markdown")

    def flush() -> None:
        nonlocal para
        if para:
            blocks.append(Block("text", Loc(line=para_start), text="\n".join(para)))
        para = []

    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.strip()
        n = i + 1
        if line.startswith("|"):
            flush()
            rows = []
            start = n
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    rows.append(cells)
                i += 1
            rows = _trim_table(rows)
            if rows:
                blocks.append(Block("table", Loc(line=start), rows=rows))
            continue
        heading = None
        if (m := _MD_H.match(line)) and is_md:
            heading = (len(m.group(1)), m.group(2), "")
        elif m := _ARTICLE_H.match(line):
            heading = (2, m.group(1), m.group(2))
        elif (m := _BRACKET_H.match(line)) and _looks_like_heading(m.group(1)) and len(m.group(2)) <= 40:
            heading = (1, line, "")
        elif (m := _NUM_H.match(line)) and _looks_like_heading(m.group(2)):
            depth = len([x for x in m.group(1).split(".") if x])
            heading = (depth, line, "")
        elif (m := _ROMAN_H.match(line)) and _looks_like_heading(m.group(2)):
            heading = (1, line, "")
        if heading:
            flush()
            level, text, rest = heading
            blocks.append(Block("heading", Loc(line=n), text=text.strip(), level=level))
            if rest.strip():
                blocks.append(Block("text", Loc(line=n), text=rest.strip()))
        elif not line:
            flush()
        else:
            if not title and not blocks and not para:
                title = line
            if not para:
                para_start = n
            para.append(line)
        i += 1
    flush()
    if is_md:
        first = next((b for b in blocks if b.type == "heading"), None)
        if first is not None and first.level == 1:
            title = first.text
    return title or path.stem, blocks, []
