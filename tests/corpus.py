"""Small test corpus and deterministic bag-of-bigram vectors (same as doc_index/tests/test_cli.py)."""

import zlib
from pathlib import Path

import numpy as np

from doc_index import tokens

DIM = 256


def _vec(text: str) -> list[float]:
    v = np.zeros(DIM, dtype=np.float32)
    for t in tokens(text.split("Query:")[-1]):
        v[zlib.crc32(t.encode()) % DIM] += 1.0
    n = np.linalg.norm(v)
    return (v / n if n else v).tolist()


def _corpus(d: Path) -> None:
    d.mkdir()
    (d / "rule_v21.txt").write_text("국내외 출장비 지급 규정 v2.1\n시행 2026-07-01\n\n제2조 (일비) 1일 30,000원.\n", encoding="utf-8")
    (d / "rule_v20.txt").write_text("국내외 출장비 지급 규정 v2.0\n시행 2025-03-01\n\n제2조 (일비) 1일 25,000원.\n", encoding="utf-8")
    (d / "meeting.txt").write_text("주간 경영회의\n\n[안건 1] NS-500 단가\n- MCU 단가 14,000원 인상, 65만원 안 검토.\n", encoding="utf-8")
    (d / "quote.txt").write_text("견적서 DS-2026-1017-03\nNS-500 하우징 금형 4,200만원\n", encoding="utf-8")
    (d / "manual.txt").write_text("품질 매뉴얼\n\n제12 장 설비 점검\n진동 기준을 확인한다.\n", encoding="utf-8")
