"""The chunker's output: a unit of text cut along a document's structure, plus token counting."""

from __future__ import annotations

from dataclasses import asdict, dataclass

# Qwen3.8 tokenizer on Korean markdown: 0.517 tokens/char (measured 2026-11-13).
TOKENS_PER_CHAR = 0.52


def est_tokens(text: str) -> int:
    return int(len(text) * TOKENS_PER_CHAR) + 1


@dataclass
class Unit:
    id: str                             # "<file stem>#<n>"
    path: str
    kind: str
    heading_path: list[str]             # e.g. ["3. NS-500 신제품", "변경 사항"]
    loc_start: str                      # "p.12", "시트:3분기", ...
    loc_end: str
    text: str                           # markdown of the unit, tables kept as tables
    tokens: int
    part: str = ""                      # "2/5" when one section had to be cut into parts

    def to_dict(self) -> dict:
        return asdict(self)
