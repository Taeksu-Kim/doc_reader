"""읽기 검수 (/read): the review screen, its API, and the A2A task view of doc_parser's reading desk.

Mounted by doc_reader.app at /read. A document an agent sends over A2A shows up in the screen's
list, and the screen can open it to check what was read.
"""

from __future__ import annotations

import base64
import urllib.parse
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response

from doc_parser import a2a
from doc_parser.registry import PARSERS
from doc_parser.service import Conflict, NotFound, Reading, Rejected, Service

STATIC = Path(__file__).resolve().parent / "static"
MAX_WAIT = 600.0                     # A2A blocking send: wait this long for the reading at most

_STATE = {"queued": "TASK_STATE_SUBMITTED", "reading": "TASK_STATE_WORKING", "done": "TASK_STATE_COMPLETED",
          "failed": "TASK_STATE_FAILED", "canceled": "TASK_STATE_CANCELED"}


class ParserTasks:
    """A2A view of the desk: one task = one reading."""

    def __init__(self, svc: Service):
        self.svc = svc

    def _task(self, r: Reading, base: str) -> dict:
        review = f"{base}/#/docs/{r.id}"
        meta = {"reviewUrl": review, "file": r.name}
        state = _STATE[r.state]
        if r.state == "failed":
            return a2a.task(r.id, r.context_id, state, text=r.error, metadata=meta)
        if r.state == "canceled":
            return a2a.task(r.id, r.context_id, state, text="취소됨", metadata=meta)
        if r.state != "done":
            return a2a.task(r.id, r.context_id, state, metadata=meta)
        res = self.svc.result(r.id)
        md_name, md = self.svc.markdown(r.id)
        figures = [b["image"] for b in res["blocks"] if b.get("image")]
        arts = [
            {"artifactId": "markdown", "name": md_name, "parts": [{"text": md, "mediaType": "text/markdown"}]},
            {"artifactId": "blocks", "name": "blocks",
             "parts": [{"data": {"title": r.title, "kind": r.kind, "pages": res["pages"], "blocks": res["blocks"],
                                 "warnings": res["warnings"]}, "mediaType": "application/json"}]},
        ]
        if figures:
            arts.append({"artifactId": "figures", "name": "figures", "parts": [
                {"url": f"{base}/api/docs/{r.id}/images/{urllib.parse.quote(f)}", "filename": f,
                 "mediaType": "image/png"} for f in figures]})
        summary = (f"{r.name}: 블록 {r.blocks}개 (표 {r.tables} · 그림 {r.figures}), 경고 {r.warnings}개. "
                   f"검수 화면: {review}")
        return a2a.task(r.id, r.context_id, state, text=summary, artifacts=arts, metadata=meta)

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
                name = p.get("filename") or _name_for(p.get("mediaType"))
            elif "url" in p:
                u = urllib.parse.urlparse(p["url"])
                if u.scheme != "file":
                    raise a2a.content_type_not_supported("url parts: only file:// on this machine")
                path = Path(urllib.parse.unquote(u.path))
                data, name = _read_local(path), p.get("filename") or path.name
            elif isinstance(p.get("data"), dict):
                d = p["data"]
                opts.update({k: bool(d[k]) for k in ("layout", "ocr") if k in d})
                if d.get("path"):
                    path = Path(str(d["path"]))
                    data, name = _read_local(path), path.name
        if data is None:
            raise a2a.invalid_params("문서가 없습니다: file part {raw, filename} 또는 data part {path} 가 필요합니다")
        if (opts.get("layout") or opts.get("ocr")) and not self.svc.model_status()["ok"]:
            raise a2a.A2AError(-32603, "레이아웃·OCR 서버가 꺼져 있습니다 (doc_parser/tools/ocr.sh start). "
                                       "옵션 없이 보내면 글자층만으로 읽습니다.")
        try:
            r, _ = self.svc.submit(name, data, layout=opts.get("layout", False), ocr=opts.get("ocr", False),
                                   source="a2a", context_id=str(message.get("contextId") or ""))
        except Rejected as exc:
            raise a2a.content_type_not_supported(str(exc)) from exc
        if not configuration.get("returnImmediately"):
            r = self.svc.wait(r.id, MAX_WAIT)
        return self._task(r, base)

    def get(self, task_id: str, base: str) -> dict:
        return self._task(self._reading(task_id), base)

    def list(self, params: dict, base: str) -> list[dict]:
        rows = self.svc.list(context_id=params.get("contextId"))
        want = params.get("status")
        return [self._task(r, base) for r in rows if not want or _STATE[r.state] == want]

    def cancel(self, task_id: str, base: str) -> dict:
        r = self._reading(task_id)
        try:
            return self._task(self.svc.cancel(r.id), base)
        except Conflict as exc:
            raise a2a.not_cancelable(task_id, _STATE[r.state]) from exc

    def _reading(self, task_id: str) -> Reading:
        try:
            return self.svc.get(task_id)
        except NotFound as exc:
            raise a2a.task_not_found(task_id) from exc


def _name_for(media_type: Optional[str]) -> str:
    ext = {"application/pdf": ".pdf", "text/plain": ".txt", "text/markdown": ".md"}.get(media_type or "", "")
    return "document" + ext


def _read_local(path: Path) -> bytes:
    if not path.is_absolute() or not path.is_file():
        raise a2a.invalid_params(f"파일이 없습니다: {path} (이 PC의 절대 경로)")
    return path.read_bytes()


SKILL = {
    "id": "parse-document",
    "name": "문서 읽기 (Parse document)",
    "description": "파일 하나를 제목·본문·표·그림 블록(쪽·위치 포함)과 마크다운으로 읽습니다. 파일은 file part {raw, filename} · "
                   "{url: file:///절대경로} · data part {\"path\": \"/절대/경로.pdf\"}, 옵션 data {\"layout\": true, \"ocr\": true}. "
                   "결과: 마크다운(text), 블록·경고(data), 그림 PNG(url), 검수 화면 링크(metadata.reviewUrl).",
    "tags": ["document", "pdf", "parsing", "ocr", "korean"],
    "examples": ["이 PDF를 읽어서 표와 본문을 블록으로 줘", "Parse this report and return Markdown"],
    "inputModes": ["application/pdf", "application/json"],
    "outputModes": ["text/markdown", "application/json", "image/png"],
}


def make_app(svc: Service) -> FastAPI:
    """The /read sub-app over one reading desk (A2A is served once, at the root, by doc_reader.app)."""
    app = FastAPI(title="doc_reader · read", docs_url=None, redoc_url=None)
    app.state.service = svc

    def found(fn, *args):
        try:
            return fn(*args)
        except NotFound as exc:
            raise HTTPException(404, "없는 문서입니다.") from exc
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "read.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/status")
    def status() -> dict:
        return {"models": svc.model_status(), "formats": sorted(s.lstrip(".") for s in PARSERS)}

    @app.get("/api/docs")
    def docs(limit: int = Query(200, ge=1, le=1000)) -> list[dict]:
        return [r.public() for r in svc.list(limit)]

    @app.post("/api/docs")
    async def upload(file: UploadFile = File(...), layout: bool = Form(False), ocr: bool = Form(False)) -> dict:
        data = await file.read()
        try:
            r, reused = svc.submit(file.filename or "", data, layout=layout, ocr=ocr, source="screen")
        except Rejected as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"doc": r.public(), "reused": reused}

    @app.get("/api/docs/{rid}")
    def doc(rid: str) -> dict:
        return found(svc.get, rid).public()

    @app.get("/api/docs/{rid}/result")
    def result(rid: str) -> dict:
        return found(svc.result, rid)

    @app.post("/api/docs/{rid}/reread")
    def reread(rid: str, layout: bool = Form(False), ocr: bool = Form(False)) -> dict:
        return found(lambda: svc.reread(rid, layout=layout, ocr=ocr)).public()

    @app.post("/api/docs/{rid}/cancel")
    def cancel(rid: str) -> dict:
        return found(svc.cancel, rid).public()

    @app.delete("/api/docs/{rid}")
    def delete(rid: str) -> dict:
        found(svc.delete, rid)
        return {"deleted": rid}

    @app.get("/api/docs/{rid}/pages/{page}.png")
    def page_png(rid: str, page: int, w: int = Query(960, ge=100, le=3000)) -> FileResponse:
        return FileResponse(found(svc.page_png, rid, page, w), headers={"Cache-Control": "max-age=86400"})

    @app.get("/api/docs/{rid}/images/{name}")
    def image(rid: str, name: str) -> FileResponse:
        return FileResponse(found(svc.image, rid, name))

    @app.get("/api/docs/{rid}/export.md")
    def export_md(rid: str) -> PlainTextResponse:
        name, md = found(svc.markdown, rid)
        return PlainTextResponse(md, media_type="text/markdown; charset=utf-8", headers=_attach(name))

    @app.get("/api/docs/{rid}/export.zip")
    def export_zip(rid: str) -> Response:
        name, data = found(svc.export_zip, rid)
        return Response(data, media_type="application/zip", headers=_attach(name))

    return app


def _attach(name: str) -> dict:
    return {"Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(name)}"}
