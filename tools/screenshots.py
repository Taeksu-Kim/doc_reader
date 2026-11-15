"""README 캡처: 가상 자료로 서버를 띄워 화면 5장을 찍는다 (docs/images/).

    python tools/screenshots.py [--out docs/images]

외부·개인 문서를 쓰지 않는다: 읽기 검수는 여기서 만드는 가상 보고서 PDF, 자료실은 doc_index/out/corpus(가상 회사
'누리센스' 묶음, doc_index/tools/make_corpus.py — 없으면 tests의 작은 묶음), 청크는 긴 문서 회귀용 가상 매뉴얼
(doc_chunker/out/longdoc/manual.docx — 없으면 tests의 시험 매뉴얼). 레이아웃·OCR·임베딩 서버는 가짜.
"""
import argparse
import io
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pymupdf  # noqa: E402
import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from doc_reader.app import create_app  # noqa: E402
from tests.corpus import _corpus  # noqa: E402
from tests.test_chunk import _manual  # noqa: E402
from tests.test_library import FakeEmbedder  # noqa: E402
from tests.test_read import FakeModels  # noqa: E402

# a proper Korean font when this PC has one (Windows 맑은 고딕); else PyMuPDF's built-in CJK font
FONTFILE = next((f for f in ("/mnt/c/Windows/Fonts/malgun.ttf", "/usr/share/fonts/truetype/nanum/NanumGothic.ttf")
                 if Path(f).is_file()), None)
K = "kr" if FONTFILE else "korea"


def report_pdf() -> bytes:
    """A two-page Korean quarterly report: title, paragraphs, a ruled table, a bar chart drawn as vectors.
    The chart sits where the fake layout model reports a figure (100,200)-(400,360)."""
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 80), "2026년 3분기 영업 보고", fontsize=20, fontname=K, fontfile=FONTFILE)
    p.insert_text((72, 110), "작성: 경영지원팀 · 2026-10-05 · 대외비 아님 (가상 회사 예시)", fontsize=9, fontname=K, fontfile=FONTFILE, color=(.4, .4, .45))
    p.insert_text((72, 145), "1. 요약", fontsize=14, fontname=K, fontfile=FONTFILE)
    p.insert_text((72, 168), "3분기 매출은 전 분기보다 12% 늘었고, 신제품 NS-500 출시 효과가 컸다.", fontsize=10.5, fontname=K, fontfile=FONTFILE)
    p.insert_text((72, 184), "아래 그림은 분기별 매출(억원) 추이다.", fontsize=10.5, fontname=K, fontfile=FONTFILE)
    # bar chart (vector) inside the figure region
    p.draw_rect(pymupdf.Rect(100, 200, 400, 360), color=(.85, .87, .9), width=0.6)
    for k, (q, v) in enumerate([("1Q", 1234), ("2Q", 1410), ("3Q", 1587)]):
        x = 140 + k * 85
        h = v / 1700 * 110
        p.draw_rect(pymupdf.Rect(x, 330 - h, x + 45, 330), color=None, fill=(.18, .36, .82))
        p.insert_text((x + 6, 325 - h), f"{v:,}", fontsize=8, fontname=K, fontfile=FONTFILE)
        p.insert_text((x + 14, 346), q, fontsize=8, fontname=K, fontfile=FONTFILE)
    p.insert_text((180, 215), "분기별 매출 (억원)", fontsize=9, fontname=K, fontfile=FONTFILE)
    p.insert_text((72, 384), "그림 1. 분기별 매출 추이", fontsize=9, fontname=K, fontfile=FONTFILE, color=(.35, .35, .4))
    p.insert_text((72, 420), "2. 제품별 실적", fontsize=14, fontname=K, fontfile=FONTFILE)
    rows = [("제품", "매출(억원)", "전 분기 대비", "비고"), ("NS-200", "612", "-3%", "단종 예정"),
            ("NS-500", "735", "+48%", "8월 출시"), ("부품·서비스", "240", "+5%", ""), ("합계", "1,587", "+12%", "")]
    x0, y0, widths, rh = 72, 438, [130, 110, 110, 110], 22
    for r, row in enumerate(rows):
        x = x0
        for c, cell in enumerate(row):
            rect = pymupdf.Rect(x, y0 + r * rh, x + widths[c], y0 + (r + 1) * rh)
            p.draw_rect(rect, color=(.55, .58, .62), width=0.6, fill=(.93, .95, .98) if r == 0 else None)
            p.insert_text((x + 6, y0 + r * rh + 15), cell, fontsize=9.5, fontname=K, fontfile=FONTFILE)
            x += widths[c]
    p.insert_text((72, 580), "3. 다음 분기 계획", fontsize=14, fontname=K, fontfile=FONTFILE)
    for k, line in enumerate(["- NS-500 해외 인증을 11월까지 마치고 12월 수출을 시작한다.",
                              "- 하우징 금형 2벌을 추가해 월 생산 능력을 1.5배로 늘린다.",
                              "- 고객 문의 응답 시간을 평균 2일에서 1일로 줄인다."]):
        p.insert_text((80, 604 + k * 18), line, fontsize=10.5, fontname=K, fontfile=FONTFILE)
    q = doc.new_page()
    q.insert_text((72, 80), "4. 위험 요인", fontsize=14, fontname=K, fontfile=FONTFILE)
    q.insert_text((72, 104), "MCU 단가가 14,000원으로 올라 원가율이 2%p 높아질 수 있다.", fontsize=10.5, fontname=K, fontfile=FONTFILE)
    q.insert_text((72, 400), "부록. 용어", fontsize=14, fontname=K, fontfile=FONTFILE)
    q.insert_text((72, 424), "NS: 누리센스 진동 센서 제품군", fontsize=10.5, fontname=K, fontfile=FONTFILE)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "images"))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.gettempdir()) / "doc_reader_demo"          # shown on screen: keep it short and neutral
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    corpus = tmp / "누리센스 자료"
    src = ROOT / "doc_index" / "out" / "corpus"
    if src.is_dir():
        shutil.copytree(src, corpus)
    else:
        _corpus(corpus)
    manual = ROOT / "doc_chunker" / "out" / "longdoc" / "manual.docx"
    if not manual.is_file():
        manual = tmp / "manual.docx"
        manual.write_bytes(_manual())
    pdf = tmp / "2026년 3분기 영업 보고.pdf"
    pdf.write_bytes(report_pdf())

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    FakeModels.up = FakeEmbedder.up = True
    app = create_app(tmp / "data", model_url="http://fake", reader_factory=FakeModels, embed_url="http://fake",
                     embedder_factory=FakeEmbedder)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    # data: index the corpus, read the report with layout, chunk the manual
    t = app.state.library.add_source(str(corpus))
    app.state.library.wait(t.id, 900)
    app.state.read.wait(app.state.read.submit(pdf.name, pdf.read_bytes(), layout=True)[0].id, 120)
    app.state.chunk.submit(manual.name, manual.read_bytes(), max_tokens=3000)

    fallback = sorted(Path.home().glob(".cache/ms-playwright/chromium-*/chrome-linux64/chrome"))
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            b = p.chromium.launch(executable_path=str(fallback[-1]))
        page = b.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)

        page.goto(base + "/library/#/search?q=" + "출장 일비" + "&mode=hybrid")
        page.wait_for_selector(".hit")
        page.locator(".hit").nth(3).click()
        page.wait_for_timeout(400)
        page.screenshot(path=str(out / "library-search.png"))

        page.goto(base + "/library/#/library")
        page.wait_for_selector(".folders li")
        page.wait_for_timeout(300)
        page.screenshot(path=str(out / "library-manage.png"))

        page.goto(base + "/read/")
        page.wait_for_selector("#docList li a")
        page.click("#docList li a")
        page.wait_for_selector("#stage img")
        page.wait_for_function("document.querySelector('#stage img').complete")
        page.locator("#blocks .blk.table").first.click()
        page.wait_for_timeout(400)
        page.screenshot(path=str(out / "read-review.png"))

        page.goto(base + "/chunk/")
        page.wait_for_selector("#list li a")
        page.click("#list li a")
        page.wait_for_selector("#units .unit")
        page.hover("#map button >> nth=6")                       # the unit map stays in view (no scroll)
        page.wait_for_timeout(400)
        page.screenshot(path=str(out / "chunk-preview.png"))

        m = b.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
        m.goto(base + "/library/#/search?q=" + "NS-500 단가" + "&mode=hybrid")
        m.wait_for_selector(".hit")
        m.wait_for_timeout(300)
        m.screenshot(path=str(out / "mobile-search.png"))
        b.close()
    srv.should_exit = True
    time.sleep(0.3)
    shutil.rmtree(tmp, ignore_errors=True)
    for f in sorted(out.glob("*.png")):
        print(f"{f.name}  {f.stat().st_size // 1024}KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
