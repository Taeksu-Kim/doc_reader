"""E2E for the one doc_reader server as a whole: one agent card, one A2A endpoint that hands each
message to the right desk, tasks found across desks, and the screens' addresses."""

import base64

from fastapi.testclient import TestClient

from doc_reader.app import create_app
from tests.corpus import _corpus


def _rpc(c, method, params):
    return c.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).json()


def _msg(*parts, ctx="ctx"):
    return {"message": {"role": "ROLE_USER", "messageId": "m", "contextId": ctx, "parts": list(parts)}}


def test_one_agent_three_desks(tmp_path):
    d = tmp_path / "corpus"
    _corpus(d)
    with TestClient(create_app(tmp_path / "data")) as c:
        card = c.get("/.well-known/agent-card.json").json()
        assert card["name"] == "doc_reader" and len(card["supportedInterfaces"]) == 1
        assert [s["id"] for s in card["skills"]] == ["parse-document", "chunk-document", "search-documents", "add-folder"]

        # who takes what, without naming the skill
        folder = _rpc(c, "SendMessage", _msg({"data": {"folder": str(d)}}))["result"]["task"]
        assert folder["metadata"]["reviewUrl"].endswith("/library/#/library")
        found = _rpc(c, "SendMessage", _msg({"text": "출장 일비"}))["result"]["task"]
        assert found["artifacts"][0]["artifactId"] == "hits"
        raw = {"raw": base64.b64encode("# 회의\n\n결정: 출시는 3월.\n".encode()).decode(), "filename": "회의.md"}
        parsed = _rpc(c, "SendMessage", _msg(raw))["result"]["task"]
        assert parsed["metadata"]["reviewUrl"].startswith("http://testserver/read/#/docs/")
        cut = _rpc(c, "SendMessage", _msg(raw, {"data": {"max_tokens": 500}}))["result"]["task"]
        assert cut["artifacts"][0]["artifactId"] == "units" and "/chunk/#/p/" in cut["metadata"]["reviewUrl"]
        named = _rpc(c, "SendMessage", _msg(raw, {"data": {"skill": "chunk-document"}}))["result"]["task"]
        assert "/chunk/#/p/" in named["metadata"]["reviewUrl"]
        assert _rpc(c, "SendMessage", _msg({"data": {"skill": "dance"}}))["error"]["code"] == -32602

        # one task list over all desks, newest first; any id is found by GetTask / CancelTask
        ids = [t["id"] for t in _rpc(c, "ListTasks", {"contextId": "ctx"})["result"]["tasks"]]
        assert set(ids) == {folder["id"], found["id"], parsed["id"], cut["id"], named["id"]}
        for t in (folder, found, parsed, cut):
            assert _rpc(c, "GetTask", {"id": t["id"]})["result"]["id"] == t["id"]
        assert _rpc(c, "CancelTask", {"id": cut["id"]})["error"]["code"] == -32002
        assert _rpc(c, "GetTask", {"id": "nope"})["error"]["code"] == -32001

        # each screen shows what came over A2A
        assert c.get("/library/api/tasks").json()[0]["source"] == "a2a"
        assert c.get("/read/api/docs").json()[0]["source"] == "a2a"
        assert c.get("/chunk/api/previews").json()[0]["source"] == "a2a"

        # addresses: home is the library; area roots without a slash redirect
        r = c.get("/", follow_redirects=False)
        assert r.status_code in (302, 307) and r.headers["location"] == "/library/"
        for area, title in (("library", "자료실 검색"), ("read", "읽기 검수"), ("chunk", "청크 미리보기")):
            assert c.get(f"/{area}", follow_redirects=False).headers["location"] == f"/{area}/"
            page = c.get(f"/{area}/").text
            assert title in page and 'class="areas"' in page and '"/api/' not in page
