"""Helpers shared by the format parsers."""

from __future__ import annotations

import datetime as dt


class UnsupportedFormat(ValueError):
    pass


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else f"{v:g}"
    if isinstance(v, (dt.datetime, dt.date)):
        return v.date().isoformat() if isinstance(v, dt.datetime) and not (v.hour or v.minute) else v.isoformat()
    return str(v).strip()


def _trim_table(rows: list[list[str]]) -> list[list[str]]:
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return []
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    keep = [j for j in range(width) if any(r[j].strip() for r in rows)]
    return [[r[j] for j in keep] for r in rows]
