"""python -m doc_reader serve [--data DIR] [--port 8767] [--model-url URL] [--embed-url URL]

One server: 자료실(/library) · 읽기 검수(/read) · 청크 미리보기(/chunk) + one A2A agent (/a2a).
127.0.0.1 only. Optional model servers may be off: the server starts, those options are locked.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
PORT = 8767


def _wsl_ip() -> str:
    """Model servers listen on the WSL IP (loopback is unreliable on this machine)."""
    try:
        return subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5).stdout.split()[0]
    except (OSError, IndexError, subprocess.SubprocessError):
        return "127.0.0.1"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="doc_reader", description="읽기 에이전트 서버: 자료실·읽기 검수·청크 미리보기 + A2A")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve", help="화면 + A2A 서버 (127.0.0.1 고정)")
    sv.add_argument("--data", default=str(DATA), help="상태를 둘 폴더 (기본: doc_reader/data, 영역별 하위 폴더)")
    sv.add_argument("--port", type=int, default=PORT)
    sv.add_argument("--model-url", default=None, help="레이아웃·OCR 서버 (기본 http://<WSL IP>:8090 = doc_parser/tools/ocr.sh)")
    sv.add_argument("--embed-url", default=None, help="임베딩 서버 (기본 http://<WSL IP>:8011/v1, 꺼져 있으면 키워드만)")
    args = ap.parse_args(argv)
    import uvicorn

    from doc_reader.app import create_app

    ip = _wsl_ip()
    model, embed = args.model_url or f"http://{ip}:8090", args.embed_url or f"http://{ip}:8011/v1"
    print(f"데이터: {args.data}\n열기: http://127.0.0.1:{args.port}  (자료실 /library · 읽기 검수 /read · 청크 /chunk)\n"
          f"A2A: http://127.0.0.1:{args.port}/.well-known/agent-card.json\n레이아웃·OCR: {model}\n임베딩: {embed}")
    uvicorn.run(create_app(args.data, model_url=model, embed_url=embed), host="127.0.0.1", port=args.port,
                log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
