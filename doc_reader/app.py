"""One server for the reading agent: three screens under one top bar, one A2A agent.

    /library/   자료실 — register folders, search, open evidence (doc_index desk)       ← home
    /read/      읽기 검수 — a document's blocks over its original pages (doc_parser desk)
    /chunk/     청크 미리보기 — how a document is cut into search units (doc_chunker desk)
    /.well-known/agent-card.json, POST /a2a   one agent "doc_reader" with four skills

Each desk keeps its own state under data/<area>/. A search result opens in 읽기 검수 in-process
(the library asks the reading desk to read the file; no HTTP between areas).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from doc_chunker.service import Desk as ChunkDesk
from doc_index.service import Desk as IndexDesk
from doc_parser import a2a
from doc_parser.service import Service as ReadDesk
from doc_reader import chunk, library, read

AREAS = ("library", "read", "chunk")

CARD = {
    "name": "doc_reader",
    "description": "문서를 읽고(블록·쪽 위치·표·그림), 검색 단위로 자르고, 등록된 자료 폴더에서 근거를 찾아 주는 읽기 에이전트. "
                   "결과마다 사람이 확인할 화면 링크(metadata.reviewUrl)를 붙입니다. "
                   "Reads documents, cuts them into retrieval units and searches registered folders, with review links.",
    "version": "0.1.0",
    "defaultInputModes": ["application/pdf", "text/plain", "text/markdown",
                          "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                          "application/vnd.openxmlformats-officedocument.presentationml.presentation", "application/json"],
    "defaultOutputModes": ["text/markdown", "application/json", "image/png", "text/plain"],
    "capabilities": {},
    "skills": [read.SKILL, chunk.SKILL, *library.SKILLS],
}
_SKILLS = {"parse-document": "read", "chunk-document": "chunk", "search-documents": "library", "add-folder": "library"}
_FILE_KEYS = ("raw", "url")


class ReaderTasks:
    """One A2A agent over three desks. Which desk takes a message: ``data.skill`` if given, else
    a folder → add-folder, a file + ``max_tokens`` → chunk, a file → parse, only text → search."""

    def __init__(self, handlers: dict[str, object]):
        self.h = handlers

    @staticmethod
    def _base(base: str, area: str) -> str:
        return f"{base}/{area}"

    def _area(self, message: dict) -> str:
        data: dict = {}
        has_file = False
        for p in message.get("parts", []):
            if not isinstance(p, dict):
                continue
            has_file = has_file or any(k in p for k in _FILE_KEYS)
            if isinstance(p.get("data"), dict):
                data.update(p["data"])
        skill = data.get("skill")
        if skill:
            if skill not in _SKILLS:
                raise a2a.invalid_params(f"알 수 없는 skill: {skill} ({', '.join(_SKILLS)})")
            return _SKILLS[skill]
        has_file = has_file or bool(data.get("path"))
        if data.get("folder"):
            return "library"
        if has_file:
            return "chunk" if "max_tokens" in data else "read"
        return "library"

    def send(self, message: dict, configuration: dict, base: str) -> dict:
        area = self._area(message)
        return self.h[area].send(message, configuration, self._base(base, area))

    def _owner(self, task_id: str, base: str) -> tuple[str, dict]:
        for area, h in self.h.items():
            try:
                return area, h.get(task_id, self._base(base, area))
            except a2a.A2AError as exc:
                if exc.code != -32001:
                    raise
        raise a2a.task_not_found(task_id)

    def get(self, task_id: str, base: str) -> dict:
        return self._owner(task_id, base)[1]

    def list(self, params: dict, base: str) -> list[dict]:
        out = [t for area, h in self.h.items() for t in h.list(params, self._base(base, area))]
        return sorted(out, key=lambda t: t["status"].get("timestamp", ""), reverse=True)

    def cancel(self, task_id: str, base: str) -> dict:
        area, _ = self._owner(task_id, base)
        return self.h[area].cancel(task_id, self._base(base, area))


def create_app(data_dir: str | Path, *, model_url: Optional[str] = None, embed_url: Optional[str] = None,
               reader_factory=None, embedder_factory=None) -> FastAPI:
    """``reader_factory`` / ``embedder_factory``: test fakes for the layout/OCR and embedding clients."""
    root = Path(data_dir)
    rdesk = ReadDesk(root / "read", model_url=model_url, **({"reader_factory": reader_factory} if reader_factory else {}))
    cdesk = ChunkDesk(root / "chunk")
    ldesk = IndexDesk(root / "library", embed_url=embed_url,
                      **({"embedder_factory": embedder_factory} if embedder_factory else {}))

    def open_doc(path: str, loc: Optional[str]) -> str:
        """A search result → 읽기 검수 at its page (the same file with the same options is not read twice)."""
        p = Path(path)
        r, _ = rdesk.submit(p.name, p.read_bytes(), source="library")
        m = re.match(r"p\.(\d+)", loc or "")
        return f"/read/#/docs/{r.id}" + (f"?p={m.group(1)}" if m else "")

    app = FastAPI(title="doc_reader", docs_url=None, redoc_url=None)
    app.state.read, app.state.chunk, app.state.library = rdesk, cdesk, ldesk
    a2a.mount_a2a(app, CARD, ReaderTasks({"read": read.ParserTasks(rdesk), "chunk": chunk.ChunkTasks(cdesk),
                                          "library": library.IndexTasks(ldesk)}))

    @app.get("/", include_in_schema=False)
    def home() -> RedirectResponse:
        return RedirectResponse("/library/")

    for area in AREAS:                    # "/read" -> "/read/" so the screens' relative api/ paths resolve
        app.add_api_route(f"/{area}", (lambda a: lambda: RedirectResponse(f"/{a}/"))(area), include_in_schema=False)
    app.mount("/library", library.make_app(ldesk, open_doc))
    app.mount("/read", read.make_app(rdesk))
    app.mount("/chunk", chunk.make_app(cdesk))
    return app
