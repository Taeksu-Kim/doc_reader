"""Synthetic long manual (docx) for splitter tests: 20 chapters x 8 sections, tables, one 500-row table.
Each section carries a unique marker sentence so coverage can be checked after splitting."""
import random, sys
from docx import Document
from docx.shared import Pt
random.seed(7)
out = sys.argv[1]
words = ("설비 점검 기준 품질 검사 절차 기록 보관 측정 장비 교정 주기 불량 원인 분석 조치 결과 보고 승인 책임자 "
         "공정 관리 온도 습도 진동 센서 데이터 이상 경보 대응 시간 고객 요구 사항 변경 이력 문서 번호 개정").split()
def sentence():
    return " ".join(random.choice(words) for _ in range(random.randint(8, 16))) + "을 확인한다."
d = Document()
d.add_paragraph("누리센스 품질관리 매뉴얼 (가상)", style="Title")
d.add_paragraph("문서번호 QM-2026-01 / 개정 3판 / 시행 2026-09-01 / 승인 품질경영위원회")
for c in range(1, 21):
    d.add_heading(f"제{c}장 품질 관리 영역 {c}", level=1)
    for s in range(1, 9):
        d.add_heading(f"{c}.{s} 세부 절차 {c}-{s}", level=2)
        for p in range(6):
            text = " ".join(sentence() for _ in range(3))
            if p == 2:
                text += f" 표식 MARK-{c:02d}-{s:02d} 의 기준값은 {c * 10 + s}.{s} 이다."
            d.add_paragraph(text)
    t = d.add_table(rows=1, cols=4)
    for j, h in enumerate(["항목", "기준", "주기", "담당"]):
        t.rows[0].cells[j].text = h
    for r in range(60):
        cells = t.add_row().cells
        cells[0].text, cells[1].text, cells[2].text, cells[3].text = f"점검 {c}-{r}", f"{r * 1.5:.1f}", "월 1회", f"담당자{r % 7}"
d.add_heading("부록 A 전체 설비 목록", level=1)
t = d.add_table(rows=1, cols=5)
for j, h in enumerate(["설비번호", "설비명", "위치", "도입일", "상태"]):
    t.rows[0].cells[j].text = h
for r in range(500):
    cells = t.add_row().cells
    vals = [f"EQ-{r:04d}", f"설비 {r}", f"{r % 5 + 1}공장", f"20{10 + r % 15}-0{r % 9 + 1}-1{r % 9}", ["가동", "정비", "대기"][r % 3]]
    for j, v in enumerate(vals):
        cells[j].text = v
d.save(out)
print("saved", out)
