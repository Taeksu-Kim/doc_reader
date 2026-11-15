"""E2E for 청크 미리보기 (/chunk) in the one doc_reader server: the screen's API and the A2A endpoint,
with real files. Nothing to fake (no model, no network)."""

import base64
import io
import json

import docx
import pytest
from fastapi.testclient import TestClient

from doc_chunker.service import Desk
from doc_reader.app import create_app
from tests.conftest import Area


def _manual(sections=12, para=6) -> bytes:
    d = docx.Document()
    d.add_heading("품질 매뉴얼", 0)
    d.add_paragraph("시행일: 2026-07-01 / 버전 v2.1")
    for s in range(1, sections + 1):
        d.add_heading(f"제{s}장 점검 영역 {s}", 1)
        for k in range(para):
            d.add_paragraph(f"{s}.{k} 설비 진동과 온도를 매일 확인하고 기준을 넘으면 즉시 보고한다. " * 6)
        t = d.add_table(rows=1, cols=3)
        t.rows[0].cells[0].text, t.rows[0].cells[1].text, t.rows[0].cells[2].text = "항목", "기준", "주기"
        for r in range(20):
            row = t.add_row().cells
            row[0].text, row[1].text, row[2].text = f"설비{s}-{r}", f"{r}mm/s 이하", "매일"
    out = io.BytesIO()
    d.save(out)
    return out.getvalue()


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "data")) as c:
        yield Area(c, "/chunk", None)


def _up(c, name, data, limit=None):
    return c.post("/api/previews", files={"file": (name, data)}, data={"max_tokens": str(limit)} if limit else {})


def test_preview_flow(client, tmp_path):
    c = client
    assert "청크 미리보기" in c.get("/").text
    data = _manual()
    r = _up(c, "품질매뉴얼.docx", data, 1500).json()
    p = r["preview"]
    assert not r["reused"] and p["state"] == "done" and p["units"] > 3 and p["max_tokens"] == 1500
    full = c.get(f"/api/previews/{p['id']}").json()
    units = full["units_list"]
    assert len(units) == p["units"] and all(u["tokens"] <= 1500 for u in units)
    assert units[0]["text"].startswith("> 품질매뉴얼.docx")                     # header names the file as given
    assert all("v2.1" in u["text"] for u in units)                             # front matter in every unit
    assert [u["text"].count("\n# ") + u["text"].count("\n## ") for u in units] == [1] * len(units)   # one chapter each
    tbl = [u for u in units if "| 항목 | 기준 | 주기 |" in u["text"]]
    assert tbl and all(u["text"].count("| 항목 | 기준 | 주기 |") >= 1 for u in tbl)   # header repeated per part

    again = _up(c, "다른이름.docx", data, 1500).json()
    assert again["reused"] and again["preview"]["id"] == p["id"]
    re3000 = c.post(f"/api/previews/{p['id']}/rechunk", data={"max_tokens": "3000"}).json()
    assert re3000["id"] != p["id"] and re3000["units"] < p["units"]
    assert [x["id"] for x in c.get("/api/previews").json()][:2] == [re3000["id"], p["id"]]
    dl = c.get(f"/api/previews/{p['id']}/units.jsonl")
    assert len(dl.text.splitlines()) == p["units"] and json.loads(dl.text.splitlines()[0])["tokens"] > 0

    assert "자를 수 없는 형식" in _up(c, "a.hwp", b"x").json()["detail"]
    assert _up(c, "e.txt", b"").status_code == 400
    assert "상한" in _up(c, "a.txt", "내용".encode(), 50).json()["detail"]
    bad = _up(c, "깨짐.pdf", b"%PDF-1.4 nope").json()["preview"]
    assert bad["state"] == "failed" and "읽지 못했습니다" in bad["error"] and "/tmp" not in bad["error"]
    assert c.delete(f"/api/previews/{p['id']}").status_code == 200
    assert c.get(f"/api/previews/{p['id']}").status_code == 404


def _rpc(c, method, params):
    return c.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).json()


def test_a2a(client, tmp_path):
    c = client
    assert "chunk-document" in {s["id"] for s in c.get("/.well-known/agent-card.json").json()["skills"]}
    raw = {"raw": base64.b64encode(_manual(4)).decode(), "filename": "매뉴얼.docx"}
    t = _rpc(c, "SendMessage", {"message": {"role": "ROLE_USER", "messageId": "m", "contextId": "c1",
                                            "parts": [raw, {"data": {"max_tokens": 1200}}]}})["result"]["task"]
    assert t["status"]["state"] == "TASK_STATE_COMPLETED" and t["contextId"] == "c1"
    units = t["artifacts"][0]["parts"][0]["data"]["units"]
    assert units and all(u["tokens"] <= 1200 for u in units)
    assert t["metadata"]["reviewUrl"] == f"http://testserver/chunk/#/p/{t['id']}"
    assert c.get("/api/previews").json()[0]["source"] == "a2a"                  # the screen sees it
    p = tmp_path / "memo.txt"
    p.write_text("# 회의\n\n결정: 출시는 3월.\n", encoding="utf-8")
    t2 = _rpc(c, "SendMessage", {"message": {"role": "ROLE_USER", "messageId": "m", "parts": [{"url": p.as_uri()}, {"data": {"skill": "chunk-document"}}]}})["result"]["task"]
    assert "결정" in t2["artifacts"][0]["parts"][0]["data"]["units"][0]["text"]
    assert _rpc(c, "GetTask", {"id": t["id"]})["result"]["id"] == t["id"]
    assert [x["id"] for x in _rpc(c, "ListTasks", {"contextId": "c1"})["result"]["tasks"]] == [t["id"]]
    assert _rpc(c, "CancelTask", {"id": t["id"]})["error"]["code"] == -32002
    assert _rpc(c, "GetTask", {"id": "nope"})["error"]["code"] == -32001
    msg = lambda *parts: {"message": {"role": "ROLE_USER", "messageId": "m", "parts": list(parts)}}  # noqa: E731
    assert _rpc(c, "SendMessage", msg({"text": "잘라줘"}, {"data": {"skill": "chunk-document"}}))["error"]["code"] == -32602
    assert _rpc(c, "SendMessage", msg({"raw": base64.b64encode(b"x").decode(), "filename": "a.hwp"},
                                      {"data": {"skill": "chunk-document"}}))["error"]["code"] == -32005
    assert _rpc(c, "SendMessage", msg({"data": {"path": str(p), "max_tokens": 10}}))["error"]["code"] == -32602
    bad = _rpc(c, "SendMessage", msg({"raw": base64.b64encode(b"%PDF-1.4 x").decode(), "filename": "b.pdf"},
                                     {"data": {"skill": "chunk-document"}}))["result"]["task"]
    assert bad["status"]["state"] == "TASK_STATE_FAILED"


def test_restart_keeps_previews(tmp_path):
    p, _ = Desk(tmp_path / "d" / "chunk").submit("a.txt", "# 제목\n\n본문.\n".encode())
    with TestClient(create_app(tmp_path / "d")) as raw:
        c = Area(raw, "/chunk", None)
        assert c.get(f"/api/previews/{p.id}").json()["units_list"][0]["text"].endswith("본문.")
