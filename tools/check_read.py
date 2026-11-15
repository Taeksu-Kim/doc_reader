"""브라우저 점검: 읽기 검수 화면(serve)을 실제 Chromium(headless)에서 눌러 본다.

    python tools/browser_check.py [PDF] [--shots DIR]

임시 폴더로 서버를 직접 띄우고 끝나면 정리한다 (실제 data/는 건드리지 않음). 레이아웃·OCR 서버는
가짜(tests/test_read.FakeModels) — GPU를 쓰지 않는다. PDF를 주면 그 파일로, 없으면 시험용 PDF로 본다.
화면을 바꾸면 이것도 돌린다.
"""
import argparse
import base64
import json
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from doc_reader.app import create_app  # noqa: E402
from tests.test_read import FakeModels, _docx, _pdf  # noqa: E402

LAYOUT_JS = """() => {
  const W = document.documentElement.clientWidth, out = {overflow: [], small: [], overlap: []};
  const vis = e => { const s = getComputedStyle(e), r = e.getBoundingClientRect();
    return s.visibility !== 'hidden' && s.display !== 'none' && r.width > 0 && r.height > 0; };
  const name = e => e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') + ' "' + (e.innerText || e.value || e.getAttribute('aria-label') || '').trim().slice(0, 20) + '"';
  for (const e of document.querySelectorAll('body *')) {
    if (!vis(e)) continue; const r = e.getBoundingClientRect();
    if (r.right > W + 1 && getComputedStyle(e.parentElement).overflowX === 'visible' && !e.closest('.rail,.facets,.strip,.tbl,.menu')) out.overflow.push(name(e));
  }
  const hits = [...document.querySelectorAll('a[href],button,input:not([type=hidden]):not([aria-hidden=true]),select,textarea,[role=button]')].filter(vis)
     .filter(e => !e.closest('.strip'));
  const tiny = r => r.width < 24 || r.height < 24;
  for (const e of hits) {
    const r = e.getBoundingClientRect();
    const lab = e.closest('label') || (e.id && document.querySelector(`label[for="${e.id}"]`));
    if (tiny(r) && !(lab && vis(lab) && !tiny(lab.getBoundingClientRect())) && !e.closest('p,li,td'))
      out.small.push(name(e) + ` ${Math.round(r.width)}x${Math.round(r.height)}`); }
  for (let i = 0; i < hits.length; i++) for (let j = i + 1; j < hits.length; j++) {
    const a = hits[i].getBoundingClientRect(), b = hits[j].getBoundingClientRect();
    if (hits[i].contains(hits[j]) || hits[j].contains(hits[i])) continue;
    if (a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1) out.overlap.push(name(hits[i]) + ' <> ' + name(hits[j]));
  }
  return out;
}"""

ap = argparse.ArgumentParser()
ap.add_argument("pdf", nargs="?")
ap.add_argument("--shots", default=None)
args = ap.parse_args()
TMP = Path(tempfile.mkdtemp(prefix="doc_parser_ui_"))
SHOTS = Path(args.shots) if args.shots else TMP / "shots"
SHOTS.mkdir(parents=True, exist_ok=True)
pdf_bytes = Path(args.pdf).read_bytes() if args.pdf else _pdf(pages=4)
pdf_name = Path(args.pdf).name if args.pdf else "시험 보고서.pdf"

with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    PORT = s.getsockname()[1]
BASE = f"http://127.0.0.1:{PORT}/"
URL = BASE + "read/"
FakeModels.up = True
app = create_app(TMP / "data", model_url="http://fake", reader_factory=FakeModels)
server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
for _ in range(40):
    try:
        urllib.request.urlopen(URL + "api/status", timeout=2)
        break
    except Exception:
        time.sleep(0.25)
FALLBACK = sorted(Path.home().glob(".cache/ms-playwright/chromium-*/chrome-linux64/chrome"))
files = {"pdf": TMP / pdf_name, "docx": TMP / "회의록.docx", "bad": TMP / "깨진 파일.pdf", "hwp": TMP / "한글.hwp"}
files["pdf"].write_bytes(pdf_bytes)
files["docx"].write_bytes(_docx())
files["bad"].write_bytes(b"%PDF-1.4 broken")
files["hwp"].write_bytes(b"x")

results = []
step = ["시작"]


def check(name, ok, detail=""):
    step[0] = name
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f" -- {detail}" if detail else ""))


def a2a_send(path: Path) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": {
        "role": "ROLE_USER", "messageId": "m", "parts": [{"raw": base64.b64encode(path.read_bytes()).decode(),
                                                          "filename": "에이전트가 보낸 " + path.name}]}}}
    req = urllib.request.Request(BASE + "a2a", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=120).read())["result"]["task"]


with sync_playwright() as p:
    try:
        b = p.chromium.launch()
    except Exception:
        b = p.chromium.launch(executable_path=str(FALLBACK[-1]))
    page = b.new_page(viewport={"width": 1280, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    # failed GETs are caught as responses (with the step they happened after); the browser's own
    # "Failed to load resource" console line would also flag the expected 400 of a rejected upload
    page.on("console", lambda m: m.type == "error" and "Failed to load resource" not in m.text and errors.append(m.text))
    page.context.on("response", lambda r: r.status >= 400 and r.request.method == "GET"
                    and errors.append(f"{r.status} {r.url} (after: {step[0]})"))

    page.goto(URL)
    page.wait_for_selector(".intake")
    check("첫 화면: 빈 목록 안내 + 파일 놓는 곳", "아직 읽은 문서가 없습니다" in page.inner_text("#docList") and page.is_visible("#drop"))
    # 영역 이동: 공통 상단 바
    check("상단 바: 네 영역 링크, 지금 영역 표시", page.locator(".areas a").count() == 4
          and page.locator(".areas a[aria-current='page']").count() == 1)
    check("모델 켜짐 표시 · 옵션 사용 가능", "켜짐" in page.inner_text("#modelPill") and page.is_enabled("#optLayout"))
    page.screenshot(path=str(SHOTS / "01_empty_1280.png"))

    page.set_input_files("#file", str(files["hwp"]))
    page.wait_for_function("document.querySelector('#dropErr').textContent.length > 0")
    check("지원 안 하는 형식: 놓는 곳 아래에 이유", "읽을 수 없는 형식" in page.inner_text("#dropErr"), page.inner_text("#dropErr"))

    page.check("#optLayout")
    t0 = time.time()
    page.set_input_files("#file", str(files["pdf"]))
    page.wait_for_selector(".review", timeout=180_000)
    check("PDF 읽기 → 대조 화면", page.is_visible("#stage .page img"), f"{time.time() - t0:.1f}s")
    pdf_id = page.evaluate("location.hash").split("/")[-1]
    page.wait_for_function("document.querySelector('#stage img').complete && document.querySelector('#stage img').naturalWidth > 0")
    nbox = page.locator("#stage rect").count()
    nblk = page.locator("#blocks .blk").count()
    check("1쪽: 페이지 위 상자 수 = 목록 블록 수", nbox > 0 and nbox == nblk, f"상자 {nbox} · 블록 {nblk}")
    page.screenshot(path=str(SHOTS / "02_doc_1280.png"))

    # 목록 블록 → 페이지 상자 선택
    page.locator("#blocks .blk").nth(min(2, nblk - 1)).click()
    i = page.locator("#blocks .blk.sel").get_attribute("data-i")
    check("블록 누르면 같은 상자가 선택됨", page.locator(f"#stage rect.sel[data-i='{i}']").count() == 1)
    # 페이지 상자 → 목록
    page.locator("#stage rect").first.dispatch_event("click")
    j = page.locator("#stage rect").first.get_attribute("data-i")
    check("상자 누르면 같은 블록이 선택됨", page.locator(f"#blocks .blk.sel[data-i='{j}']").count() == 1)

    # 쪽 이동: 키보드, 띠, 입력
    page.keyboard.press("ArrowRight")
    check("→ 키로 다음 쪽 (쪽 띠 현재 표시도 따라감)", page.input_value("#pgIn") == "2"
          and page.get_attribute("#strip button[aria-current='true']", "data-p") == "2")
    page.locator("#strip button").last.click()
    last = page.locator("#strip button").count()
    check("쪽 띠로 마지막 쪽", page.input_value("#pgIn") == str(last))
    page.fill("#pgIn", "1")
    page.press("#pgIn", "Enter")
    check("쪽 번호 입력", page.input_value("#pgIn") == "1")

    # 종류 거르기 + 전체 범위
    page.click(".facet[data-f='table']")
    kinds = set(page.eval_on_selector_all("#blocks .blk", "els => els.map(e => e.className)"))
    check("표만 거르기: 목록·상자 모두 표", all("table" in k for k in kinds) and
          all("table" in c for c in page.eval_on_selector_all("#stage rect", "els => els.map(e => e.getAttribute('class'))")))
    page.click("[data-scope='all']")
    divs = page.locator("#blocks .page-div").count()
    check("전체 범위: 쪽 구분선", divs >= 1, f"{divs}개")
    page.click(".facet[data-f='']")
    page.click("[data-scope='page']")

    # 위치 표시 끄기
    page.uncheck("#boxTg")
    check("읽힌 자리 표시 끄기", not page.is_visible("#stage svg"))
    page.check("#boxTg")

    # 그림 (레이아웃 켬)
    if page.locator(".facet[data-f='figure'] .n").inner_text() != "0":
        page.click(".facet[data-f='figure']")
        page.click("[data-scope='all']")
        page.wait_for_selector("#blocks .fig img")
        imgs = page.locator("#blocks .fig img")
        for k in range(min(3, imgs.count())):          # lazy images load when scrolled to
            imgs.nth(k).scroll_into_view_if_needed()
        page.wait_for_timeout(800)
        ok = page.evaluate("[...document.querySelectorAll('#blocks .fig img')].slice(0, 3).every(i => i.complete && i.naturalWidth > 0)")
        check("그림 블록: 잘라낸 그림 이미지 보임", ok, f"그림 {imgs.count()}개")
        page.screenshot(path=str(SHOTS / "03_figures_1280.png"))
        page.click(".facet[data-f='']")
        page.click("[data-scope='page']")

    # 내려받기
    with page.expect_download() as dl:
        page.click("a[href$='export.zip']")
    check("내려받기: zip", dl.value.suggested_filename.endswith(".zip"), dl.value.suggested_filename)

    # 같은 파일 다시 → 이전 결과
    page.goto(URL + "#/new")
    page.wait_for_selector(".intake")
    check("옵션 기억 (레이아웃 체크 유지)", page.is_checked("#optLayout"))
    page.set_input_files("#file", str(files["pdf"]))
    page.wait_for_selector("#toast.show")
    check("같은 파일 같은 방식: 이전 결과를 연다고 안내", "그것을 엽니다" in page.inner_text("#toast"), page.inner_text("#toast"))
    check("목록에 한 건", page.locator("#docList li a").count() == 1)

    # 깨진 파일
    page.goto(URL + "#/new")
    page.wait_for_selector(".intake")
    page.set_input_files("#file", str(files["bad"]))
    page.wait_for_selector(".state .err", timeout=60_000)
    check("깨진 파일: 실패 이유 + 다시 읽기", "읽지 못했습니다" in page.inner_text(".state") and page.is_visible("#rereadBtn"))
    page.screenshot(path=str(SHOTS / "04_failed_1280.png"))

    # docx: 페이지 없이 블록만
    page.goto(URL + "#/new")
    page.wait_for_selector(".intake")
    page.set_input_files("#file", str(files["docx"]))
    page.wait_for_selector(".review.nopages", timeout=60_000)
    check("docx: 페이지 영역 없이 블록 목록", page.locator("#blocks .blk").count() >= 2 and not page.is_visible(".viewer"))

    # 에이전트(A2A)가 보낸 문서가 목록에 뜨는지 (5초 주기 새로고침)
    ag = TMP / "agent.pdf"
    ag.write_bytes(_pdf(("Sent by another agent.",), pages=1))
    task = a2a_send(ag)
    page.wait_for_function("document.querySelector('#docList').textContent.includes('에이전트가 보낸')", timeout=12_000)
    check("A2A로 들어온 문서가 목록에 '에이전트' 표시로 나타남", page.locator("#docList .tag.agent").count() == 1)
    page.goto(task["metadata"]["reviewUrl"])
    page.wait_for_selector(".review")
    check("A2A 결과의 reviewUrl로 바로 검수 화면", "에이전트가 보냄" in page.inner_text(".doc-meta"))
    page.goto(URL + "#/docs/" + pdf_id + "?p=3")
    page.wait_for_function("document.querySelector('#pgIn') && document.querySelector('#pgIn').value === '3'")
    check("주소의 ?p=3으로 그 쪽을 바로 엶 (자료실 '원본에서 보기')", page.input_value("#pgIn") == "3")

    # 모델 꺼짐: 옵션 잠김 + 켜는 법
    FakeModels.up = False
    app.state.read._model_seen = (0.0, {})
    page.goto(URL + "#/new")
    page.reload()
    page.wait_for_selector(".intake")
    page.wait_for_function("document.querySelector('#modelState').textContent === '꺼짐'")
    check("모델 꺼짐: 옵션 잠김 + 켜는 명령 안내", page.is_disabled("#optLayout") and page.is_visible("#serverNote"))
    page.screenshot(path=str(SHOTS / "05_models_off_1280.png"))

    # 지우기 (두 번 눌러 확인)
    page.goto(URL + "#/docs/" + task["id"])
    page.wait_for_selector(".review")
    before = page.locator("#docList li a").count()
    page.click("#moreBtn")
    page.click("#delBtn")
    check("지우기 1회: 확인 문구로 바뀜", "한 번 더" in page.inner_text("#delBtn"))
    page.click("#delBtn")
    page.wait_for_function(f"document.querySelectorAll('#docList li a').length === {before - 1}")
    check("지우기 2회: 목록에서 빠짐", True)

    # 폭 3종 + 다크
    page.goto(URL)
    page.wait_for_selector("#docList li a")
    pdf_link = [h for h in page.eval_on_selector_all("#docList li a", "els => els.map(e => e.getAttribute('href'))")]
    for w, h in [(1280, 900), (768, 1024), (390, 844)]:
        page.set_viewport_size({"width": w, "height": h})
        for route in pdf_link[-1:]:          # the first PDF read
            page.goto(URL + route)
            page.wait_for_selector(".review")
            page.wait_for_timeout(400)
            page.screenshot(path=str(SHOTS / f"10_doc_{w}.png"), full_page=w == 390)
            lay = page.evaluate(LAYOUT_JS)
            check(f"{w}px: 화면 밖 요소·작은/겹친 버튼 없음", not any(lay.values()), json.dumps(lay, ensure_ascii=False)[:300])
            check(f"{w}px: 가로 스크롤 없음", page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
            text = page.inner_text("body")
            check(f"{w}px: null/undefined/NaN 없음", not any(x in text for x in ("null", "undefined", "NaN")))
        if w == 390:
            page.click("[data-tab='blocks']")
            check("390px: 원본/블록 탭 전환", page.is_visible("#blocks") and not page.is_visible("#stage"))
            page.locator("#blocks .blk").first.click()
            check("390px: 블록 누르면 원본 탭으로 가서 자리 표시", page.is_visible("#stage") and page.locator("#stage rect.sel").count() == 1)
            page.screenshot(path=str(SHOTS / "11_doc_390_page.png"))
            page.click("#railBtn")
            page.wait_for_timeout(300)
            check("390px: 문서 목록 서랍", page.locator("#rail").bounding_box()["x"] >= -1)
            page.screenshot(path=str(SHOTS / "12_rail_390.png"))
            page.keyboard.press("Escape")
            page.goto(URL + "#/new")
            page.wait_for_selector(".intake")
            page.screenshot(path=str(SHOTS / "13_empty_390.png"), full_page=True)
    dark = b.new_context(color_scheme="dark", viewport={"width": 1280, "height": 900}).new_page()
    dark.on("pageerror", lambda e: errors.append(str(e)))
    dark.goto(URL + pdf_link[-1])
    dark.wait_for_selector(".review")
    dark.wait_for_timeout(500)
    dark.screenshot(path=str(SHOTS / "20_doc_dark_1280.png"))
    check("JS 오류 없음", not errors, "; ".join(errors[:5]))
    b.close()

server.should_exit = True
import shutil  # noqa: E402

shutil.rmtree(TMP / "data", ignore_errors=True)       # keep only the screenshots
print(f"\n{sum(results)}/{len(results)} PASS · 스크린샷: {SHOTS}")
sys.exit(0 if all(results) else 1)
