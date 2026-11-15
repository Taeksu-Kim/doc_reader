"""E2E at the CLI entry point (`python -m doc_parser`): fixtures are built here with the format
libraries (no other project needed); we check the blocks the parser reports."""

import json
import re
from collections import Counter
from pathlib import Path

import pymupdf
from docx import Document
from openpyxl import Workbook
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.util import Inches

from doc_parser import parse_file
from doc_parser.__main__ import main


def _text(capsys, path: Path) -> str:
    assert main([str(path)]) == 0
    return capsys.readouterr().out


def _blocks(capsys, path: Path) -> list[dict]:
    assert main([str(path), "--json"]) == 0
    return [json.loads(l) for l in capsys.readouterr().out.splitlines()]


# ---------------------------------------------------------------- txt


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


def test_text_headings_tables_encoding(tmp_path, capsys):
    src = tmp_path / "rule.txt"
    src.write_text(RULE, encoding="utf-8")
    blocks = _blocks(capsys, src)
    heads = [b["text"] for b in blocks if b["type"] == "heading"]
    assert heads[:5] == ["제1조 (목적)", "제2조 (일비)", "제3조 (숙박비)", "제4조 (교통비)", "제5조 (정산)"]
    assert "1. 개요" in heads                                             # ends with 요 but is a title
    assert any(b["text"] == "1일 30,000원. 당일 출장은 50% 지급." for b in blocks)   # article body split off
    assert [b["rows"] for b in blocks if b["type"] == "table"] == [[["구분", "금액"], ["일비", "30,000"]]]
    assert blocks[0]["loc"]["line"] == 1
    cp = tmp_path / "cp949.txt"
    cp.write_bytes("1. 개요\n한글 cp949 문서".encode("cp949"))
    assert "한글 cp949 문서" in _text(capsys, cp)


def test_errors(tmp_path, capsys):
    assert main([str(tmp_path / "nope.pdf")]) == 2
    (tmp_path / "r.hwp").write_bytes(b"\xd0\xcf")
    assert main([str(tmp_path / "r.hwp")]) == 2 and "지원하지 않는 형식: .hwp" in capsys.readouterr().err
    (tmp_path / "b.docx").write_bytes(b"not a zip")
    assert main([str(tmp_path / "b.docx")]) == 1 and "읽기 실패" in capsys.readouterr().err


# ---------------------------------------------------------------- docx / xlsx / pptx


def test_office_formats(tmp_path, capsys):
    d = Document()
    d.add_paragraph("3분기 보고", style="Title")
    d.add_heading("1. 실적", level=1)
    d.add_paragraph("매출은 4,280백만원이다.")
    t = d.add_table(rows=3, cols=3)
    t.cell(0, 0).merge(t.cell(0, 1)).text = "제품군"
    t.cell(0, 2).text = "매출"
    for r, (a, b, c) in enumerate([("센서", "NS-200", "1,315"), ("센서", "NS-300", "795")], 1):
        t.cell(r, 0).text, t.cell(r, 1).text, t.cell(r, 2).text = a, b, c
    box = d.add_table(rows=1, cols=1)
    box.cell(0, 0).text = "주의: 단가 미확정"
    d.save(tmp_path / "r.docx")

    wb = Workbook()
    ws = wb.active
    ws.title = "요약"
    ws["A1"] = "3분기 실적 표"
    ws.append([])
    ws.append(["제품", "매출", "비중"])
    ws.append(["NS-200", 1315, 0.307])
    ws.append(["NS-300", 795, 0.186])
    ws.append(["합계", "=SUM(B4:B5)", None])
    for r in (4, 5):
        ws.cell(r, 2).number_format = "#,##0"
        ws.cell(r, 3).number_format = "0.0%"
    ws["A8"] = "비고: 확정치 기준"
    wb.save(tmp_path / "r.xlsx")

    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "실적 요약"
    s.placeholders[1].text_frame.text = "매출 42.8억"
    p = s.placeholders[1].text_frame.add_paragraph()
    p.text, p.level = "목표의 95.1%", 1
    data = CategoryChartData()
    data.categories = ["7월", "8월"]
    data.add_series("매출", (1385, 1338))
    s.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(4), Inches(4), Inches(2), data)
    s.notes_slide.notes_text_frame.text = "잠정치와 혼동 주의"
    prs.save(tmp_path / "r.pptx")
    docx, xlsx, pptx = (_text(capsys, tmp_path / f"r.{k}") for k in ("docx", "xlsx", "pptx"))
    assert "## 1. 실적" in docx and "| 센서 | NS-200 | 1,315 |" in docx
    assert "| 제품군 | 매출 |" in docx                                    # merged cell appears once
    assert "주의: 단가 미확정" in docx and "| 주의" not in docx            # 1x1 box is text, not a table
    assert "| NS-200 | 1,315 | 30.7% |" in xlsx                         # numbers as the sheet shows them
    assert "3분기 실적 표" in xlsx and "비고: 확정치 기준" in xlsx
    assert "=SUM(B4:B5)" in xlsx and "계산값이 저장되지 않은 수식 1개" in xlsx
    assert "## 슬라이드 1: 실적 요약" in pptx and "  - 목표의 95.1%" in pptx
    assert "| 7월 | 1385 |" in pptx and "발표자 노트: 잠정치와 혼동 주의" in pptx


# ---------------------------------------------------------------- pdf


def _pdf(path: Path) -> None:
    """3+ pages: running header/footer, a title, a wrapped paragraph (hyphen break), a zebra table
    without vertical rules that continues over pages with its header repeated, and a ruled grid."""
    F, W = "korea", 595
    doc = pymupdf.open()
    rows = [(f"EQ-{r:04d}", f"{r % 5}공장", str(r * 3)) for r in range(110)]
    i = 0
    page_no = 0
    def chrome(page, n):
        page.insert_text((60, 30), "설비 관리 대장 (가상)", fontname=F, fontsize=8)
        page.insert_text((280, 822), f"{n} / #", fontname=F, fontsize=8)

    while i < len(rows):
        page = doc.new_page(width=W, height=842)
        page_no += 1
        chrome(page, page_no)
        y = 70
        if page_no == 1:
            page.insert_text((60, y + 20), "설비 관리 대장", fontname=F, fontsize=20)
            y += 50
            for line in ("설비 점검은 매월 실시하며 ", "결과는 대장에 기록한다. 표식 MARK-09-",
                         "02 의 기준값은 92.2 이다."):
                page.insert_text((60, y), line, fontname=F, fontsize=10)
                y += 14
            y += 16
            page.insert_text((60, y), "1. 설비 목록", fontname=F, fontsize=14)
            y += 24

        def row(vals, yy, fill=None, color=(0, 0, 0)):
            if fill:
                page.draw_rect(pymupdf.Rect(60, yy - 12, 520, yy + 5), color=None, fill=fill)
            page.draw_line((60, yy + 5), (520, yy + 5), color=(0.8, 0.8, 0.8), width=0.5)
            page.insert_text((66, yy), vals[0], fontname=F, fontsize=10, color=color)
            page.insert_text((240, yy), vals[1], fontname=F, fontsize=10, color=color)
            tw = pymupdf.get_text_length(vals[2], fontname=F, fontsize=10)
            page.insert_text((510 - tw, yy), vals[2], fontname=F, fontsize=10, color=color)
        row(("설비번호", "위치", "가동시간"), y, fill=(0.12, 0.23, 0.37), color=(1, 1, 1))   # repeated per page
        y += 18
        k = 0
        while i < len(rows) and y < 780:
            row(rows[i], y, fill=(0.96, 0.97, 0.98) if k % 2 else None)
            i, k, y = i + 1, k + 1, y + 18
    page = doc.new_page(width=W, height=842)                  # ruled grid on its own last page
    page_no += 1
    chrome(page, page_no)
    page.insert_text((60, 90), "2. 점검 기준", fontname=F, fontsize=14)
    grid = [("항목", "기준", "주기"), ("진동", "4.5", "월"), ("온도", "80", "주"), ("습도", "60", "주")]
    for r, vals in enumerate(grid):
        for c, v in enumerate(vals):
            rect = pymupdf.Rect(60 + c * 120, 110 + r * 20, 180 + c * 120, 110 + (r + 1) * 20)
            page.draw_rect(rect, color=(0, 0, 0), width=0.7)
            page.insert_text((rect.x0 + 6, rect.y1 - 6), v, fontname=F, fontsize=10)
    doc.save(path)


def test_pdf_tables_wraps_and_running_lines(tmp_path, capsys):
    src = tmp_path / "eq.pdf"
    _pdf(src)
    blocks = _blocks(capsys, src)
    text = "\n".join(b["text"] for b in blocks)
    assert "MARK-09-02" in text                                       # hyphen wrap joined without a space
    assert "매월 실시하며 결과는" in text                                # normal wrap keeps its space
    assert "설비 관리 대장 (가상)" not in text                            # running header removed
    assert not re.search(r"\d+ / #", text)                            # running footer removed
    assert [b["text"] for b in blocks if b["type"] == "heading"] == ["설비 관리 대장", "1. 설비 목록", "2. 점검 기준"]
    tables = [b for b in blocks if b["type"] == "table"]
    zebra = tables[0]["rows"]                                          # one table, merged over 3 pages
    assert zebra[0] == ["설비번호", "위치", "가동시간"] and len(zebra) == 111
    found = Counter(r[0] for r in zebra[1:])
    assert len(found) == 110 and max(found.values()) == 1             # repeated page headers are not rows
    assert zebra[-1] == ["EQ-0109", "4공장", "327"]
    assert tables[1]["rows"][1] == ["진동", "4.5", "월"]                # ruled grid via find_tables
    assert tables[0]["loc"]["page"] == 1 and tables[1]["loc"]["page"] == 4
    assert parse_file(src).blocks == parse_file(src).blocks             # deterministic


def test_pdf_bullets_typed_as_text(tmp_path, capsys):
    """Bullets typed as characters ("- ", "• ") start their own paragraphs (Korean reports use "- " a lot);
    a wrapped line without a marker still joins its item."""
    import pymupdf

    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 80), "다음 분기 계획", fontsize=14, fontname="korea")
    y = 104
    for line in ["- 해외 인증을 11월까지 마친다.", "- 금형 2벌을 추가해 생산 능력을", "  1.5배로 늘린다.", "• 응답 시간을 1일로 줄인다."]:
        p.insert_text((80, y), line, fontsize=10.5, fontname="korea")
        y += 15
    pdf = tmp_path / "plan.pdf"
    doc.save(str(pdf))
    assert main([str(pdf), "--json"]) == 0
    texts = [json.loads(l)["text"] for l in capsys.readouterr().out.splitlines()]
    items = [t for t in texts if t.lstrip().startswith(("-", "•"))]
    assert len(items) == 3, texts
    assert "1.5배" in items[1]                                           # wrapped line stays with its item
