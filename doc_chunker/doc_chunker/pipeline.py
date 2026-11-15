"""Batch: files / folders -> parsed documents + units (``parse_and_chunk``). One bad file never stops the rest."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from doc_chunker.chunker import DEFAULT_MAX_TOKENS, TokenCount, chunk
from doc_chunker.model import Unit, est_tokens
from doc_parser import ParsedDoc, UnsupportedFormat, parse_file


@dataclass
class ChunkResult:
    docs: list[ParsedDoc] = field(default_factory=list)
    units: list[Unit] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)     # (path, reason)
    duplicates: list[tuple[str, str]] = field(default_factory=list)  # (path, same content as path)


def list_files(paths: list[str | Path]) -> list[Path]:
    """Files given directly, plus every file under given folders (Office lock files and dotfiles skipped)."""
    out: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if not p.exists():
            raise FileNotFoundError(f"경로가 없습니다: {raw}")
        if p.is_dir():
            out += sorted(f for f in p.rglob("*") if f.is_file() and not f.name.startswith(("~$", ".")))
        else:
            out.append(p)
    seen, uniq = set(), []
    for f in out:
        if f.resolve() not in seen:
            seen.add(f.resolve())
            uniq.append(f)
    return uniq


def parse_and_chunk(paths: list[str | Path], max_tokens: int = DEFAULT_MAX_TOKENS,
           count: TokenCount = est_tokens) -> ChunkResult:
    res = ChunkResult()
    by_hash: dict[str, str] = {}
    for f in list_files(paths):
        try:
            doc = parse_file(f)
        except UnsupportedFormat as exc:
            res.skipped.append((str(f), str(exc)))
            continue
        except Exception as exc:  # corrupt / password-protected / truncated files
            res.skipped.append((str(f), f"읽기 실패: {type(exc).__name__}: {str(exc)[:120]}"))
            continue
        if doc.sha256 in by_hash:
            res.duplicates.append((str(f), by_hash[doc.sha256]))
            continue
        by_hash[doc.sha256] = str(f)
        if not doc.blocks:
            res.skipped.append((str(f), "내용 없음" + (f" ({'; '.join(doc.warnings)})" if doc.warnings else "")))
            continue
        res.docs.append(doc)
        res.units += chunk(doc, max_tokens, count)
    return res
