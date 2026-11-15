"""E2E for the opt-in OCR path at the CLI entry point (`--ocr-url`). The model server is the only
thing faked: a stdlib HTTP server speaking tools/ocr_server.py's protocol."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pymupdf
import pytest

from doc_parser.__main__ import main


def _png(text: str, w: int = 300, h: int = 150) -> bytes:
    d = pymupdf.open()
    p = d.new_page(width=w, height=h)
    p.insert_text((20, h / 2), text, fontsize=20)
    return p.get_pixmap(dpi=72).tobytes("png")


def _make_pdf(path: Path) -> None:
    doc = pymupdf.open()
    p1 = doc.new_page()                                     # text page with a figure in the middle
    p1.insert_text((72, 100), "Model architecture is described below.", fontsize=11)
    p1.insert_image(pymupdf.Rect(150, 150, 450, 400), stream=_png("FIGURE"))
    p1.insert_text((72, 450), "The encoder maps an input sequence.", fontsize=11)
    p2 = doc.new_page()                                     # scanned page: image only, no text layer
    p2.insert_image(p2.rect, stream=_png("SCAN", 595, 842))
    p3 = doc.new_page()                                     # tiny logo: not worth a model call
    p3.insert_text((72, 100), "Results are reported in Table 2.", fontsize=11)
    p3.insert_image(pymupdf.Rect(500, 20, 530, 40), stream=_png("logo", 30, 20))
    doc.save(str(path))


PAGE_ITEMS = [
    {"type": "heading", "level": 1, "text": "스캔 제목", "bbox": [0, 0, 100, 20]},
    {"type": "text", "text": "스캔 본문 첫 문단.", "bbox": [0, 30, 100, 50], "looped": True},
    {"type": "table", "rows": [["항목", "값"], ["BLEU", "28.4"]], "bbox": [0, 60, 100, 90]},
]
REGION_ITEMS = [
    {"type": "text", "text": "Multi-Head Attention", "bbox": [0, 0, 10, 10]},
    {"type": "text", "text": "Add & Norm", "bbox": [0, 20, 10, 30], "note": "차트로 인식됐지만 숫자 표가 아니라 그림 글자로 처리"},
    {"type": "image", "text": "", "bbox": [0, 0, 1, 1]},     # non-text labels are ignored
]


FIG = pymupdf.Rect(100, 200, 400, 360)      # where the word-grid figure is drawn (points)
_S = 150 / 72                               # doc_parser renders pages for layout at 150 dpi
LAYOUT_REGIONS = [
    {"label": "text", "score": 0.9, "bbox": [72 * _S, 90 * _S, 500 * _S, 110 * _S]},
    {"label": "chart", "score": 0.8, "bbox": [FIG.x0 * _S, FIG.y0 * _S, FIG.x1 * _S, FIG.y1 * _S]},
]


def _make_figure_pdf(path: Path) -> None:
    """Body text above and below a figure made of words on a grid (like an attention map): the
    text layer alone turns it into one-word paragraphs and an aligned 'table'."""
    doc = pymupdf.open()
    p = doc.new_page()
    p.insert_text((72, 100), "Attention weights are shown in the figure below.", fontsize=11)
    words = [["The", "Law", "will", "never"], ["be", "perfect", "but", "its"], ["application", "should", "be", "just"]]
    for r, row in enumerate(words):
        for c, w in enumerate(row):
            p.insert_text((FIG.x0 + 10 + c * 70, FIG.y0 + 30 + r * 40), w, fontsize=9)
    p.draw_rect(FIG, color=(0.5, 0.5, 0.5))
    p.insert_text((72, 400), "Figure 5: Two heads attend to sentence structure.", fontsize=11)
    p.insert_text((72, 430), "The model learns different tasks per head.", fontsize=11)
    doc.save(str(path))


def test_layout_groups_figure_words(tmp_path, capsys, fake_ocr):
    url, calls = fake_ocr
    pdf = tmp_path / "fig.pdf"
    _make_figure_pdf(pdf)
    body = ["Attention weights are shown in the figure below.", "Figure 5: Two heads attend to sentence structure.",
            "The model learns different tasks per head."]

    # the defect this option is for: the word grid becomes an aligned "table" that also swallows
    # the caption and the next paragraph as rows
    code, plain, _ = _run(capsys, str(pdf))
    assert code == 0 and [b["type"] for b in plain] == ["text", "table"]
    assert [r[1] for r in plain[1]["rows"][-2:]] == body[1:]

    code, blocks, _ = _run(capsys, str(pdf), "--layout-url", url)
    assert code == 0 and calls["layout"] == 1 and calls["page"] == calls["region"] == 0   # layout only
    assert [b["type"] for b in blocks] == ["text"] * 4
    assert [b["text"] for b in blocks if not b["text"].startswith("[그림]")] == body   # body untouched, in place
    fig = [b["text"] for b in blocks if b["text"].startswith("[그림]")]
    assert fig == ["[그림] The / Law / will / never / be / perfect / but / its / application / should / be / just"]
    assert blocks[1]["text"] == fig[0]                                         # between the text above and below
    assert _run(capsys, str(pdf), "--layout-url", url)[1] == blocks           # same input twice
    assert main([str(pdf), "--layout-url", url]) == 0
    assert "p.1: 그림 영역 1개 — 안의 글자 12줄을 그림 블록으로 묶음" in capsys.readouterr().out   # one word = one line

    # layout server failing on a page: warned, the page reads exactly as without the option
    calls["fail_layout"] = True
    assert _run(capsys, str(pdf), "--layout-url", url)[1] == plain
    assert main([str(pdf), "--layout-url", url]) == 0
    assert "p.1: 레이아웃 읽기 실패 (HTTPError) — 글자층만으로 읽음" in capsys.readouterr().out


@pytest.fixture
def fake_ocr():
    calls = {"page": 0, "region": 0, "fail_page": False}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._send(200, {"model": "fake-vl"})

        def do_POST(self):
            png = self.rfile.read(int(self.headers["Content-Length"]))
            assert png[:4] == b"\x89PNG"
            if self.path == "/layout":
                calls["layout"] = calls.get("layout", 0) + 1
                if calls.get("fail_layout"):
                    return self._send(500, {"error": "boom"})
                return self._send(200, {"regions": LAYOUT_REGIONS})
            mode = self.path.split("mode=")[1]
            calls[mode] += 1
            if mode == "page" and calls["fail_page"]:
                return self._send(500, {"error": "boom"})
            self._send(200, {"items": PAGE_ITEMS if mode == "page" else REGION_ITEMS})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", calls
    srv.shutdown()


def _run(capsys, *argv) -> tuple[int, list[dict], str]:
    code = main(list(argv) + ["--json"])
    out = capsys.readouterr()
    return code, [json.loads(l) for l in out.out.splitlines()], out.err


def test_ocr_opt_in(tmp_path, capsys, fake_ocr):
    url, calls = fake_ocr
    pdf = tmp_path / "paper.pdf"
    _make_pdf(pdf)

    # default: no model call, scanned page skipped exactly as before
    code, plain, _ = _run(capsys, str(pdf))
    assert code == 0 and calls == {"page": 0, "region": 0, "fail_page": False}
    assert not any("스캔" in b["text"] or "[그림]" in b["text"] for b in plain)

    code, blocks, _ = _run(capsys, str(pdf), "--ocr-url", url)
    assert code == 0
    assert calls["page"] == 1 and calls["region"] == 1          # logo on p.3 not sent
    p1 = [b["text"] for b in blocks if b["loc"]["page"] == 1]
    assert p1 == ["Model architecture is described below.", "[그림] Multi-Head Attention / Add & Norm",
                  "The encoder maps an input sequence."]               # figure sits where it is on the page
    p2 = [b for b in blocks if b["loc"]["page"] == 2]
    assert [(b["type"], b["text"] or b["rows"]) for b in p2] == [
        ("heading", "스캔 제목"), ("text", "스캔 본문 첫 문단."), ("table", [["항목", "값"], ["BLEU", "28.4"]])]
    assert [b["text"] for b in blocks if b["loc"]["page"] == 3] == ["Results are reported in Table 2."]

    # same input twice -> same blocks; human output names what the model read
    assert _run(capsys, str(pdf), "--ocr-url", url)[1] == blocks
    assert main([str(pdf), "--ocr-url", url]) == 0
    out = capsys.readouterr().out
    assert "p.2: 글자층 없음 → fake-vl로 읽음" in out and "p.1: 이미지 1개 영역을 fake-vl로 읽음" in out
    assert "p.2: OCR 모델이 같은 내용을 반복 생성해 잘라냄" in out and "p.1: OCR 모델이" not in out
    assert out.count("p.1: 차트로 인식됐지만 숫자 표가 아니라 그림 글자로 처리") == 1

    # one page failing at the server: warned, the rest of the document still comes back
    calls["fail_page"] = True
    assert main([str(pdf), "--ocr-url", url]) == 0
    out = capsys.readouterr().out
    assert "p.2: 글자층 없음, fake-vl 읽기 실패 (HTTPError)" in out and "[그림] Multi-Head" in out


def test_ocr_server_down(tmp_path, capsys):
    pdf = tmp_path / "paper.pdf"
    _make_pdf(pdf)
    code, blocks, err = _run(capsys, str(pdf), "--ocr-url", "http://127.0.0.1:9")
    assert code == 2 and blocks == [] and "OCR 서버에 연결 못 함" in err


def test_extract_output(tmp_path, capsys, fake_ocr):
    """--extract-output DIR: markdown + blocks.jsonl + meta.json + one PNG per figure, in place."""
    url, calls = fake_ocr
    pdf = tmp_path / "paper.pdf"
    _make_pdf(pdf)                          # p.1 raster figure, p.2 scanned page, p.3 tiny logo
    out = tmp_path / "out"
    (out / "images").mkdir(parents=True)
    (out / "images" / "p099_fig1.png").write_bytes(b"stale")      # left over from an earlier run
    (out / "images" / "keep.png").write_bytes(b"not ours")

    # no server: raster figures and the unread scanned page are still saved; the logo is not
    assert main([str(pdf), "--extract-output", str(out)]) == 0
    assert "--layout-url 없이는 래스터 이미지만" in capsys.readouterr().out
    assert sorted(p.name for p in (out / "images").iterdir()) == ["keep.png", "p001_fig1.png", "p002_fig1.png"]
    assert calls["page"] == calls["region"] == 0
    md = (out / "paper.md").read_text(encoding="utf-8")
    assert md.index("Model architecture") < md.index("](images/p001_fig1.png)") < md.index("The encoder maps")
    assert "<!-- p.2 -->\n\n![p.2 p002_fig1.png](images/p002_fig1.png)" in md
    blocks = [json.loads(l) for l in (out / "blocks.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [b["image"] for b in blocks if b["image"]] == ["p001_fig1.png", "p002_fig1.png"]
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    assert meta["figures"] == 2 and meta["blocks"] == len(blocks) and meta["source"] == str(pdf)
    png = (out / "images" / "p001_fig1.png").read_bytes()
    assert png[:4] == b"\x89PNG" and pymupdf.Pixmap(png).width >= 1000   # rendered from the PDF, sharp

    # with OCR: the figure block carries the model's labels and the scanned page its text
    assert main([str(pdf), "--ocr-url", url, "--extract-output", str(out)]) == 0
    capsys.readouterr()
    md2 = (out / "paper.md").read_text(encoding="utf-8")
    assert "](images/p001_fig1.png)\n\nMulti-Head Attention / Add & Norm" in md2 and "스캔 본문 첫 문단." in md2
    assert (out / "images" / "p001_fig1.png").read_bytes() == png                # same input, same crop


def test_extract_vector_figure_needs_layout(tmp_path, capsys, fake_ocr):
    url, _ = fake_ocr
    pdf = tmp_path / "fig.pdf"
    _make_figure_pdf(pdf)                   # the figure is vector: words + a drawn frame, no raster
    assert main([str(pdf), "--extract-output", str(tmp_path / "a")]) == 0
    assert not (tmp_path / "a" / "images").exists() or not any((tmp_path / "a" / "images").iterdir())
    assert main([str(pdf), "--layout-url", url, "--extract-output", str(tmp_path / "b")]) == 0
    capsys.readouterr()
    assert [p.name for p in (tmp_path / "b" / "images").iterdir()] == ["p001_fig1.png"]
    md = (tmp_path / "b" / "fig.md").read_text(encoding="utf-8")
    assert "![p.1 p001_fig1.png](images/p001_fig1.png)\n\nThe / Law / will" in md
    assert md.index("shown in the figure below") < md.index("images/p001_fig1.png") < md.index("Figure 5:")


def test_scan_figure_box_cut_by_model(tmp_path, capsys, fake_ocr, monkeypatch):
    """The model's figure box on a scan starts in the middle of the figure's title: the box is
    widened to where the ink ends and the labels are read again from the wider crop. A figure
    whose box already has white margin is left alone (no extra model call)."""
    import sys

    me = sys.modules[__name__]                     # the fake server reads these module globals

    url, calls = fake_ocr
    k = 200 / 72                                   # page items come in 200 dpi pixels
    src = pymupdf.open()
    p = src.new_page()
    p.insert_text((100, 200), "Scaled Dot-Product Attention", fontsize=12)   # ink from x=100, a 12pt title
    p.draw_rect(pymupdf.Rect(150, 400, 300, 500), color=(0, 0, 0), width=2)  # a clean figure
    scan = pymupdf.open()
    s = scan.new_page()
    s.insert_image(s.rect, stream=p.get_pixmap(dpi=150).tobytes("png"))
    pdf = tmp_path / "scan.pdf"
    scan.save(str(pdf))
    monkeypatch.setattr(me, "PAGE_ITEMS", [
        {"type": "text", "text": "[그림] ot-Product Attention", "bbox": [150 * k, 185 * k, 420 * k, 210 * k]},  # cut 50pt in
        {"type": "text", "text": "[그림] box", "bbox": [140 * k, 390 * k, 310 * k, 510 * k]},
    ])
    monkeypatch.setattr(me, "REGION_ITEMS", [{"type": "text", "text": "Scaled Dot-Product Attention", "bbox": [0, 0, 1, 1]}])
    code, blocks, _ = _run(capsys, str(pdf), "--ocr-url", url)
    assert code == 0
    cut, clean = [b for b in blocks if b["text"].startswith("[그림]")]
    assert cut["text"] == "[그림] Scaled Dot-Product Attention" and cut["bbox"][0] < 101, cut
    assert clean["text"] == "[그림] box" and clean["bbox"] == [140.0, 390.0, 310.0, 510.0]
    assert calls["region"] == 1

    # a 20pt title: its word spaces (~7pt) are wider than the 6pt gap that ends a figure
    src = pymupdf.open()
    p = src.new_page()
    p.insert_text((100, 200), "Scaled Dot-Product Attention", fontsize=20)
    p.insert_text((100, 300), "Body text far below.", fontsize=10)
    scan = pymupdf.open()
    scan.new_page().insert_image(scan[0].rect, stream=p.get_pixmap(dpi=150).tobytes("png"))
    scan.save(str(pdf))
    monkeypatch.setattr(me, "PAGE_ITEMS", [
        {"type": "text", "text": "[그림] Dot-Product Attention", "bbox": [170 * k, 178 * k, 420 * k, 206 * k]},
        {"type": "text", "text": "Body text far below.", "bbox": [100 * k, 290 * k, 200 * k, 302 * k]},
    ])
    code, blocks, _ = _run(capsys, str(pdf), "--ocr-url", url)
    big = next(b for b in blocks if b["text"].startswith("[그림]"))
    assert big["bbox"][0] < 101 and big["bbox"][3] < 215, big          # whole title, not down into the body


def test_text_drawn_as_paths(tmp_path, capsys, fake_ocr):
    """Design-tool exports (PDFKit) draw every letter as a vector path: no fonts, no images.
    Without OCR the page must not vanish silently; with OCR it is read like a scan."""
    url, calls = fake_ocr
    doc = pymupdf.open()
    p = doc.new_page()
    for k in range(20):                                   # stand-in for outlined glyphs
        p.draw_rect(pymupdf.Rect(72 + 12 * k, 100, 80 + 12 * k, 112), color=(0, 0, 0), fill=(0, 0, 0))
    pdf = tmp_path / "outlined.pdf"
    doc.save(str(pdf))
    code, blocks, _ = _run(capsys, str(pdf))
    assert blocks == []
    main([str(pdf)])
    assert "글자층 없음 (스캔본이거나 글자를 도형으로 그린 PDF)" in capsys.readouterr().out
    code, blocks, _ = _run(capsys, str(pdf), "--ocr-url", url)
    assert code == 0 and calls["page"] == 1 and any(b["text"] == "스캔 본문 첫 문단." for b in blocks)


def test_scan_header_dropped_only_when_repeated(tmp_path, capsys, fake_ocr, monkeypatch):
    """The layout model marks page headers, but it also calls a one-page form's title "header".
    Like the text-layer path, a marked header is dropped only when it repeats on most pages."""
    import sys

    me = sys.modules[__name__]
    url, _ = fake_ocr
    monkeypatch.setattr(me, "PAGE_ITEMS", [
        {"type": "text", "text": "캠페인 기획서", "bbox": [0, 0, 100, 20], "edge": True},
        {"type": "text", "text": "× × × ×", "bbox": [0, 0, 50, 5], "edge": True},          # decoration
        {"type": "text", "text": "본문 문단.", "bbox": [0, 30, 100, 50]},
    ])

    def scan(n: int) -> str:
        doc = pymupdf.open()
        for _ in range(n):
            doc.new_page().insert_image(pymupdf.Rect(0, 0, 595, 842), stream=_png("SCAN", 595, 842))
        path = tmp_path / f"scan{n}.pdf"
        doc.save(str(path))
        return str(path)

    _, one, _ = _run(capsys, scan(1), "--ocr-url", url)
    assert [b["text"] for b in one] == ["캠페인 기획서", "본문 문단."]          # a one-page title survives
    _, three, _ = _run(capsys, scan(3), "--ocr-url", url)
    assert [b["text"] for b in three] == ["본문 문단."] * 3                      # a running header goes
