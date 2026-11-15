"""doc_parser — read business documents into heading / text / table blocks with locations.

    from doc_parser import parse_file
    doc = parse_file("report.pdf")
    for b in doc.blocks:                  # Block(type, loc, text | rows, level)
        print(b.loc.label(), b.render())

Formats: .txt .md .markdown .docx .xlsx .pptx .pdf (text layer). Add one with ``register_parser``.
Tables stay tables (header + rows); every block keeps its page / sheet / slide / line.
By default no model, no network: same file -> same blocks. PDF opt-ins (see doc_parser.ocr):
``parse_file(path, ocr=HttpOcr(url))`` reads scanned pages and raster images,
``parse_file(path, layout=HttpOcr(url))`` keeps the words drawn inside figures out of the body.
Cutting into chunks is ``doc_chunker``'s job.
"""

from doc_parser._common import UnsupportedFormat
from doc_parser.model import Block, BlockType, Loc, ParsedDoc
from doc_parser.ocr import HttpOcr, LayoutReader, OcrItem, OcrReader, OcrUnavailable, Region
from doc_parser.registry import PARSERS, ParserFn, parse_file, register_parser

__version__ = "0.1.0"

__all__ = ["Block", "BlockType", "HttpOcr", "LayoutReader", "Loc", "OcrItem", "OcrReader", "OcrUnavailable",
           "PARSERS", "ParsedDoc", "ParserFn", "Region", "UnsupportedFormat", "parse_file", "register_parser"]
