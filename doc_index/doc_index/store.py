"""SQLite index: units (doc_chunker output) + keyword FTS5 + embedding vectors, in one file.

Incremental: a file whose bytes did not change (sha256) is not re-parsed or re-embedded; files
that disappeared from the indexed paths are removed. Search: keyword (BM25 over Korean bigrams),
vector (cosine, brute force with numpy — fine up to ~10^5 units), hybrid (reciprocal rank fusion).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from doc_chunker import DEFAULT_MAX_TOKENS, chunk, list_files
from doc_index.embed import Embedder
from doc_index.text import tokens
from doc_parser import UnsupportedFormat, parse_file

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, title TEXT, kind TEXT,
                                  status TEXT NOT NULL, note TEXT);
CREATE TABLE IF NOT EXISTS units (rowid INTEGER PRIMARY KEY, id TEXT UNIQUE, path TEXT NOT NULL,
                                  heading_path TEXT, loc_start TEXT, loc_end TEXT, part TEXT,
                                  tokens INTEGER, text TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS units_path ON units(path);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(tok, content='', tokenize='unicode61 remove_diacritics 0');
CREATE TABLE IF NOT EXISTS vecs (rowid INTEGER PRIMARY KEY, vec BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


@dataclass
class Hit:
    unit_id: str
    path: str
    score: float
    text: str = ""
    loc: str = ""
    ranks: dict[str, int] = field(default_factory=dict)   # mode -> rank (hybrid shows both)


@dataclass
class BuildReport:
    added: int = 0
    unchanged: int = 0
    removed: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)
    duplicates: list[tuple[str, str]] = field(default_factory=list)
    units: int = 0
    embedded: int = 0


class Index:
    def __init__(self, db: str | Path, embedder: Embedder | None = None) -> None:
        self.db = sqlite3.connect(str(db))
        self.db.executescript(SCHEMA)
        self.embedder = embedder
        self._mat: np.ndarray | None = None
        self._rows: np.ndarray | None = None

    # ------------------------------------------------------------ build

    def _remove(self, path: str) -> None:
        rows = [r for (r,) in self.db.execute("SELECT rowid FROM units WHERE path=?", (path,))]
        for r in rows:
            tok = self.db.execute("SELECT text FROM units WHERE rowid=?", (r,)).fetchone()[0]
            self.db.execute("INSERT INTO fts(fts, rowid, tok) VALUES('delete', ?, ?)", (r, " ".join(tokens(tok))))
        self.db.executemany("DELETE FROM vecs WHERE rowid=?", [(r,) for r in rows])
        self.db.execute("DELETE FROM units WHERE path=?", (path,))
        self.db.execute("DELETE FROM files WHERE path=?", (path,))

    def build(self, paths: list[str | Path], max_tokens: int = DEFAULT_MAX_TOKENS, embed: bool = True) -> BuildReport:
        rep = BuildReport()
        files = list_files(paths)
        present = {str(f) for f in files}
        for (p,) in self.db.execute("SELECT path FROM files").fetchall():
            if p not in present:
                self._remove(p)
                rep.removed += 1
        known = dict(self.db.execute("SELECT path, sha256 FROM files"))
        by_sha = {s: p for p, s in self.db.execute("SELECT path, sha256 FROM files WHERE status='ok'")}
        new_rows: list[tuple[int, str]] = []
        for f in files:
            p = str(f)
            sha = hashlib.sha256(f.read_bytes()).hexdigest()
            if known.get(p) == sha:
                rep.unchanged += 1
                continue
            if p in known:
                self._remove(p)
            if sha in by_sha and by_sha[sha] != p:
                rep.duplicates.append((p, by_sha[sha]))
                self.db.execute("INSERT INTO files VALUES (?,?,?,?,?,?)", (p, sha, None, None, "duplicate", by_sha[sha]))
                continue
            try:
                doc = parse_file(f)
            except UnsupportedFormat as exc:
                rep.skipped.append((p, str(exc)))
                self.db.execute("INSERT INTO files VALUES (?,?,?,?,?,?)", (p, sha, None, None, "skipped", str(exc)))
                continue
            except Exception as exc:
                note = f"읽기 실패: {type(exc).__name__}: {str(exc)[:120]}"
                rep.skipped.append((p, note))
                self.db.execute("INSERT INTO files VALUES (?,?,?,?,?,?)", (p, sha, None, None, "skipped", note))
                continue
            units = chunk(doc, max_tokens) if doc.blocks else []
            if not units:
                rep.skipped.append((p, "내용 없음"))
                self.db.execute("INSERT INTO files VALUES (?,?,?,?,?,?)", (p, sha, doc.title, doc.kind, "skipped", "내용 없음"))
                continue
            self.db.execute("INSERT INTO files VALUES (?,?,?,?,?,?)", (p, sha, doc.title, doc.kind, "ok", None))
            by_sha[sha] = p
            for u in units:
                cur = self.db.execute(
                    "INSERT INTO units (id, path, heading_path, loc_start, loc_end, part, tokens, text) VALUES (?,?,?,?,?,?,?,?)",
                    (f"{p}#{u.id.rsplit('#', 1)[1]}", p, json.dumps(u.heading_path, ensure_ascii=False),
                     u.loc_start, u.loc_end, u.part, u.tokens, u.text))
                self.db.execute("INSERT INTO fts(rowid, tok) VALUES (?, ?)", (cur.lastrowid, " ".join(tokens(u.text))))
                new_rows.append((cur.lastrowid, u.text))
            rep.added += 1
            rep.units += len(units)
        self.db.commit()
        if embed and self.embedder is not None:
            missing = self.db.execute("SELECT u.rowid, u.text FROM units u LEFT JOIN vecs v ON u.rowid=v.rowid "
                                      "WHERE v.rowid IS NULL").fetchall()
            for k in range(0, len(missing), 64):
                part = missing[k:k + 64]
                vecs = self.embedder.embed([t for _, t in part])
                self.db.executemany("INSERT INTO vecs VALUES (?, ?)",
                                    [(r, np.asarray(v, dtype=np.float32).tobytes()) for (r, _), v in zip(part, vecs)])
                self.db.commit()
                rep.embedded += len(part)
        self._mat = None
        return rep

    # ------------------------------------------------------------ search

    def _hit(self, rowid: int, score: float) -> Hit:
        uid, path, text, loc = self.db.execute("SELECT id, path, text, loc_start FROM units WHERE rowid=?", (rowid,)).fetchone()
        return Hit(uid, path, score, text, loc)

    def keyword(self, query: str, k: int = 10) -> list[Hit]:
        toks = list(dict.fromkeys(tokens(query)))
        if not toks:
            return []
        q = " OR ".join('"' + t.replace('"', '') + '"' for t in toks)
        rows = self.db.execute("SELECT rowid, bm25(fts) FROM fts WHERE fts MATCH ? ORDER BY bm25(fts) LIMIT ?", (q, k)).fetchall()
        return [self._hit(r, -s) for r, s in rows]

    def _matrix(self) -> tuple[np.ndarray, np.ndarray]:
        if self._mat is None:
            rows = self.db.execute("SELECT rowid, vec FROM vecs").fetchall()
            self._rows = np.array([r for r, _ in rows], dtype=np.int64)
            self._mat = (np.vstack([np.frombuffer(v, dtype=np.float32) for _, v in rows]) if rows
                         else np.zeros((0, 1), dtype=np.float32))
        return self._rows, self._mat

    def vector(self, query: str, k: int = 10) -> list[Hit]:
        if self.embedder is None:
            raise RuntimeError("vector search needs an embedder")
        rows, mat = self._matrix()
        if not len(rows):
            return []
        q = np.asarray(self.embedder.embed([query], query=True)[0], dtype=np.float32)
        sims = mat @ q
        top = np.argsort(-sims)[:k]
        return [self._hit(int(rows[i]), float(sims[i])) for i in top]

    def hybrid(self, query: str, k: int = 10, pool: int = 50, rrf_k: int = 60) -> list[Hit]:
        lists = {"keyword": self.keyword(query, pool)}
        if self.embedder is not None:
            lists["vector"] = self.vector(query, pool)
        fused: dict[str, Hit] = {}
        for mode, hits in lists.items():
            for rank, h in enumerate(hits, 1):
                cur = fused.setdefault(h.unit_id, Hit(h.unit_id, h.path, 0.0, h.text, h.loc))
                cur.score += 1.0 / (rrf_k + rank)
                cur.ranks[mode] = rank
        return sorted(fused.values(), key=lambda h: -h.score)[:k]

    def search(self, query: str, k: int = 10, mode: str = "hybrid") -> list[Hit]:
        return {"keyword": self.keyword, "vector": self.vector, "hybrid": self.hybrid}[mode](query, k)

    def reindex_keywords(self) -> int:
        """Rebuild the keyword index from stored units (after a tokenizer change); vectors are kept."""
        self.db.execute("INSERT INTO fts(fts) VALUES('delete-all')")
        rows = self.db.execute("SELECT rowid, text FROM units").fetchall()
        self.db.executemany("INSERT INTO fts(rowid, tok) VALUES (?, ?)", [(r, " ".join(tokens(t))) for r, t in rows])
        self.db.commit()
        return len(rows)

    def family(self, path: str, limit: int = 3) -> list[str]:
        """Other indexed files of the same family (same name once dates, versions, quarters, status words
        and numbering are removed), newest first: 주간회의록_2026-10-20 ~ 03_주간회의록_2026-10-06,
        출장비규정_v1.5 ~ 08_출장비규정_v2.1. Lets the caller always compare a hit with its latest siblings."""
        key = family_key(Path(path).name)
        if not key:
            return []
        rows = [p for (p,) in self.db.execute("SELECT path FROM files WHERE status='ok'")]
        sib = [p for p in rows if p != path and family_key(Path(p).name) == key]
        return sorted(sib, key=lambda p: recency_key(Path(p).name), reverse=True)[:limit]

    def file_text(self, path: str) -> str:
        """All units of one indexed file, in order (each keeps its location header)."""
        rows = self.db.execute("SELECT text FROM units WHERE path=? ORDER BY rowid", (path,)).fetchall()
        return "\n\n".join(t for (t,) in rows)

    def stats(self) -> dict:
        q = lambda s: self.db.execute(s).fetchone()[0]  # noqa: E731
        return {"files_ok": q("SELECT count(*) FROM files WHERE status='ok'"),
                "files_skipped": q("SELECT count(*) FROM files WHERE status='skipped'"),
                "files_duplicate": q("SELECT count(*) FROM files WHERE status='duplicate'"),
                "units": q("SELECT count(*) FROM units"), "vectors": q("SELECT count(*) FROM vecs")}


_DATE = re.compile(r"(20\d\d)[-._]?([01]\d)[-._]?([0-3]\d)")
_QUARTER = re.compile(r"(20\d\d)\s*[_-]?\s*(?:q([1-4])|([1-4])분기)", re.I)
_VERSION = re.compile(r"v(\d+(?:\.\d+)*)", re.I)


def family_key(name: str) -> str:
    s = Path(name).stem.lower()
    s = re.sub(r"^\d+[_-]", "", s)                                   # leading numbering "03_"
    s = _DATE.sub(" ", s)
    s = _QUARTER.sub(" ", s)
    s = _VERSION.sub(" ", s)
    s = re.sub(r"(구버전|확정|잠정|초안|최종|수정본?|rev\.?\d*)", " ", s)
    s = re.sub(r"\d+", " ", s)
    return re.sub(r"[\s_\-.()]+", "", s)


def recency_key(name: str) -> tuple:
    """Sort key: date in the name, else year+quarter, else version number."""
    if m := _DATE.search(name):
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)), ())
    if m := _QUARTER.search(name):
        return (int(m.group(1)), int(m.group(2) or m.group(3)) * 3, 0, ())
    if m := _VERSION.search(name):
        return (0, 0, 0, tuple(int(x) for x in m.group(1).split(".")))
    return (0, 0, 0, ())


def files_of(hits: list[Hit]) -> list[str]:
    """Hits -> file paths in rank order (a file counts at its best unit)."""
    return list(dict.fromkeys(h.path for h in hits))
