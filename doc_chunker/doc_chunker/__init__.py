"""doc_chunker — cut parsed documents into units along their own structure (for search / RAG).

    from doc_chunker import chunk, parse_and_chunk
    units = chunk(doc, max_tokens=3000)          # one doc_parser.ParsedDoc -> [Unit]
    res = parse_and_chunk(["folder/"])           # files / folders -> res.units, res.docs, res.skipped, res.duplicates

Units never split a table row; table parts repeat the header; every unit carries its location
and the document's front matter. Token counting is pluggable (``count=``). Parsing: ``doc_parser``.
"""

from doc_chunker.chunker import DEFAULT_MAX_TOKENS, TokenCount, chunk
from doc_chunker.model import Unit, est_tokens
from doc_chunker.pipeline import ChunkResult, list_files, parse_and_chunk

__version__ = "0.1.0"

__all__ = ["ChunkResult", "DEFAULT_MAX_TOKENS", "TokenCount", "Unit", "chunk", "est_tokens", "list_files",
           "parse_and_chunk"]
