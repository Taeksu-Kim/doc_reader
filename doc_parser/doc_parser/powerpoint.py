"""PowerPoint .pptx: one heading per slide, text frames, tables, charts as data tables, notes."""

from __future__ import annotations

from pathlib import Path

from doc_parser.model import Block, Loc
from doc_parser._common import _cell, _trim_table


def parse_pptx(path: Path) -> tuple[str, list[Block], list[str]]:
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER

    prs = Presentation(str(path))
    blocks: list[Block] = []
    title = (prs.core_properties.title or "").strip()

    def shapes_of(container):
        for sh in container:
            if sh.shape_type == MSO_SHAPE_TYPE.GROUP:
                yield from shapes_of(sh.shapes)
            else:
                yield sh

    for n, slide in enumerate(prs.slides, 1):
        loc = Loc(slide=n)
        shapes = sorted(shapes_of(slide.shapes), key=lambda s: ((s.top or 0), (s.left or 0)))
        slide_title = ""
        for sh in shapes:
            if sh.is_placeholder and sh.placeholder_format.type in (PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE):
                slide_title = sh.text_frame.text.strip()
        body: list[Block] = []
        for sh in shapes:
            if sh.has_text_frame:
                lines = []
                for p in sh.text_frame.paragraphs:
                    t = "".join(r.text for r in p.runs).strip()
                    if t:
                        lines.append(("  " * p.level + "- " if p.level else "") + t)
                text = "\n".join(lines)
                if not text or text == slide_title or text.isdigit():   # page numbers
                    continue
                if not slide_title and len(text) <= 60 and "\n" not in text and not text.isdigit():
                    # no title placeholder: the first short line that is not a page number
                    slide_title = text
                    continue
                body.append(Block("text", loc, text=text))
            elif sh.has_table:
                rows = _trim_table([[c.text.strip() for c in r.cells] for r in sh.table.rows])
                if rows:
                    body.append(Block("table", loc, rows=rows))
            elif sh.has_chart:
                ch = sh.chart
                plot = ch.plots[0]
                cats = [str(c) for c in plot.categories]
                series = list(plot.series)
                rows = [["구분"] + [s.name for s in series]]
                for k, c in enumerate(cats):
                    rows.append([c] + [_cell(s.values[k]) for s in series])
                body.append(Block("table", loc, rows=rows))
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                body.append(Block("text", loc, text="발표자 노트: " + notes))
        if n == 1 and not title:
            title = slide_title
        blocks.append(Block("heading", loc, text=f"슬라이드 {n}" + (f": {slide_title}" if slide_title else ""), level=1))
        blocks += body
    return title or path.stem, blocks, []
