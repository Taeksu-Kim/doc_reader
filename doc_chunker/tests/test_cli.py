"""E2E at the CLI entry point (`python -m doc_chunker`): parse + chunk, outputs are the JSONL units.
Parser details (PDF layouts, Office specifics) are tested in doc_parser."""

import json
import re
from collections import Counter
from pathlib import Path

from docx import Document

from doc_chunker import chunk
from doc_chunker.__main__ import main
from doc_parser import parse_file


def _units(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]


def _run(tmp_path: Path, *args: str) -> list[dict]:
    out = tmp_path / "units.jsonl"
    assert main([*args, "--out", str(out)]) == 0
    return _units(out)


RULE = """국내외 출장비 지급 규정 v2.1
제정 2023-01-01 / 시행 2026-07-01

제1조 (목적) 임직원 출장 시 경비 지급 기준을 정한다.
제2조 (일비) 1일 30,000원. 당일 출장은 50% 지급.
제3조 (숙박비) 실비 정산, 1박 상한
- 수도권·광역시: 120,000원
- 그 외 지역: 90,000원
제4조 (교통비) KTX 일반실 원칙. 편도 3시간 초과 구간은 특실 허용.
제5조 (정산) 출장 종료 후 10영업일 이내, 영수증 첨부.

1. 개요
이 장은 요약이다.

| 구분 | 금액 |
|---|---|
| 일비 | 30,000 |
"""


def test_structure_bound_and_front_matter(tmp_path, capsys):
    src = tmp_path / "rule.txt"
    src.write_text(RULE * 3, encoding="utf-8")
    units = _run(tmp_path, str(src), "--max-tokens", "250")
    assert len(units) >= 3 and max(u["tokens"] for u in units) <= 250      # header lines included
    assert all("시행 2026-07-01" in u["text"] for u in units)              # front matter on every unit
    text = "\n".join(u["text"] for u in units)
    assert text.count("### 제3조 (숙박비)") == 3                            # articles never cut in half
    one = _run(tmp_path, str(src))
    assert len(one) == 1                                                  # small file -> one unit
    doc = parse_file(src)
    by_chars = chunk(doc, 400, count=len)                                 # pluggable token counting
    assert max(len(u.text) for u in by_chars) <= 400 and max(u.tokens for u in by_chars) <= 400


def test_bad_files_do_not_stop_the_batch(tmp_path, capsys):
    d = tmp_path / "in"
    d.mkdir()
    (d / "a.txt").write_text(RULE, encoding="utf-8")
    (d / "a_copy.txt").write_text(RULE, encoding="utf-8")
    (d / "empty.txt").write_text("  \n", encoding="utf-8")
    (d / "broken.docx").write_bytes(b"not a zip at all")
    (d / "broken.pdf").write_bytes(b"%PDF-1.7 truncated")
    (d / "report.hwp").write_bytes(b"\xd0\xcf\x11\xe0")
    (d / "~$lock.docx").write_bytes(b"office lock file")
    (d / "cp949.txt").write_bytes("1. 개요\n한글 cp949 문서".encode("cp949"))
    units = _run(tmp_path, str(d))
    log = capsys.readouterr().out
    assert "중복   a_copy.txt = a.txt" in log
    assert "건너뜀 empty.txt: 내용 없음" in log
    assert "건너뜀 broken.docx: 읽기 실패" in log and "건너뜀 broken.pdf: 읽기 실패" in log
    assert "건너뜀 report.hwp: 지원하지 않는 형식: .hwp" in log
    assert "lock" not in log
    assert sorted(Path(u["path"]).name for u in units) == ["a.txt", "cp949.txt"]
    assert "한글 cp949 문서" in next(u["text"] for u in units if u["path"].endswith("cp949.txt"))
    first = (tmp_path / "units.jsonl").read_text(encoding="utf-8")
    _run(tmp_path, str(d))
    assert (tmp_path / "units.jsonl").read_text(encoding="utf-8") == first   # same input twice -> same units
    assert main([str(tmp_path / "nope")]) == 2


def test_long_docx_split_keeps_everything(tmp_path, capsys):
    d = Document()
    d.add_paragraph("긴 매뉴얼", style="Title")
    d.add_paragraph("문서번호 QM-1 / 시행 2026-09-01")
    for c in range(1, 7):
        d.add_heading(f"제{c}장 영역 {c}", level=1)
        for s in range(1, 6):
            d.add_heading(f"{c}.{s} 절차", level=2)
            for p in range(4):
                d.add_paragraph(("설비 점검 기준을 확인한다. " * 12) + (f"표식 MARK-{c}-{s} 이다." if p == 1 else ""))
    d.add_heading("부록", level=1)
    t = d.add_table(rows=1, cols=3)
    for j, h in enumerate(["설비번호", "위치", "상태"]):
        t.rows[0].cells[j].text = h
    for r in range(300):
        cells = t.add_row().cells
        cells[0].text, cells[1].text, cells[2].text = f"EQ-{r:04d}", f"{r % 5}공장", "가동"
    d.save(tmp_path / "manual.docx")
    units = _run(tmp_path, str(tmp_path / "manual.docx"), "--max-tokens", "800")
    assert len(units) > 10 and max(u["tokens"] for u in units) <= 800
    text = "\n".join(u["text"] for u in units)
    assert sorted(set(re.findall(r"MARK-\d-\d", text))) == sorted(f"MARK-{c}-{s}" for c in range(1, 7) for s in range(1, 6))
    for u in units:                                                    # every marker under its own chapter
        for c, _ in re.findall(r"MARK-(\d)-(\d)", u["text"]):
            assert any(h.startswith(f"제{c}장") for h in u["heading_path"]) or f"제{c}장" in u["text"]
    rows = Counter(re.findall(r"\| (EQ-\d{4}) \|", text))
    assert len(rows) == 300 and max(rows.values()) == 1               # no row lost, none duplicated
    parts = [u for u in units if "EQ-" in u["text"]]
    assert len(parts) > 1 and all("| 설비번호 | 위치 | 상태 |" in u["text"] for u in parts)
    assert all("시행 2026-09-01" in u["text"] for u in units)
