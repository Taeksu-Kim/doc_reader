"""CLI: python -m doc_parser FILE [--json] [--ocr-url URL] [--layout-url URL] [--extract-output DIR]

Prints what the parser read: title, warnings, then every block with its location
(markdown form), or the blocks as JSON lines with --json.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from doc_parser import HttpOcr, OcrUnavailable, UnsupportedFormat, parse_file
from doc_parser.export import IMAGES, clear_images, write_export


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="doc_parser", description="문서를 읽어 제목/본문/표 블록으로 출력. "
                                 "화면·A2A 서버는 doc_reader (`python -m doc_reader serve`)")
    ap.add_argument("file")
    ap.add_argument("--json", action="store_true", help="블록을 JSON 한 줄씩 출력")
    ap.add_argument("--ocr-url", metavar="URL",
                    help="PDF의 스캔 페이지·이미지를 이 OCR 서버(tools/ocr_server.py)로 읽음. 없으면 글자층만")
    ap.add_argument("--layout-url", metavar="URL",
                    help="PDF 페이지의 그림 영역을 이 서버로 찾아, 그림 안 글자를 본문·표에서 빼고 그림 블록으로 묶음")
    ap.add_argument("--extract-output", metavar="DIR",
                    help="텍스트(DIR/<이름>.md, blocks.jsonl)와 그림 이미지(DIR/images/, PDF만)를 이 폴더에 뽑음. "
                         "벡터 그림까지 뽑으려면 --layout-url 도")
    args = ap.parse_args(argv)
    path = Path(args.file)
    if not path.is_file():
        print(f"오류: 파일이 없습니다: {path}", file=sys.stderr)
        return 2
    out = Path(args.extract_output) if args.extract_output else None
    if out is not None:
        clear_images(out)
    servers = {u: HttpOcr(u) for u in {args.ocr_url, args.layout_url} if u}   # same URL -> one client
    try:
        doc = parse_file(path, ocr=servers.get(args.ocr_url), layout=servers.get(args.layout_url),
                         images=out / IMAGES if out is not None else None)
    except (UnsupportedFormat, OcrUnavailable) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # corrupt / protected file
        print(f"오류: 읽기 실패: {type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
        return 1
    if out is not None:
        meta = write_export(doc, out)
        print(f"{out}/{meta['markdown']} · blocks.jsonl · meta.json — 블록 {meta['blocks']}개, "
              f"그림 {meta['figures']}개({out / IMAGES}), 경고 {len(doc.warnings)}개")
        if doc.kind == "pdf" and not args.layout_url:
            print("참고: --layout-url 없이는 래스터 이미지만 뽑힘 (벡터로 그린 그림·차트는 빠짐)")
        return 0
    if args.json:
        for b in doc.blocks:
            print(json.dumps(asdict(b), ensure_ascii=False))
        return 0
    kinds = {t: sum(1 for b in doc.blocks if b.type == t) for t in ("heading", "text", "table")}
    print(f"{doc.kind} «{doc.title}» 제목 {kinds['heading']} · 본문 {kinds['text']} · 표 {kinds['table']}")
    for w in doc.warnings:
        print(f"⚠ {w}")
    for b in doc.blocks:
        print(f"\n[{b.loc.label()}]\n{b.render()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
