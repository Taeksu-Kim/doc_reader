"""브라우저 점검: 자료실 검색 화면(serve)을 실제 Chromium(headless)에서 눌러 본다.

    python tools/browser_check.py [--corpus DIR] [--shots DIR]

임시 폴더로 서버 두 개(doc_index, 원본 보기용 doc_parser)를 띄우고 끝나면 정리한다 (실제 data/는 건드리지 않음).
임베딩 서버는 가짜(tests/test_library.FakeEmbedder) — GPU·CPU 임베딩 서버를 쓰지 않는다. 자료는 --corpus(기본 doc_index/out/corpus,
없으면 tests의 시험 묶음) + 시험용 PDF 폴더. 화면을 바꾸면 이것도 돌린다.
"""
import argparse
import json
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
from tests.test_library import FakeEmbedder  # noqa: E402

LAYOUT_JS = (ROOT / "tools" / "check_read.py").read_text(encoding="utf-8")
LAYOUT_JS = LAYOUT_JS.split('LAYOUT_JS = """', 1)[1].split('"""', 1)[0].replace(".rail,.facets,.strip,.tbl,.menu", ".tbl,.filters,.areas")

ap = argparse.ArgumentParser()
ap.add_argument("--corpus", default=str(ROOT / "doc_index" / "out" / "corpus"))
ap.add_argument("--shots", default=None)
args = ap.parse_args()
TMP = Path(tempfile.mkdtemp(prefix="doc_index_ui_"))
SHOTS = Path(args.shots) if args.shots else TMP / "shots"
SHOTS.mkdir(parents=True, exist_ok=True)
corpus = Path(args.corpus)
if not corpus.is_dir():
    corpus = TMP / "corpus"
    _corpus(corpus)
pdfs = TMP / "보고서 PDF"
pdfs.mkdir()
doc = pymupdf.open()
for n in range(4):
    p = doc.new_page()
    p.insert_text((72, 80), f"{n + 1}. 금형 투자 검토", fontsize=16, fontname="korea")
    p.insert_text((72, 120), f"NS-500 하우징 금형 비용은 {4200 + n * 100}만원으로 확정했다. 쪽 {n + 1}.", fontsize=11, fontname="korea")
doc.save(str(pdfs / "금형 투자 검토서.pdf"))
(pdfs / "빈 문서.pdf").write_bytes(b"%PDF-1.4 broken")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = free_port()
FakeEmbedder.up = True
app = create_app(TMP / "data", embed_url="http://fake", embedder_factory=FakeEmbedder)
srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
threading.Thread(target=srv.run, daemon=True).start()
while not srv.started:
    time.sleep(0.05)
BASE = f"http://127.0.0.1:{PORT}/"
URL = BASE + "library/"
FALLBACK = sorted(Path.home().glob(".cache/ms-playwright/chromium-*/chrome-linux64/chrome"))
results, step = [], ["시작"]


def check(name, ok, detail=""):
    step[0] = name
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f" -- {detail}" if detail else ""))


with sync_playwright() as p:
    try:
        b = p.chromium.launch()
    except Exception:
        b = p.chromium.launch(executable_path=str(FALLBACK[-1]))
    ctx = b.new_context(viewport={"width": 1280, "height": 900})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: m.type == "error" and "Failed to load resource" not in m.text and errors.append(m.text))
    ctx.on("response", lambda r: r.status >= 400 and r.request.method == "GET" and errors.append(f"{r.status} {r.url} (after: {step[0]})"))

    page.goto(URL)
    page.wait_for_selector("#addForm")
    # 영역 이동: 공통 상단 바
    check("상단 바: 네 영역 링크, 지금 영역 표시", page.locator(".areas a").count() == 4
          and page.locator(".areas a[aria-current='page']").count() == 1)
    check("첫 화면: 폴더 등록 안내", "자료 폴더부터 등록" in page.inner_text("#main"))
    page.screenshot(path=str(SHOTS / "01_onboard_1280.png"))
    page.fill("#folder", "상대/경로")
    page.click("#addForm button")
    page.wait_for_function("document.querySelector('#addErr').textContent.length > 0")
    check("잘못된 경로: 이유 표시", "절대 경로" in page.inner_text("#addErr"))

    t0 = time.time()
    page.fill("#folder", str(corpus))
    page.click("#addForm button")
    page.wait_for_selector(".lib")
    check("폴더 등록 → 자료 관리 화면 + 색인 진행 표시", "색인" in page.inner_text(".buildbox"))
    page.wait_for_function("document.querySelector('#buildText').textContent.startsWith('파일')", timeout=900_000)
    took = time.time() - t0
    page.goto(URL + "#/library")
    page.reload()
    page.wait_for_selector(".folders li")
    page.fill("#folder", str(pdfs))
    page.click("#addForm button")
    page.wait_for_function("document.querySelectorAll('.folders li').length === 2", timeout=30_000)
    page.wait_for_function("!document.querySelector('.buildbox .bar') && document.querySelector('#buildText').textContent.startsWith('파일')",
                           timeout=300_000)
    page.reload()
    page.wait_for_selector(".folders li")
    check("자료 관리: 폴더 2개, 읽음/건너뜀 수", page.locator(".folders li").count() == 2 and "건너뜀" in page.inner_text(".folders"),
          f"첫 색인 {took:.0f}s")
    page.click(".chip[data-f='skipped']")
    check("건너뜀 거르기: 깨진 파일과 이유", "빈 문서.pdf" in page.inner_text("#ftable") and "읽기 실패" in page.inner_text("#ftable"))
    page.screenshot(path=str(SHOTS / "02_library_1280.png"))
    page.click(".chip[data-f='all']")

    page.click("#navSearch")
    page.wait_for_selector("#q")
    page.fill("#q", "출장 일비")
    page.press("#q", "Enter")
    page.wait_for_selector(".hit")
    n = page.locator(".hit").count()
    check("검색: 결과 목록 + 근거 미리보기", n > 3 and page.is_visible("#preview .pv-body"), f"결과 {n}개 · {page.inner_text('#resMeta')}")
    check("결과에 찾은 방식(키워드/의미 순위)·위치", page.locator(".hit .by").count() > 0 and page.locator(".hit .h-path").count() == n)
    check("검색어 강조", page.locator(".hit mark").count() > 0)
    page.screenshot(path=str(SHOTS / "03_search_1280.png"))
    page.locator(".hit").nth(1).click()
    check("결과 고르면 미리보기 바뀜", page.get_attribute(".hit[data-i='1']", "aria-selected") == "true")
    page.keyboard.press("ArrowDown")
    check("↓ 키로 다음 결과", page.get_attribute(".hit[data-i='2']", "aria-selected") == "true")
    fam = page.locator(".hit .fam").count()
    check("같은 계열(버전 다른 파일) 표시", fam > 0, f"{fam}개")

    page.click("[data-mode='keyword']")
    page.wait_for_function("location.hash.includes('mode=keyword') && document.querySelector('#resMeta').textContent.includes('키워드')")
    check("방식 바꾸기: 키워드", "키워드" in page.inner_text("#resMeta") and page.locator(".hit").count() > 0)
    page.fill("#q", "없는말쿼쿼쿼")
    page.press("#q", "Enter")
    page.wait_for_selector(".empty h1")
    check("결과 없음 안내", "맞는 자료가 없습니다" in page.inner_text(".empty"))

    page.fill("#q", "금형 투자 검토 4300만원")
    page.press("#q", "Enter")
    page.wait_for_function("[...document.querySelectorAll('.hit .h-name')].some(e => e.textContent.includes('검토서.pdf'))")
    page.locator(".hit", has_text="금형 투자 검토서.pdf").first.click()
    page.click("#openBtn")
    page.wait_for_url("**/read/#/docs/**", timeout=30_000)
    page.wait_for_selector(".review", timeout=60_000)
    page.wait_for_function("document.querySelector('#pgIn')")
    check("원본에서 보기: 같은 서버의 읽기 검수가 그 쪽으로 열림", "/read/#/docs/" in page.url
          and page.input_value("#pgIn") != "" and "자료실에서 엶" in page.inner_text(".doc-meta"),
          f"{page.url} → p.{page.input_value('#pgIn')}")
    page.screenshot(path=str(SHOTS / "04_opened_in_read.png"))
    page.click("#navSearch")
    page.wait_for_selector("#q")
    check("상단 바로 자료실 검색에 돌아옴", "/library/" in page.url)

    # 임베딩 꺼짐
    FakeEmbedder.up = False
    app.state.library._seen = (0.0, {})
    page.goto(URL + "#/search?q=" + "출장 일비" + "&mode=hybrid")
    page.reload()
    page.wait_for_function("document.querySelector('#embedText').textContent === '꺼짐'")
    page.wait_for_selector(".hit")
    check("의미 검색 꺼짐: 키워드로 찾았다고 표시, 의미 버튼 잠김",
          "키워드로 찾음" in page.inner_text("#resMeta") and page.is_disabled("[data-mode='vector']"))
    page.screenshot(path=str(SHOTS / "05_embed_off_1280.png"))
    FakeEmbedder.up = True
    app.state.library._seen = (0.0, {})

    # 폭 3종 + 다크
    for w, h in [(1280, 900), (768, 1024), (390, 844)]:
        page.set_viewport_size({"width": w, "height": h})
        for route, name in [("#/search?q=" + "NS-500 단가" + "&mode=hybrid", "search"), ("#/library", "library")]:
            page.goto(URL + route)
            page.reload()
            page.wait_for_selector(".hit" if name == "search" else ".folders li")
            page.wait_for_timeout(300)
            page.screenshot(path=str(SHOTS / f"10_{name}_{w}.png"), full_page=w == 390)
            lay = page.evaluate(LAYOUT_JS)
            check(f"{w}px {name}: 화면 밖·작은/겹친 버튼 없음", not any(lay.values()), json.dumps(lay, ensure_ascii=False)[:300])
            check(f"{w}px {name}: 가로 스크롤 없음", page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
            text = page.inner_text("body")
            check(f"{w}px {name}: null/undefined/NaN 없음", not any(x in text for x in ("null", "undefined", "NaN")))
        if w == 390:
            page.goto(URL + "#/search?q=" + "NS-500 단가" + "&mode=hybrid")
            page.wait_for_selector(".hit")
            page.locator(".hit").first.click()
            check("390px: 결과 누르면 근거 화면, ← 로 돌아옴", page.is_visible("#preview .pv-body"))
            page.screenshot(path=str(SHOTS / "11_preview_390.png"))
            page.click("#pvBack")
            check("390px: 결과 목록으로", not page.is_visible("#preview"))
    dark = b.new_context(color_scheme="dark", viewport={"width": 1280, "height": 900}).new_page()
    dark.on("pageerror", lambda e: errors.append(str(e)))
    dark.goto(URL + "#/search?q=" + "출장 일비" + "&mode=hybrid")
    dark.wait_for_selector(".hit")
    dark.screenshot(path=str(SHOTS / "20_search_dark_1280.png"))
    check("JS 오류 없음", not errors, "; ".join(errors[:5]))
    b.close()

srv.should_exit = True
time.sleep(0.5)
shutil.rmtree(TMP / "data", ignore_errors=True)
print(f"\n{sum(results)}/{len(results)} PASS · 스크린샷: {SHOTS}")
sys.exit(0 if all(results) else 1)
