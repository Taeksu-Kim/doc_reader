"""Excel .xlsx: one heading per sheet, text lines vs. table runs, numbers as the sheet shows them."""

from __future__ import annotations

import re
from pathlib import Path

from doc_parser.model import Block, Loc
from doc_parser._common import _cell, _trim_table


def _xl_number(v: float, fmt: str) -> str:
    """Show a number the way the sheet shows it: thousands separator, decimals, percent."""
    section = fmt.split(";")[0]
    m = re.search(r"0\.(0+)", section)
    dec = len(m.group(1)) if m else 0
    if "%" in section:
        return f"{v * 100:,.{dec}f}%"
    if section == "General":
        return _cell(float(v))
    return f"{v:,.{dec}f}" if "," in section else f"{v:.{dec}f}"


def parse_xlsx(path: Path) -> tuple[str, list[Block], list[str]]:
    from openpyxl import load_workbook

    vals = load_workbook(str(path), data_only=True, read_only=True)
    forms = load_workbook(str(path), data_only=False, read_only=True)
    blocks: list[Block] = []
    warnings: list[str] = []
    for ws in vals.worksheets:
        fws = forms[ws.title]
        rows: list[list[str]] = []
        uncached = 0
        for vr, fr in zip(ws.iter_rows(), fws.iter_rows()):
            out = []
            for vc, fc in zip(vr, fr):
                v = vc.value
                if v is None and isinstance(fc.value, str) and fc.value.startswith("="):
                    v = fc.value                       # formula never calculated (no cached value)
                    uncached += 1
                elif isinstance(v, (int, float)) and not isinstance(v, bool):
                    v = _xl_number(v, vc.number_format or "General")
                out.append(_cell(v))
            rows.append(out)
        if uncached:
            warnings.append(f"시트:{ws.title}: 계산값이 저장되지 않은 수식 {uncached}개 (수식 그대로 표시)")
        rows = _trim_table(rows)
        if not rows:
            continue
        blocks.append(Block("heading", Loc(sheet=ws.title), text=ws.title, level=1))
        loc = Loc(sheet=ws.title)
        # a sheet mixes text lines (one filled cell) and tables (runs of rows with >= 2 cells)
        k = 0
        while k < len(rows):
            if sum(1 for c in rows[k] if c) >= 2:
                j = k
                while j < len(rows) and sum(1 for c in rows[j] if c) >= 2:
                    j += 1
                run = _trim_table(rows[k:j])
                if len(run) >= 2:
                    blocks.append(Block("table", loc, rows=run))
                else:
                    blocks.append(Block("text", loc, text=" | ".join(c for c in run[0] if c)))
                k = j
            else:
                j = k
                while j < len(rows) and sum(1 for c in rows[j] if c) < 2:
                    j += 1
                text = "\n".join(" ".join(c for c in r if c) for r in rows[k:j] if any(r))
                if text:
                    blocks.append(Block("text", loc, text=text))
                k = j
    vals.close()
    forms.close()
    return path.stem, blocks, warnings
