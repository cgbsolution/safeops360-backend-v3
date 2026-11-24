"""Shared formatting for rule titles."""

from __future__ import annotations

from datetime import datetime

from app.services.ptw_ops.impairment_check import type_label


def fmt_dur(minutes: float | int | None) -> str:
    if minutes is None:
        return "0 m"
    m = max(0, int(minutes))
    if m < 60:
        return f"{m} m"
    h, mm = divmod(m, 60)
    if h < 48:
        return f"{h}h {mm:02d}m" if mm else f"{h}h"
    return f"{h // 24}d {h % 24}h"


def mins(a: datetime, b: datetime) -> int:
    return int((b - a).total_seconds() // 60)


def tl(t: str) -> str:
    s = type_label(t)
    return s[:1].upper() + s[1:]


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"
