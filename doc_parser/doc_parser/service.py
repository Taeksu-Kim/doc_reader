"""Reading desk: the one owner of state behind the screen (app.py) and the A2A endpoint.

    data/parser.db                     one row per reading (file + options), SQLite
    data/docs/<id>/source.<ext>        the file as received
    data/docs/<id>/out/                write_export output: <stem>.md, blocks.jsonl, meta.json, images/
    data/docs/<id>/pages.json          PDF page sizes in points (for drawing block boxes on page images)
    data/docs/<id>/pages/              rendered page PNGs (cache)

One worker thread reads one document at a time (the OCR/layout server is one GPU process anyway).
Readings left queued/reading by a stopped server are read again on start.
"""

from __future__ import annotations

import hashlib
import io
import json
import queue
import shutil
import sqlite3
import threading
import time
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Literal, Optional

from doc_parser.export import IMAGES, write_export
from doc_parser.ocr import HttpOcr, OcrUnavailable
from doc_parser.registry import PARSERS, parse_file

State = Literal["queued", "reading", "done", "failed", "canceled"]
ACTIVE: tuple[State, ...] = ("queued", "reading")
MAX_BYTES = 200 * 1024 * 1024
PAGE_WIDTHS = (480, 960, 1440)          # rendered page widths (px) the screen may ask for


class Rejected(ValueError):
    """The input cannot be read (unsupported type, empty, too big). Message is for people."""


class NotFound(KeyError):
    pass


class Conflict(RuntimeError):
    """The action does not fit the reading's state (cancel a finished one, delete while reading)."""


@dataclass
class Reading:
    id: str
    name: str                       # file name as given
    kind: str                       # suffix without dot
    size: int
    sha256: str
    layout: bool
    ocr: bool
    source: str                     # "screen" | "a2a" | "api"
    state: State
    created: float
    context_id: str = ""
    started: Optional[float] = None
    finished: Optional[float] = None
    error: str = ""
    title: str = ""
    pages: int = 0
    blocks: int = 0
    tables: int = 0
    figures: int = 0
    warnings: int = 0
    markdown: str = ""              # file name of the .md under out/
    extra: dict = field(default_factory=dict)

    def public(self) -> dict:
        d = asdict(self)
        d.pop("extra")
        return d


_COLS = [f for f in Reading.__dataclass_fields__ if f != "extra"]


class Service:
    def __init__(self, data_dir: str | Path, *, model_url: Optional[str] = None,
                 reader_factory: Callable[[str], object] = HttpOcr, start: bool = True):
        self.root = Path(data_dir)
        (self.root / "docs").mkdir(parents=True, exist_ok=True)
        self.model_url = model_url
        self._factory = reader_factory
        self._reader = None
        self._model_seen: tuple[float, dict] = (0.0, {})
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.root / "parser.db", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute(f"""CREATE TABLE IF NOT EXISTS readings (
            id TEXT PRIMARY KEY, name TEXT, kind TEXT, size INTEGER, sha256 TEXT, layout INTEGER, ocr INTEGER,
            source TEXT, state TEXT, created REAL, context_id TEXT, started REAL, finished REAL, error TEXT,
            title TEXT, pages INTEGER, blocks INTEGER, tables INTEGER, figures INTEGER, warnings INTEGER,
            markdown TEXT, cancel INTEGER DEFAULT 0)""")
        self._db.commit()
        self._queue: queue.Queue[str] = queue.Queue()
        self._done = threading.Condition(self._lock)
        for r in self._rows("SELECT * FROM readings WHERE state IN ('queued','reading') ORDER BY created"):
            self._set(r.id, state="queued", started=None)        # interrupted by a restart: read again
            self._queue.put(r.id)
        if start:
            threading.Thread(target=self._work, name="doc-parser-reader", daemon=True).start()

    # ---- store -------------------------------------------------------------------------------
    def _rows(self, sql: str, args: tuple = ()) -> list[Reading]:
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [Reading(**{k: (bool(r[k]) if k in ("layout", "ocr") else r[k]) for k in _COLS}) for r in rows]

    def _set(self, rid: str, **kw) -> None:
        with self._lock:
            self._db.execute(f"UPDATE readings SET {', '.join(k + '=?' for k in kw)} WHERE id=?", (*kw.values(), rid))
            self._db.commit()
            self._done.notify_all()

    def _dir(self, rid: str) -> Path:
        return self.root / "docs" / rid

    def get(self, rid: str) -> Reading:
        rows = self._rows("SELECT * FROM readings WHERE id=?", (rid,))
        if not rows:
            raise NotFound(rid)
        return rows[0]

    def list(self, limit: int = 200, context_id: Optional[str] = None) -> list[Reading]:
        if context_id:
            return self._rows("SELECT * FROM readings WHERE context_id=? ORDER BY created DESC LIMIT ?", (context_id, limit))
        return self._rows("SELECT * FROM readings ORDER BY created DESC LIMIT ?", (limit,))

    # ---- model server ------------------------------------------------------------------------
    def model_status(self) -> dict:
        """Layout/OCR server state for the screen. Checked at most every 5 s (a quick /health)."""
        if not self.model_url:
            return {"configured": False, "ok": False, "url": None, "name": None}
        at, last = self._model_seen
        if time.time() - at < 5 and last:
            return last
        reader = self._factory(self.model_url)
        try:
            reader.check()
            st = {"configured": True, "ok": True, "url": self.model_url, "name": getattr(reader, "name", None)}
        except OcrUnavailable:
            st = {"configured": True, "ok": False, "url": self.model_url, "name": None}
        self._model_seen = (time.time(), st)
        return st

    # ---- actions -----------------------------------------------------------------------------
    def submit(self, name: str, data: bytes, *, layout: bool = False, ocr: bool = False, source: str = "screen",
               context_id: str = "", reuse: bool = True) -> tuple[Reading, bool]:
        """Queue a reading. With ``reuse`` the same bytes with the same options return the existing
        reading (done or still running) instead of reading again. Returns (reading, reused)."""
        name = Path(name or "").name
        suffix = Path(name).suffix.lower()
        if suffix not in PARSERS:
            known = " · ".join(sorted({s.lstrip(".") for s in PARSERS}))
            raise Rejected(f"읽을 수 없는 형식입니다 ({suffix or '확장자 없음'}). 지원: {known}")
        if not data:
            raise Rejected("빈 파일입니다.")
        if len(data) > MAX_BYTES:
            raise Rejected(f"파일이 너무 큽니다 ({len(data) // 1024 // 1024}MB). {MAX_BYTES // 1024 // 1024}MB까지 읽습니다.")
        if suffix != ".pdf":
            layout = ocr = False            # the models only apply to PDF pages
        sha = hashlib.sha256(data).hexdigest()
        if reuse:
            same = self._rows("SELECT * FROM readings WHERE sha256=? AND layout=? AND ocr=? AND state IN "
                              "('queued','reading','done') ORDER BY created DESC LIMIT 1", (sha, int(layout), int(ocr)))
            if same:
                return same[0], True
        r = Reading(id=uuid.uuid4().hex[:12], name=name, kind=suffix.lstrip("."), size=len(data), sha256=sha,
                    layout=layout, ocr=ocr, source=source, state="queued", created=time.time(),
                    context_id=context_id or uuid.uuid4().hex[:12])
        d = self._dir(r.id)
        d.mkdir(parents=True)
        (d / f"source{suffix}").write_bytes(data)
        row = r.public()
        with self._lock:
            self._db.execute(f"INSERT INTO readings ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                             tuple(int(v) if isinstance(v, bool) else v for v in row.values()))
            self._db.commit()
        self._queue.put(r.id)
        return r, False

    def reread(self, rid: str, *, layout: bool, ocr: bool) -> Reading:
        r = self.get(rid)
        data = self._source(rid).read_bytes()
        return self.submit(r.name, data, layout=layout, ocr=ocr, source="screen", context_id=r.context_id,
                           reuse=False)[0]

    def cancel(self, rid: str) -> Reading:
        r = self.get(rid)
        if r.state == "queued":
            self._set(rid, state="canceled", finished=time.time())
        elif r.state == "reading":
            self._set(rid, cancel=1)            # the worker drops the result when it finishes this file
        else:
            raise Conflict(f"이미 끝난 읽기입니다 ({r.state}).")
        return self.get(rid)

    def delete(self, rid: str) -> None:
        r = self.get(rid)
        if r.state == "reading":
            raise Conflict("읽는 중에는 지울 수 없습니다. 먼저 취소하세요.")
        with self._lock:
            self._db.execute("DELETE FROM readings WHERE id=?", (rid,))
            self._db.commit()
        shutil.rmtree(self._dir(rid), ignore_errors=True)

    def wait(self, rid: str, timeout: float) -> Reading:
        """Block until the reading leaves queued/reading (or timeout)."""
        end = time.time() + timeout
        with self._lock:
            while True:
                r = self.get(rid)
                left = end - time.time()
                if r.state not in ACTIVE or left <= 0:
                    return r
                self._done.wait(min(left, 1.0))

    # ---- results -----------------------------------------------------------------------------
    def _source(self, rid: str) -> Path:
        return next(self._dir(rid).glob("source.*"))

    def _done_dir(self, rid: str) -> Path:
        r = self.get(rid)
        if r.state != "done":
            raise Conflict("아직 결과가 없습니다.")
        return self._dir(rid) / "out"

    def result(self, rid: str) -> dict:
        out = self._done_dir(rid)
        blocks = [json.loads(l) for l in (out / "blocks.jsonl").read_text(encoding="utf-8").splitlines() if l]
        meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
        pages_file = self._dir(rid) / "pages.json"
        pages = json.loads(pages_file.read_text()) if pages_file.exists() else []
        return {"blocks": blocks, "warnings": meta["warnings"], "pages": pages}

    def markdown(self, rid: str) -> tuple[str, str]:
        r = self.get(rid)
        return r.markdown, (self._done_dir(rid) / r.markdown).read_text(encoding="utf-8")

    def image(self, rid: str, name: str) -> Path:
        p = self._done_dir(rid) / IMAGES / Path(name).name
        if not p.is_file():
            raise NotFound(name)
        return p

    def page_png(self, rid: str, page: int, width: int) -> Path:
        import pymupdf

        r = self.get(rid)
        if r.kind != "pdf":
            raise NotFound("page")
        width = min(PAGE_WIDTHS, key=lambda w: abs(w - width))
        cache = self._dir(rid) / "pages" / f"p{page:03d}_{width}.png"
        if not cache.exists():
            with pymupdf.open(str(self._source(rid))) as doc:
                if not 1 <= page <= doc.page_count:
                    raise NotFound("page")
                pg = doc[page - 1]
                zoom = width / pg.rect.width
                cache.parent.mkdir(exist_ok=True)
                pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).save(str(cache))
        return cache

    def export_zip(self, rid: str) -> tuple[str, bytes]:
        out = self._done_dir(rid)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(out.rglob("*")):
                if f.is_file():
                    z.write(f, f.relative_to(out).as_posix())
        return Path(self.get(rid).markdown).stem + ".zip", buf.getvalue()

    # ---- worker ------------------------------------------------------------------------------
    def _reader_for(self) -> object:
        if self._reader is None:
            self._reader = self._factory(self.model_url)
        return self._reader

    def _work(self) -> None:
        while True:
            self.run_one(self._queue.get())

    def run_one(self, rid: str) -> None:
        try:
            r = self.get(rid)
        except NotFound:
            return                              # deleted while queued
        if r.state != "queued":
            return                              # canceled while queued
        self._set(rid, state="reading", started=time.time())
        d = self._dir(rid)
        out = d / "out"
        shutil.rmtree(out, ignore_errors=True)
        try:
            if (r.layout or r.ocr) and not self.model_url:
                raise OcrUnavailable("레이아웃·OCR 서버 주소가 설정되지 않았습니다 (serve --model-url).")
            reader = self._reader_for() if (r.layout or r.ocr) else None
            doc = parse_file(self._source(rid), ocr=reader if r.ocr else None,
                             layout=reader if r.layout else None, images=out / IMAGES if r.kind == "pdf" else None)
            doc.path = r.name                   # exports name the file as the user knows it
            if doc.title == self._source(rid).stem:   # parsers fall back to the file stem: ours is "source"
                doc.title = Path(r.name).stem
            meta = write_export(doc, out)
            pages = 0
            if r.kind == "pdf":
                import pymupdf

                with pymupdf.open(str(self._source(rid))) as pdf:
                    sizes = [[round(p.rect.width, 1), round(p.rect.height, 1)] for p in pdf]
                (d / "pages.json").write_text(json.dumps(sizes))
                pages = len(sizes)
            fields = dict(title=doc.title, pages=pages, blocks=len(doc.blocks),
                          tables=sum(1 for b in doc.blocks if b.type == "table"), figures=meta["figures"],
                          warnings=len(doc.warnings), markdown=meta["markdown"], error="")
        except OcrUnavailable as exc:
            fields = dict(error=f"레이아웃·OCR 서버에 연결할 수 없습니다. 서버를 켠 뒤 다시 읽으세요. ({exc})")
        except Exception as exc:  # corrupt / protected file: the reading fails, the desk keeps going
            detail = str(exc).replace(str(self._source(rid)), r.name).replace(str(d), "")
            fields = dict(error=f"파일을 읽지 못했습니다 — 손상됐거나 암호가 걸린 파일일 수 있습니다. ({type(exc).__name__}: {detail[:200]})")
        with self._lock:
            canceled = self._db.execute("SELECT cancel FROM readings WHERE id=?", (rid,)).fetchone()
        if canceled is None:
            return                              # deleted meanwhile
        state: State = "canceled" if canceled[0] else ("failed" if fields.get("error") else "done")
        self._set(rid, state=state, finished=time.time(), **fields)
