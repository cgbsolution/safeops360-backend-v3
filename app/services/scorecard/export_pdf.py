"""EHS Scorecard — PDF export.

**Reuses the platform's existing shared PDF generator** (`app/services/report_pdf`)
rather than introducing a third path. That module already provides, and has in
production use for CAMS audit reports and BRSR:

  • fpdf2 — pure Python, no system binaries, which is what makes it deployable
    into an airgapped client site (WeasyPrint/wkhtmltopdf both need a browser
    engine or Qt)
  • the `_Report` base: latin-1 sanitisation of every string (fpdf2 core fonts
    are latin-1 only), running header, CONFIDENTIAL footer, page numbering,
    and a `doc_label` hook added precisely so non-audit documents could reuse it
  • `_h` / `_kv` / `_para` / `_dt` — section headings numbered by render order,
    key/value rows, and one timezone convention for every timestamp

Writing a second PDF stack for this module would have duplicated all of that,
including the latin-1 trap, and would have drifted from it within a release.

Input is `payload.build_payload()` — the same dict the dashboard renders. There
is no export-only query.
"""

from __future__ import annotations

from datetime import datetime, timezone
from collections.abc import Mapping
from typing import Any

from app.services.display_labels import label

from app.services.report_pdf import (
    GOLD,
    GOLD_INK,
    GREY,
    ICE,
    NAVY,
    NAVY_MID,
    RED,
    _dt,
    _h,
    _para,
    _Report,
    _s,
)

_UNIT_SUFFIX = {"pct": "%", "rate": "", "count": "", "score": ""}


def _fmt(value: Any, unit: str) -> str:
    """A number, or an em dash. NEVER a zero standing in for an absent value."""
    if value is None:
        return "—"
    if unit == "pct":
        return f"{value:g}%"
    if unit == "rate":
        return f"{value:g}"
    if unit == "score":
        return f"{value:g}"
    return f"{int(value):,}"


def _delta(cur: Any, prev: Any, unit: str, good: str) -> tuple[str, tuple[int, int, int]]:
    """Movement against the prior period, and the colour that judges it.

    Returns "—" in neutral grey when there is nothing to compare against. The
    deck must never imply a trend it cannot see: this is the same rule the
    dashboard's ContextKPI enforces, applied to paper.
    """
    if cur is None or prev is None:
        return ("no prior period", GREY)
    diff = round(float(cur) - float(prev), 2)
    if diff == 0:
        return ("no change", GREY)
    arrow = "up" if diff > 0 else "down"
    txt = f"{'+' if diff > 0 else '-'}{abs(diff):g}{_UNIT_SUFFIX.get(unit, '')} {arrow}"
    if good == "neutral":
        return (txt, GREY)
    improving = (diff > 0) if good == "up" else (diff < 0)
    return (txt, NAVY_MID if improving else RED)


def render_scorecard_pdf(payload: dict[str, Any], labels: Mapping[str, str] | None = None) -> bytes:
    """Render the scorecard deck. Same data, same filters, as the screen.

    `labels` are the display-label overrides for the plants in scope; every
    lookup falls through to the literal this renderer always used.
    """
    grain_label = "Monthly" if payload.get("grain") == "month" else "Quarterly"
    site_label = payload.get("siteName") or label(labels, "term.all_sites", "All sites")
    periods = payload.get("periods") or []
    current_label = periods[-1] if periods else "—"

    pdf = _Report(
        report_type=grain_label.upper(),
        audit_code=current_label,
        snapshot_hash=(payload.get("generatedAt") or "")[:19],
        doc_label="EHS Scorecard",
    )
    pdf.alias_nb_pages()
    pdf.add_page()

    # ── Cover ───────────────────────────────────────────────────────────────
    pdf.set_fill_color(*NAVY)
    pdf.rect(0, 0, 210, 52, style="F")
    pdf.set_y(16)
    pdf.set_font("Helvetica", "B", 22)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(0, 10, "EHS Scorecard", ln=1, align="C")
    pdf.set_font("Helvetica", "", 12)
    pdf.set_text_color(*GOLD)
    pdf.cell(0, 7, f"{grain_label} leading & lagging indicators", ln=1, align="C")
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, f"{site_label}  |  {current_label}", ln=1, align="C")
    pdf.set_y(60)
    pdf.set_text_color(0, 0, 0)

    if payload.get("empty"):
        pdf.set_font("Helvetica", "", 11)
        _para(pdf, 6, payload.get("message") or "No scorecard data for this selection.")
        return bytes(pdf.output())

    cov = payload.get("sourceCoverage") or {}
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*GREY)
    pdf.multi_cell(
        0, 5,
        f"Generated {_dt(payload.get('generatedAt'))}. Computed from frozen monthly rollups "
        f"({cov.get('plantMonths', 0)} {label(labels, 'scorecard.plant_months', 'plant-months')}, {cov.get('firstPeriod') or '—'} to "
        f"{cov.get('lastPeriod') or '—'}). Frequency rates are read from the Manhours module's "
        f"own submitted figures, not recomputed.",
    )
    pdf.ln(3)
    pdf.set_text_color(0, 0, 0)

    cur = payload.get("current") or {}
    prior = payload.get("prior") or {}
    indicators = payload.get("indicators") or []

    # ── Indicator tables, leading then lagging ──────────────────────────────
    for band, title in (("leading", "Leading indicators"), ("lagging", "Lagging indicators")):
        rows = [i for i in indicators if i["band"] == band]
        if not rows:
            continue
        _h(pdf, title)
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_fill_color(*ICE)
        pdf.set_text_color(*NAVY)
        pdf.cell(88, 7, "Indicator", border=0, fill=True)
        pdf.cell(32, 7, current_label, border=0, fill=True, align="R")
        pdf.cell(32, 7, "Prior", border=0, fill=True, align="R")
        pdf.cell(38, 7, "Movement", border=0, fill=True, align="R", ln=1)
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Helvetica", "", 9)
        for i in rows:
            c, p = cur.get(i["key"]), prior.get(i["key"])
            txt, colour = _delta(c, p, i["unit"], i["goodDirection"])
            pdf.cell(88, 6, i["label"], border=0)
            pdf.cell(32, 6, _fmt(c, i["unit"]), border=0, align="R")
            pdf.cell(32, 6, _fmt(p, i["unit"]), border=0, align="R")
            pdf.set_text_color(*colour)
            pdf.cell(38, 6, txt, border=0, align="R", ln=1)
            pdf.set_text_color(0, 0, 0)
        pdf.ln(4)

    # ── Trend table. A table, not a chart image: fpdf2 draws no charts and a
    #    rendered PNG would need a headless browser, which the airgap forbids.
    #    The numbers are the deliverable; the dashboard carries the chart.
    _h(pdf, f"Trend — last {len(periods)} periods")
    trend_keys = ["observationsLogged", "nearMissReported", "incidentsTotal", "ltiCount", "ltifr"]
    trend_rows = [i for i in indicators if i["key"] in trend_keys]
    col_w = min(26.0, 150.0 / max(len(periods), 1))
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_fill_color(*ICE)
    pdf.set_text_color(*NAVY)
    pdf.cell(52, 6, "Indicator", border=0, fill=True)
    for p_key in periods:
        pdf.cell(col_w, 6, p_key, border=0, fill=True, align="R")
    pdf.ln(6)
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "", 8)
    by_period = {s["period"]: s for s in payload.get("series", [])}
    for i in trend_rows:
        pdf.cell(52, 5.5, i["label"], border=0)
        for p_key in periods:
            pdf.cell(col_w, 5.5, _fmt(by_period.get(p_key, {}).get(i["key"]), i["unit"]),
                     border=0, align="R")
        pdf.ln(5.5)
    pdf.ln(4)

    # ── Per-site breakdown ──────────────────────────────────────────────────
    by_site = payload.get("bySite") or []
    if len(by_site) > 1:
        _h(pdf, f"{label(labels, 'scorecard.by_site', 'By site')} — {current_label}")
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_fill_color(*ICE)
        pdf.set_text_color(*NAVY)
        for w, t, a in ((76, label(labels, "term.site", "Site"), "L"), (24, "Obs", "R"), (24, "Near miss", "R"),
                        (24, "Incidents", "R"), (22, "LTI", "R"), (20, "LTIFR", "R")):
            pdf.cell(w, 6, t, border=0, fill=True, align=a)
        pdf.ln(6)
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Helvetica", "", 8)
        for s in by_site[:18]:
            pdf.cell(76, 5.5, (s.get("siteName") or "")[:46], border=0)
            pdf.cell(24, 5.5, _fmt(s.get("observationsLogged"), "count"), border=0, align="R")
            pdf.cell(24, 5.5, _fmt(s.get("nearMissReported"), "count"), border=0, align="R")
            pdf.cell(24, 5.5, _fmt(s.get("incidentsTotal"), "count"), border=0, align="R")
            pdf.cell(22, 5.5, _fmt(s.get("ltiCount"), "count"), border=0, align="R")
            pdf.cell(20, 5.5, _fmt(s.get("ltifr"), "rate"), border=0, align="R", ln=1)
        pdf.ln(4)

    # ── What could not be measured. Last, and never omitted. ────────────────
    gaps = payload.get("gaps") or []
    _h(pdf, "Data completeness")
    if not gaps:
        pdf.set_font("Helvetica", "", 9)
        _para(pdf, 5, "Every indicator on this scorecard was computable for the selected period.")
    else:
        pdf.set_font("Helvetica", "", 9)
        _para(pdf, 5,
              "The indicators below could NOT be computed for this period. A blank or a zero "
              "against them is an absence of data, not a result:")
        pdf.ln(1)
        for g in gaps:
            pdf.set_text_color(*GOLD_INK)
            pdf.set_font("Helvetica", "B", 9)
            pdf.cell(0, 5, f"- {g.get('indicator', 'Indicator')}", ln=1)
            pdf.set_text_color(0, 0, 0)
            pdf.set_font("Helvetica", "", 9)
            pdf.set_x(14)
            pdf.multi_cell(180, 4.6, _s(g.get("reason", "")))
            pdf.ln(1)

    if cov.get("lastPeriodWithExposure") and cov.get("lastPeriod") != cov.get("lastPeriodWithExposure"):
        pdf.ln(2)
        pdf.set_text_color(*GOLD_INK)
        pdf.set_font("Helvetica", "B", 9)
        pdf.multi_cell(
            0, 4.6,
            _s(f"Exposure data (manhours) is only recorded to {cov['lastPeriodWithExposure']}, "
               f"while activity data runs to {cov['lastPeriod']}. Frequency rates are therefore "
               f"unavailable for the most recent periods — they are not zero."),
        )
        pdf.set_text_color(0, 0, 0)

    return bytes(pdf.output())


__all__ = ["render_scorecard_pdf"]
