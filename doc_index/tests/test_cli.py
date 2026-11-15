"""E2E at the CLI entry point (`python -m doc_index`). The embedding server is faked by a local HTTP
server returning deterministic bag-of-bigram vectors (only the network dependency is replaced)."""

import json
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest

from doc_index import Index, OpenAICompatEmbedder, files_of, tokens
from doc_index.__main__ import main

DIM = 256


def _vec(text: str) -> list[float]:
    v = np.zeros(DIM, dtype=np.float32)
    for t in tokens(text.split("Query:")[-1]):
        v[zlib.crc32(t.encode()) % DIM] += 1.0
    n = np.linalg.norm(v)
    return (v / n if n else v).tolist()


@pytest.fixture()
def embed_url():
    calls = {"n": 0}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls["n"] += len(body["input"])
            data = json.dumps({"data": [{"index": i, "embedding": _vec(t)} for i, t in enumerate(body["input"])]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1", calls
    srv.shutdown()


def _corpus(d: Path) -> None:
    d.mkdir()
    (d / "rule_v21.txt").write_text("국내외 출장비 지급 규정 v2.1\n시행 2026-07-01\n\n제2조 (일비) 1일 30,000원.\n", encoding="utf-8")
    (d / "rule_v20.txt").write_text("국내외 출장비 지급 규정 v2.0\n시행 2025-03-01\n\n제2조 (일비) 1일 25,000원.\n", encoding="utf-8")
    (d / "meeting.txt").write_text("주간 경영회의\n\n[안건 1] NS-500 단가\n- MCU 단가 14,000원 인상, 65만원 안 검토.\n", encoding="utf-8")
    (d / "quote.txt").write_text("견적서 DS-2026-1017-03\nNS-500 하우징 금형 4,200만원\n", encoding="utf-8")
    (d / "manual.txt").write_text("품질 매뉴얼\n\n제12 장 설비 점검\n진동 기준을 확인한다.\n", encoding="utf-8")


def test_build_search_incremental(tmp_path, capsys, embed_url):
    url, calls = embed_url
    src, db = tmp_path / "src", str(tmp_path / "i.sqlite")
    _corpus(src)
    (src / "quote_copy.txt").write_text((src / "quote.txt").read_text(encoding="utf-8"), encoding="utf-8")
    (src / "x.hwp").write_bytes(b"\xd0\xcf")
    assert main(["build", str(src), "--db", db, "--embed-url", url]) == 0
    out = capsys.readouterr().out
    assert "추가 5" in out and "중복 1" in out and "건너뜀 1" in out and "임베딩 5" in out
    assert calls["n"] == 5

    def top(q, mode="keyword"):
        assert main(["search", q, "--db", db, "--mode", mode, "-k", "3", "--embed-url", url]) == 0
        return capsys.readouterr().out.splitlines()[0]

    assert "meeting.txt" in top("단가")                     # 2-syllable word
    assert "meeting.txt" in top("NS500 단가는")              # ns-500 normalised, particle tolerated
    assert "meeting.txt" in top("14000원")                   # thousands separator
    assert "quote.txt" in top("DS-2026-1017-03")
    assert "manual.txt" in top("제12장")                     # Word-PDF style "제12 장" normalised
    assert "rule_v2" in top("출장 일비", "vector")
    hybrid = top("하우징 금형", "hybrid")
    assert "quote.txt" in hybrid and "keyword#" in hybrid and "vector#" in hybrid

    n0 = calls["n"]                                                         # queries were embedded too
    assert main(["build", str(src), "--db", db, "--embed-url", url]) == 0      # nothing changed
    assert "추가 0 · 변경 없음 7" in capsys.readouterr().out and calls["n"] == n0

    (src / "meeting.txt").write_text("주간 경영회의\n\n[안건 1] 채용 계획 검토\n", encoding="utf-8")
    (src / "rule_v20.txt").unlink()
    assert main(["build", str(src), "--db", db, "--embed-url", url]) == 0
    out = capsys.readouterr().out
    assert "추가 1" in out and "삭제 1" in out and calls["n"] == n0 + 1     # only the changed file re-embedded
    assert main(["search", "단가", "--db", db, "--mode", "keyword"]) == 0
    assert "meeting.txt" not in capsys.readouterr().out                     # old text is gone from the index
    idx = Index(db, OpenAICompatEmbedder(url))
    assert idx.stats() == {"files_ok": 4, "files_skipped": 1, "files_duplicate": 1, "units": 4, "vectors": 4}
    assert all("rule_v20" not in p for p in files_of(idx.search("출장비 규정", 10)))


def test_family_and_reindex(tmp_path, capsys):
    src = tmp_path / "src"
    src.mkdir()
    for name, body in [("출장비규정_v1.0.txt", "출장비 규정 v1.0"), ("07_출장비규정_v2.0_구버전.txt", "출장비 규정 v2.0"),
                       ("08_출장비규정_v2.1.txt", "출장비 규정 v2.1"), ("주간회의록_2026-09-07.txt", "회의 9월"),
                       ("03_주간회의록_2026-10-06.txt", "회의 10월 6일"), ("주간회의록_2026-10-20.txt", "회의 10월 20일 3분기 확정"),
                       ("고객VOC_2026Q2.txt", "VOC 2분기"), ("06_고객VOC_2026Q3.txt", "VOC 3분기")]:
        (src / name).write_text(body + "\n", encoding="utf-8")
    db = str(tmp_path / "i.sqlite")
    assert main(["build", str(src), "--db", db]) == 0
    idx = Index(db)
    name = lambda ps: [Path(p).name for p in ps]  # noqa: E731
    assert name(idx.family(str(src / "출장비규정_v1.0.txt"))) == ["08_출장비규정_v2.1.txt", "07_출장비규정_v2.0_구버전.txt"]
    assert name(idx.family(str(src / "주간회의록_2026-09-07.txt"), 2)) == ["주간회의록_2026-10-20.txt", "03_주간회의록_2026-10-06.txt"]
    assert name(idx.family(str(src / "고객VOC_2026Q2.txt"))) == ["06_고객VOC_2026Q3.txt"]
    assert idx.reindex_keywords() == 8                          # keyword index rebuilt from stored units
    ranked = name(h.path for h in idx.keyword("3분기", 5))
    assert set(ranked[:2]) == {"06_고객VOC_2026Q3.txt", "주간회의록_2026-10-20.txt"}   # exact "3분기" outranks...
    assert ranked.index("고객VOC_2026Q2.txt") >= 2                                    # ..."분기" alone


def test_errors(tmp_path, capsys):
    assert main(["search", "x", "--db", str(tmp_path / "none.sqlite")]) == 2
    assert main(["build", str(tmp_path / "nope"), "--db", str(tmp_path / "i.sqlite")]) == 2
    src = tmp_path / "s"
    _corpus(src)
    assert main(["build", str(src), "--db", str(tmp_path / "i.sqlite")]) == 0        # keyword only, no server
    assert "임베딩 0" in capsys.readouterr().out
    assert main(["search", "단가", "--db", str(tmp_path / "i.sqlite"), "--mode", "vector"]) == 2
    assert main(["search", "단가", "--db", str(tmp_path / "i.sqlite")]) == 0        # hybrid falls back to keyword
    assert "meeting.txt" in capsys.readouterr().out
