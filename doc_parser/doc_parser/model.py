"""Common shape every parser produces.

ParsedDoc.blocks is a flat, ordered list of heading / text / table blocks. Each block keeps
where it came from (page, sheet, slide, line) so citations can point inside a 500-page file.
Tables stay tables (header + rows) so a number never loses its column header or unit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

BlockType = Literal["heading", "text", "table"]



@dataclass
class Loc:
    page: Optional[int] = None    # pdf (1-based)
    sheet: Optional[str] = None   # xlsx
    slide: Optional[int] = None   # pptx (1-based)
    line: Optional[int] = None    # txt / md (1-based), docx paragraph index

    def label(self) -> str:
        if self.page is not None:
            return f"p.{self.page}"
        if self.sheet is not None:
            return f"시트:{self.sheet}"
        if self.slide is not None:
            return f"슬라이드 {self.slide}"
        if self.line is not None:
            return f"줄 {self.line}"
        return ""


@dataclass
class Block:
    type: BlockType
    loc: Loc
    text: str = ""                      # heading / text
    level: int = 0                      # heading level, 1 = top
    rows: list[list[str]] = field(default_factory=list)   # table: rows[0] is the header
    image: Optional[str] = None         # figure anchor: image file name, set only when images are extracted
    bbox: Optional[list[float]] = None  # pdf: [x0, y0, x1, y1] in points on loc.page (None = position unknown)

    def render(self) -> str:
        """Markdown form used for token estimates and for reading the unit."""
        if self.type == "heading":
            return "#" * min(self.level + 1, 6) + " " + self.text
        if self.type == "table":
            if not self.rows:
                return ""
            width = max(len(r) for r in self.rows)
            rows = [[c.replace("|", "/").replace("\n", " ") for c in r] + [""] * (width - len(r)) for r in self.rows]
            out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
            out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
            return "\n".join(out)
        return self.text


@dataclass
class ParsedDoc:
    path: str
    kind: str                           # txt, md, docx, xlsx, pptx, pdf
    title: str
    blocks: list[Block]
    sha256: str
    warnings: list[str] = field(default_factory=list)
