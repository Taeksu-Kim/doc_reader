"""Optional model help for what the text layer cannot give. Off by default: ``parse_file(path)``
never calls a model. Two opt-ins, both served by ``tools/ocr_server.py`` (its own env, so paddle
never touches the env that imports doc_parser):

    from doc_parser import parse_file, HttpOcr
    srv = HttpOcr("http://<wsl-ip>:8090")
    parse_file("scan.pdf", ocr=srv)       # read scanned pages and raster images (PaddleOCR-VL)
    parse_file("paper.pdf", layout=srv)   # layout regions: words inside figures stop being body text

Anything with the same methods works, which is how the tests use a fake server.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Literal, Protocol

ReadMode = Literal["page", "region"]   # page: whole scanned page / region: one cropped image


@dataclass
class OcrItem:
    type: Literal["heading", "text", "table"]
    bbox: tuple[float, float, float, float]       # pixels of the image that was sent
    text: str = ""
    level: int = 0
    rows: list[list[str]] = field(default_factory=list)
    looped: bool = False          # the model repeated itself and the server cut the repeats
    note: str = ""                # server's remark on this item, shown as a warning
    edge: bool = False            # the layout called it a page header/footer (dropped only if it repeats)


@dataclass
class Region:
    label: str                                    # PP-DocLayoutV3 label: image, chart, table, text, ...
    bbox: tuple[float, float, float, float]       # pixels of the image that was sent
    score: float = 0.0


class OcrUnavailable(RuntimeError):
    """The reader was asked for but cannot be reached (raised before any page is read)."""


class OcrReader(Protocol):
    name: str

    def check(self) -> None: ...                  # raise OcrUnavailable if not usable

    def read(self, png: bytes, mode: ReadMode) -> list[OcrItem]: ...


class LayoutReader(Protocol):
    name: str

    def check(self) -> None: ...

    def layout(self, png: bytes) -> list[Region]: ...


class HttpOcr:
    """Client for tools/ocr_server.py: POST /read?mode=page|region and POST /layout with PNG bytes."""

    def __init__(self, url: str, timeout: float = 300.0):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.name = "ocr"

    def check(self) -> None:
        try:
            with urllib.request.urlopen(self.url + "/health", timeout=10) as r:
                info = json.loads(r.read())
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise OcrUnavailable(f"OCR 서버에 연결 못 함: {self.url} ({exc})") from exc
        self.name = str(info.get("model") or "ocr")

    def _post(self, path: str, png: bytes) -> dict:
        req = urllib.request.Request(self.url + path, data=png, headers={"Content-Type": "image/png"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read())

    def read(self, png: bytes, mode: ReadMode) -> list[OcrItem]:
        data = self._post(f"/read?mode={mode}", png)
        return [OcrItem(type=i["type"], bbox=tuple(i["bbox"]), text=i.get("text", ""),
                        level=int(i.get("level", 0)), rows=i.get("rows") or [], looped=bool(i.get("looped")),
                        note=str(i.get("note") or ""), edge=bool(i.get("edge")))
                for i in data["items"] if i.get("type") in ("heading", "text", "table")]

    def layout(self, png: bytes) -> list[Region]:
        data = self._post("/layout", png)
        return [Region(label=str(r["label"]), bbox=tuple(r["bbox"]), score=float(r.get("score", 0)))
                for r in data["regions"]]
