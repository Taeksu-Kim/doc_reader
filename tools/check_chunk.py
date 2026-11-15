"""브라우저 점검: 청크 미리보기 화면(serve)을 실제 Chromium(headless)에서 눌러 본다.

    python tools/check_chunk.py [--doc FILE] [--shots DIR]

임시 폴더로 서버를 띄우고 끝나면 정리한다 (실제 data/는 건드리지 않음). 기본 문서는 긴 문서 회귀용 215쪽 매뉴얼
(doc_chunker/out/longdoc/manual.docx, 없으면 tests의 시험 매뉴얼). 화면을 바꾸면 이것도 돌린다.
"""
import argparse
import base64
import json
import shutil
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
from tests.test_chunk import _manual  # noqa: E402

LAYOUT_JS = (ROOT / "tools" / "check_read.py").read_text(encoding="utf-8")
LAYOUT_JS = LAYOUT_JS.split('LAYOUT_JS = """', 1)[1].split('"""', 1)[0].replace(".rail,.facets,.strip,.tbl,.menu", ".rail,.tbl,.map-row").replace("closest('.strip')", "closest('.map-row')")
# the unit map is an overview (like doc_parser's page strip): its cells are small on purpose; the unit list is the main way in

ap = argparse.ArgumentParser()
ap.add_argument("--doc", default=str(ROOT / "doc_chunker" / "out" / "longdoc" / "manual.docx"))
ap.add_argument("--shots", default=None)
args = ap.parse_args()
TMP = Path(tempfile.mkdtemp(prefix="doc_chunker_ui_"))
SHOTS = Path(args.shots) if args.shots else TMP / "shots"
SHOTS.mkdir(parents=True, exist_ok=True)
doc = Path(args.doc)
if not doc.is_file():
    doc = TMP / "manual.docx"
    doc.write_bytes(_manual())
bad = TMP / "깨진 파일.pdf"
bad.write_bytes(b"%PDF-1.4 broken")
hwp = TMP / "한글.hwp"
hwp.write_bytes(b"x")
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    PORT = s.getsockname()[1]
srv = uvicorn.Server(uvicorn.Config(create_app(TMP / "data"), host="127.0.0.1", port=PORT, log_level="warning"))
threading.Thread(target=srv.run, daemon=True).start()
while not srv.started:
    time.sleep(0.05)
BASE = f"http://127.0.0.1:{PORT}/"
URL = BASE + "chunk/"
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
    page.wait_for_selector(".intake")
    # 영역 이동: 공통 상단 바
    check("상단 바: 네 영역 링크, 지금 영역 표시", page.locator(".areas a").count() == 4
          and page.locator(".areas a[aria-current='page']").count() == 1)
    check("첫 화면: 빈 목록 + 놓는 곳 + 상한 입력", "아직 미리본 문서가 없습니다" in page.inner_text("#list") and page.is_visible("#limitIn"))
    page.screenshot(path=str(SHOTS / "01_empty_1280.png"))
    page.set_input_files("#file", str(hwp))
    page.wait_for_function("document.querySelector('#err') && document.querySelector('#err').textContent.length > 0")
    check("지원 안 하는 형식: 이유", "자를 수 없는 형식" in page.inner_text("#err"))

    t0 = time.time()
    page.set_input_files("#file", str(doc))
    page.wait_for_selector("#units .unit", timeout=300_000)
    n3000 = page.locator("#units .unit").count()
    check("문서 자르기 → 단위 지도 + 단위 목록", n3000 > 1 and page.locator("#map button").count() == n3000,
          f"단위 {n3000}개 · {time.time() - t0:.1f}s")
    check("단위마다 머리(절 이름)·토큰 막대", page.locator(".unit .u-path b").count() == n3000 and page.locator(".unit .meter").count() == n3000)
    page.screenshot(path=str(SHOTS / "02_doc_1280.png"))
    page.locator("#map button").nth(min(5, n3000 - 1)).click()
    page.wait_for_timeout(600)
    check("지도 칸 누르면 그 단위로", page.locator(f"#u{min(5, n3000 - 1)}.cur").count() == 1)
    first_more = page.locator("[data-more]").first
    if first_more.count():
        first_more.click()
        check("긴 단위 전체 보기", first_more.inner_text() == "접기")
    with page.expect_download() as dl:
        page.click("a[href$='units.jsonl']")
    check("JSONL 내려받기", dl.value.suggested_filename.endswith("_units_3000.jsonl"), dl.value.suggested_filename)

    page.fill("#reLimit", "1500")
    page.click("#reBtn")
    page.wait_for_function(f"document.querySelectorAll('#units .unit').length > {n3000}", timeout=300_000)
    n1500 = page.locator("#units .unit").count()
    check("상한 1500으로 다시 자르기 → 단위 늘어남, 이전 결과도 목록에", n1500 > n3000 and page.locator("#list li a").count() == 2,
          f"{n3000} → {n1500}")
    page.screenshot(path=str(SHOTS / "03_doc_1500_1280.png"))

    page.goto(URL + "#/new")
    page.wait_for_selector(".intake")
    page.fill("#limitIn", "1500")
    page.dispatch_event("#limitIn", "change")
    page.set_input_files("#file", str(doc))
    page.wait_for_selector("#toast.show")
    check("같은 파일 같은 상한: 이전 결과를 엶", "그것을 엽니다" in page.inner_text("#toast"))
    page.goto(URL + "#/new")
    page.wait_for_selector(".intake")
    page.set_input_files("#file", str(bad))
    page.wait_for_selector(".state .err")
    check("깨진 파일: 이유", "읽지 못했습니다" in page.inner_text(".state"))

    body = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": {"role": "ROLE_USER", "messageId": "m",
            "parts": [{"raw": base64.b64encode(_manual(3)).decode(), "filename": "에이전트가 보낸 매뉴얼.docx"},
                      {"data": {"skill": "chunk-document"}}]}}}
    req = urllib.request.Request(BASE + "a2a", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    task = json.loads(urllib.request.urlopen(req, timeout=60).read())["result"]["task"]
    page.wait_for_function("document.querySelector('#list').textContent.includes('에이전트가 보낸')", timeout=12_000)
    check("A2A로 자른 문서가 목록에 '에이전트' 표시로", page.locator("#list .tag.agent").count() == 1)
    page.goto(task["metadata"]["reviewUrl"])
    page.wait_for_selector("#units .unit")
    check("A2A 결과의 reviewUrl로 바로 미리보기", "에이전트가 보냄" in page.inner_text(".doc-meta"))

    page.goto(URL + "#/new")
    page.wait_for_selector(".intake")
    target = page.eval_on_selector_all("#list li a", "els => els.map(e => e.getAttribute('href'))")[-1]
    for w, h in [(1280, 900), (768, 1024), (390, 844)]:
        page.set_viewport_size({"width": w, "height": h})
        page.goto(URL + target)
        page.wait_for_selector("#units .unit")
        page.wait_for_timeout(300)
        page.screenshot(path=str(SHOTS / f"10_doc_{w}.png"), full_page=False)
        lay = page.evaluate(LAYOUT_JS)
        check(f"{w}px: 화면 밖·작은/겹친 버튼 없음", not any(lay.values()), json.dumps(lay, ensure_ascii=False)[:300])
        check(f"{w}px: 가로 스크롤 없음", page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
        text = page.inner_text("body")
        check(f"{w}px: null/undefined/NaN 없음", not any(x in text for x in ("null", "undefined", "NaN")))
        if w == 390:
            page.click("#railBtn")
            page.wait_for_timeout(300)
            check("390px: 목록 서랍", page.locator("#rail").bounding_box()["x"] >= -1)
            page.screenshot(path=str(SHOTS / "11_rail_390.png"))
            page.keyboard.press("Escape")
    dark = b.new_context(color_scheme="dark", viewport={"width": 1280, "height": 900}).new_page()
    dark.on("pageerror", lambda e: errors.append(str(e)))
    dark.goto(URL + target)
    dark.wait_for_selector("#units .unit")
    dark.screenshot(path=str(SHOTS / "20_doc_dark_1280.png"))
    check("JS 오류 없음", not errors, "; ".join(errors[:5]))
    b.close()

srv.should_exit = True
time.sleep(0.3)
shutil.rmtree(TMP / "data", ignore_errors=True)
print(f"\n{sum(results)}/{len(results)} PASS · 스크린샷: {SHOTS}")
sys.exit(0 if all(results) else 1)
