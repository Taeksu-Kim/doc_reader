"""Chunk preview desk: the one owner of state behind the preview screen (app.py) and the A2A endpoint.

    data/chunker.db                 one row per preview (file + token limit)
    data/previews/<id>/source.<ext> the file as received
    data/previews/<id>/units.jsonl  the units (doc_chunker.Unit.to_dict per line)

Chunking runs in the request (seconds even for long files: a 215-page manual reads + chunks in
~38 s as PDF, 0.9 s as docx — README). The same bytes with the same limit return the earlier preview.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from doc_chunker.chunker import DEFAULT_MAX_TOKENS, chunk
from doc_parser import parse_file
from doc_parser.registry import PARSERS

MAX_BYTES = 200 * 1024 * 1024
MIN_TOKENS, MAX_TOKENS = 200, 32000


class Rejected(ValueError):
    """The input cannot be chunked (type, size, limit). Message is for people."""


class NotFound(KeyError):
    pass


@dataclass
class Preview:
    id: str
    name: str
    kind: str
    size: int
    sha256: str
    max_tokens: int
    source: str                 # "screen" | "a2a"
    created: float
    state: str                  # "done" | "failed"
    context_id: str = ""
    title: str = ""
    units: int = 0
    tokens: int = 0             # sum over units
    largest: int = 0
    blocks: int = 0
    warnings: str = "[]"        # json list (parser warnings)
    error: str = ""
    ms: int = 0

    def public(self) -> dict:
        d = asdict(self)
        d["warnings"] = json.loads(self.warnings or "[]")
        return d


_COLS = list(Preview.__dataclass_fields__)


class Desk:
    def __init__(self, data_dir: str | Path):
        self.root = Path(data_dir)
        (self.root / "previews").mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.root / "chunker.db", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute(f"CREATE TABLE IF NOT EXISTS previews ({', '.join(c + (' TEXT PRIMARY KEY' if c == 'id' else '') for c in _COLS)})")
        self._db.commit()

    def _rows(self, sql: str, args: tuple = ()) -> list[Preview]:
        with self._lock:
            return [Preview(**dict(r)) for r in self._db.execute(sql, args).fetchall()]

    def get(self, pid: str) -> Preview:
        rows = self._rows("SELECT * FROM previews WHERE id=?", (pid,))
        if not rows:
            raise NotFound(pid)
        return rows[0]

    def list(self, limit: int = 200, context_id: Optional[str] = None) -> list[Preview]:
        if context_id:
            return self._rows("SELECT * FROM previews WHERE context_id=? ORDER BY created DESC LIMIT ?", (context_id, limit))
        return self._rows("SELECT * FROM previews ORDER BY created DESC LIMIT ?", (limit,))

    def _dir(self, pid: str) -> Path:
        return self.root / "previews" / pid

    def submit(self, name: str, data: bytes, *, max_tokens: int = DEFAULT_MAX_TOKENS, source: str = "screen",
               context_id: str = "", reuse: bool = True) -> tuple[Preview, bool]:
        name = Path(name or "").name
        suffix = Path(name).suffix.lower()
        if suffix not in PARSERS:
            known = " · ".join(sorted({s.lstrip(".") for s in PARSERS}))
            raise Rejected(f"자를 수 없는 형식입니다 ({suffix or '확장자 없음'}). 지원: {known}")
        if not data:
            raise Rejected("빈 파일입니다.")
        if len(data) > MAX_BYTES:
            raise Rejected(f"파일이 너무 큽니다. {MAX_BYTES // 1024 // 1024}MB까지 받습니다.")
        max_tokens = self._limit(max_tokens)
        sha = hashlib.sha256(data).hexdigest()
        if reuse:
            same = self._rows("SELECT * FROM previews WHERE sha256=? AND max_tokens=? AND state='done' "
                              "ORDER BY created DESC LIMIT 1", (sha, max_tokens))
            if same:
                return same[0], True
        p = Preview(id=uuid.uuid4().hex[:12], name=name, kind=suffix.lstrip("."), size=len(data), sha256=sha,
                    max_tokens=max_tokens, source=source, created=time.time(), state="done",
                    context_id=context_id or uuid.uuid4().hex[:12])
        d = self._dir(p.id)
        d.mkdir(parents=True)
        (d / f"source{suffix}").write_bytes(data)
        self._run(p)
        row = asdict(p)
        with self._lock:
            self._db.execute(f"INSERT INTO previews ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))
            self._db.commit()
        return p, False

    def rechunk(self, pid: str, max_tokens: int) -> Preview:
        """Cut the same file again with another limit (a new preview; the old one stays to compare)."""
        p = self.get(pid)
        return self.submit(p.name, self._source(pid).read_bytes(), max_tokens=max_tokens, source="screen",
                           context_id=p.context_id)[0]

    def delete(self, pid: str) -> None:
        self.get(pid)
        with self._lock:
            self._db.execute("DELETE FROM previews WHERE id=?", (pid,))
            self._db.commit()
        shutil.rmtree(self._dir(pid), ignore_errors=True)

    def units(self, pid: str) -> list[dict]:
        p = self.get(pid)
        f = self._dir(pid) / "units.jsonl"
        if p.state != "done" or not f.exists():
            return []
        return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l]

    def jsonl(self, pid: str) -> tuple[str, bytes]:
        p = self.get(pid)
        return f"{Path(p.name).stem}_units_{p.max_tokens}.jsonl", (self._dir(pid) / "units.jsonl").read_bytes()

    def _source(self, pid: str) -> Path:
        return next(self._dir(pid).glob("source.*"))

    @staticmethod
    def _limit(max_tokens) -> int:
        try:
            n = int(max_tokens)
        except (TypeError, ValueError) as exc:
            raise Rejected("단위 상한은 숫자로 적어 주세요.") from exc
        if not MIN_TOKENS <= n <= MAX_TOKENS:
            raise Rejected(f"단위 상한은 {MIN_TOKENS:,}~{MAX_TOKENS:,} 토큰 사이로 정해 주세요.")
        return n

    def _run(self, p: Preview) -> None:
        t = time.time()
        src = self._source(p.id)
        try:
            doc = parse_file(src)
            doc.path = p.name                       # unit ids and headers name the file as the user knows it
            if doc.title == src.stem:
                doc.title = Path(p.name).stem
            units = chunk(doc, p.max_tokens) if doc.blocks else []
            (self._dir(p.id) / "units.jsonl").write_text(
                "".join(json.dumps(u.to_dict(), ensure_ascii=False) + "\n" for u in units), encoding="utf-8")
            p.title, p.units, p.blocks = doc.title, len(units), len(doc.blocks)
            p.tokens, p.largest = sum(u.tokens for u in units), max((u.tokens for u in units), default=0)
            p.warnings = json.dumps(doc.warnings, ensure_ascii=False)
            if not units:
                p.state, p.error = "failed", "읽은 내용이 없습니다" + (f" — {doc.warnings[0]}" if doc.warnings else ".")
        except Exception as exc:  # corrupt / protected file: this preview fails, the desk keeps going
            detail = str(exc).replace(str(src), p.name)
            p.state, p.error = "failed", f"파일을 읽지 못했습니다 — 손상됐거나 암호가 걸린 파일일 수 있습니다. ({type(exc).__name__}: {detail[:200]})"
        p.ms = round((time.time() - t) * 1000)
