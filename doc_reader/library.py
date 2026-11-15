"""자료실 (/library): the search screen, its API, and the A2A task view of doc_index's desk.

Mounted by doc_reader.app at /library. Folders an agent adds over A2A appear on the screen, and a
search result opens at its page in the 읽기 검수 screen (``open_doc``, given by doc_reader.app).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from doc_index.service import Conflict, Desk, NotFound, Rejected, Task
from doc_parser import a2a

STATIC = Path(__file__).resolve().parent / "static"
MAX_WAIT = 3600.0

SKILLS = [
    {"id": "search-documents", "name": "자료 검색 (Search documents)",
     "description": "등록된 자료 폴더에서 근거 조각을 찾습니다. text part = 찾을 말, 옵션 data {\"mode\": \"hybrid|keyword|vector\", \"k\": 20}. "
                    "결과: data {hits: [{name, path, loc_start, loc_end, heading_path, text, ranks, family}]}, metadata.reviewUrl = 같은 검색을 연 화면.",
     "tags": ["search", "retrieval", "korean", "rag"], "examples": ["출장 일비 얼마야", "NS-500 하우징 금형 비용"],
     "inputModes": ["text/plain", "application/json"], "outputModes": ["application/json"]},
    {"id": "add-folder", "name": "자료 폴더 추가 (Add folder)",
     "description": "data part {\"folder\": \"/절대/경로\"} — 등록하고 색인. 기본은 색인이 끝날 때까지 기다림.",
     "tags": ["index"], "examples": ["{\"folder\": \"/mnt/c/Users/me/자료\"}"],
     "inputModes": ["application/json"], "outputModes": ["application/json"]},
]

_STATE = {"queued": "TASK_STATE_SUBMITTED", "running": "TASK_STATE_WORKING", "done": "TASK_STATE_COMPLETED",
          "failed": "TASK_STATE_FAILED", "canceled": "TASK_STATE_CANCELED"}


def _search_url(base: str, query: str, mode: str) -> str:
    from urllib.parse import quote

    return f"{base}/#/search?q={quote(query)}&mode={mode}"


class IndexTasks:
    """A2A view of the desk: a search or a folder build = one task."""

    def __init__(self, desk: Desk):
        self.desk = desk

    def _task(self, t: Task, base: str) -> dict:
        state = _STATE[t.state]
        if t.kind == "search":
            q = (t.request or {}).get("query", "")
            meta = {"reviewUrl": _search_url(base, q, (t.request or {}).get("mode", "hybrid"))}
            if t.state != "done":
                return a2a.task(t.id, t.context_id, state, text=t.error or None, metadata=meta)
            r = t.result
            lines = [f"{n}. {h['name']} {h['loc_start'] or ''} — {' › '.join(h['heading_path'])}".rstrip(" —")
                     for n, h in enumerate(r["hits"][:10], 1)]
            text = f"'{q}' 결과 {len(r['hits'])}개 ({r['mode']}):\n" + "\n".join(lines)
            return a2a.task(t.id, t.context_id, state, text=text, metadata=meta, artifacts=[
                {"artifactId": "hits", "name": "hits", "parts": [{"data": r, "mediaType": "application/json"}]}])
        meta = {"reviewUrl": f"{base}/#/library"}
        if t.state == "failed":
            return a2a.task(t.id, t.context_id, state, text=t.error, metadata=meta)
        if t.state != "done":
            return a2a.task(t.id, t.context_id, state, metadata=meta)
        r = t.result
        s = r["stats"]
        text = (f"색인 완료: 파일 {s['files_ok']}개 · 단위 {s['units']}개 · 벡터 {s['vectors']}개"
                f"{'' if r['vectors'] else ' (임베딩 서버 꺼짐 — 키워드만)'} · 건너뜀 {s['files_skipped']}")
        return a2a.task(t.id, t.context_id, state, text=text, metadata=meta, artifacts=[
            {"artifactId": "build", "name": "build", "parts": [{"data": r, "mediaType": "application/json"}]}])

    def send(self, message: dict, configuration: dict, base: str) -> dict:
        if message.get("taskId"):
            raise a2a.unsupported("follow-up messages to an existing task (send a new message instead)")
        text = " ".join(p["text"] for p in message["parts"] if isinstance(p, dict) and isinstance(p.get("text"), str)).strip()
        data: dict = {}
        for p in message["parts"]:
            if isinstance(p, dict) and isinstance(p.get("data"), dict):
                data.update(p["data"])
        ctx = str(message.get("contextId") or "")
        if data.get("folder"):
            try:
                t = self.desk.add_source(str(data["folder"]), source="a2a", context_id=ctx)
            except Rejected as exc:
                raise a2a.invalid_params(str(exc)) from exc
            if not configuration.get("returnImmediately"):
                t = self.desk.wait(t.id, MAX_WAIT)
            return self._task(t, base)
        query = text or str(data.get("query") or "")
        if not query:
            raise a2a.invalid_params("찾을 말(text part) 또는 data {\"folder\": 경로} 가 필요합니다")
        mode = str(data.get("mode") or "hybrid")
        if mode not in ("hybrid", "keyword", "vector"):
            raise a2a.invalid_params(f"mode: hybrid | keyword | vector ({mode})")
        k = max(1, min(100, int(data.get("k") or 20)))
        return self._task(self.desk.record_search(query, mode, k, ctx), base)

    def get(self, task_id: str, base: str) -> dict:
        return self._task(self._find(task_id), base)

    def list(self, params: dict, base: str) -> list[dict]:
        want = params.get("status")
        return [self._task(t, base) for t in self.desk.tasks(context_id=params.get("contextId"))
                if not want or _STATE[t.state] == want]

    def cancel(self, task_id: str, base: str) -> dict:
        t = self._find(task_id)
        try:
            return self._task(self.desk.cancel(t.id), base)
        except Conflict as exc:
            raise a2a.not_cancelable(task_id, _STATE[t.state]) from exc

    def _find(self, task_id: str) -> Task:
        try:
            return self.desk.task(task_id)
        except NotFound as exc:
            raise a2a.task_not_found(task_id) from exc


class FolderBody(BaseModel):
    path: str


class OpenBody(BaseModel):
    path: str
    loc: Optional[str] = None


def make_app(desk: Desk, open_doc: Optional[Callable[[str, Optional[str]], str]] = None) -> FastAPI:
    """The /library sub-app over one index desk. ``open_doc(path, loc) -> url`` opens a file in the
    읽기 검수 screen (None turns "원본에서 보기" off). A2A is served once, at the root."""
    app = FastAPI(title="doc_reader · library", docs_url=None, redoc_url=None)
    app.state.desk = desk

    def call(fn, *args, **kw):
        try:
            return fn(*args, **kw)
        except Rejected as exc:
            raise HTTPException(400, str(exc)) from exc
        except NotFound as exc:
            raise HTTPException(404, "없는 항목입니다.") from exc
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "library.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/status")
    def status() -> dict:
        builds = desk.tasks(kind="build", limit=1)
        return {"embed": desk.embed_status(), "stats": desk.stats(), "parser": open_doc is not None,
                "build": builds[0].public() if builds else None}

    @app.get("/api/sources")
    def sources() -> list[dict]:
        return desk.sources()

    @app.post("/api/sources")
    def add_source(body: FolderBody) -> dict:
        return call(desk.add_source, body.path).public()

    @app.delete("/api/sources")
    def remove_source(path: str = Query(...)) -> dict:
        return call(desk.remove_source, path).public()

    @app.post("/api/build")
    def build() -> dict:
        return desk.build().public()

    @app.get("/api/tasks")
    def tasks(kind: Optional[str] = None, limit: int = Query(30, ge=1, le=500)) -> list[dict]:
        return [t.public() for t in desk.tasks(kind=kind, limit=limit)]

    @app.post("/api/tasks/{tid}/cancel")
    def cancel(tid: str) -> dict:
        return call(desk.cancel, tid).public()

    @app.get("/api/search")
    def search(q: str = "", mode: str = "hybrid", k: int = Query(20, ge=1, le=100)) -> dict:
        return call(desk.search, q, mode, k)

    @app.get("/api/files")
    def files() -> list[dict]:
        return desk.files()

    @app.get("/api/units")
    def units(path: str = Query(...)) -> list[dict]:
        return call(desk.units, path)

    @app.post("/api/open")
    def open_in_reader(body: OpenBody) -> dict:
        """Read the file on the 읽기 검수 desk (without waiting) and return its screen link at the page."""
        if open_doc is None:
            raise HTTPException(503, "원본 보기가 꺼져 있습니다.")
        if not Path(body.path).is_file():
            raise HTTPException(404, "파일이 더 이상 그 자리에 없습니다. 다시 색인하세요.")
        try:
            return {"url": open_doc(body.path, body.loc)}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    return app
