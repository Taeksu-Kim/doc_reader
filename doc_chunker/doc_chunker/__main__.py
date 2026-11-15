"""CLI: python -m doc_chunker PATH... [--max-tokens 3000] [--out units.jsonl] [--show N]

Parses and splits files, prints one line per file, and optionally writes every unit as JSONL.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from doc_chunker import DEFAULT_MAX_TOKENS, parse_and_chunk


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="doc_chunker", description="파일을 읽어 구조 단위로 나눔. 화면·A2A 서버는 doc_reader")
    ap.add_argument("paths", nargs="+", help="파일·폴더 (폴더는 하위까지)")
    ap.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS, help="단위 하나의 최대 토큰(추정)")
    ap.add_argument("--out", default=None, help="단위를 JSONL로 저장할 경로")
    ap.add_argument("--show", type=int, default=0, metavar="N", help="앞의 N개 단위 본문을 화면에 출력")
    args = ap.parse_args(argv)
    try:
        res = parse_and_chunk(args.paths, args.max_tokens)
    except FileNotFoundError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    per_file = defaultdict(list)
    for u in res.units:
        per_file[u.path].append(u)
    for d in res.docs:
        us = per_file[d.path]
        print(f"{d.kind:5s} 단위 {len(us):4d}  최대 {max(u.tokens for u in us):6,} 토큰  {Path(d.path).name}  «{d.title[:40]}»")
        for w in d.warnings:
            print(f"      ⚠ {w}")
    for path, reason in res.skipped:
        print(f"건너뜀 {Path(path).name}: {reason}")
    for path, same in res.duplicates:
        print(f"중복   {Path(path).name} = {Path(same).name}")
    total = sum(u.tokens for u in res.units)
    print(f"--- 파일 {len(res.docs)}개 · 단위 {len(res.units)}개 · 합계 약 {total:,} 토큰 · "
          f"건너뜀 {len(res.skipped)} · 중복 {len(res.duplicates)}")
    for u in res.units[:args.show]:
        print(f"\n=== {u.id} ({u.tokens:,} 토큰)\n{u.text}")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            for u in res.units:
                f.write(json.dumps(u.to_dict(), ensure_ascii=False) + "\n")
        print(f"저장: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
