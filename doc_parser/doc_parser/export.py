"""Write a parsed document out as files: ``--extract-output DIR``.

    DIR/<stem>.md        the document in reading order: headings, paragraphs, tables, and each figure
                         as ![](images/...) followed by its labels; <!-- p.N --> marks page changes
    DIR/blocks.jsonl     the same blocks as JSON lines (``image`` = file under images/)
    DIR/images/          p{page:03d}_fig{k}.png, one per figure (PDF only)
    DIR/meta.json        source path, sha256, title, block / figure counts, warnings
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from doc_parser.model import ParsedDoc

IMAGES = "images"


def clear_images(out: Path) -> None:
    """Drop figure files a previous run left behind (only ours: p###_fig#.png)."""
    for f in (out / IMAGES).glob("p[0-9][0-9][0-9]_fig*.png"):
        f.unlink()


def write_export(doc: ParsedDoc, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    md, page = [f"# {doc.title}"], None
    for b in doc.blocks:
        if b.loc.page is not None and b.loc.page != page:
            page = b.loc.page
            md.append(f"<!-- p.{page} -->")
        if b.image:
            label = b.text.removeprefix("[그림]").strip()
            md.append(f"![p.{b.loc.page} {b.image}]({IMAGES}/{b.image})" + (f"\n\n{label}" if label else ""))
        else:
            md.append(b.render())
    stem = Path(doc.path).stem
    (out / f"{stem}.md").write_text("\n\n".join(md) + "\n", encoding="utf-8")
    with (out / "blocks.jsonl").open("w", encoding="utf-8") as f:
        for b in doc.blocks:
            f.write(json.dumps(asdict(b), ensure_ascii=False) + "\n")
    meta = {"source": doc.path, "sha256": doc.sha256, "kind": doc.kind, "title": doc.title,
            "blocks": len(doc.blocks), "figures": sum(1 for b in doc.blocks if b.image),
            "markdown": f"{stem}.md", "warnings": doc.warnings}
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return meta
