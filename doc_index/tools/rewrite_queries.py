"""Ask an LLM to turn each document request into several search queries; save them for eval.

    python tools/rewrite_queries.py --queries tools/queries_nurisense.json --out out/rewrites.json \
        --llm-url http://<ip>:8002/v1 --model qwen27b

The rewrite belongs to the agent at run time; here it is cached to a file so the retrieval
measurement is reproducible (same rewrites -> same ranking).
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path

PROMPT = """당신은 회사 공유 드라이브에서 자료를 찾는 담당자다. 아래 요청으로 문서를 쓰려면 어떤 사내 파일이 필요한지 생각하고,
그 파일들을 찾기 위한 검색어를 3~5개 만들어라.
- 요청에 직접 나오지 않아도 필요할 자료를 포함한다 (예: "주요 이슈"라면 최근 경영회의록, 고객 불만(VOC), 진행 중 프로젝트 일정).
- 사내 문서에 실제로 쓰였을 법한 말로 바꿔 쓴다 (예: "겉케이스 틀" → "하우징 금형", "하루 받는 돈" → "출장 일비").
- 각 검색어는 짧은 명사구로 쓴다.

요청: {q}"""

SCHEMA = {"type": "object", "properties": {"queries": {"type": "array", "items": {"type": "string"},
                                                        "minItems": 3, "maxItems": 5}},
          "required": ["queries"]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--llm-url", required=True)
    ap.add_argument("--model", required=True)
    args = ap.parse_args()
    qs = json.loads(Path(args.queries).read_text(encoding="utf-8"))
    out = {}
    t0 = time.time()
    for q in qs:
        body = {"model": args.model, "temperature": 0, "max_tokens": 300,
                "messages": [{"role": "user", "content": PROMPT.format(q=q["q"])}],
                "response_format": {"type": "json_schema", "json_schema": {"name": "queries", "schema": SCHEMA}},
                "chat_template_kwargs": {"enable_thinking": False}}
        req = urllib.request.Request(args.llm_url.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            content = json.load(r)["choices"][0]["message"]["content"]
        out[q["id"]] = json.loads(content)["queries"]
        print(q["id"], out[q["id"]], flush=True)
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(out)}개, 요청당 {(time.time() - t0) / len(qs):.1f}초 -> {args.out}")


if __name__ == "__main__":
    main()
