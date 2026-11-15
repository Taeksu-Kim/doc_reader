"""Korean-aware keyword tokens shared by indexing and querying.

Hangul runs -> character bigrams (so 2-syllable words like 매출·단가 and words with particles
like 출장비는 still match). ASCII/digit runs -> one token with inner punctuation removed
(NS-500 -> ns500, v2.1 -> v21). Thousands separators are dropped (14,000 -> 14000) and the space
Word PDFs put between a digit and Hangul is removed (제12 장 -> 제12장).
A number directly followed by Hangul also yields a joined token (3분기 -> "3분기" besides "3", "분기"),
so periods and counts match exactly: "3분기" no longer scores like "2분기" plus a stray "3".
"""

from __future__ import annotations

import re
import unicodedata

_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3})")
_DIGIT_SPACE_HANGUL = re.compile(r"(?<=\d)\s+(?=[가-힣])")
_RUN = re.compile(r"[가-힣]+|[a-z0-9]+(?:[-./][a-z0-9]+)*")


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).lower()
    t = _THOUSANDS.sub("", t)
    return _DIGIT_SPACE_HANGUL.sub("", t)


def tokens(text: str) -> list[str]:
    out: list[str] = []
    prev = None
    for m in _RUN.finditer(normalize(text)):
        w = m.group()
        if "가" <= w[0] <= "힣":
            out += [w] if len(w) == 1 else [w[i:i + 2] for i in range(len(w) - 1)]
            if prev is not None and prev.end() == m.start() and prev.group().isdigit():
                out.append(prev.group() + w[:2])          # 3분기 -> "3분기", 10월 -> "10월"
        else:
            out.append(re.sub(r"[-./]", "", w))
        prev = m
    return out
