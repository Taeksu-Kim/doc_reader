"""PaddleOCR-VL behind a tiny HTTP server, for ``doc_parser --ocr-url``.

Two processes: vLLM generates the text (fast), this server runs layout detection in the paddle env
and speaks doc_parser's protocol. Start/stop both with tools/ocr.sh (README "OCR 옵션").

    GET  /health                 -> {"model": "PaddleOCR-VL-1.6"}
    POST /read?mode=page|region  (body: PNG) -> {"items": [{type, text, level, rows, bbox, looped, note}]}
    POST /layout                 (body: PNG) -> {"regions": [{label, score, bbox}]}  (PP-DocLayoutV3 only)

page:   a whole scanned page, items in the model's reading order (headers/footers/page numbers dropped,
        text inside figures as "[그림] a / b")
region: one image cropped from a text page; the labels inside it come back as plain text
bbox is in pixels of the posted image. One request at a time.

Who reads what:
- body text, titles, tables, real charts -> PaddleOCR-VL (layout + VLM)
- labels inside figures -> PP-OCRv5 (Korean model, reads Latin too): detection + line recognition
  cannot loop the way the VLM does on arrow diagrams ("Linear ↑ ↑" x670 on the Transformer fig. 2)
- a "chart" whose table is mostly words (word-grid figures) is not trusted as a table -> figure labels
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import threading
import time
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HEADING = {"doc_title": 1, "paragraph_title": 2}
DROP = {"number", "header_image", "footer_image", "seal", "formula_number", "footnote_number"}
# header/footer text is sent marked ``edge``: the layout model also calls a one-page form's title
# "header" (MiriCanvas 캠페인 기획서), so doc_parser drops it only when it repeats across pages
EDGE = {"header", "footer"}
TABLE = {"table"}
CHART = {"chart"}
IMAGE = {"image"}
PREDICT = dict(use_doc_orientation_classify=False, use_doc_unwarping=False,
               use_ocr_for_image_block=False, use_chart_recognition=True)
LINE_OCR_LANG = "korean"
MIN_FIGURE_SCORE = 0.7     # median line-OCR score below this: the figure's text is not trusted

_LOOP = re.compile(r"(.{3,120}?)(?:\s*\1){4,}", re.S)      # same piece 5+ times in a row
_NUM = re.compile(r"^[\s(+\-−–▲▼△▽]*[\d][\d,.]*\s*(%|%p|[xX배원억만조달러$€¥]{1,3}|bp|[kKmMbB])?\)?\s*$")
_WORD = re.compile(r"[0-9A-Za-z가-힣]")


def squash(text: str) -> tuple[str, bool]:
    """Small VLMs sometimes loop. Keep one copy, report it."""
    out = _LOOP.sub(r"\1", text)
    return out, out != text


def dedupe_rows(rows: list[list[str]]) -> tuple[list[list[str]], bool]:
    out: list[list[str]] = []
    for r in rows:
        if len(out) >= 2 and r == out[-1] == out[-2]:
            continue
        out.append(r)
    return out, len(out) != len(rows)


def chart_is_data(rows: list[list[str]]) -> bool:
    """A table read off a real chart is mostly numbers outside its header row / label column.
    Word-grid figures (attention maps) come back as tables of words."""
    cells = [c for r in rows[1:] for c in r[1:] if c.strip()]
    nums = sum(1 for c in cells if _NUM.match(c))
    return nums >= 2 and nums >= 0.5 * len(cells)


class _Table(HTMLParser):
    """<table> html -> rows; colspan/rowspan cells keep their text once, spans become ''."""

    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self.spans: dict[tuple[int, int], str] = {}
        self.cell: list[str] | None = None
        self.attrs: dict = {}

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.rows.append([])
        elif tag in ("td", "th"):
            self.cell, self.attrs = [], dict(attrs)

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None and self.rows:
            r = len(self.rows) - 1
            row = self.rows[-1]
            while (r, len(row)) in self.spans:
                row.append(self.spans.pop((r, len(row))))
            c = len(row)
            row.append(" ".join("".join(self.cell).split()))
            cs, rs = int(self.attrs.get("colspan", 1) or 1), int(self.attrs.get("rowspan", 1) or 1)
            row.extend([""] * (cs - 1))
            for dr in range(1, rs):
                for dc in range(cs):
                    self.spans[(r + dr, c + dc)] = ""
            self.cell = None


def table_rows(content: str) -> list[list[str]]:
    if "<t" in content:
        p = _Table()
        p.feed(content)
        rows = p.rows
    else:                                   # markdown / pipe table (charts come back this way)
        rows = [[c.strip() for c in ln.strip().strip("|").split("|")]
                for ln in content.splitlines() if "|" in ln and not re.fullmatch(r"[\s|:\-]+", ln)]
    rows = [r for r in rows if any(c for c in r)]
    width = max((len(r) for r in rows), default=0)
    return [r + [""] * (width - len(r)) for r in rows]


class Reader:
    def __init__(self, version: str, vl_url: str | None, vl_model: str):
        from paddleocr import PaddleOCR, PaddleOCRVL

        kw = dict(pipeline_version=version)
        if vl_url:      # text generation on a vLLM server: paddle's own decoder is ~100x slower on WSL
            # one block at a time: concurrent blocks batch differently run to run and greedy
            # decoding then drifts (measured: 4 of ~170 blocks differed between two runs)
            kw.update(vl_rec_backend="vllm-server", vl_rec_server_url=vl_url, vl_rec_api_model_name=vl_model,
                      vl_rec_max_concurrency=1)
        self.pipe = PaddleOCRVL(**kw)
        self.lines = PaddleOCR(lang=LINE_OCR_LANG, use_doc_orientation_classify=False,
                               use_doc_unwarping=False, use_textline_orientation=False)
        from paddleocr import LayoutDetection
        self.layout_model = LayoutDetection(model_name="PP-DocLayoutV3")
        self.name = f"PaddleOCR-VL-{version.lstrip('v')}"
        self.lock = threading.Lock()

    def figure_lines(self, img, bbox) -> list[str] | None:
        """Labels inside a figure, top-to-bottom then left-to-right. None = read, but not trusted:
        rotated / tiny text comes back as garbage with a median score ~0.4-0.5 (Transformer p.13-14),
        while clean labels sit at ~0.996+ (measured 2026-11-14)."""
        x0, y0, x1, y1 = (int(round(v)) for v in bbox)
        crop = img[max(0, y0):y1, max(0, x0):x1]
        if crop.size == 0:
            return []
        r = self.lines.predict(crop)[0].json
        r = r.get("res", r)
        scores = sorted(r.get("rec_scores", []))
        if scores and scores[len(scores) // 2] < MIN_FIGURE_SCORE:
            return None
        boxes = [(list(map(float, b)), t.strip()) for t, b in zip(r.get("rec_texts", []), r.get("rec_boxes", []))
                 if _WORD.search(t)]                            # drop lone arrows / symbols
        if boxes:   # an arrow head between two boxes reads as "4" (score 0.965): one character, half height
            mid = sorted(b[3] - b[1] for b, _ in boxes)[len(boxes) // 2]
            boxes = [(b, t) for b, t in boxes if len(t) > 1 or b[3] - b[1] >= 0.6 * mid]
        rows: list[dict] = []           # 1) boxes that overlap vertically by half their height = one row
        for b, t in sorted(boxes, key=lambda f: f[0][1]):
            for row in rows:
                ov = min(row["y1"], b[3]) - max(row["y0"], b[1])
                if ov >= 0.5 * min(row["y1"] - row["y0"], b[3] - b[1]):
                    row["items"].append((b, t))
                    row["y0"], row["y1"] = min(row["y0"], b[1]), max(row["y1"], b[3])
                    break
            else:
                rows.append({"y0": b[1], "y1": b[3], "items": [(b, t)]})
        phrases = []                    # 2) within a row, left to right; a word-sized gap splits phrases
        for row in sorted(rows, key=lambda r: r["y0"]):
            cur, right = [], None
            for b, t in sorted(row["items"], key=lambda f: f[0][0]):
                if cur and b[0] - right > 1.5 * (b[3] - b[1]):
                    phrases.append(" ".join(cur))
                    cur = []
                cur.append(t)
                right = b[2]
            phrases.append(" ".join(cur))
        return phrases

    def figure_text(self, img, bbox) -> str | None:
        lines = self.figure_lines(img, bbox)
        return None if lines is None else " / ".join(lines)

    def chart_caption(self, img, bbox, rows: list[list[str]]) -> str:
        """Chart -> table keeps the numbers but drops the title, unit and legend ("분기별 매출액
        (억원)"). Put back what the line OCR sees that is not already a cell or a number."""
        key = lambda s: re.sub(r"[\s,]", "", s)  # noqa: E731
        cells = {key(c) for r in rows for c in r if c.strip()}
        keep = [t for t in self.figure_lines(img, bbox) or []     # axis ticks "0 250 500" are numbers too
                if key(t) not in cells and not all(_NUM.match(w) for w in t.split())]
        return " / ".join(dict.fromkeys(keep))

    def items(self, img, blocks: list[dict], mode: str) -> list[dict]:
        out = []
        for b in blocks:
            label, content = b.get("block_label", ""), (b.get("block_content") or "").strip()
            bbox = [float(v) for v in (b.get("block_bbox") or [0, 0, 0, 0])][:4]
            if label in DROP:
                continue
            if label in EDGE:
                if content:
                    out.append({"type": "text", "text": squash(content)[0], "bbox": bbox, "looped": False,
                                "note": "", "edge": True})
                continue
            note = ""
            rows = table_rows(content) if label in TABLE | CHART and content else []
            if label in CHART and rows and not chart_is_data(rows):
                note, rows, label = "차트로 인식됐지만 숫자 표가 아니라 그림 글자로 처리", [], "image"
            if rows:                        # never regex-squash table html: runs of empty cells are real
                if label in CHART and (cap := self.chart_caption(img, bbox, rows)):
                    out.append({"type": "text", "text": cap if mode == "region" else "[차트] " + cap,
                                "bbox": bbox, "looped": False, "note": ""})
                rows, looped = dedupe_rows(rows)
                out.append({"type": "table", "rows": rows, "bbox": bbox, "looped": looped, "note": note})
                continue
            # region = a raster figure the text layer lacks: whatever layout calls it (diagrams come
            # back as "formula" -> a LaTeX array of arrows), read its labels line by line
            if label in IMAGE | CHART or mode == "region":
                text = self.figure_text(img, bbox)
                if text is None:
                    out.append({"type": "text", "text": "", "bbox": bbox, "looped": False,
                                "note": "그림 글자 인식 신뢰도 낮음(회전·작은 글씨 추정) — 생략"})
                elif text:
                    out.append({"type": "text", "text": text if mode == "region" else "[그림] " + text,
                                "bbox": bbox, "looped": False, "note": note})
                continue
            if not content:
                continue
            content, looped = squash(content)
            item = ({"type": "heading", "level": HEADING[label], "text": content} if label in HEADING
                    else {"type": "text", "text": content})
            out.append(dict(item, bbox=bbox, looped=looped, note=note))
        if mode == "region" and not any(o["type"] == "table" for o in out):
            # a figure with no data table: its sub-blocks can leave out a title above them and
            # repeat one shared by two blocks (Transformer scan p.4) -> read the whole crop at once
            h, w = img.shape[:2]
            text = self.figure_text(img, [0, 0, w, h])
            if text is None:
                return [{"type": "text", "text": "", "bbox": [0, 0, w, h], "looped": False,
                         "note": "그림 글자 인식 신뢰도 낮음(회전·작은 글씨 추정) — 생략"}]
            return [{"type": "text", "text": text, "bbox": [0, 0, w, h], "looped": False, "note": ""}] if text else []
        return out

    def layout(self, png: bytes) -> list[dict]:
        """Layout regions only (~0.05 s/page): what doc_parser's text-layer path needs to tell a
        figure's words from body text. Same model as the VL pipeline's first stage."""
        import cv2
        import numpy as np

        img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
        with self.lock:
            r = self.layout_model.predict(img)[0].json
        r = r.get("res", r)
        return [{"label": b["label"], "score": round(float(b["score"]), 3),
                 "bbox": [float(v) for v in b["coordinate"]]} for b in r.get("boxes", [])]

    def read(self, png: bytes, mode: str) -> list[dict]:
        import cv2
        import numpy as np

        img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
        with self.lock, tempfile.NamedTemporaryFile(suffix=".png") as f:
            f.write(png)
            f.flush()
            res = list(self.pipe.predict(f.name, **PREDICT))
            blocks = []
            for r in res:
                j = r.json
                blocks += j.get("res", j).get("parsing_res_list", [])
            return self.items(img, blocks, mode)


def serve(reader: Reader, host: str, port: int) -> None:
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
            sys.stderr.write("[ocr] " + (fmt % a) + "\n")

        def _send(self, code: int, obj: dict) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if urlparse(self.path).path == "/health":
                return self._send(200, {"model": reader.name})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            u = urlparse(self.path)
            if u.path == "/layout":
                png = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                t = time.time()
                try:
                    regions = reader.layout(png)
                except Exception as exc:
                    return self._send(500, {"error": f"{type(exc).__name__}: {exc}"[:500]})
                sys.stderr.write(f"[ocr] layout -> {len(regions)} regions, {time.time() - t:.2f}s\n")
                return self._send(200, {"regions": regions})
            mode = parse_qs(u.query).get("mode", ["page"])[0]
            if u.path != "/read" or mode not in ("page", "region"):
                return self._send(404, {"error": "POST /read?mode=page|region"})
            png = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            t = time.time()
            try:
                items = reader.read(png, mode)
            except Exception as exc:
                return self._send(500, {"error": f"{type(exc).__name__}: {exc}"[:500]})
            sys.stderr.write(f"[ocr] {mode} {len(png) // 1024}KB -> {len(items)} items, {time.time() - t:.1f}s\n")
            self._send(200, {"items": items})

    srv = HTTPServer((host, port), H)
    sys.stderr.write(f"[ocr] {reader.name} ready on http://{host}:{port}\n")
    srv.serve_forever()


def main() -> None:
    ap = argparse.ArgumentParser(description="PaddleOCR-VL HTTP server for doc_parser --ocr-url")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--version", default="v1.6", help="PaddleOCRVL pipeline_version")
    ap.add_argument("--vl-url", help="vLLM server for text generation, e.g. http://<wsl-ip>:8091/v1 "
                                     "(without it paddle decodes in-process: minutes per page on WSL)")
    ap.add_argument("--vl-model", default="PaddleOCR-VL-1.6-0.9B", help="served model name on --vl-url")
    ap.add_argument("--once", metavar="PNG", help="read one image, print items, exit (no server)")
    ap.add_argument("--mode", default="page", choices=["page", "region"])
    args = ap.parse_args()
    reader = Reader(args.version, args.vl_url, args.vl_model)
    if args.once:
        print(json.dumps(reader.read(Path(args.once).read_bytes(), args.mode), ensure_ascii=False, indent=1))
        return
    serve(reader, args.host, args.port)


if __name__ == "__main__":
    main()
