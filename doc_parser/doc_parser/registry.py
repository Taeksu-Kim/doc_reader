"""Registry: file suffix -> parser; ``parse_file`` dispatches. file -> ParsedDoc (heading / text / table blocks with locations).

Built in: .txt .md .markdown .docx .xlsx .pptx .pdf (text layer).
Not supported on purpose: .hwp/.hwpx, legacy .doc/.xls/.ppt. Scanned PDF pages are warned and
skipped unless ``parse_file(..., ocr=...)`` is given.
Add a format with ``register_parser(".ext", fn)`` where ``fn(path) -> (title, blocks, warnings)``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable, Optional

from doc_parser.model import Block, ParsedDoc
from doc_parser.ocr import LayoutReader, OcrReader
from doc_parser._common import UnsupportedFormat
from doc_parser.excel import parse_xlsx
from doc_parser.pdf import parse_pdf
from doc_parser.powerpoint import parse_pptx
from doc_parser.text import parse_text
from doc_parser.word import parse_docx

ParserFn = Callable[[Path], tuple[str, list[Block], list[str]]]

PARSERS: dict[str, ParserFn] = {
    ".txt": parse_text, ".md": parse_text, ".markdown": parse_text,
    ".docx": parse_docx, ".xlsx": parse_xlsx, ".pptx": parse_pptx, ".pdf": parse_pdf,
}


def register_parser(suffix: str, fn: ParserFn) -> None:
    """Add or replace the parser for a file suffix (e.g. ".hwpx")."""
    PARSERS[suffix.lower() if suffix.startswith(".") else "." + suffix.lower()] = fn


def parse_file(path: str | Path, ocr: Optional[OcrReader] = None,
               layout: Optional[LayoutReader] = None, images: str | Path | None = None) -> ParsedDoc:
    """Opt-in, PDF only (other formats ignore them): ``ocr`` reads scanned pages and raster images;
    ``layout`` finds figure regions so the words drawn inside figures stop being body text;
    ``images`` is a folder to save every figure in as PNG (its block's ``image`` names the file)."""
    p = Path(path)
    fn = PARSERS.get(p.suffix.lower())
    if fn is None:
        raise UnsupportedFormat(f"지원하지 않는 형식: {p.suffix or '(확장자 없음)'}")
    if fn is parse_pdf and (ocr is not None or layout is not None or images is not None):
        title, blocks, warnings = fn(p, ocr=ocr, layout=layout, images=Path(images) if images else None)
    else:
        title, blocks, warnings = fn(p)
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    return ParsedDoc(path=str(p), kind=p.suffix.lower().lstrip("."), title=title, blocks=blocks,
                     sha256=sha, warnings=warnings)


__all__ = ["PARSERS", "ParserFn", "UnsupportedFormat", "parse_docx", "parse_file", "parse_pdf",
           "parse_pptx", "parse_text", "parse_xlsx", "register_parser"]
