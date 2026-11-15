"""청크 미리보기 (/chunk): the preview screen, its API, and the A2A task view of doc_chunker's desk.

Mounted by doc_reader.app at /chunk. A file an agent sends over A2A shows up in the screen's list,
so a person can see how it was cut.
"""

from __future__ import annotations

import base64
import urllib.parse
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response

from doc_chunker.chunker import DEFAULT_MAX_TOKENS
from doc_chunker.service import Desk, NotFound, Preview, Rejected
from doc_parser import a2a

STATIC = Path(__file__).resolve().parent / "static"

SKILL = {
    "id": "chunk-document", "name": "문서 자르기 (Chunk document)",
    "description": "문서를 장·절·표 구조를 따라 검색 단위로 자릅니다(단위마다 '파일 · 문서 › 장 › 절 · 쪽' 머리, 큰 표는 행 단위 + 머리 행 반복). "
                   "파일은 file part {raw, filename} · {url: file:///…} · data {path}, data {\"skill\": \"chunk-document\", \"max_tokens\": 3000}. "
                   "결과: data {units: [{id, heading_path, loc_start, loc_end, part, tokens, text}]}, metadata.reviewUrl = 미리보기 화면.",
    "tags": ["chunking", "rag", "document"], "examples": ["이 매뉴얼을 2000토큰 단위로 잘라줘"],
    "inputModes": ["application/pdf", "application/json"], "outputModes": ["application/json"]}


class ChunkTasks:
    """A2A view of the desk: one task = one preview (chunking finishes within the request)."""

    def __init__(self, desk: Desk):
        self.desk = desk

    def _task(self, p: Preview, base: str) -> dict:
        meta = {"reviewUrl": f"{base}/#/p/{p.id}", "file": p.name}
        if p.state == "failed":
            return a2a.task(p.id, p.context_id, "TASK_STATE_FAILED", text=p.error, metadata=meta)
        units = self.desk.units(p.id)
        text = (f"{p.name}: 단위 {p.units}개 (상한 {p.max_tokens:,}, 최대 {p.largest:,} · 합계 {p.tokens:,} 토큰). "
                f"미리보기: {meta['reviewUrl']}")
        return a2a.task(p.id, p.context_id, "TASK_STATE_COMPLETED", text=text, metadata=meta, artifacts=[
            {"artifactId": "units", "name": "units", "parts": [{"data": {"title": p.title, "max_tokens": p.max_tokens,
                                                                         "units": units}, "mediaType": "application/json"}]}])

    def send(self, message: dict, configuration: dict, base: str) -> dict:
        if message.get("taskId"):
            raise a2a.unsupported("follow-up messages to an existing task (send a new message instead)")
        name, data, opts = None, None, {}
        for p in message["parts"]:
            if not isinstance(p, dict):
                continue
            if "raw" in p:
                try:
                    data = base64.b64decode(p["raw"], validate=True)
                except ValueError as exc:
                    raise a2a.invalid_params("raw is not valid base64") from exc
                name = p.get("filename") or "document"
            elif "url" in p:
                u = urllib.parse.urlparse(p["url"])
                if u.scheme != "file":
                    raise a2a.content_type_not_supported("url parts: only file:// on this machine")
                path = Path(urllib.parse.unquote(u.path))
                data, name = _read_local(path), p.get("filename") or path.name
            elif isinstance(p.get("data"), dict):
                opts.update(p["data"])
                if opts.get("path"):
                    path = Path(str(opts["path"]))
                    data, name = _read_local(path), path.name
        if data is None:
            raise a2a.invalid_params("문서가 없습니다: file part {raw, filename} 또는 data part {path} 가 필요합니다")
        try:
            p, _ = self.desk.submit(name, data, max_tokens=opts.get("max_tokens", DEFAULT_MAX_TOKENS), source="a2a",
                                    context_id=str(message.get("contextId") or ""))
        except Rejected as exc:
            if "형식" in str(exc):
                raise a2a.content_type_not_supported(str(exc)) from exc
            raise a2a.invalid_params(str(exc)) from exc
        return self._task(p, base)

    def get(self, task_id: str, base: str) -> dict:
        return self._task(self._find(task_id), base)

    def list(self, params: dict, base: str) -> list[dict]:
        want = params.get("status")
        out = [self._task(p, base) for p in self.desk.list(context_id=params.get("contextId"))]
        return [t for t in out if not want or t["status"]["state"] == want]

    def cancel(self, task_id: str, base: str) -> dict:
        p = self._find(task_id)
        raise a2a.not_cancelable(task_id, "TASK_STATE_COMPLETED" if p.state == "done" else "TASK_STATE_FAILED")

    def _find(self, task_id: str) -> Preview:
        try:
            return self.desk.get(task_id)
        except NotFound as exc:
            raise a2a.task_not_found(task_id) from exc


def _read_local(path: Path) -> bytes:
    if not path.is_absolute() or not path.is_file():
        raise a2a.invalid_params(f"파일이 없습니다: {path} (이 PC의 절대 경로)")
    return path.read_bytes()


def make_app(desk: Desk) -> FastAPI:
    """The /chunk sub-app over one chunk desk (A2A is served once, at the root, by doc_reader.app)."""
    app = FastAPI(title="doc_reader · chunk", docs_url=None, redoc_url=None)
    app.state.desk = desk

    def call(fn, *args, **kw):
        try:
            return fn(*args, **kw)
        except Rejected as exc:
            raise HTTPException(400, str(exc)) from exc
        except NotFound as exc:
            raise HTTPException(404, "없는 미리보기입니다.") from exc

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "chunk.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/status")
    def status() -> dict:
        from doc_parser.registry import PARSERS

        return {"formats": sorted({s.lstrip(".") for s in PARSERS}), "default_max_tokens": DEFAULT_MAX_TOKENS}

    @app.get("/api/previews")
    def previews(limit: int = Query(200, ge=1, le=1000)) -> list[dict]:
        return [p.public() for p in desk.list(limit)]

    @app.post("/api/previews")
    def upload(file: UploadFile = File(...), max_tokens: int = Form(DEFAULT_MAX_TOKENS)) -> dict:
        data = file.file.read()
        p, reused = call(desk.submit, file.filename or "", data, max_tokens=max_tokens)
        return {"preview": p.public(), "reused": reused}

    @app.get("/api/previews/{pid}")
    def preview(pid: str) -> dict:
        return {**call(desk.get, pid).public(), "units_list": desk.units(pid)}

    @app.post("/api/previews/{pid}/rechunk")
    def rechunk(pid: str, max_tokens: int = Form(...)) -> dict:
        return call(desk.rechunk, pid, max_tokens).public()

    @app.delete("/api/previews/{pid}")
    def delete(pid: str) -> dict:
        call(desk.delete, pid)
        return {"deleted": pid}

    @app.get("/api/previews/{pid}/units.jsonl")
    def download(pid: str) -> Response:
        name, data = call(desk.jsonl, pid)
        return Response(data, media_type="application/x-ndjson",
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(name)}"})

    return app
