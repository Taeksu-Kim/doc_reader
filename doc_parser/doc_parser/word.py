"""Word .docx: heading styles / outline levels, tables (merged cells de-duplicated)."""

from __future__ import annotations

import re
from pathlib import Path

from doc_parser.model import Block, Loc
from doc_parser._common import _trim_table


_DOCX_H = re.compile(r"(heading|제목)\s*(\d)", re.I)


def parse_docx(path: Path) -> tuple[str, list[Block], list[str]]:
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = Document(str(path))
    blocks: list[Block] = []
    title = (d.core_properties.title or "").strip()
    idx = 0
    for el in d.element.body.iterchildren():
        if el.tag == qn("w:p"):
            idx += 1
            p = Paragraph(el, d)
            text = p.text.strip()
            if not text:
                continue
            name = (p.style.name if p.style is not None else "") or ""
            lvl = None
            if name.lower() in ("title", "제목"):
                title = title or text
                continue
            if m := _DOCX_H.search(name):
                lvl = int(m.group(2))
            else:
                ol = el.find(qn("w:pPr") + "/" + qn("w:outlineLvl"))
                if ol is not None and ol.get(qn("w:val")) not in (None, "9"):
                    lvl = int(ol.get(qn("w:val"))) + 1
            if lvl:
                blocks.append(Block("heading", Loc(line=idx), text=text, level=lvl))
            else:
                prefix = "- " if "list" in name.lower() or "목록" in name else ""
                blocks.append(Block("text", Loc(line=idx), text=prefix + text))
        elif el.tag == qn("w:tbl"):
            t = Table(el, d)
            rows = []
            for r in t.rows:
                seen, cells = set(), []
                for c in r.cells:                 # merged cells repeat the same _tc
                    if id(c._tc) in seen:
                        continue
                    seen.add(id(c._tc))
                    cells.append(c.text.strip())
                rows.append(cells)
            rows = _trim_table(rows)
            if len(rows) == 1 and len(rows[0]) == 1:      # 1x1 box (callout) -> text
                blocks.append(Block("text", Loc(line=idx), text=rows[0][0]))
            elif rows:
                blocks.append(Block("table", Loc(line=idx), rows=rows))
    return title or path.stem, blocks, []
