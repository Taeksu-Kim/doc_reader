"""PDF (text layer; scanned pages are reported and skipped unless an OCR reader is passed,
which then also reads raster images inside text pages — see doc_parser.ocr).

Headings from bookmarks or font size, running headers/footers removed, tables from ruled grids
(find_tables, sanity-checked) or from text alignment (no vertical rules / zebra rows), tables
continued across pages merged (incl. an orphan header row), wrapped lines re-joined.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path
from typing import Optional

from doc_parser.model import Block, Loc
from doc_parser._common import _cell, _trim_table
from doc_parser.ocr import LayoutReader, OcrItem, OcrReader


def _norm_line(t: str) -> str:
    return re.sub(r"\d+", "#", t.strip())


_BULLETS = {"•", "◦", "▪", "■", "□", "●", "○", "-", "–", "·", "※"}
_NUMBER_ONLY = re.compile(r"^[+\-▲▼△▽]?\s*[\d,.]+\s*(%|[가-힣A-Za-z]{0,4})$")   # "4,280 백만원", "44.0 %"


def _heading_text_ok(text: str) -> bool:
    t = text.strip()
    return (len(t) <= 60 and not _NUMBER_ONLY.match(t) and not re.fullmatch(r"[\d\s./]+", t)
            and len(re.findall(r"[가-힣A-Za-z]", t)) >= 2)


def _cell_text(page, rect) -> str:
    """Text inside one table cell in reading order (Table.extract() can scramble '1,315' -> '1315 ,')."""
    if rect is None:
        return ""
    import pymupdf

    words = page.get_text("words", clip=pymupdf.Rect(rect))
    words.sort(key=lambda w: (round(w[1] / 3), w[0]))
    return " ".join(w[4] for w in words).strip()


def _sane_table(rows: list[list[str]]) -> bool:
    """Reject grids from find_tables that merged cells: shaded (zebra) rows often come back as one
    cell holding the whole row ("EQ-0000 0공장 0", "", ""). Those tables are re-read by alignment."""
    if len(rows) < 2 or not rows[0]:
        return False
    ncols = len(rows[0])
    cells = [c for r in rows for c in r]
    if sum(1 for c in cells if c) / len(cells) < 0.6:
        return False
    if ncols >= 3:
        lonely = sum(1 for r in rows[1:] if sum(1 for c in r if c) == 1)
        if lonely > 0.15 * (len(rows) - 1):
            return False
    return True


def _aligned_tables(lines: list[dict]) -> tuple[list[tuple[float, list[list[str]], list[float]]], list[dict]]:
    """Tables drawn without vertical rules (HTML/Chromium, many reports): rows of short cells
    whose columns line up (left, right or centre) for >= 3 consecutive visual rows.
    Returns (tables as (y, rows, bbox), remaining lines)."""
    rows: list[list[dict]] = []
    for l in sorted(lines, key=lambda l: ((l["y"] + l["y1"]) / 2, l["x"])):
        c = (l["y"] + l["y1"]) / 2
        if rows and abs(((rows[-1][0]["y"] + rows[-1][0]["y1"]) / 2) - c) < 3:
            rows[-1].append(l)
        else:
            rows.append([l])
    for r in rows:
        r.sort(key=lambda l: l["x"])

    def col_of(cell: dict, cols: list[dict]) -> int | None:
        for k, c in enumerate(cols):
            if min(abs(cell["x"] - c["x"]), abs(cell["x1"] - c["x1"]),
                   abs((cell["x"] + cell["x1"]) / 2 - (c["x"] + c["x1"]) / 2)) < 30:
                return k
        return None

    tables, used = [], set()
    i = 0
    while i < len(rows):
        head = rows[i]
        if len(head) < 2 or any(len(c["text"]) > 40 for c in head):
            i += 1
            continue
        cols = head
        run = [head]
        j = i + 1
        while j < len(rows):
            r = rows[j]
            ks = [col_of(c, cols) for c in r]
            if len(r) < 2 and len(cols) > 2 and not all(k is not None for k in ks):
                break
            if None in ks or len(set(ks)) != len(ks) or any(len(c["text"]) > 60 for c in r):
                break
            run.append(r)
            j += 1
        if len(run) >= 3:
            grid = []
            for r in run:
                row = [""] * len(cols)
                for c in r:
                    row[col_of(c, cols)] = c["text"]
                grid.append(row)
            cells = [c for r in run for c in r]
            box = [round(v, 1) for v in (min(c["x"] for c in cells), min(c["y"] for c in cells),
                                         max(c["x1"] for c in cells), max(c["y1"] for c in cells))]
            tables.append((run[0][0]["y"], grid, box))
            used.update(id(c) for r in run for c in r)
            i = j
        else:
            i += 1
    return tables, [l for l in lines if id(l) not in used]


_SCAN_DPI = 200
_MIN_REGION = 0.01        # raster images under 1% of the page (logos, icons) are not read
_MIN_REGION_PX = 1000     # crops are rendered so the long side has at least this many pixels


def _ocr_blocks(items: list[OcrItem], pno: int, warnings: list[str]) -> list[Block]:
    if any(it.looped for it in items):
        warnings.append(f"p.{pno}: OCR 모델이 같은 내용을 반복 생성해 잘라냄 — 이 페이지 결과 확인 필요")
    for note in dict.fromkeys(it.note for it in items if it.note):
        warnings.append(f"p.{pno}: {note}")
    out = []
    for it in items:
        if it.type == "table" and it.rows:
            out.append(Block("table", Loc(), rows=_trim_table(it.rows)))
        elif it.type == "heading" and it.text.strip():
            out.append(Block("heading", Loc(), text=it.text.strip(), level=max(1, it.level)))
        elif it.text.strip():
            out.append(Block("text", Loc(), text=it.text.strip()))
    return out


FIGURE = "[그림]"          # marks the block standing for one figure (its labels follow, if any)
_CROP_PAD = 4.0           # points of margin around a saved figure


def _save_crop(page, rect, images: Path, name: str) -> str:
    """Figure as a PNG from the PDF itself (vector figures stay sharp)."""
    import pymupdf

    zoom = max(_SCAN_DPI / 72, _MIN_REGION_PX / max(rect.width, rect.height, 1))
    images.mkdir(parents=True, exist_ok=True)
    page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=rect).save(str(images / name))
    return name


_GROW_DPI = 100
_GROW_STEP = 2.0          # points per step when widening a figure box
_GROW_MAX = 72.0          # never widen a side by more than this (or half the box); blockers and the gap rule come first
_GROW_GAP = 6.0           # white this wide ends the figure ...
_GROW_GAP_TEXT = 0.8      # ... or, sideways through text, 0.8 x the letters' ink height: a word space is ~0.55 x
                          # the cap height (20pt Helvetica: 7.8pt gap, 14.3pt letters), plus step rounding
_GROW_GAP_MAX = 16.0      # tall ink at the edge (a vertical line) must not stretch the gap without bound


def _grow_to_ink(gray, rect, blockers: list):
    """Layout boxes on scans can cut through a figure's own title ("Scal|ed Dot-Product ...").
    Widen each side while the strip just outside still has ink, until a white gap, another
    block's box, the page edge or the cap. ``gray``: the page rendered in grayscale at _GROW_DPI."""
    import pymupdf

    k = _GROW_DPI / 72
    w, h, px = gray.width, gray.height, gray.samples

    def ink(x0, y0, x1, y1) -> float:
        """0 = no ink (one speck is noise); else the tallest run of inked rows, in points."""
        xs = range(max(0, int(x0 * k)), min(w, int(x1 * k) + 1))
        ys = range(max(0, int(y0 * k)), min(h, int(y1 * k) + 1))
        rows = [any(px[y * w + x] < 160 for x in xs) for y in ys]
        if sum(rows) < 2:
            return 0.0
        best = run = gap = 0
        for dark in rows:                    # a 1-px hole does not split a letter
            if dark:
                run, gap = run + 1 + gap, 0
            elif run and gap < 1:
                gap += 1
            else:
                run = gap = 0
            best = max(best, run)
        return max(best, 1) / k

    r = pymupdf.Rect(rect)
    lim = (min(_GROW_MAX, rect.width / 2), min(_GROW_MAX, rect.height / 2))
    others = [o for o in blockers if not o.intersects(rect)]
    page_rect = pymupdf.Rect(0, 0, w / k, h / k)
    for side in ("x0", "x1", "y0", "y1"):
        sideways = side in ("x0", "x1")
        grown, cap = 0.0, lim[0] if sideways else lim[1]
        # letter height at the cut edge (just inside the box) sets how wide a word space may be
        tall = ink(r.x0, r.y0, r.x0 + _GROW_STEP, r.y1) if side == "x0" else \
            ink(r.x1 - _GROW_STEP, r.y0, r.x1, r.y1) if side == "x1" else 0.0
        while grown < cap:
            gap = min(_GROW_GAP_MAX, max(_GROW_GAP, _GROW_GAP_TEXT * tall)) if sideways else _GROW_GAP
            # look up to ``gap`` ahead: a gap between letters or words is not the figure's edge
            for s in (_GROW_STEP * n for n in range(1, math.ceil(gap / _GROW_STEP) + 1)):
                strip = {"x0": (r.x0 - s, r.y0, r.x0 - s + _GROW_STEP, r.y1), "x1": (r.x1 + s - _GROW_STEP, r.y0, r.x1 + s, r.y1),
                         "y0": (r.x0, r.y0 - s, r.x1, r.y0 - s + _GROW_STEP), "y1": (r.x0, r.y1 + s - _GROW_STEP, r.x1, r.y1 + s)}[side]
                if (got := ink(*strip)):
                    tall = max(tall, got)
                    break
            else:
                break                                   # a real white gap: the figure ends here
            nxt = pymupdf.Rect(r) | pymupdf.Rect(strip)
            if grown + s > cap or nxt not in page_rect or any(nxt.intersects(o) for o in others):
                break
            r, grown = nxt, grown + s
    return r


def _read_scanned_page(page, pno: int, ocr: OcrReader, warnings: list[str], images: Optional[Path]):
    """No text layer: the model reads the whole page in its own reading order (multi-column safe).
    Figures come back as "[그림] …" / "[차트] …" items with their box; with ``images`` they are cropped."""
    import pymupdf

    try:
        items = ocr.read(page.get_pixmap(dpi=_SCAN_DPI).tobytes("png"), "page")
    except Exception as exc:  # one bad page must not lose the document
        warnings.append(f"p.{pno}: 글자층 없음, {ocr.name} 읽기 실패 ({type(exc).__name__}) — 건너뜀")
        return [], [], {}
    _ocr_blocks(items, pno, warnings)               # warnings only (loops, server notes)
    extra, tables, k, n, grown = [], [], 0, 0, 0
    edges: dict[int, str] = {}                       # id(block) -> normalised text of header/footer items
    scale = 72 / _SCAN_DPI
    boxes = [pymupdf.Rect(*(v * scale for v in it.bbox)) for it in items if it.bbox]
    gray = page.get_pixmap(dpi=_GROW_DPI, colorspace=pymupdf.csGRAY)
    for it in items:
        y = n * 10.0                                 # synthetic y keeps the model's order
        n += 1
        box = [round(v * scale, 1) for v in it.bbox] if it.bbox else None
        figure = it.type == "text" and (not it.text.strip() or it.text.startswith((FIGURE, "[차트]")))
        if figure:
            if not it.text.strip() and images is None:
                continue                             # unreadable figure, nothing to anchor
            text = it.text.strip() or FIGURE
            rect = pymupdf.Rect(*(v * scale for v in it.bbox)) if it.bbox else None
            if rect is not None and not rect.is_empty:
                wide = _grow_to_ink(gray, rect, [o for o in boxes if o != rect])
                if wide != rect:
                    grown += 1
                    rect = wide
                    box = _box(rect)
                    if text.startswith(FIGURE):      # its labels were read from the cut box: read them again
                        zoom = max(_SCAN_DPI / 72, _MIN_REGION_PX / max(rect.width, rect.height))
                        try:
                            png = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=rect).tobytes("png")
                            got = [x.text for x in _ocr_blocks(ocr.read(png, "region"), pno, warnings) if x.type != "table"]
                            text = (FIGURE + " " + " / ".join(got)) if got else FIGURE
                        except Exception as exc:
                            warnings.append(f"p.{pno}: 넓힌 그림 영역 다시 읽기 실패 ({type(exc).__name__}) — 처음 읽은 글자 유지")
            b = Block("text", Loc(), text=text, bbox=box)
            if images is not None and rect is not None:
                k += 1
                rect = (rect + (-_CROP_PAD, -_CROP_PAD, _CROP_PAD, _CROP_PAD)) & page.rect
                if not rect.is_empty:
                    b.image = _save_crop(page, rect, images, f"p{pno:03d}_fig{k}.png")
            extra.append((y, 0.0, b))
        elif it.type == "table" and it.rows:
            tables.append((y, _trim_table(it.rows), box))
        elif it.type == "heading" and it.text.strip():
            extra.append((y, 0.0, Block("heading", Loc(), text=it.text.strip(), level=max(1, it.level), bbox=box)))
        elif it.edge and not re.search(r"\w", it.text):
            continue                                 # a decorative header ("× × × ×"): nothing to read
        elif it.text.strip():
            blk = Block("text", Loc(), text=it.text.strip(), bbox=box)
            if it.edge:
                edges[id(blk)] = _norm_line(blk.text)
            extra.append((y, 0.0, blk))
    if grown:
        warnings.append(f"p.{pno}: 그림 {grown}개 — 모델 상자가 그림을 잘라 잉크가 끝나는 곳까지 넓혀 다시 읽음")
    warnings.append(f"p.{pno}: 글자층 없음 → {ocr.name}로 읽음 (블록 {len(extra) + len(tables)}개)")
    return extra, tables, edges


def _raster_rects(page) -> list:
    """Raster images big enough to matter (logos and icons under 1% of the page are not)."""
    import pymupdf

    area = page.rect.get_area()
    rects: list = []
    for info in page.get_image_info():
        r = pymupdf.Rect(info["bbox"]) & page.rect
        if r.is_empty or r.get_area() < _MIN_REGION * area:
            continue
        for k, o in enumerate(rects):                   # tiled / overlapping images -> one region
            if o.intersects(r):
                rects[k] = o | r
                break
        else:
            rects.append(r)
    return rects


def _page_figures(page, pno: int, figs: list, fig_lines: list[dict], lines: list[dict],
                  ocr: Optional[OcrReader], images: Optional[Path], warnings: list[str]):
    """One block per figure on a text page, placed where the figure is. A figure is a layout
    figure region (``figs``) and/or a raster image. Its text: the text-layer words inside a layout
    region (``fig_lines``, already taken out of the body) + what OCR reads in a raster image the
    text layer does not cover. With ``images`` every figure is also saved as a PNG."""
    import pymupdf

    regions = [{"rect": pymupdf.Rect(f), "rasters": []} for f in figs]
    if ocr is not None or images is not None:
        for r in _raster_rects(page):
            for reg in regions:
                if _mostly_in(r, [reg["rect"]], 0.5):
                    reg["rasters"].append(r)
                    reg["rect"] |= r
                    break
            else:
                regions.append({"rect": pymupdf.Rect(r), "rasters": [r]})
    regions.sort(key=lambda g: (round(g["rect"].y0 / 3), g["rect"].x0))
    rect_of = lambda l: pymupdf.Rect(l["x"], l["y"], l["x1"], l["y1"])  # noqa: E731
    extra, read, k = [], 0, 0
    pool = list(fig_lines)
    for reg in regions:
        r = reg["rect"]
        mine = [l for l in pool if _mostly_in(rect_of(l), [r], 0.6)]
        for l in mine:
            pool.remove(l)
        mine.sort(key=lambda l: (round(l["y"] / 3), l["x"]))
        texts = [l["text"] for l in mine]
        tables: list[Block] = []
        for ras in reg["rasters"] if ocr is not None else []:
            # each raster on its own: text-layer labels next to it (sub-figure titles) are already
            # in ``texts``, and an OCR'd scan has invisible text over its image -> nothing to read
            if sum(len(l["text"]) for l in lines + mine if rect_of(l) in ras) >= 20:
                continue
            zoom = max(_SCAN_DPI / 72, _MIN_REGION_PX / max(ras.width, ras.height))
            try:
                png = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=ras).tobytes("png")
                got = _ocr_blocks(ocr.read(png, "region"), pno, warnings)
                texts += [b.text for b in got if b.type != "table"]
                tables += [b for b in got if b.type == "table"]
                read += 1
            except Exception as exc:
                warnings.append(f"p.{pno}: 이미지 영역 {ocr.name} 읽기 실패 ({type(exc).__name__})")
        if not texts and not tables and images is None:
            continue
        crop = pymupdf.Rect(r)          # the figure with its own labels (layout boxes can clip them)
        for l in mine:
            crop |= rect_of(l)
        crop = (crop + (-_CROP_PAD, -_CROP_PAD, _CROP_PAD, _CROP_PAD)) & page.rect
        b = Block("text", Loc(), text=(FIGURE + " " + " / ".join(texts)) if texts else FIGURE, bbox=_box(crop))
        for t in tables:
            t.bbox = _box(r)
        if images is not None:
            k += 1
            b.image = _save_crop(page, crop, images, f"p{pno:03d}_fig{k}.png")
        extra.append((r.y0, r.x0, b))
        extra += [(r.y0 + 0.1, r.x0, t) for t in tables]
    if read:
        warnings.append(f"p.{pno}: 이미지 {read}개 영역을 {ocr.name}로 읽음")
    return extra


_LAYOUT_DPI = 150
_FIGURE_LABELS = {"image", "chart"}   # PP-DocLayoutV3 calls the same kind of figure either (Transformer p.13-15)


def _box(r) -> list[float]:
    return [round(r.x0, 1), round(r.y0, 1), round(r.x1, 1), round(r.y1, 1)]


def _figure_regions(page, pno: int, layout: LayoutReader, warnings: list[str]) -> list:
    import pymupdf

    zoom = _LAYOUT_DPI / 72
    try:
        regions = layout.layout(page.get_pixmap(dpi=_LAYOUT_DPI).tobytes("png"))
    except Exception as exc:  # layout is a refinement: without it the page reads as before
        warnings.append(f"p.{pno}: 레이아웃 읽기 실패 ({type(exc).__name__}) — 글자층만으로 읽음")
        return []
    return [pymupdf.Rect(*(v / zoom for v in r.bbox)) for r in regions if r.label in _FIGURE_LABELS]


def _mostly_in(r, figs: list, frac: float) -> bool:
    area = r.get_area()
    return area > 0 and any((r & f).get_area() >= frac * area for f in figs)


def parse_pdf(path: Path, ocr: Optional[OcrReader] = None, layout: Optional[LayoutReader] = None,
              images: Optional[Path] = None) -> tuple[str, list[Block], list[str]]:
    """``images``: save every figure (layout figure regions, raster images, figures on scanned
    pages) as ``p{page:03d}_fig{k}.png`` there; its block gets ``image`` = that file name.
    Vector figures are only found with ``layout``."""
    import pymupdf

    for reader in {id(r): r for r in (ocr, layout) if r is not None}.values():
        reader.check()                    # fail before reading anything if it was asked for
    doc = pymupdf.open(str(path))
    warnings: list[str] = []
    toc: dict[int, list[tuple[int, str]]] = {}
    for level, text, page in doc.get_toc(simple=True):
        toc.setdefault(page, []).append((level, text.strip()))
    pages = []
    edge_of: dict[int, str] = {}          # scanned pages: id(block) -> text of a header/footer the model marked
    sizes: Counter = Counter()
    for pno, page in enumerate(doc, 1):
        tabs = []
        try:
            tabs = list(page.find_tables().tables)
        except Exception as exc:  # table finder is heuristic; never lose the text over it
            warnings.append(f"p.{pno}: 표 인식 실패 ({type(exc).__name__})")
        figs = _figure_regions(page, pno, layout, warnings) if layout is not None and page.get_text().strip() else []
        if figs:                          # a ruled grid drawn inside a figure is part of the figure
            tabs = [t for t in tabs if not _mostly_in(pymupdf.Rect(t.bbox), figs, 0.5)]
        rects = [pymupdf.Rect(t.bbox) for t in tabs]
        h = page.rect.height
        lines = []
        for b in page.get_text("dict")["blocks"]:
            if b.get("type") != 0:
                continue
            for ln in b["lines"]:
                raw = "".join(s["text"] for s in ln["spans"])
                text = raw.strip()
                if not text:
                    continue
                r = pymupdf.Rect(ln["bbox"])
                if any(r.intersects(t) and (r & t).get_area() > 0.5 * r.get_area() for t in rects):
                    continue
                size = round(max(s["size"] for s in ln["spans"]), 1)
                edge = r.y1 < h * 0.08 or r.y0 > h * 0.92
                if not edge:                     # running headers/footers would skew the body size
                    sizes[size] += len(text)
                lines.append({"text": text, "size": size, "x": r.x0, "x1": r.x1, "y": r.y0, "y1": r.y1,
                              "tail_space": raw.endswith((" ", "\u3000")), "edge": edge})
        # bullet glyphs are often separate lines with a slightly different baseline: attach each
        # to the nearest text line on its right at the same height, then drop the glyph
        glyphs = [l for l in lines if l["text"] in _BULLETS]
        lines = [l for l in lines if l["text"] not in _BULLETS]
        for g in glyphs:
            gc = (g["y"] + g["y1"]) / 2
            near = [l for l in lines if l["x"] > g["x"] and abs((l["y"] + l["y1"]) / 2 - gc) < l["size"] * 0.8]
            if near:
                min(near, key=lambda l: l["x"] - g["x"])["bullet"] = True
        tables = []
        rejected = []
        for t in tabs:
            rows = _trim_table([[_cell_text(page, c) for c in row.cells] for row in t.rows])
            if _sane_table(rows):
                tables.append((t.bbox[1], rows, _box(pymupdf.Rect(t.bbox))))
            else:
                rejected.append(pymupdf.Rect(t.bbox))
        if rejected:   # put back the text of grids we did not trust, then try alignment
            for b in page.get_text("dict")["blocks"]:
                if b.get("type") != 0:
                    continue
                for ln in b["lines"]:
                    raw = "".join(s["text"] for s in ln["spans"])
                    r = pymupdf.Rect(ln["bbox"])
                    if raw.strip() and any(r.intersects(t) and (r & t).get_area() > 0.5 * r.get_area() for t in rejected):
                        size = round(max(s["size"] for s in ln["spans"]), 1)
                        lines.append({"text": raw.strip(), "size": size, "x": r.x0, "x1": r.x1, "y": r.y0,
                                      "y1": r.y1, "tail_space": raw.endswith(" "),
                                      "edge": r.y1 < h * 0.08 or r.y0 > h * 0.92})
        fig_lines: list[dict] = []
        if figs:                          # words inside figures leave the body before table alignment
            fig_lines = [l for l in lines if _mostly_in(pymupdf.Rect(l["x"], l["y"], l["x1"], l["y1"]), figs, 0.6)]
            if fig_lines:
                ids = {id(l) for l in fig_lines}
                lines = [l for l in lines if id(l) not in ids]
                warnings.append(f"p.{pno}: 그림 영역 {len(figs)}개 — 안의 글자 {len(fig_lines)}줄을 그림 블록으로 묶음")
        edge_lines = [l for l in lines if l["edge"]]          # header/footer candidates: never table cells
        aligned, rest = _aligned_tables([l for l in lines if not l["edge"]])
        lines = rest + edge_lines
        tables += aligned
        extra: list[tuple[float, float, Block]] = []          # (y, x, block) placed by position
        # no text layer but something on the page: a scan (images) or text drawn as vector outlines
        # (PDFKit / design-tool exports: 0 fonts, hundreds of paths) -- both only readable as a picture
        inked = not (lines or tables or fig_lines) and bool(page.get_images() or page.get_drawings())
        if lines or tables or fig_lines:
            extra = _page_figures(page, pno, figs, fig_lines, lines, ocr, images, warnings)
        elif ocr is not None and inked:
            extra, tables, edges = _read_scanned_page(page, pno, ocr, warnings, images)
            edge_of.update(edges)
        elif images is not None and inked:                   # not read: keep the page as a picture
            extra = [(0.0, 0.0, Block("text", Loc(), text=FIGURE, bbox=_box(page.rect),
                                      image=_save_crop(page, page.rect, images, f"p{pno:03d}_fig1.png")))]
        if inked and ocr is None:
            warnings.append(f"p.{pno}: 글자층 없음 (스캔본이거나 글자를 도형으로 그린 PDF) — 글자는 OCR 옵션을 켜야 읽힘")
        if not lines and not tables and not extra:
            continue
        pages.append((pno, lines, tables, extra))
    # running headers/footers: lines in the top/bottom 8% of the page whose text (digits
    # normalised) repeats on at least half of the pages. Body lines are never dropped.
    freq: Counter = Counter()
    for _, lines, _, _ in pages:
        for key in {_norm_line(l["text"]) for l in lines if l["edge"]}:
            freq[key] += 1
    for _, _, _, extra in pages:
        for key in {edge_of[id(b)] for _, _, b in extra if id(b) in edge_of}:
            freq[key] += 1
    running = {k for k, v in freq.items() if len(pages) >= 3 and v >= max(3, len(pages) // 2)}
    body = sizes.most_common(1)[0][0] if sizes else 10.0
    # heading sizes only from lines that read like headings (big KPI numbers are not headings)
    big = sorted({l["size"] for _, lines, _, _ in pages for l in lines
                  if l["size"] >= body * 1.2 and _heading_text_ok(l["text"])}, reverse=True)
    level_of = {s: min(k + 1, 3) for k, s in enumerate(big)}
    keeps_spaces = any(l["tail_space"] for _, lines, _, _ in pages for l in lines)
    blocks: list[Block] = []
    title = (doc.metadata or {}).get("title", "").strip()
    if toc and not title and pages:          # bookmarks only: title = biggest line on the first page
        cands = [l for l in pages[0][1] if _heading_text_ok(l["text"])]
        if cands:
            title = max(cands, key=lambda l: l["size"])["text"]

    def joiner(prev_text: str, prev_tail: bool, nxt: str) -> str:
        if prev_text.endswith(("-", "/")) and nxt[:1].isalnum():
            return ""                          # "MARK-09-" + "02" -> "MARK-09-02"
        if keeps_spaces:
            return " " if prev_tail else ""    # the PDF tells us where the spaces were
        return " "
    norm = lambda t: re.sub(r"\s+", "", t)  # noqa: E731
    table_end: dict[int, int] = {}            # id(table block) -> last page it spans
    for pno, lines, tables, extra in pages:
        loc = Loc(page=pno)
        pending_toc = list(toc.get(pno, []))
        for l in lines:                        # place each bookmark where its text appears on the page
            for k, (level, text) in enumerate(pending_toc):
                if norm(l["text"]) == norm(text):
                    l["toc"] = (level, text)
                    pending_toc.pop(k)
                    break
        for level, text in pending_toc:        # not found on the page: put it at the top
            blocks.append(Block("heading", loc, text=text, level=level))
        first_on_page = len(blocks)
        items = [("line", l["y"], l["x"], l) for l in lines if not (l["edge"] and _norm_line(l["text"]) in running)]
        items += [("table", y, 0.0, (rows, box)) for y, rows, box in tables]
        items += [("ocr", y, x, b) for y, x, b in extra if edge_of.get(id(b)) not in running]
        items.sort(key=lambda x: (round(x[1] / 3), x[2]))     # top-to-bottom, then left-to-right
        para: list[str] = []
        para_box: list[float] = []
        prev = None
        line_box = lambda l: [round(l["x"], 1), round(l["y"], 1), round(l["x1"], 1), round(l["y1"], 1)]  # noqa: E731

        def flush() -> None:
            if para:
                blocks.append(Block("text", loc, text="".join(para), bbox=list(para_box)))
            para.clear()
            para_box.clear()

        for kind, _, _, obj in items:
            if kind == "ocr":
                flush()
                obj.loc = loc
                blocks.append(obj)
                prev = None
                continue
            if kind == "table":
                flush()
                obj, box = obj
                if obj:
                    last = blocks[-1] if blocks else None
                    if (len(blocks) == first_on_page and last is not None and last.type == "table"
                            and table_end.get(id(last)) == pno - 1 and len(last.rows[0]) == len(obj[0])):
                        # table continued from the previous page: append rows, drop a repeated header
                        more_rows = obj[1:] if obj[0] == last.rows[0] else obj
                        last.rows.extend(more_rows)
                        table_end[id(last)] = pno
                    else:
                        n = len(obj[0])
                        tail = blocks[-n:] if len(blocks) >= n and len(blocks) == first_on_page else []
                        if tail and all(t.type == "text" and t.loc.page == pno - 1 and len(t.text) <= 20
                                        and "\n" not in t.text for t in tail):
                            # orphan header: a 1-row header left alone at the bottom of the previous
                            # page is not detected as a table and arrives as n short text blocks
                            obj = [[t.text for t in tail]] + obj
                            del blocks[-n:]
                        blocks.append(Block("table", loc, rows=obj, bbox=box))
                        table_end[id(blocks[-1])] = pno
                prev = None
                continue
            text, size = obj["text"], obj["size"]
            bullet = bool(obj.get("bullet"))
            if "toc" in obj:
                flush()
                blocks.append(Block("heading", loc, text=obj["toc"][1], level=obj["toc"][0], bbox=line_box(obj)))
                prev = None
                continue
            if not toc and size in level_of and _heading_text_ok(text):
                flush()
                if not title and level_of[size] == 1:
                    title = text
                blocks.append(Block("heading", loc, text=text, level=level_of[size], bbox=line_box(obj)))
                prev = None
                continue
            same_size = abs(prev["size"] - size) < 0.6 if prev is not None else False
            body_text = prev is not None and max(prev["size"], size) <= body * 1.1   # e.g. small "[S1]" after body
            joins = (prev is not None and not bullet and (same_size or body_text)
                     and 0 <= obj["y"] - prev["y1"] < max(size, prev["size"]) * 0.9)
            if not joins:
                flush()
                para.append(("- " if bullet else "") + text)
            else:
                para.append(joiner(prev["text"], prev["tail_space"], text) + text)
            lb = line_box(obj)
            para_box[:] = lb if not para_box else [min(para_box[0], lb[0]), min(para_box[1], lb[1]),
                                                   max(para_box[2], lb[2]), max(para_box[3], lb[3])]
            prev = obj
        flush()
    doc.close()
    return title or path.stem, blocks, warnings
