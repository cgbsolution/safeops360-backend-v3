"""Step 1: turn an observation register export (.xlsx) into de-identified rows.

    python -I prepare_observations.py <register.xlsx> <out_dir> [--chunks 10]

Writes to <out_dir>:
  rows.json              one object per row, WITHOUT observer / area-in-charge /
                         task-executor / individual names or employee ids
  names.json             every person name in the register — only for the PII scan
                         in build_library.py; never ship it
  chunks/chunk_NN.jsonl  the rows split for review (see REVIEW_BRIEF.md)

Expected columns (the plant's export): Inspection Code, Created Date, Observation
Type, Department Code, Area, Section, Location, Category Code, Category,
Observation, Action Plan, Severity, Risk Level, Function, Target Date, Status,
Special Category, Impacted Individual, Closing Remarks, Closing Rejection Remarks,
Exceptional Remarks, Approve Remarks.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
from pathlib import Path

import openpyxl

NAME_COLUMNS = ("Observer Name", "Area I/C Name", "Task Executor Name", "Individual Name")


def clean(v):
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v.strftime("%Y-%m-%d")
    v = " ".join(str(v).split())
    return v or None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx")
    ap.add_argument("out")
    ap.add_argument("--chunks", type=int, default=10)
    a = ap.parse_args()
    wb = openpyxl.load_workbook(a.xlsx, read_only=True, data_only=True)
    raw = list(wb.worksheets[0].iter_rows(values_only=True))
    hdr = list(raw[0])
    data = [dict(zip(hdr, r)) for r in raw[1:] if any(r)]

    names = sorted({str(d[k]).strip() for d in data for k in NAME_COLUMNS if d.get(k)})
    rows = []
    for d in data:
        t, c = d.get("Target Date"), d.get("Created Date")
        rows.append({
            "code": d["Inspection Code"], "date": clean(c), "type": d.get("Observation Type"), "dept": d.get("Department Code"),
            "area": clean(d.get("Area")), "section": clean(d.get("Section")), "location": clean(d.get("Location")),
            "cat": d.get("Category Code"), "category": d.get("Category"), "obs": clean(d.get("Observation")),
            "action": clean(d.get("Action Plan")), "severity": d.get("Severity"), "risk": d.get("Risk Level"),
            "function": d.get("Function"),
            "target_days": (t - c).days if isinstance(t, dt.datetime) and isinstance(c, dt.datetime) else None,
            "status": d.get("Status"), "special": d.get("Special Category"), "impacted": d.get("Impacted Individual"),
            "closing": clean(d.get("Closing Remarks")), "closing_rejected": clean(d.get("Closing Rejection Remarks")),
            "exceptional": clean(d.get("Exceptional Remarks")), "approve": clean(d.get("Approve Remarks")),
        })
    dup = collections.Counter((r["obs"] or "").lower() for r in rows)
    for r in rows:
        r["dup_text_count"] = dup[(r["obs"] or "").lower()]

    out = Path(a.out)
    (out / "chunks").mkdir(parents=True, exist_ok=True)
    (out / "rows.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    (out / "names.json").write_text(json.dumps(names, ensure_ascii=False), encoding="utf-8")
    size = (len(rows) + a.chunks - 1) // a.chunks
    for i in range(a.chunks):
        with open(out / "chunks" / f"chunk_{i + 1:02d}.jsonl", "w", encoding="utf-8") as f:
            for r in rows[i * size:(i + 1) * size]:
                f.write(json.dumps({k: v for k, v in r.items() if v is not None}, ensure_ascii=False) + "\n")
    print(f"{len(rows)} rows, {len(names)} names, {a.chunks} chunks of {size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
