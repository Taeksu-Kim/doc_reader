"""E2E for 자료실 (/library) in the one doc_reader server: the screen's API and the A2A endpoint, with a
real build worker and real files. Faked: the embedding server (in-process object) only.
"원본에서 보기" goes to 읽기 검수 in the same server."""

import time
from types import SimpleNamespace

import pymupdf
import pytest
from fastapi.testclient import TestClient

from doc_index.service import Desk
from doc_reader.app import create_app
from tests.conftest import Area
from tests.corpus import _corpus, _vec


class FakeEmbedder:
    up = True

    def __init__(self, url):
        self.url, self.timeout = url, 600

    def embed(self, texts, query=False):
        if not FakeEmbedder.up:
            raise OSError("down")
        return [_vec(t) for t in texts]


def _wait(c, tid, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        t = next(x for x in c.get("/api/tasks").json() if x["id"] == tid)
        if t["state"] not in ("queued", "running"):
            return t
        time.sleep(0.05)
    raise AssertionError("build still running")


@pytest.fixture
def client(tmp_path):
    FakeEmbedder.up = True
    with TestClient(create_app(tmp_path / "data", embed_url="http://fake", embedder_factory=FakeEmbedder)) as c:
        yield Area(c, "/library", SimpleNamespace(desk=c.app.state.library))


def test_library_and_search(client, tmp_path):
    c = client
    assert "자료실 검색" in c.get("/").text
    st = c.get("/api/status").json()
    assert st["embed"]["ok"] and st["stats"]["units"] == 0 and st["build"] is None
    assert c.post("/api/sources", json={"path": "relative/dir"}).status_code == 400
    assert "폴더가 없습니다" in c.post("/api/sources", json={"path": str(tmp_path / "nope")}).json()["detail"]

    d = tmp_path / "corpus"
    _corpus(d)
    t = c.post("/api/sources", json={"path": str(d)}).json()
    done = _wait(c, t["id"])
    assert done["state"] == "done" and done["result"]["added"] == 5 and done["result"]["vectors"], done
    assert c.post("/api/sources", json={"path": str(d / "")}).status_code == 400           # already there
    src = c.get("/api/sources").json()
    assert src[0]["ok"] == 5 and src[0]["exists"]

    r = c.get("/api/search", params={"q": "출장 일비", "mode": "hybrid"}).json()
    assert r["mode"] == "hybrid" and r["hits"][0]["name"].startswith("rule_v2")
    top = r["hits"][0]
    assert top["ranks"].get("keyword") and top["ranks"].get("vector") and top["loc_start"]
    assert top["family"] and top["family"][0].endswith(("rule_v21.txt", "rule_v20.txt"))      # sibling version
    assert c.get("/api/search", params={"q": "NS-500 금형", "mode": "keyword"}).json()["hits"][0]["name"] == "quote.txt"
    assert c.get("/api/search", params={"q": "  "}).status_code == 400
    units = c.get("/api/units", params={"path": top["path"]}).json()
    assert units and "일비" in units[0]["text"]
    assert {f["status"] for f in c.get("/api/files").json()} == {"ok"}

    # embedding server off: searches fall back to keywords and say so; builds index keywords only
    FakeEmbedder.up = False
    c.app.state.desk._seen = (0.0, {})
    r = c.get("/api/search", params={"q": "출장 일비", "mode": "hybrid"}).json()
    assert r["mode"] == "keyword" and r["asked_mode"] == "hybrid" and r["hits"]
    (d / "meeting.txt").write_text("주간 경영회의\n\n[안건 1] NS-700 출시 일정 확정.\n", encoding="utf-8")
    (d / "new.txt").write_text("새 문서: 휴가 규정 연차 15일.\n", encoding="utf-8")
    b = _wait(c, c.post("/api/build").json()["id"])
    assert (b["result"]["added"], b["result"]["unchanged"], b["result"]["vectors"]) == (2, 4, False)
    st = c.get("/api/status").json()["stats"]
    assert st["units"] > st["vectors"]                                                       # new units lack vectors
    FakeEmbedder.up = True
    c.app.state.desk._seen = (0.0, {})
    b = _wait(c, c.post("/api/build").json()["id"])
    assert b["result"]["added"] == 0 and b["result"]["embedded"] >= 2                          # filled in later
    st = c.get("/api/status").json()["stats"]
    assert st["units"] == st["vectors"]

    # removing the folder empties the index
    assert c.delete("/api/sources", params={"path": src[0]["path"]}).status_code == 200
    _wait(c, c.get("/api/tasks").json()[0]["id"])
    assert c.get("/api/status").json()["stats"]["units"] == 0 and c.get("/api/sources").json() == []
    assert c.delete("/api/sources", params={"path": "/x"}).status_code == 404


def _rpc(c, method, params):
    return c.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).json()


def _msg(*parts, ctx=None):
    m = {"role": "ROLE_USER", "messageId": "m", "parts": list(parts)}
    if ctx:
        m["contextId"] = ctx
    return {"message": m}


def test_a2a(client, tmp_path):
    c = client
    card = c.get("/.well-known/agent-card.json").json()
    assert {"search-documents", "add-folder"} <= {s["id"] for s in card["skills"]}
    d = tmp_path / "corpus"
    _corpus(d)
    built = _rpc(c, "SendMessage", _msg({"data": {"folder": str(d)}}, ctx="job-1"))["result"]["task"]
    assert built["status"]["state"] == "TASK_STATE_COMPLETED" and "파일 5개" in built["status"]["message"]["parts"][0]["text"]
    assert c.get("/api/tasks").json()[0]["source"] == "a2a"                                  # the screen sees it

    found = _rpc(c, "SendMessage", _msg({"text": "출장 일비"}, {"data": {"mode": "keyword", "k": 3}}, ctx="job-1"))["result"]["task"]
    assert found["status"]["state"] == "TASK_STATE_COMPLETED"
    hits = found["artifacts"][0]["parts"][0]["data"]["hits"]
    assert 1 <= len(hits) <= 3 and hits[0]["name"].startswith("rule_v2")   # only files with the words
    assert found["metadata"]["reviewUrl"].startswith("http://testserver/library/#/search?q=")
    assert _rpc(c, "GetTask", {"id": found["id"]})["result"]["artifacts"][0]["parts"][0]["data"]["hits"] == hits
    assert {t["id"] for t in _rpc(c, "ListTasks", {"contextId": "job-1"})["result"]["tasks"]} == {built["id"], found["id"]}
    assert _rpc(c, "CancelTask", {"id": found["id"]})["error"]["code"] == -32002
    assert _rpc(c, "GetTask", {"id": "nope"})["error"]["code"] == -32001
    assert _rpc(c, "SendMessage", _msg({"data": {}}))["error"]["code"] == -32602
    assert _rpc(c, "SendMessage", _msg({"data": {"folder": "relative"}}))["error"]["code"] == -32602
    assert _rpc(c, "SendMessage", _msg({"text": "x"}, {"data": {"mode": "fuzzy"}}))["error"]["code"] == -32602


def test_build_queue_and_restart(tmp_path):
    d = tmp_path / "corpus"
    _corpus(d)
    desk = Desk(tmp_path / "data" / "library", start=False)
    t = desk.add_source(str(d))
    assert desk.build().id == t.id                     # a waiting build already covers this request
    with TestClient(create_app(tmp_path / "data")) as raw:     # "restart": the queued build runs
        c = Area(raw, "/library", None)
        assert _wait(c, t.id)["state"] == "done"
        assert c.get("/api/status").json()["stats"]["files_ok"] == 5
        assert c.post(f"/api/tasks/{t.id}/cancel").status_code == 409


def test_open_in_reader(client, tmp_path):
    """A result's "원본에서 보기" reads the file on the 읽기 검수 desk and links to its screen at the page."""
    c = client
    d = tmp_path / "pdfs"
    d.mkdir()
    doc = pymupdf.open()
    for n in range(3):
        doc.new_page().insert_text((72, 100), f"Page {n + 1}: 하우징 금형 비용 {n}", fontsize=11, fontname="korea")
    doc.save(str(d / "견적.pdf"))
    _wait(c, c.post("/api/sources", json={"path": str(d)}).json()["id"])
    hit = c.get("/api/search", params={"q": "금형 비용 2", "mode": "keyword"}).json()["hits"][0]
    r = c.post("/api/open", json={"path": hit["path"], "loc": "p.3"}).json()
    assert r["url"].startswith("/read/#/docs/") and r["url"].endswith("?p=3")
    rid = r["url"].split("/docs/")[1].split("?")[0]
    again = c.post("/api/open", json={"path": hit["path"], "loc": "p.1"}).json()
    assert again["url"].split("/docs/")[1].split("?")[0] == rid                 # not read twice
    doc_row = c.c.get(f"/read/api/docs/{rid}").json()
    assert doc_row["source"] == "library" and doc_row["name"] == "견적.pdf"
    assert c.post("/api/open", json={"path": str(d / "gone.pdf")}).status_code == 404
