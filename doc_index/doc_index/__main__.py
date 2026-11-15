"""CLI:
    python -m doc_index build PATH... --db index.sqlite [--embed-url URL | --no-embed] [--max-tokens N]
    python -m doc_index search "질의" --db index.sqlite [--mode hybrid|keyword|vector] [-k 10] [--embed-url URL]
    python -m doc_index stats --db index.sqlite
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from doc_index import Index, OpenAICompatEmbedder


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="doc_index", description="문서 색인(키워드+벡터)과 검색")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("paths", nargs="+")
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--mode", default="hybrid", choices=["hybrid", "keyword", "vector"])
    s.add_argument("-k", type=int, default=10)
    st = sub.add_parser("stats")
    for p in (b, s, st):
        p.add_argument("--db", required=True)
    for p in (b, s):
        p.add_argument("--embed-url", default=None, help="OpenAI 호환 임베딩 주소 (없으면 키워드만)")
    b.add_argument("--max-tokens", type=int, default=3000)
    args = ap.parse_args(argv)

    if args.cmd != "build" and not Path(args.db).exists():
        print(f"오류: 색인 파일이 없습니다: {args.db}", file=sys.stderr)
        return 2
    emb = OpenAICompatEmbedder(args.embed_url) if getattr(args, "embed_url", None) else None
    idx = Index(args.db, emb)
    if args.cmd == "build":
        t = time.time()
        try:
            rep = idx.build(args.paths, args.max_tokens, embed=emb is not None)
        except FileNotFoundError as exc:
            print(f"오류: {exc}", file=sys.stderr)
            return 2
        print(f"추가 {rep.added} · 변경 없음 {rep.unchanged} · 삭제 {rep.removed} · 건너뜀 {len(rep.skipped)} · "
              f"중복 {len(rep.duplicates)} · 새 단위 {rep.units} · 임베딩 {rep.embedded} · {time.time() - t:.1f}초")
        for p, why in rep.skipped:
            print(f"  건너뜀 {Path(p).name}: {why}")
        return 0
    if args.cmd == "stats":
        print(idx.stats())
        return 0
    if args.mode in ("vector", "hybrid") and emb is None:
        if args.mode == "vector":
            print("오류: vector 검색은 --embed-url 이 필요합니다", file=sys.stderr)
            return 2
    for n, h in enumerate(idx.search(args.query, args.k, args.mode), 1):
        ranks = " ".join(f"{m}#{r}" for m, r in h.ranks.items())
        print(f"{n:2d}. {h.score:.4f} {Path(h.path).name} {h.loc} {ranks}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
