"""HAZOP worksheet export (XLSX, openpyxl).

Standard IEC 61882 worksheet layout, one sheet per concern:

  Study            cover: study header, team, sessions
  Worksheet        node banner (design intent, drawing, boundaries) followed by
                   that node's rows: Ref | Guideword | Parameter | Deviation |
                   Causes | Consequences | Existing safeguards | S | L | Risk |
                   Recommendations | Rec no. | Responsible | Target date |
                   Residual S | Residual L | Residual risk | Remarks
  Recommendations  the recommendation register

Input is the dict the router's /report endpoint returns, so the printed report
and the spreadsheet are built from one read. Pure: tests/test_hazop.py opens
the bytes back with openpyxl and checks the shape.
"""

from __future__ import annotations

import io
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from app.services import pha_library as lib

WORKSHEET_COLUMNS: list[tuple[str, int]] = [
    ("Ref", 7), ("Guideword", 12), ("Parameter", 13), ("Deviation", 26), ("Causes", 34), ("Consequences", 36),
    ("Existing safeguards", 34), ("S", 5), ("L", 5), ("Risk", 11), ("Recommendations", 38), ("Rec no.", 9),
    ("Responsible", 18), ("Target date", 12), ("Residual S", 7), ("Residual L", 7), ("Residual risk", 11), ("Remarks", 26),
]
REC_COLUMNS: list[tuple[str, int]] = [
    ("Rec no.", 9), ("Node", 26), ("Deviation", 30), ("Recommendation", 50), ("Priority", 10), ("Responsible", 20),
    ("Target date", 12), ("Status", 12), ("CAPA", 22), ("Justification / closure note", 40),
]

_THIN = Side(style="thin", color="94A3B8")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_HEAD_FILL = PatternFill("solid", fgColor="1E3A5F")
_NODE_FILL = PatternFill("solid", fgColor="E2E8F0")
_WRAP = Alignment(wrap_text=True, vertical="top")
_CENTER = Alignment(horizontal="center", vertical="top", wrap_text=True)


def _fill(hex_color: str | None) -> PatternFill | None:
    if not hex_color:
        return None
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return None
    return PatternFill("solid", fgColor=h.upper())


def _d(iso: str | None) -> str:
    return (iso or "")[:10]


def _header(ws, row: int, cols: list[tuple[str, int]]) -> None:
    for i, (name, width) in enumerate(cols, start=1):
        c = ws.cell(row=row, column=i, value=name)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = _HEAD_FILL
        c.alignment = _CENTER
        c.border = _BORDER
        ws.column_dimensions[get_column_letter(i)].width = width


def _risk_cell(ws, row: int, col: int, level: str | None, score: int | None, color: str | None) -> None:
    c = ws.cell(row=row, column=col, value=f"{level} ({score})" if level else "")
    c.alignment = _CENTER
    c.border = _BORDER
    f = _fill(color)
    if f is not None:
        c.fill = f
        c.font = Font(bold=True)


def build_workbook(report: dict[str, Any]) -> Workbook:
    study = report["study"]
    wb = Workbook()

    # ── Study (cover) ────────────────────────────────────────────────────────
    ws = wb.active
    ws.title = "Study"
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 70
    ws["A1"] = f"{study.get('methodLabel') or study.get('method')} STUDY"
    ws["A1"].font = Font(bold=True, size=14)
    info = [
        ("Study no.", study.get("studyNo")), ("Title", study.get("title")), ("Plant", study.get("plantName")),
        ("Unit", study.get("unit")), ("Area", study.get("area")), ("Method", study.get("methodLabel") or study.get("method")),
        ("Status", study.get("status")), ("Scope", study.get("scope")),
        ("Design intent (summary)", study.get("designIntentSummary")),
        ("Drawings (P&ID / PFD)", ", ".join(study.get("drawingRefs") or [])),
        ("Risk matrix", (report.get("matrix") or {}).get("name")),
        ("Facilitator", study.get("facilitatorName")), ("Scribe", study.get("scribeName")),
        ("Approver", study.get("approverName")), ("Approved on", _d(study.get("approvedAt"))),
        ("Revalidation due", _d(study.get("revalidationDueDate"))), ("MOC reference", study.get("mocId")),
    ]
    r = 3
    for k, v in info:
        ws.cell(row=r, column=1, value=k).font = Font(bold=True)
        c = ws.cell(row=r, column=2, value=v or "")
        c.alignment = _WRAP
        r += 1
    r += 1
    ws.cell(row=r, column=1, value="Team").font = Font(bold=True, size=12)
    r += 1
    for m in report.get("team", []):
        ws.cell(row=r, column=1, value=m.get("roleLabel") or m.get("role"))
        ws.cell(row=r, column=2, value=" · ".join(x for x in (m.get("name"), m.get("discipline"), m.get("organisation")) if x))
        r += 1
    r += 1
    ws.cell(row=r, column=1, value="Sessions").font = Font(bold=True, size=12)
    r += 1
    for s in report.get("sessions", []):
        ws.cell(row=r, column=1, value=f"Session {s.get('sessionNo')} · {_d(s.get('sessionDate'))}")
        att = ", ".join([a.get("name") for a in s.get("attendees", [])] + list(s.get("attendeeNames") or []))
        dur = f"{s.get('durationMinutes')} min" if s.get("durationMinutes") else ""
        c = ws.cell(row=r, column=2, value=" | ".join(x for x in (dur, f"Nodes: {', '.join(s.get('nodeLabels') or [])}", f"Attendees: {att}", s.get("notes") or "") if x))
        c.alignment = _WRAP
        r += 1

    # ── Worksheet ────────────────────────────────────────────────────────────
    ws = wb.create_sheet("Worksheet")
    ncol = len(WORKSHEET_COLUMNS)
    ws.cell(row=1, column=1, value=f"{study.get('studyNo')} · {study.get('title')}").font = Font(bold=True, size=13)
    ws.cell(row=2, column=1, value=f"{study.get('plantName') or ''} · {study.get('unit') or ''} · Drawings: {', '.join(study.get('drawingRefs') or []) or '-'}")
    _header(ws, 4, WORKSHEET_COLUMNS)
    ws.freeze_panes = "A5"
    row = 5
    for node in report.get("nodes", []):
        banner = (
            f"Node {node.get('nodeNo')}: {node.get('title')}"
            f"   |   Design intent: {node.get('designIntent') or '-'}"
            f"   |   Drawing: {node.get('drawingRef') or '-'}"
            f"   |   Boundaries: {node.get('boundaries') or '-'}"
        )
        params = node.get("operatingParameters") or []
        if params:
            banner += "   |   Parameters: " + "; ".join(
                f"{p.get('name')}: {p.get('value')} {p.get('unit') or ''}".strip() for p in params if isinstance(p, dict)
            )
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=ncol)
        c = ws.cell(row=row, column=1, value=banner)
        c.font = Font(bold=True)
        c.fill = _NODE_FILL
        c.alignment = _WRAP
        ws.row_dimensions[row].height = 30
        row += 1
        for i, dev in enumerate(node.get("deviations", []), start=1):
            recs = dev.get("recommendations", [])
            vals = [
                f"{node.get('nodeNo')}.{i}", lib.guideword_label(dev.get("guideword")), lib.parameter_label(dev.get("parameter")),
                dev.get("deviation"), dev.get("causes"), dev.get("consequences"), dev.get("safeguards"),
                dev.get("severity"), dev.get("likelihood"), None,
                "\n".join(r_.get("text") or "" for r_ in recs),
                "\n".join(r_.get("recLabel") or "" for r_ in recs),
                "\n".join(r_.get("ownerName") or "-" for r_ in recs),
                "\n".join(_d(r_.get("dueDate")) or "-" for r_ in recs),
                dev.get("residualSeverity"), dev.get("residualLikelihood"), None, dev.get("remarks"),
            ]
            for col, v in enumerate(vals, start=1):
                cell = ws.cell(row=row, column=col, value=v if v not in (None, "") else None)
                cell.border = _BORDER
                cell.alignment = _CENTER if col in (1, 8, 9, 12, 14, 15, 16) else _WRAP
            _risk_cell(ws, row, 10, dev.get("riskLevel"), dev.get("riskScore"), dev.get("riskColor"))
            _risk_cell(ws, row, 17, dev.get("residualLevel"), dev.get("residualScore"), dev.get("residualColor"))
            row += 1
        if not node.get("deviations"):
            ws.cell(row=row, column=1, value="(no worksheet rows)").font = Font(italic=True, color="64748B")
            row += 1

    # ── Recommendations ──────────────────────────────────────────────────────
    ws = wb.create_sheet("Recommendations")
    _header(ws, 1, REC_COLUMNS)
    ws.freeze_panes = "A2"
    for i, rec in enumerate(report.get("recommendations", []), start=2):
        note = rec.get("justification") if rec.get("status") == "REJECTED" else rec.get("closureNote")
        vals = [
            rec.get("recLabel"), rec.get("nodeLabel"), rec.get("deviationText"), rec.get("text"), rec.get("priority"),
            rec.get("ownerName"), _d(rec.get("dueDate")), rec.get("status"),
            " ".join(x for x in (rec.get("capaNumber"), f"({rec.get('capaState')})" if rec.get("capaState") else None) if x),
            note,
        ]
        for col, v in enumerate(vals, start=1):
            cell = ws.cell(row=i, column=col, value=v or None)
            cell.border = _BORDER
            cell.alignment = _WRAP
    return wb


def worksheet_xlsx(report: dict[str, Any]) -> bytes:
    buf = io.BytesIO()
    build_workbook(report).save(buf)
    return buf.getvalue()
