"""Retrieval accuracy on a labelled query set, per search mode.

    python tools/eval_retrieval.py --db out/idx.sqlite --queries tools/queries_nurisense.json \
        [--embed-url http://<ip>:8011/v1] [--modes keyword,vector,hybrid] [--detail]
        [--rewrites out/rewrites.json]   # LLM search queries per request (tools/rewrite_queries.py)

With --rewrites, each request is searched as itself AND as every rewritten query; the file rankings
are fused with reciprocal rank fusion (original query kept, so a bad rewrite cannot remove a hit).

Per query, files are ranked by their best unit. Reported (file level):
  recall@k   share of the needed files found in the top k
  all@k      share of queries whose needed files are ALL in the top k
  MRR        1 / rank of the first needed file
  trap>need  queries where a trap file (older version, preliminary figures) ranks above every needed file
"""

from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path

from doc_index import Index, OpenAICompatEmbedder, files_of

KS = (5, 10, 20)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--queries", required=True)
    ap.add_argument("--embed-url", default=None)
    ap.add_argument("--modes", default="keyword,vector,hybrid")
    ap.add_argument("--detail", action="store_true")
    ap.add_argument("--rewrites", default=None)
    args = ap.parse_args()
    qs = json.loads(Path(args.queries).read_text(encoding="utf-8"))
    rewrites = json.loads(Path(args.rewrites).read_text(encoding="utf-8")) if args.rewrites else {}
    idx = Index(args.db, OpenAICompatEmbedder(args.embed_url) if args.embed_url else None)
    modes = [m for m in args.modes.split(",") if m in ("keyword", "hybrid") or args.embed_url]
    print(f"질의 {len(qs)}개 · 색인 {idx.stats()}")
    for mode in modes:
        agg: dict[str, dict] = defaultdict(lambda: defaultdict(float))
        t0 = time.time()
        for q in qs:
            variants = [q["q"]] + rewrites.get(q["id"], [])
            fused: dict[str, float] = defaultdict(float)
            for v in variants:
                for r, p in enumerate(files_of(idx.search(v, 60, mode)), 1):
                    fused[Path(p).name] += 1.0 / (60 + r)
            ranked = sorted(fused, key=lambda f: -fused[f])
            pos = {f: ranked.index(f) + 1 for f in q["need"] if f in ranked}
            trap_pos = [ranked.index(f) + 1 for f in q["trap"] if f in ranked]
            first = min(pos.values()) if pos else None
            for g in ("전체", q["type"]):
                a = agg[g]
                a["n"] += 1
                for k in KS:
                    a[f"r{k}"] += sum(1 for r in pos.values() if r <= k) / len(q["need"])
                    a[f"all{k}"] += all(f in pos and pos[f] <= k for f in q["need"])
                a["mrr"] += 1 / first if first else 0
                a["trapq"] += bool(q["trap"])
                a["trap"] += bool(trap_pos) and (first is None or min(trap_pos) < first)
            if args.detail:
                miss = [f"{f}:{pos.get(f, '-')}" for f in q["need"]]
                traps = [f"{f}:{ranked.index(f) + 1}" for f in q["trap"] if f in ranked]
                print(f"  [{mode}] {q['id']} 정답 순위 {miss}" + (f" | 함정 {traps}" if traps else ""))
        dt = (time.time() - t0) / len(qs)
        print(f"\n== {mode}{' + LLM 검색어' if rewrites else ''} (요청당 {dt:.2f}초)")
        print(f"{'구분':10s} {'n':>3s} " + " ".join(f"{'R@' + str(k):>6s}" for k in KS) + " "
              + " ".join(f"{'all@' + str(k):>6s}" for k in KS) + f" {'MRR':>5s} {'함정>정답':>8s}")
        for g in ["전체"] + sorted(k for k in agg if k != "전체"):
            a = agg[g]
            n = a["n"]
            print(f"{g:10s} {int(n):3d} " + " ".join(f"{a[f'r{k}'] / n:6.2f}" for k in KS) + " "
                  + " ".join(f"{a[f'all{k}'] / n:6.2f}" for k in KS) + f" {a['mrr'] / n:5.2f} "
                  f"{int(a['trap']):>3d}/{int(a['trapq'])}")


if __name__ == "__main__":
    main()
