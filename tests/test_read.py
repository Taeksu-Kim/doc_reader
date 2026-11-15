"""E2E for 읽기 검수 (/read) in the one doc_reader server: the screen's API and the A2A endpoint,
with a real worker thread and real files. Only the layout/OCR model server is faked (in-process)."""

import base64
from types import SimpleNamespace
import io
import time
import zipfile
from pathlib import Path

import docx
import pymupdf
import pytest
from fastapi.testclient import TestClient

from doc_parser.service import Service
from doc_reader.app import create_app
from tests.conftest import Area
from doc_parser.ocr import OcrItem, OcrUnavailable, Region


class FakeModels:
    """Layout finds one figure; OCR reads one label. ``up=False`` = server off."""
    name = "fake-vl"
    up = True

    def __init__(self, url):
        self.url = url

    def check(self):
        if not FakeModels.up:
            raise OcrUnavailable("down")

    def layout(self, png):
        s = 150 / 72
        return [Region("image", [100 * s, 200 * s, 400 * s, 360 * s], 0.9)]

    def read(self, png, mode):
        return [OcrItem("text", [0, 0, 10, 10], "Encoder")]


def _pdf(text_lines=("Model architecture.", "The encoder maps an input sequence."), pages=2) -> bytes:
    doc = pymupdf.open()
    for n in range(pages):
        p = doc.new_page()
        p.insert_text((72, 60), f"Section {n + 1}", fontsize=18)
        for k, t in enumerate(text_lines):
            p.insert_text((72, 100 + 16 * k), t, fontsize=11)
        for r, row in enumerate([("Metric", "Score"), ("BLEU", "28.4"), ("PPL", "4.9"), ("Steps", "100K")]):
            for c, cell in enumerate(row):
                p.insert_text((90 + 150 * c, 420 + 18 * r), cell, fontsize=10)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def _docx() -> bytes:
    d = docx.Document()
    d.add_heading("회의록", 1)
    d.add_paragraph("다음 분기 계획을 논의했다.")
    out = io.BytesIO()
    d.save(out)
    return out.getvalue()


def _xlsx() -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    wb.active.append(["항목", "금액"])
    wb.active.append(["매출", 100])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _wait(c: TestClient, rid: str, timeout=60) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        d = c.get(f"/api/docs/{rid}").json()
        if d["state"] not in ("queued", "reading"):
            return d
        time.sleep(0.1)
    raise AssertionError(f"still {d['state']}")


@pytest.fixture
def client(tmp_path):
    FakeModels.up = True
    with TestClient(create_app(tmp_path / "data", model_url="http://fake", reader_factory=FakeModels)) as c:
        yield Area(c, "/read", SimpleNamespace(service=c.app.state.read))


def _upload(c, name, data, **opts):
    return c.post("/api/docs", files={"file": (name, data)}, data={k: str(v).lower() for k, v in opts.items()})


def test_screen_flow(client, tmp_path):
    c = client
    assert "읽기 검수" in c.get("/").text
    st = c.get("/api/status").json()
    assert st["models"]["ok"] and "pdf" in st["formats"]

    pdf = _pdf()
    r = _upload(c, "보고서.pdf", pdf)
    assert r.status_code == 200 and not r.json()["reused"]
    rid = r.json()["doc"]["id"]
    d = _wait(c, rid)
    assert d["state"] == "done" and d["pages"] == 2 and d["blocks"] > 0 and d["tables"] == 2, d
    assert c.get("/api/docs").json()[0]["title"] == "Section 1"
    res = c.get(f"/api/docs/{rid}/result").json()
    assert res["pages"] == [[595.0, 842.0], [595.0, 842.0]]
    assert all(b["bbox"] and b["loc"]["page"] for b in res["blocks"])          # every block can be drawn
    table = next(b for b in res["blocks"] if b["type"] == "table")
    assert table["rows"][1] == ["BLEU", "28.4"] and 400 < table["bbox"][1] < 430
    png = c.get(f"/api/docs/{rid}/pages/1.png?w=960")
    assert png.status_code == 200 and png.content[:4] == b"\x89PNG"
    assert c.get(f"/api/docs/{rid}/pages/9.png").status_code == 404
    md = c.get(f"/api/docs/{rid}/export.md")
    assert "| BLEU | 28.4 |" in md.text and "보고서.md" in md.headers["content-disposition"].replace("%EB%B3%B4%EA%B3%A0%EC%84%9C", "보고서")
    z = zipfile.ZipFile(io.BytesIO(c.get(f"/api/docs/{rid}/export.zip").content))
    assert {"보고서.md", "blocks.jsonl", "meta.json"} <= set(z.namelist())

    # same file, same options -> the earlier reading, not a second one
    again = _upload(c, "다른이름.pdf", pdf).json()
    assert again["reused"] and again["doc"]["id"] == rid
    # same file with layout -> a new reading; figure words become one figure block with its image
    lay = _upload(c, "보고서.pdf", pdf, layout=True).json()["doc"]
    d2 = _wait(c, lay["id"])
    assert d2["state"] == "done" and d2["layout"] and d2["figures"] == 2
    fig = next(b for b in c.get(f"/api/docs/{lay['id']}/result").json()["blocks"] if b.get("image"))
    assert c.get(f"/api/docs/{lay['id']}/images/{fig['image']}").content[:4] == b"\x89PNG"
    assert [x["id"] for x in c.get("/api/docs").json()][:2] == [lay["id"], rid]   # newest first

    # not readable
    assert "읽을 수 없는 형식" in _upload(c, "a.hwp", b"x").json()["detail"]
    assert _upload(c, "empty.pdf", b"").status_code == 400
    bad = _upload(c, "broken.pdf", b"%PDF-1.4 not really").json()["doc"]
    fd = _wait(c, bad["id"])
    assert fd["state"] == "failed" and "파일을 읽지 못했습니다" in fd["error"]
    assert c.get(f"/api/docs/{bad['id']}/result").status_code == 409

    # non-PDF: blocks, no pages; model options are ignored
    w = _upload(c, "회의록.docx", _docx(), layout=True).json()["doc"]
    wd = _wait(c, w["id"])
    assert wd["state"] == "done" and wd["pages"] == 0 and not wd["layout"]
    t = _wait(c, _upload(c, "3분기 매출.xlsx", _xlsx()).json()["doc"]["id"])
    assert t["title"] == "3분기 매출", t["title"]             # xlsx title = file name, not the stored "source"
    assert c.get(f"/api/docs/{w['id']}/result").json()["blocks"][0]["text"] == "회의록"
    assert c.get(f"/api/docs/{w['id']}/pages/1.png").status_code == 404

    # model server off: the option fails that reading with a way out, plain reading still works
    FakeModels.up = False
    off = _upload(c, "보고서.pdf", pdf, ocr=True).json()["doc"]
    od = _wait(c, off["id"])
    assert od["state"] == "failed" and "서버" in od["error"]
    rr = c.post(f"/api/docs/{off['id']}/reread", data={"layout": "false", "ocr": "false"}).json()
    assert _wait(c, rr["id"])["state"] == "done" and rr["id"] != off["id"]

    # delete
    assert c.delete(f"/api/docs/{rid}").status_code == 200
    assert c.get(f"/api/docs/{rid}").status_code == 404
    assert not (tmp_path / "data" / "docs" / rid).exists()
    assert c.post(f"/api/docs/{rid}/cancel").status_code == 404


def _rpc(c, method, params, rid=1):
    return c.post("/a2a", json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params}).json()


def _msg(*parts, **kw):
    return {"message": {"role": "ROLE_USER", "messageId": "m1", "parts": list(parts), **kw}}


def test_a2a(client, tmp_path):
    c = client
    card = c.get("/.well-known/agent-card.json").json()
    assert card["supportedInterfaces"][0] == {"url": "http://testserver/a2a", "protocolBinding": "JSONRPC",
                                              "protocolVersion": "1.0"}
    assert card["name"] == "doc_reader" and card["skills"][0]["id"] == "parse-document" and card["capabilities"]["streaming"] is False

    pdf = _pdf()
    raw = {"raw": base64.b64encode(pdf).decode(), "filename": "에이전트.pdf", "mediaType": "application/pdf"}
    out = _rpc(c, "SendMessage", _msg(raw, contextId="ctx-1"))
    t = out["result"]["task"]
    assert t["status"]["state"] == "TASK_STATE_COMPLETED" and t["contextId"] == "ctx-1"
    arts = {a["artifactId"]: a for a in t["artifacts"]}
    assert "| BLEU | 28.4 |" in arts["markdown"]["parts"][0]["text"]
    data = arts["blocks"]["parts"][0]["data"]
    assert data["pages"] and all(b["bbox"] for b in data["blocks"])
    assert t["metadata"]["reviewUrl"] == f"http://testserver/read/#/docs/{t['id']}"
    # it is on the screen's list, marked as sent by an agent
    row = next(d for d in c.get("/api/docs").json() if d["id"] == t["id"])
    assert row["source"] == "a2a" and row["name"] == "에이전트.pdf"

    assert _rpc(c, "GetTask", {"id": t["id"]})["result"]["status"]["state"] == "TASK_STATE_COMPLETED"
    lst = _rpc(c, "ListTasks", {"contextId": "ctx-1"})["result"]
    assert [x["id"] for x in lst["tasks"]] == [t["id"]] and lst["totalSize"] == 1
    assert _rpc(c, "CancelTask", {"id": t["id"]})["error"]["code"] == -32002
    assert _rpc(c, "GetTask", {"id": "nope"})["error"]["code"] == -32001
    assert _rpc(c, "SendStreamingMessage", _msg(raw))["error"]["code"] == -32004
    assert _rpc(c, "Nope", {})["error"]["code"] == -32601
    assert c.post("/a2a", content=b"{oops").json()["error"]["code"] == -32700
    assert _rpc(c, "SendMessage", _msg({"text": "읽어줘"}, {"data": {"skill": "parse-document"}}))["error"]["code"] == -32602
    assert _rpc(c, "SendMessage", _msg({"raw": base64.b64encode(b"x").decode(), "filename": "a.hwp"}))["error"]["code"] == -32005
    assert _rpc(c, "SendMessage", _msg({"data": {"path": "relative.pdf"}}))["error"]["code"] == -32602

    # a local path with options, without waiting; the same file twice is one reading
    p = tmp_path / "local.pdf"
    p.write_bytes(_pdf(("Different text here.",), pages=1))
    first = _rpc(c, "SendMessage", {**_msg({"data": {"path": str(p), "layout": True}}),
                                    "configuration": {"returnImmediately": True}})["result"]["task"]
    assert first["status"]["state"] in ("TASK_STATE_SUBMITTED", "TASK_STATE_WORKING", "TASK_STATE_COMPLETED")
    second = _rpc(c, "SendMessage", _msg({"url": p.as_uri()}, {"data": {"layout": True}}))["result"]["task"]
    assert second["id"] == first["id"] and second["status"]["state"] == "TASK_STATE_COMPLETED"
    assert _rpc(c, "SendMessage", _msg({"url": "https://example.com/a.pdf"}))["error"]["code"] == -32005

    FakeModels.up = False
    c.app.state.service._model_seen = (0.0, {})
    err = _rpc(c, "SendMessage", _msg({"data": {"path": str(p), "ocr": True}}))["error"]
    assert err["code"] == -32603 and "꺼져" in err["message"]


def test_cancel_and_restart(tmp_path):
    """Queued readings survive a restart and are read; cancel works only before the end."""
    svc = Service(tmp_path / "d" / "read", start=False)
    a, _ = svc.submit("a.pdf", _pdf(), source="api")
    b, _ = svc.submit("b.pdf", _pdf(("other",)), source="api")
    assert svc.cancel(b.id).state == "canceled"
    with pytest.raises(Exception):
        svc.cancel(b.id)
    with TestClient(create_app(tmp_path / "d")) as raw:          # "restart" over the same folder
        c = Area(raw, "/read", None)
        assert _wait(c, a.id)["state"] == "done"
        assert c.get(f"/api/docs/{b.id}").json()["state"] == "canceled"
        assert c.post(f"/api/docs/{b.id}/cancel").status_code == 409
        ok = _rpc(c, "CancelTask", {"id": b.id})
        assert ok["error"]["code"] == -32002
