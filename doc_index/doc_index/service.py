"""Library desk: the one owner of state behind the search screen (app.py) and the A2A endpoint.

    data/index.sqlite     the Index (units, keyword FTS, vectors) -- WAL, so searches run during a build
    data/desk.db          sources (registered folders) and tasks (builds, A2A searches)

Builds run one at a time in a worker thread and always cover *all* registered folders (``Index.build``
removes files that are not under the given paths). Without a reachable embedding server the build
indexes keywords only; units without a vector are embedded by the next build that has one.
"""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Literal, Optional

from doc_index.embed import OpenAICompatEmbedder
from doc_index.store import Index

State = Literal["queued", "running", "done", "failed", "canceled"]
ACTIVE = ("queued", "running")


class Rejected(ValueError):
    """Bad input (not a folder, unknown source). Message is for people."""


class NotFound(KeyError):
    pass


class Conflict(RuntimeError):
    pass


@dataclass
class Task:
    id: str
    kind: str                     # "build" | "search"
    state: State
    source: str                   # "screen" | "a2a"
    created: float
    context_id: str = ""
    started: Optional[float] = None
    finished: Optional[float] = None
    request: dict | None = None
    result: dict | None = None
    error: str = ""

    def public(self) -> dict:
        return asdict(self)


class Desk:
    def __init__(self, data_dir: str | Path, *, embed_url: Optional[str] = None,
                 embedder_factory: Callable[[str], object] = OpenAICompatEmbedder, start: bool = True):
        self.root = Path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.sqlite"
        self.embed_url = embed_url
        self._factory = embedder_factory
        self._seen: tuple[float, dict] = (0.0, {})
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._db = sqlite3.connect(self.root / "desk.db", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS sources (path TEXT PRIMARY KEY, added REAL);
            CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, kind TEXT, state TEXT, source TEXT, created REAL,
                context_id TEXT, started REAL, finished REAL, request TEXT, result TEXT, error TEXT);""")
        idx = Index(self.index_path)
        idx.db.execute("PRAGMA journal_mode=WAL")
        idx.db.close()
        self._queue: queue.Queue[str] = queue.Queue()
        for t in self.tasks(kind="build", states=ACTIVE):
            self._set(t.id, state="queued", started=None)          # interrupted by a restart: run again
            self._queue.put(t.id)
        if start:
            threading.Thread(target=self._work, name="doc-index-builder", daemon=True).start()

    # ---- store -------------------------------------------------------------------------------
    def _task(self, r: sqlite3.Row) -> Task:
        d = dict(r)
        for k in ("request", "result"):
            d[k] = json.loads(d[k]) if d[k] else None
        return Task(**d)

    def _set(self, tid: str, **kw) -> None:
        for k in ("request", "result"):
            if k in kw and kw[k] is not None:
                kw[k] = json.dumps(kw[k], ensure_ascii=False)
        with self._lock:
            self._db.execute(f"UPDATE tasks SET {', '.join(k + '=?' for k in kw)} WHERE id=?", (*kw.values(), tid))
            self._db.commit()
            self._cond.notify_all()

    def _new(self, kind: str, source: str, request: dict, context_id: str = "", state: State = "queued") -> Task:
        t = Task(id=uuid.uuid4().hex[:12], kind=kind, state=state, source=source, created=time.time(),
                 context_id=context_id or uuid.uuid4().hex[:12], request=request)
        d = t.public()
        d["request"] = json.dumps(request, ensure_ascii=False)
        with self._lock:
            self._db.execute(f"INSERT INTO tasks ({', '.join(d)}) VALUES ({', '.join('?' * len(d))})", tuple(d.values()))
            self._db.commit()
        return t

    def task(self, tid: str) -> Task:
        with self._lock:
            r = self._db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if r is None:
            raise NotFound(tid)
        return self._task(r)

    def tasks(self, kind: Optional[str] = None, states: tuple = (), context_id: Optional[str] = None,
              limit: int = 200) -> list[Task]:
        sql, args = "SELECT * FROM tasks WHERE 1=1", []
        if kind:
            sql, args = sql + " AND kind=?", args + [kind]
        if states:
            sql, args = sql + f" AND state IN ({','.join('?' * len(states))})", args + list(states)
        if context_id:
            sql, args = sql + " AND context_id=?", args + [context_id]
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY created DESC LIMIT ?", (*args, limit)).fetchall()
        return [self._task(r) for r in rows]

    # ---- embedder ----------------------------------------------------------------------------
    def embed_status(self) -> dict:
        """Is the embedding server answering? (one tiny request, at most every 10 s)"""
        if not self.embed_url:
            return {"configured": False, "ok": False, "url": None}
        at, last = self._seen
        if time.time() - at < 10 and last:
            return last
        try:
            probe = self._factory(self.embed_url)
            if hasattr(probe, "timeout"):
                probe.timeout = 3.0
            probe.embed(["ping"])
            st = {"configured": True, "ok": True, "url": self.embed_url}
        except Exception:
            st = {"configured": True, "ok": False, "url": self.embed_url}
        self._seen = (time.time(), st)
        return st

    def _index(self, for_vectors: bool) -> Index:
        emb = self._factory(self.embed_url) if for_vectors and self.embed_url and self.embed_status()["ok"] else None
        idx = Index(self.index_path, emb)
        idx.db.execute("PRAGMA busy_timeout=10000")
        return idx

    # ---- sources -----------------------------------------------------------------------------
    def sources(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT path, added FROM sources ORDER BY added").fetchall()
        idx = Index(self.index_path)
        try:
            out = []
            for r in rows:
                like = r["path"].rstrip("/") + "/%"
                c = dict(idx.db.execute("SELECT status, count(*) FROM files WHERE path LIKE ? GROUP BY status", (like,)))
                out.append({"path": r["path"], "added": r["added"], "exists": Path(r["path"]).is_dir(),
                            "ok": c.get("ok", 0), "skipped": c.get("skipped", 0), "duplicate": c.get("duplicate", 0)})
            return out
        finally:
            idx.db.close()

    def add_source(self, path: str, source: str = "screen", context_id: str = "") -> Task:
        p = Path(str(path).strip()).expanduser()
        if not p.is_absolute():
            raise Rejected("폴더는 이 PC의 절대 경로로 적어 주세요 (예: /mnt/c/Users/…/자료).")
        if not p.is_dir():
            raise Rejected(f"폴더가 없습니다: {p}")
        p = p.resolve()
        with self._lock:
            known = [r[0] for r in self._db.execute("SELECT path FROM sources")]
            for k in known:
                if str(p) == k or str(p).startswith(k.rstrip("/") + "/"):
                    raise Rejected(f"이미 등록된 폴더 안에 있습니다: {k}")
            self._db.execute("INSERT INTO sources VALUES (?, ?)", (str(p), time.time()))
            self._db.commit()
        return self.build(source=source, context_id=context_id, reason=f"폴더 추가: {p}")

    def remove_source(self, path: str) -> Task:
        with self._lock:
            n = self._db.execute("DELETE FROM sources WHERE path=?", (path,)).rowcount
            self._db.commit()
        if not n:
            raise NotFound(path)
        return self.build(reason=f"폴더 제거: {path}")

    # ---- build -------------------------------------------------------------------------------
    def build(self, source: str = "screen", context_id: str = "", reason: str = "다시 색인") -> Task:
        """Queue a build of all sources. A build already waiting covers this request too."""
        waiting = self.tasks(kind="build", states=("queued",))
        if waiting:
            return waiting[0]
        t = self._new("build", source, {"reason": reason}, context_id)
        self._queue.put(t.id)
        return t

    def cancel(self, tid: str) -> Task:
        t = self.task(tid)
        if t.state != "queued":
            raise Conflict("대기 중인 색인만 취소할 수 있습니다." if t.state == "running" else "이미 끝난 작업입니다.")
        self._set(tid, state="canceled", finished=time.time())
        return self.task(tid)

    def wait(self, tid: str, timeout: float) -> Task:
        end = time.time() + timeout
        with self._lock:
            while True:
                t = self.task(tid)
                if t.state not in ACTIVE or time.time() >= end:
                    return t
                self._cond.wait(min(1.0, end - time.time()))

    def _work(self) -> None:
        while True:
            self.run_one(self._queue.get())

    def run_one(self, tid: str) -> None:
        if self.task(tid).state != "queued":
            return
        self._set(tid, state="running", started=time.time())
        paths = [s["path"] for s in self.sources() if s["exists"]]
        missing = [s["path"] for s in self.sources() if not s["exists"]]
        try:
            idx = self._index(for_vectors=True)
            try:
                rep = idx.build(paths, embed=idx.embedder is not None) if paths else None
                if not paths:                    # no folders left: empty the index
                    for (p,) in idx.db.execute("SELECT path FROM files").fetchall():
                        idx._remove(p)
                    idx.db.commit()
                stats = idx.stats()
            finally:
                idx.db.close()
            result = {"stats": stats, "vectors": idx.embedder is not None, "missing_folders": missing}
            if rep is not None:
                result.update(added=rep.added, unchanged=rep.unchanged, removed=rep.removed, units=rep.units,
                              embedded=rep.embedded, skipped=rep.skipped[:200], duplicates=len(rep.duplicates))
            self._set(tid, state="done", finished=time.time(), result=result, error="")
        except Exception as exc:  # a broken build must not stop the desk
            self._set(tid, state="failed", finished=time.time(), error=f"{type(exc).__name__}: {str(exc)[:300]}")

    # ---- search & browse ---------------------------------------------------------------------
    def stats(self) -> dict:
        idx = Index(self.index_path)
        try:
            return idx.stats()
        finally:
            idx.db.close()

    def search(self, query: str, mode: str = "hybrid", k: int = 20) -> dict:
        query = (query or "").strip()
        if not query:
            raise Rejected("찾을 말을 입력하세요.")
        if mode not in ("hybrid", "keyword", "vector"):
            raise Rejected(f"알 수 없는 방식: {mode}")
        vec_ok = bool(self.embed_url) and self.embed_status()["ok"]
        used = mode if vec_ok or mode == "keyword" else "keyword"
        idx = self._index(for_vectors=used != "keyword")
        try:
            t = time.time()
            hits = idx.search(query, k, used)
            out = []
            for h in hits:
                row = idx.db.execute("SELECT u.heading_path, u.loc_start, u.loc_end, u.part, u.tokens, f.title, f.kind "
                                     "FROM units u JOIN files f ON f.path=u.path WHERE u.id=?", (h.unit_id,)).fetchone()
                hp, ls, le, part, tok, title, kind = row
                out.append({"unit_id": h.unit_id, "path": h.path, "name": Path(h.path).name, "title": title,
                            "kind": kind, "heading_path": json.loads(hp or "[]"), "loc_start": ls, "loc_end": le,
                            "part": part, "tokens": tok, "score": round(h.score, 5), "ranks": h.ranks, "text": h.text,
                            "family": idx.family(h.path)})
            return {"query": query, "mode": used, "asked_mode": mode, "hits": out,
                    "ms": round((time.time() - t) * 1000)}
        finally:
            idx.db.close()

    def files(self) -> list[dict]:
        idx = Index(self.index_path)
        try:
            rows = idx.db.execute("SELECT f.path, f.title, f.kind, f.status, f.note, count(u.rowid) FROM files f "
                                  "LEFT JOIN units u ON u.path=f.path GROUP BY f.path ORDER BY f.path").fetchall()
            return [{"path": p, "name": Path(p).name, "title": t, "kind": k, "status": s, "note": n, "units": c}
                    for p, t, k, s, n, c in rows]
        finally:
            idx.db.close()

    def units(self, path: str) -> list[dict]:
        idx = Index(self.index_path)
        try:
            rows = idx.db.execute("SELECT id, heading_path, loc_start, loc_end, part, tokens, text FROM units "
                                  "WHERE path=? ORDER BY rowid", (path,)).fetchall()
            if not rows:
                raise NotFound(path)
            return [{"unit_id": i, "heading_path": json.loads(h or "[]"), "loc_start": ls, "loc_end": le, "part": pt,
                     "tokens": tk, "text": tx} for i, h, ls, le, pt, tk, tx in rows]
        finally:
            idx.db.close()

    # ---- A2A searches are kept as tasks so GetTask / ListTasks can return them -----------------
    def record_search(self, query: str, mode: str, k: int, context_id: str) -> Task:
        t = self._new("search", "a2a", {"query": query, "mode": mode, "k": k}, context_id, state="running")
        try:
            self._set(t.id, state="done", finished=time.time(), result=self.search(query, mode, k))
        except Rejected as exc:
            self._set(t.id, state="failed", finished=time.time(), error=str(exc))
        return self.task(t.id)
