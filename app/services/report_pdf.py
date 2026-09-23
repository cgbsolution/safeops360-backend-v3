"""P2-9 — Audit report PDF generation (fpdf2; pure-Python, no system deps).

Renders an AuditReport's immutable snapshot to a branded A4 PDF: cover page,
INTERIM 'PROVISIONAL' watermark on every page, category compliance, findings
register, CAPA summary, sign-off block (FINAL), page numbers + confidential footer.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from collections.abc import Mapping
from typing import Any

from app.services.display_labels import label as _lbl
from zoneinfo import ZoneInfo

from fpdf import FPDF

# No tenant-level timezone setting exists anywhere in the platform (checked:
# app/core/config.py has none), and every deployment so far is India-based —
# the checkpoint library cites the Factories Act. So: IST by default, overridable
# per deployment, and never a bare UTC timestamp sitting next to a local one.
_TZ_NAME = os.environ.get("REPORT_TIMEZONE", "Asia/Kolkata")
REPORT_TZ_LABEL = os.environ.get("REPORT_TIMEZONE_LABEL", "IST")
try:
    REPORT_TZ = ZoneInfo(_TZ_NAME)
except Exception:  # noqa: BLE001 — a bad env var must not break report generation
    REPORT_TZ, REPORT_TZ_LABEL = timezone.utc, "UTC"

_REPL = {"—": "-", "–": "-", "‘": "'", "’": "'", "“": '"', "”": '"',
         "•": "*", "₹": "Rs ", "→": "->", "≥": ">=", "≤": "<=", " ": " "}


def _s(text: Any) -> str:
    """Sanitise to latin-1 (fpdf2 core fonts) — map common Unicode then drop the rest."""
    t = str(text)
    for k, v in _REPL.items():
        t = t.replace(k, v)
    return t.encode("latin-1", "replace").decode("latin-1")


# Midnight Executive, as RGB tuples for fpdf2. Generated from
# src/lib/design/midnight.ts on the frontend — one design system, two runtimes.
#
# These constants were the platform's last off-brand surface: `PURPLE` (88,28,135)
# titled every section of every audit and BRSR report, and `AMBER` (230,126,34) is
# a plain orange. Both are gone. **This changes the CAMS audit report and the BRSR
# report too** — deliberately, and the same call as retinting `primary` and
# `PageHeader`: a design system that stops at the screen edge is not one.
NAVY = (11, 31, 77)          # #0B1F4D — the brand navy
NAVY_MID = (40, 74, 148)     # #284A94 — readable navy for links/keys
GOLD = (201, 169, 97)        # #C9A961 — the brand gold
GOLD_INK = (124, 94, 7)      # #7C5E07 — gold that is legible as text (6.07:1)
ICE = (232, 238, 247)        # #E8EEF7 — the brand ice, a surface
PURPLE = NAVY                # kept as a name so no caller breaks; now navy
GREY = (91, 105, 128)        # #5B6980 — Midnight muted ink
LIGHT = ICE
RED = (155, 44, 44)          # #9B2C2C — the reserved status crimson
AMBER = GOLD_INK             # no orange anywhere in the design system
GREEN = (40, 74, 148)        # a "good" reading is navy here, not green


def _rag(pct: float | None) -> tuple[int, int, int]:
    if pct is None:
        return GREY
    return GREEN if pct >= 85 else (AMBER if pct >= 70 else RED)


class _Report(FPDF):
    # `doc_label` keeps this class reusable for documents that are not audits.
    # It defaults to "Audit Report" so every existing caller renders byte-for-byte
    # as before; BRSR passes its own label rather than forking the class (and
    # inheriting a second copy of the latin-1 sanitisation, watermark and footer).
    def __init__(
        self,
        report_type: str,
        audit_code: str,
        snapshot_hash: str,
        *,
        doc_label: str = "Audit Report",
    ):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.report_type = (report_type or "").upper()
        self.audit_code = audit_code
        self.snapshot_hash = snapshot_hash
        self.doc_label = doc_label
        self.set_auto_page_break(auto=True, margin=20)
        self.set_title(_s(f"{doc_label} {audit_code}"))

    # Centralised sanitisation — fpdf2 core fonts are latin-1 only.
    def cell(self, *a, **k):  # type: ignore[override]
        if len(a) >= 3 and isinstance(a[2], str):
            a = (a[0], a[1], _s(a[2])) + a[3:]
        for key in ("txt", "text"):
            if key in k and isinstance(k[key], str):
                k[key] = _s(k[key])
        return super().cell(*a, **k)

    def multi_cell(self, *a, **k):  # type: ignore[override]
        if len(a) >= 3 and isinstance(a[2], str):
            a = (a[0], a[1], _s(a[2])) + a[3:]
        for key in ("txt", "text"):
            if key in k and isinstance(k[key], str):
                k[key] = _s(k[key])
        return super().multi_cell(*a, **k)

    def text(self, x, y, txt=""):  # type: ignore[override]
        return super().text(x, y, _s(txt))

    def header(self):
        if self.page_no() == 1:
            return
        self.set_font("Helvetica", "B", 9)
        self.set_text_color(*NAVY)
        self.cell(0, 8, f"SafeOps360 — {self.doc_label} {self.audit_code}", border=0, ln=0, align="L")
        self.cell(0, 8, self.report_type, border=0, ln=1, align="R")
        self.set_draw_color(*LIGHT)
        self.line(10, 18, 200, 18)
        self.ln(4)
        if self.report_type == "INTERIM":
            self._watermark()

    def _watermark(self):
        self.set_text_color(230, 210, 210)
        self.set_font("Helvetica", "B", 50)
        with self.rotation(45, x=105, y=150):
            self.text(55, 150, "PROVISIONAL")
        self.set_text_color(0, 0, 0)

    def footer(self):
        self.set_y(-15)
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(*GREY)
        self.cell(0, 6, "CONFIDENTIAL", border=0, ln=0, align="L")
        self.cell(0, 6, f"Page {self.page_no()} of {{nb}}", border=0, ln=0, align="C")
        self.cell(0, 6, f"hash {self.snapshot_hash[:12]}", border=0, ln=1, align="R")


def _h(pdf: _Report, text: str):
    """Section heading, numbered by RENDER ORDER.

    The numbers used to be hardcoded into the strings ("1. Executive Summary" …
    "12. Record Integrity") while six of the twelve sections are conditional, so
    a report that suppressed Independence and Clause Index printed 8 → 10 → 12
    and read as though pages were missing. Counting here means the number can
    only ever describe what actually rendered — on interim and final alike.
    """
    pdf._section_no = getattr(pdf, "_section_no", 0) + 1
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(*PURPLE)
    pdf.cell(0, 8, f"{pdf._section_no}. {text}", border=0, ln=1)
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "", 10)


def _human(value: str | None) -> str:
    """`integrated_compliance_audit` -> `Integrated Compliance Audit`."""
    if not value:
        return "—"
    return " ".join(w.capitalize() for w in str(value).replace("_", " ").split())


def _who(user_id: str | None, names: dict[str, str] | None) -> str:
    """A person's name, never a raw id.

    Falls back to nothing rather than printing a cuid: an unresolved id in a
    report is noise a reader cannot act on, and a blank is honest.
    """
    if not user_id:
        return ""
    return (names or {}).get(user_id) or ""


def _dt(value: str | None, *, with_time: bool = True) -> str:
    """ISO -> `22 Jul 2026, 09:00 IST`.

    The cover already printed `Generated: … UTC` beside raw ISO strings like
    `2026-07-22T03:30:00`, i.e. two conventions on one page. Everything the
    report renders now goes through here, in the tenant's timezone.
    """
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(REPORT_TZ)
    return local.strftime("%d %b %Y, %H:%M " + REPORT_TZ_LABEL) if with_time else local.strftime("%d %b %Y")


def render_audit_report_pdf(
    report: dict[str, Any],
    generated_by_name: str = "—",
    register: list[dict[str, Any]] | None = None,
    user_names: dict[str, str] | None = None,
    register_truncated: int = 0,
    labels: Mapping[str, str] | None = None,
) -> bytes:
    """Render the report.

    `labels`: display-label overrides for the audited site (fall through to literals).

    `register` is the full checkpoint register, passed in by the caller because
    it is deliberately NOT stored in the snapshot (a 1,500-checkpoint audit
    would bloat every read of the report row). `user_names` resolves the owner
    and actor ids the register carries, so the PDF never prints a raw cuid.
    """
    snap: dict[str, Any] = report.get("snapshot") or {}
    rtype = report.get("reportType") or snap.get("reportType") or "INTERIM"
    code = snap.get("auditCode") or report.get("reportCode") or "—"
    pdf = _Report(rtype, code, report.get("id", ""))
    pdf.alias_nb_pages()

    # ── Cover page ──
    pdf.add_page()
    pdf.set_fill_color(*NAVY)
    pdf.rect(0, 0, 210, 45, style="F")
    pdf.set_y(14)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 22)
    pdf.cell(0, 12, "SafeOps360", border=0, ln=1, align="C")
    pdf.set_font("Helvetica", "", 12)
    pdf.cell(0, 7, "Audit & Compliance Report", border=0, ln=1, align="C")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(20)
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_x(10)
    pdf.multi_cell(0, 9, snap.get("title") or "Audit Report", align="C")
    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 13)
    badge = RED if rtype.upper() == "INTERIM" else GREEN
    pdf.set_text_color(*badge)
    pdf.cell(0, 8, f"{rtype.upper()} REPORT" + (" — PROVISIONAL, SUBJECT TO CHANGE" if rtype.upper() == "INTERIM" else ""), border=0, ln=1, align="C")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(6)
    pdf.set_font("Helvetica", "", 11)
    now = datetime.now(REPORT_TZ).strftime("%d %b %Y, %H:%M " + REPORT_TZ_LABEL)
    for label, val in [
        ("Audit Code", code),
        # `integrated_compliance_audit` is a storage key, not a label.
        ("Audit Type", _human(snap.get("auditType"))),
        # The snapshot has carried the resolved `plantName` since WP-12; this
        # renderer was still printing the raw CUID from `siteId`.
        (_lbl(labels, "term.site", "Site"), snap.get("plantName") or snap.get("siteId") or "—"),
        ("Planned", _dt(snap.get("plannedDate"), with_time=False)),
        ("Closed", _dt(snap.get("closedAt"))),
        ("Generated", now), ("Generated by", generated_by_name),
    ]:
        pdf.cell(50, 7, f"{label}:", border=0, ln=0)
        pdf.cell(0, 7, _s(str(val))[:80], border=0, ln=1)
    pdf.ln(4)

    # ── Headline verdict, or an honest refusal to give one ──────────────
    # Below the coverage floor no grade renders at all: "100.0% (CONFORMING)"
    # over 1 of 82 checkpoints is the 78.9%-over-0-of-82 defect with a
    # disclaimer nobody reading the cover will see. The replacement occupies
    # the same position and weight — stated, not demoted.
    pct = snap.get("overallScorePct")
    grade = snap.get("grade") or {}
    pdf.set_font("Helvetica", "B", 14)
    if grade and not grade.get("showGrade", True):
        pdf.set_text_color(120, 120, 120)
        pdf.cell(0, 10, _s(f"{grade.get('label', 'Insufficient coverage')} - no grade issued"),
                 border=0, ln=1)
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Helvetica", "", 9)
        pdf.multi_cell(0, 5, _s(
            f"Coverage is below the {grade.get('threshold', 20):g}% minimum required for a "
            "compliance grade. No overall percentage or conformance verdict is issued for this "
            "report."
        ))
    else:
        pdf.set_text_color(*_rag(pct))
        assessed_frac = (
            f"   [{grade.get('assessed')} of {grade.get('applicable')} assessed]"
            if grade else ""
        )
        pdf.cell(0, 10, _s(
            f"Overall compliance: {pct if pct is not None else '-'}%   "
            f"({snap.get('overallResult') or '-'}){assessed_frac}"
        ), border=0, ln=1)
        pdf.set_text_color(0, 0, 0)
        # The rule behind the verdict (F-22) — a number without its rule is not
        # a result.
        gate = snap.get("gate") or {}
        if gate.get("explanation"):
            pdf.set_font("Helvetica", "", 9)
            pdf.multi_cell(0, 5, _s(gate["explanation"]))
    pdf.set_text_color(0, 0, 0)

    # ── Executive summary ──
    pdf.add_page()
    _h(pdf, "Executive Summary")
    pdf.set_x(10)
    # "Open iterations" counts findings awaiting a response. It used to be
    # derived from `not _is_terminal(...)`, which made every UNASSESSED
    # checkpoint an open iteration — hence "Open iterations 81" on an audit
    # whose Findings Register correctly read 0. Not-yet-started is now reported
    # as its own number, because that is what a reader actually wants to know.
    _not_assessed = snap.get("notAssessedCount")
    pdf.multi_cell(0, 6, (
        f"Checkpoints assessed: {snap.get('checkpointsAssessed', 0)} of {snap.get('checkpointsTotal', 0)}. "
        f"Pass {snap.get('passCount', 0)}, Fail {snap.get('failCount', 0)}, Partial {snap.get('partialCount', 0)}, N/A {snap.get('naCount', 0)}. "
        f"Failures by severity — Critical {snap.get('criticalFailures', 0)}, Major {snap.get('majorFailures', 0)}, Minor {snap.get('minorFailures', 0)}. "
        f"Findings awaiting response: {snap.get('openIterationsCount', 0)} ({snap.get('criticalOpenCount', 0)} critical)."
        + (f" Not yet assessed: {_not_assessed}." if _not_assessed else "")
    ))
    pdf.ln(3)

    # ── Scope, methodology & limitations (WP-12) ──
    # A certification body reads this BEFORE the numbers. The limitations list is
    # what earns trust: a report that states what it could not establish is more
    # credible than one implying total coverage.
    meth = snap.get("methodology") or {}
    if meth:
        _h(pdf, "Scope, Methodology & Limitations")
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(0, 5, "Audit criteria", border=0, ln=1)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_x(10)
        pdf.multi_cell(0, 5, ", ".join(meth.get("criteria") or ["Not specified"]))
        if meth.get("scopeDescription"):
            pdf.ln(1)
            pdf.set_font("Helvetica", "B", 9)
            pdf.cell(0, 5, "Scope", border=0, ln=1)
            pdf.set_font("Helvetica", "", 9)
            pdf.set_x(10)
            pdf.multi_cell(0, 5, _s(meth["scopeDescription"]))
        pdf.ln(1)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(0, 5, "Method", border=0, ln=1)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_x(10)
        pdf.multi_cell(0, 5, _s(meth.get("method") or "—"))
        pdf.ln(1)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(0, 5, "Limitations", border=0, ln=1)
        pdf.set_font("Helvetica", "", 9)
        for lim in meth.get("limitations") or []:
            pdf.set_x(10)
            pdf.multi_cell(0, 5, f"-  {_s(lim)}")
        pdf.ln(2)

    # ── Auditor independence (docs/cams/09 §2.1.6) ──
    # Asserts absence explicitly. A reader must be able to tell "none issued"
    # from "not tracked", and only a sentence does that.
    ind = snap.get("independence") or {}
    if ind:
        _h(pdf, "Auditor Independence")
        pdf.set_font("Helvetica", "", 9)
        pdf.set_x(10)
        pdf.multi_cell(0, 5, _s(ind.get("statement") or "—"))
        for w in ind.get("waivers") or []:
            pdf.ln(1)
            pdf.set_font("Helvetica", "B", 8)
            pdf.set_text_color(*RED)
            pdf.set_x(10)
            pdf.multi_cell(0, 5, f"Waiver — {_s(w.get('subject'))}")
            pdf.set_text_color(0, 0, 0)
            pdf.set_font("Helvetica", "", 8)
            if w.get("conflict"):
                pdf.set_x(10)
                pdf.multi_cell(0, 4.5, f"Conflict: {_s(w['conflict'])}")
            pdf.set_x(10)
            pdf.multi_cell(0, 4.5, f"Justification: {_s(w.get('justification'))}")
            pdf.set_x(10)
            pdf.multi_cell(0, 4.5, f"Approved by {_s(w.get('approvedBy'))} on {_s(w.get('approvedAt'))}")
        pdf.ln(2)

    # ── Opening & closing meetings (ISO 19011 §6.4) ──
    mtg = snap.get("meetings") or {}
    if mtg:
        _h(pdf, "Opening & Closing Meetings")
        pdf.set_font("Helvetica", "", 9)
        for key, label in (("opening", "Opening meeting"), ("closing", "Closing meeting")):
            m = mtg.get(key) or {}
            pdf.set_font("Helvetica", "B", 9)
            pdf.cell(0, 5, label, border=0, ln=1)
            pdf.set_font("Helvetica", "", 8)
            if not m.get("recorded"):
                # Never assert a meeting the product has no record of.
                pdf.set_x(10)
                pdf.multi_cell(0, 4.5, f"No {label.lower()} was recorded.")
            else:
                pdf.set_x(10)
                pdf.multi_cell(0, 4.5, f"Held: {_s(m.get('heldAt'))}")
                names = ", ".join(a.get("name", "") for a in (m.get("attendees") or []))
                pdf.set_x(10)
                pdf.multi_cell(0, 4.5, f"Attendees: {_s(names) or '-'}")
                if key == "opening" and m.get("scopeConfirmed"):
                    pdf.set_x(10)
                    pdf.multi_cell(0, 4.5, "Scope and criteria confirmed with the auditee.")
                if key == "closing":
                    pdf.set_x(10)
                    pdf.multi_cell(
                        0, 4.5,
                        f"Auditee acknowledgement: {_s(m.get('auditeeAcknowledgedBy')) or 'not recorded'}",
                    )
            pdf.ln(1)
        pdf.ln(1)

    # ── Category compliance (RAG) ──
    # Two defects here. (a) The keys were wrong: the snapshot writes
    # `category_name` / `score_pct` (from `_compute_score`) and this read
    # `category` / `scorePct`, so EVERY row rendered "- / -" regardless of data.
    # (b) Rendering ten empty rows at all is a zero-state chart, which Appendix D
    # bans — one honest sentence replaces it until there is something to show.
    cats = snap.get("categoryScores") or []
    if isinstance(cats, dict):
        cats = [{"category_name": k, **(v if isinstance(v, dict) else {"score_pct": v})}
                for k, v in cats.items()]

    def _cat_pct(c: dict[str, Any]) -> float | None:
        assessed = (c.get("passed", 0) or 0) + (c.get("partial", 0) or 0) + (c.get("failed", 0) or 0)
        if not assessed:
            return None
        v = c.get("score_pct", c.get("scorePct", c.get("score")))
        return v if isinstance(v, (int, float)) else None

    scored = [c for c in cats if _cat_pct(c) is not None]
    if scored:
        _h(pdf, "Category-wise Compliance")
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_fill_color(*LIGHT)
        pdf.cell(120, 7, "Category", border=1, ln=0, fill=True)
        pdf.cell(30, 7, "Assessed", border=1, ln=0, fill=True, align="C")
        pdf.cell(30, 7, "Score %", border=1, ln=1, fill=True, align="C")
        pdf.set_font("Helvetica", "", 9)
        for c in scored[:30]:
            name = str(c.get("category_name") or c.get("category") or c.get("name") or "-")[:52]
            sc = _cat_pct(c)
            done = (c.get("passed", 0) or 0) + (c.get("partial", 0) or 0) + (c.get("failed", 0) or 0)
            pdf.cell(120, 6, name, border=1, ln=0)
            pdf.cell(30, 6, f"{done} of {c.get('total', done)}", border=1, ln=0, align="C")
            pdf.set_text_color(*_rag(sc))
            pdf.cell(30, 6, f"{sc}", border=1, ln=1, align="C")
            pdf.set_text_color(0, 0, 0)
        pdf.ln(3)
    elif cats:
        _h(pdf, "Category-wise Compliance")
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(*GREY)
        pdf.set_x(10)
        pdf.multi_cell(0, 5, "Category-level compliance will appear once assessment begins.")
        pdf.set_text_color(0, 0, 0)
        pdf.ln(3)

    # ── Findings register ──
    _h(pdf, "Findings Register")
    findings = snap.get("findings") or []
    if not findings:
        pdf.cell(0, 6, "No findings recorded.", border=0, ln=1)
    else:
        # One block per finding.
        #
        # This section used to print "[major]  -" and a bare "-" for every row.
        # It read `standardClauseRef`/`clause` and `title`/`description`, none of
        # which the snapshot has ever produced — `_build_report_snapshot` writes
        # `standard`, `requirementReference`, `question` and `observation`. Four
        # key names, zero matches, so every finding rendered as two dashes and
        # the register was worthless. The keys below are the ones the snapshot
        # actually emits; the legacy names are kept as fallbacks so an old
        # stored snapshot still renders.
        for f in findings:
            sev = str(f.get("severity") or "-")
            code_ = str(f.get("checkpointCode") or "-")
            disc = str(f.get("discipline") or "")
            result = str(f.get("assessmentStatus") or "")
            adverse = "CRIT" in sev.upper() or "MAJOR" in sev.upper() or result == "FAIL"

            pdf.set_x(10)
            pdf.set_font("Helvetica", "B", 8.5)
            pdf.set_text_color(*(RED if adverse else AMBER))
            head = f"{code_}   [{sev[:16]}]"
            if result:
                head += f"   {result}"
            if disc:
                head += f"   -   {disc}"
            pdf.multi_cell(190, 5, _s(head), border=0)
            pdf.set_text_color(0, 0, 0)

            # The requirement being assessed.
            pdf.set_font("Helvetica", "", 8)
            pdf.set_x(10)
            pdf.multi_cell(190, 4.5, _s(f.get("question") or f.get("title") or f.get("description") or "-"), border=0)

            # What the auditor actually saw. Without this the "finding" is only
            # a question with a verdict attached, which is not a finding.
            obs = (f.get("observation") or "").strip()
            pdf.set_font("Helvetica", "I", 8)
            pdf.set_text_color(*GREY)
            pdf.set_x(12)
            pdf.multi_cell(188, 4.5, _s(f"Observation: {obs or 'None recorded.'}"), border=0)

            meta = []
            std = f.get("standard") or f.get("standardClauseRef") or f.get("clause")
            ref = f.get("requirementReference")
            if std:
                meta.append(f"Standard: {std}")
            if ref:
                meta.append(f"Clause: {ref}")
            if f.get("workflowState"):
                meta.append(f"State: {_human(f.get('workflowState'))} (round {f.get('round', 0)})")
            owner = _who(f.get("ownerId"), user_names)
            if owner:
                meta.append(f"Owner: {owner}")
            if f.get("capaNumber"):
                meta.append(f"CAPA: {f['capaNumber']} ({f.get('capaStatus') or 'open'})")
            if f.get("isAdHoc"):
                meta.append("Ad-hoc checkpoint")
            if meta:
                pdf.set_font("Helvetica", "", 7.5)
                pdf.set_x(12)
                pdf.multi_cell(188, 4, _s("   |   ".join(meta)), border=0)
            pdf.set_text_color(0, 0, 0)
            pdf.ln(2)
    pdf.ln(3)

    # ── CAPA summary ──
    _h(pdf, "CAPA Summary")
    cs = snap.get("capaSummary") or {}
    pdf.cell(0, 6, f"Total CAPAs: {cs.get('total', 0)}   Open: {cs.get('open', 0)}   Overdue: {cs.get('overdue', 0)}", border=0, ln=1)
    pdf.ln(3)

    # ── Clause index (WP-12) ──
    # The index an assessor navigates by. Worst clauses first: they open this to
    # find problems, not to read A-Z.
    clauses = snap.get("clauseIndex") or []
    if clauses:
        pdf.add_page()
        _h(pdf, "Clause Index")

        # Provenance caveat, printed BEFORE the table rather than as a trailing
        # note. Most of this library's citations are AI drafts, and the index
        # cannot distinguish them from sourced ones — a reader who takes the
        # clause column as verified fact has been misled by the time they reach
        # a footnote underneath it.
        _foot = ((snap.get("citationProvenance") or {}).get("footnote")) or None
        if _foot:
            pdf.set_font("Helvetica", "I", 8)
            pdf.set_text_color(*RED)
            pdf.set_x(10)
            pdf.multi_cell(0, 4.4, _s(_foot.get("statement")))
            pdf.set_text_color(0, 0, 0)
            pdf.ln(1.5)

        pdf.set_font("Helvetica", "B", 8)
        pdf.set_fill_color(*LIGHT)
        for w, t in ((45, "Standard"), (60, "Clause"), (15, "CPs"), (15, "Pass"),
                     (15, "Fail"), (15, "Part."), (15, "N/A")):
            pdf.cell(w, 6, t, border=1, ln=0, fill=True, align="C" if w < 40 else "L")
        pdf.ln()
        pdf.set_font("Helvetica", "", 8)
        for e in clauses[:70]:
            pdf.cell(45, 5.5, _s(e.get("standard"))[:30], border=1, ln=0)
            pdf.cell(60, 5.5, _s(e.get("clause"))[:42], border=1, ln=0)
            pdf.cell(15, 5.5, str(e.get("total", 0)), border=1, ln=0, align="C")
            pdf.cell(15, 5.5, str(e.get("pass", 0)), border=1, ln=0, align="C")
            fail = e.get("fail", 0)
            pdf.set_text_color(*(RED if fail else GREY))
            pdf.cell(15, 5.5, str(fail), border=1, ln=0, align="C")
            pdf.set_text_color(0, 0, 0)
            pdf.cell(15, 5.5, str(e.get("partial", 0)), border=1, ln=0, align="C")
            pdf.cell(15, 5.5, str(e.get("na", 0)), border=1, ln=1, align="C")
        if len(clauses) > 70:
            pdf.set_font("Helvetica", "I", 8)
            pdf.cell(0, 5, f"... {len(clauses) - 70} further clause row(s) in the register.", ln=1)
        pdf.ln(3)

    # ── Full checkpoint register ──
    #
    # Every checkpoint, not only the ones that failed. A reader who wants to
    # know what was assessed and found compliant had no way to see it: the PDF
    # printed the findings and stopped, so a 19-checkpoint audit showed 3 rows
    # and the other 16 existed nowhere in the download. The register is passed
    # in rather than read from the snapshot because it is deliberately not
    # stored there.
    if register:
        pdf.add_page()
        _h(pdf, "Checkpoint Register")
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*GREY)
        pdf.multi_cell(190, 4.5, _s(
            f"All {len(register)} checkpoint(s) assessed on this audit, in sequence, with the "
            "auditor's observation, the assignment, any corrective action and the full iteration "
            "history for each."), border=0)
        pdf.set_text_color(0, 0, 0)
        pdf.ln(2)

        _RESULT_COLOUR = {"PASS": GREEN, "PARTIAL": AMBER, "FAIL": RED, "NA": GREY, "NOT_ASSESSED": GREY}
        current_disc = None
        for cp in register:
            # Discipline banner, printed once per group.
            disc = cp.get("discipline") or "Uncategorised"
            if disc != current_disc:
                current_disc = disc
                pdf.ln(1)
                pdf.set_font("Helvetica", "B", 9)
                pdf.set_fill_color(*LIGHT)
                pdf.set_x(10)
                pdf.cell(190, 6, _s(f"  {disc}"), border=0, ln=1, fill=True)
                pdf.ln(1)

            result = str(cp.get("assessmentStatus") or "NOT_ASSESSED")
            sev = str(cp.get("severity") or "-")
            pdf.set_font("Helvetica", "B", 8.5)
            pdf.set_text_color(*_RESULT_COLOUR.get(result, GREY))
            pdf.set_x(10)
            head = f"{cp.get('checkpointCode') or '-'}   {result}   [{sev}]"
            if cp.get("isAdHoc"):
                head += "   (ad-hoc)"
            pdf.multi_cell(190, 5, _s(head), border=0)

            pdf.set_text_color(0, 0, 0)
            pdf.set_font("Helvetica", "", 8)
            pdf.set_x(10)
            pdf.multi_cell(190, 4.5, _s(cp.get("question") or "-"), border=0)

            obs = (cp.get("observation") or "").strip()
            if obs:
                pdf.set_font("Helvetica", "I", 8)
                pdf.set_text_color(*GREY)
                pdf.set_x(12)
                pdf.multi_cell(188, 4.5, _s(f"Observation: {obs}"), border=0)
                pdf.set_text_color(0, 0, 0)

            meta = []
            if cp.get("standard"):
                meta.append(f"Standard: {cp['standard']}")
            if cp.get("requirementReference"):
                meta.append(f"Clause: {cp['requirementReference']}")
            if cp.get("workflowState"):
                meta.append(f"State: {_human(cp.get('workflowState'))}")
            owner = _who(cp.get("ownerId"), user_names)
            if owner:
                meta.append(f"Owner: {owner}")
            if cp.get("capaNumber"):
                meta.append(f"CAPA: {cp['capaNumber']}")
            ev = len(cp.get("auditorEvidenceIds") or []) + len(cp.get("auditeeEvidenceIds") or [])
            if ev:
                meta.append(f"Evidence: {ev} photo(s)")
            if meta:
                pdf.set_font("Helvetica", "", 7.5)
                pdf.set_text_color(*GREY)
                pdf.set_x(12)
                pdf.multi_cell(188, 4, _s("   |   ".join(meta)), border=0)
                pdf.set_text_color(0, 0, 0)

            # The iteration thread — who said what, when. This is the part an
            # assessor asks for and the part a screenshot cannot provide.
            for it in cp.get("interactions") or []:
                pdf.set_font("Helvetica", "", 7.5)
                pdf.set_text_color(*GREY)
                pdf.set_x(14)
                actor = _who(it.get("actorId"), user_names) or _human(it.get("actorRole"))
                line = (f"R{it.get('round', 0)}  {_dt(it.get('timestamp'))}  -  "
                        f"{_human(it.get('action'))}  by {actor}  ->  {_human(it.get('resultingState'))}")
                pdf.multi_cell(186, 4, _s(line), border=0)
                if it.get("comment"):
                    pdf.set_x(16)
                    pdf.multi_cell(184, 4, _s(f'"{it["comment"]}"'), border=0)
                pdf.set_text_color(0, 0, 0)
            pdf.ln(2)

        # Never truncate silently — a register that quietly stops is worse than
        # one that says where it stopped.
        if register_truncated:
            pdf.set_font("Helvetica", "I", 8)
            pdf.set_text_color(*RED)
            pdf.multi_cell(190, 5, _s(
                f"{register_truncated} further checkpoint(s) are on this audit but are not printed "
                "here — this PDF caps the register to keep the file openable. Use the on-screen "
                "register for the remainder."), border=0)
            pdf.set_text_color(0, 0, 0)
        pdf.ln(3)

    # ── Sign-off (FINAL) ──
    if rtype.upper() == "FINAL":
        _h(pdf, "Sign-Off")
        signs = report.get("signOffs") or []
        if not signs:
            pdf.cell(0, 6, "Awaiting sign-off.", border=0, ln=1)
        for s in signs:
            pdf.cell(0, 6, f"{s.get('role', '-')}: {s.get('name', '-')}  -  {s.get('signedAt', '')}", border=0, ln=1)
        pdf.ln(3)

    # ── Distribution list (WP-12) ──
    dist = snap.get("distributionList") or []
    if dist:
        _h(pdf, "Distribution")
        pdf.set_font("Helvetica", "", 9)
        for d in dist:
            pdf.cell(0, 5.5, f"{_s(d.get('role'))}: {_s(d.get('name'))}", border=0, ln=1)
        # A heading called "Distribution" over a single name reads as a
        # rendering failure. It is not — the builder correctly walks lead
        # auditor, co-auditors, plant manager and auditees; this engagement
        # simply has none of the latter. Say which, so the reader can tell an
        # empty team from a broken report.
        if len(dist) == 1:
            pdf.ln(1)
            pdf.set_font("Helvetica", "I", 8)
            pdf.set_text_color(*GREY)
            pdf.set_x(10)
            pdf.multi_cell(0, 4.5, (
                "No co-auditors, plant manager or auditee owners are assigned to this "
                "engagement, so the distribution is the lead auditor only."
            ))
            pdf.set_text_color(0, 0, 0)
        pdf.ln(3)

    # ── Revision history + errata (WP-12, §2.5) ──
    revs = snap.get("revisionHistory") or []
    errata = report.get("errata") or []
    if revs or errata:
        _h(pdf, "Revision History")
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(0, 5.5, f"This is issue {snap.get('revision', 1)} of this audit's reports.", ln=1)
        for r in revs:
            pdf.cell(
                0, 5,
                f"  {_s(r.get('reportCode'))} ({_s(r.get('reportType'))}) - "
                f"{_s(r.get('generatedAt'))} - superseded",
                border=0, ln=1,
            )
        for e in errata:
            pdf.ln(1)
            pdf.set_font("Helvetica", "B", 8)
            pdf.cell(0, 5, f"Erratum {e.get('sequence')} - {_s(e.get('createdAt'))}", ln=1)
            pdf.set_font("Helvetica", "", 8)
            pdf.set_x(10)
            pdf.multi_cell(0, 4.5, _s(e.get("text")))
            pdf.set_x(10)
            pdf.multi_cell(
                0, 4.5,
                f"Raised by {_s(e.get('raisedBy'))}, approved by {_s(e.get('approvedBy'))}",
            )
        pdf.ln(2)

    # ── Integrity footer ──
    _h(pdf, "Record Integrity")
    pdf.set_font("Helvetica", "", 8)
    pdf.set_x(10)
    pdf.multi_cell(
        0, 4.5,
        "This report is generated from an immutable snapshot taken at issue. Its SHA-256 digest "
        f"is {_s(report.get('snapshotHashFull') or snap.get('snapshotHash') or '-')}. "
        "Any change to the underlying record after issue appears as an erratum above, never as a "
        "silent edit.",
    )

    out = pdf.output()
    return bytes(out)


# â”€â”€ BRSR â€” Business Responsibility & Sustainability Report â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# Renders the BRSR report payload (services/brsr_report.assemble, or the frozen
# snapshot) using the same _Report chrome as the audit PDF: latin-1
# sanitisation, page numbering, confidential footer, integrity hash. Reusing
# the class rather than forking it is deliberate â€” a second copy of the
# sanitisation would drift, and the one thing a disclosure PDF must never do is
# silently mangle a number.


def _para(pdf: _Report, h: float, text: str, **kw) -> None:
    """Full-width paragraph that always starts at the left margin.

    fpdf2 leaves the cursor at the cell's RIGHT edge after multi_cell, so a
    following `multi_cell(0, ...)` computes zero remaining width and raises
    "Not enough horizontal space to render a single character". Every
    full-width write in the BRSR renderer goes through here, so the crash
    cannot come back the next time an entity field happens to be populated.
    (It stayed hidden through one live test only because that test cycle had
    no CIN, address or contact details.)
    """
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(0, h, text, **kw)


def _kv(pdf: _Report, label: str, value: str, label_w: float = 42.0) -> None:
    """A bold label beside a wrapping value, on the cover block.

    Written as an explicit two-column pair rather than cell + full-width
    multi_cell: the value must wrap within the REMAINING width, and it must
    keep starting at the label's right edge on every wrapped line, not jump
    back under the label.
    """
    y0 = pdf.get_y()
    pdf.set_x(pdf.l_margin)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(label_w, 5.5, f"{label}:", ln=0)
    pdf.set_font("Helvetica", "", 9)
    avail = pdf.w - pdf.r_margin - (pdf.l_margin + label_w)
    pdf.set_xy(pdf.l_margin + label_w, y0)
    pdf.multi_cell(avail, 5.5, value)


def _brsr_value(block: dict[str, Any]) -> str:
    """One indicator's answer, rendered for print."""
    if block.get("provenance") == "NOT_APPLICABLE":
        return "Not applicable"
    v = block.get("value")
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, (int, float)):
        unit = block.get("unit") or ""
        return f"{v:,.2f} {unit}".strip()
    if isinstance(v, list):
        return f"{len(v)} row(s) - see structured export"
    if isinstance(v, dict):
        return "Entered - see structured export"
    return str(v)


def _brsr_indicator_table(pdf: _Report, blocks: list[dict[str, Any]]) -> None:
    """Indicator rows: code | disclosure | value | provenance.

    Provenance is printed, not just held in the UI. An assurance provider
    reading the PDF has to see which figures the platform derived, and from how
    many records, without opening the application.
    """
    if not blocks:
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(*GREY)
        pdf.cell(0, 6, "No indicators in this section.", ln=1)
        pdf.set_text_color(0, 0, 0)
        return

    widths = (22, 96, 40, 32)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_fill_color(*LIGHT)
    for w, head in zip(widths, ("Code", "Disclosure", "Value", "Source")):
        pdf.cell(w, 6, head, border=0, ln=0, fill=True)
    pdf.ln(6)

    for b in blocks:
        label = str(b.get("label") or "")
        # Estimate the wrapped height so a row is never split across a page break.
        est_lines = max(1, int(len(_s(label)) / 58) + 1)
        if pdf.get_y() + est_lines * 4.5 + 2 > pdf.h - 22:
            pdf.add_page()

        y0 = pdf.get_y()
        pdf.set_font("Helvetica", "", 7)
        pdf.set_text_color(*GREY)
        pdf.cell(widths[0], 4.5, str(b.get("code") or ""), border=0, ln=0)

        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Helvetica", "", 8)
        x_label = pdf.get_x()
        pdf.multi_cell(widths[1], 4.5, label, border=0)
        y_after_label = pdf.get_y()

        pdf.set_xy(x_label + widths[1], y0)
        pdf.set_font("Helvetica", "B", 8)
        pdf.multi_cell(widths[2], 4.5, _brsr_value(b), border=0)

        pdf.set_xy(x_label + widths[1] + widths[2], y0)
        prov = b.get("provenance") or "unanswered"
        src = b.get("sourceModule")
        n = b.get("sourceRecordCount")
        tag = {
            "AUTO": f"Platform{f' ({src})' if src else ''}",
            "AUTO_OVERRIDDEN": "Overridden",
            "MANUAL": "Manual entry",
            "NOT_APPLICABLE": "N/A",
        }.get(prov, "Unanswered")
        if prov == "AUTO" and n:
            tag += f" - {n} rec"
        pdf.set_font("Helvetica", "", 7)
        pdf.set_text_color(*GREY)
        pdf.multi_cell(widths[3], 4.5, tag, border=0)
        pdf.set_text_color(0, 0, 0)

        pdf.set_y(max(y_after_label, pdf.get_y()))
        pdf.set_draw_color(*LIGHT)
        pdf.line(10, pdf.get_y() + 0.5, 200, pdf.get_y() + 0.5)
        pdf.ln(1.5)


def render_brsr_report_pdf(payload: dict[str, Any]) -> bytes:
    """Filing-ready BRSR PDF from an assembled or frozen report payload."""
    meta = payload.get("meta") or {}
    entity = payload.get("entity") or {}
    fy = str(meta.get("financialYear") or "")
    frozen = bool(meta.get("frozen"))
    # A draft carries the PROVISIONAL watermark; a filed disclosure does not â€”
    # the same INTERIM/FINAL signal the audit report uses. A draft BRSR must
    # never be mistakable for the filed article.
    report_type = "FINAL" if frozen else "INTERIM"
    pdf = _Report(report_type, fy, str(meta.get("snapshotHash") or ""), doc_label="BRSR")
    pdf.alias_nb_pages()
    pdf.add_page()

    # â”€â”€ cover â”€â”€
    pdf.set_font("Helvetica", "B", 17)
    pdf.set_text_color(*NAVY)
    _para(pdf, 9, "Business Responsibility & Sustainability Report", align="L")
    pdf.ln(1)
    pdf.set_font("Helvetica", "", 12)
    pdf.set_text_color(0, 0, 0)
    pdf.cell(0, 7, str(entity.get("name") or "Listed entity"), ln=1)
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(*GREY)
    pdf.cell(0, 6, f"Financial year {fy}", ln=1)
    pdf.cell(
        0, 6,
        f"Reporting period {_dt(meta.get('periodStart'), with_time=False)} to "
        f"{_dt(meta.get('periodEnd'), with_time=False)}",
        ln=1,
    )
    pdf.set_text_color(0, 0, 0)
    pdf.ln(3)

    for label, key in (
        ("CIN", "cin"),
        ("Registered office", "registeredOfficeAddress"),
        ("Contact", "contactName"),
        ("E-mail", "contactEmail"),
        ("Telephone", "contactPhone"),
        ("Assurance provider", "assuranceProvider"),
        ("Assurance type", "assuranceType"),
    ):
        val = entity.get(key)
        if not val:
            continue
        _kv(pdf, label, str(val))
    codes = entity.get("stockExchangeCodes") or []
    if codes:
        _kv(pdf, "Listed on", ", ".join(str(c) for c in codes))

    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_fill_color(*LIGHT)
    pdf.cell(
        0, 7,
        f"  Status: {meta.get('status', '-')}     "
        f"Completion: {float(meta.get('completionPct') or 0):.1f}%     "
        f"From platform data: {float(meta.get('autoPopulatedPct') or 0):.1f}% of answered",
        ln=1, fill=True,
    )
    pdf.ln(2)

    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(*GREY)
    _para(pdf, 4.5, str(meta.get("filingNote") or ""))
    if not frozen:
        pdf.set_text_color(*AMBER)
        _para(
            pdf, 4.5,
            "DRAFT - assembled from current data and subject to change. This becomes an "
            "immutable snapshot only when the cycle is marked filed.",
        )
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)

    approval = payload.get("approval") or {}
    if approval.get("filedAt"):
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(
            0, 5.5,
            f"Filed: {_dt(approval.get('filedAt'))}     "
            f"Reference: {approval.get('filingReference') or '-'}",
            ln=1,
        )

    # â”€â”€ Section A â”€â”€
    pdf.add_page()
    _h(pdf, "Section A - Details of the listed entity")
    _brsr_indicator_table(pdf, payload.get("sectionA") or [])

    # â”€â”€ Section B â”€â”€
    pdf.add_page()
    _h(pdf, "Section B - Management and process disclosures")
    _brsr_indicator_table(pdf, payload.get("sectionB") or [])

    # â”€â”€ Section C, one page per principle â”€â”€
    for p in payload.get("sectionC") or []:
        pdf.add_page()
        _h(pdf, f"Principle {str(p.get('principle', ''))[1:]} - {p.get('title', '')}")

        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*GREY)
        line = f"Completion {float(p.get('completionPct') or 0):.0f}%"
        if not p.get("isPlatformSourced", True):
            # Stated in the document, not only on screen.
            line += "   |   Not sourced from platform data - manual entry only"
        pdf.cell(0, 5, line, ln=1)
        pdf.set_text_color(0, 0, 0)
        pdf.ln(1)

        if p.get("narrative"):
            pdf.set_font("Helvetica", "I", 9)
            _para(pdf, 4.8, str(p["narrative"]))
            pdf.ln(1.5)

        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(*PURPLE)
        pdf.cell(0, 6, "Essential indicators", ln=1)
        pdf.set_text_color(0, 0, 0)
        _brsr_indicator_table(pdf, p.get("essentialIndicators") or [])

        leadership = p.get("leadershipIndicators") or []
        if leadership:
            pdf.ln(2)
            pdf.set_font("Helvetica", "B", 9)
            pdf.set_text_color(*PURPLE)
            pdf.cell(0, 6, "Leadership indicators (voluntary)", ln=1)
            pdf.set_text_color(0, 0, 0)
            _brsr_indicator_table(pdf, leadership)

    # â”€â”€ environmental annex â”€â”€
    env = payload.get("environmentalSummary") or {}
    if env:
        pdf.add_page()
        _h(pdf, "Environmental summary - Principle 6")
        pdf.set_font("Helvetica", "", 9)
        for label, val, unit in (
            ("Sites reporting", env.get("sitesReporting"), ""),
            ("Total energy", env.get("energyTotalGj"), "GJ"),
            ("  of which renewable", env.get("energyRenewableGj"), "GJ"),
            ("Water withdrawn", env.get("waterWithdrawnKl"), "kL"),
            ("Water consumed", env.get("waterConsumedKl"), "kL"),
            ("Water discharged", env.get("waterDischargedKl"), "kL"),
            ("Scope 1 emissions", env.get("scope1TCo2e"), "tCO2e"),
            ("Scope 2 emissions", env.get("scope2TCo2e"), "tCO2e"),
            ("Scope 3 emissions (manual)", env.get("scope3TCo2e"), "tCO2e"),
            ("Waste generated", env.get("wasteGeneratedT"), "MT"),
            ("Waste recovered", env.get("wasteRecoveredT"), "MT"),
            ("Waste disposed", env.get("wasteDisposedT"), "MT"),
            ("Waste diverted", env.get("wasteDivertedPct"), "%"),
            ("Turnover reported", env.get("turnoverInr"), "INR"),
        ):
            pdf.cell(70, 5.5, label, ln=0)
            pdf.set_font("Helvetica", "B", 9)
            if val is None:
                shown = "-"
            elif isinstance(val, float):
                shown = f"{val:,.2f}"
            else:
                shown = f"{val:,}"
            pdf.cell(40, 5.5, f"{shown} {unit}".strip(), ln=1)
            pdf.set_font("Helvetica", "", 9)

        unresolved = env.get("unresolvedEmissionLines") or 0
        if unresolved:
            # An incomplete emissions total must never present itself as complete.
            pdf.ln(2)
            pdf.set_text_color(*RED)
            pdf.set_font("Helvetica", "B", 9)
            _para(
                pdf, 5,
                f"WARNING: {unresolved} emission line(s) could not be resolved (missing "
                "emission factor or unconvertible unit) and are EXCLUDED from the Scope 1 "
                "and Scope 2 totals above. Those figures understate the entity.",
            )
            pdf.set_text_color(0, 0, 0)

    # â”€â”€ integrity â”€â”€
    pdf.ln(4)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(*GREY)
    if frozen and meta.get("snapshotHash"):
        _para(
            pdf, 4.5,
            f"Record integrity: SHA-256 {meta['snapshotHash']}. This PDF was rendered from "
            "the immutable snapshot frozen when the cycle was marked filed; the hash lets a "
            "reader confirm the content has not changed since.",
        )
    else:
        _para(
            pdf, 4.5,
            "Record integrity: no snapshot hash - this is a live draft, not a filed record.",
        )
    pdf.set_text_color(0, 0, 0)

    return bytes(pdf.output())


# ─────────────────────────────────────────────────────────────────────────────
# Kaizen register (Business Excellence §8)
# ─────────────────────────────────────────────────────────────────────────────
#: Column widths in mm for the landscape register, summing to the printable
#: width of an A4 landscape page (297 - 2*10 margins = 277).
_KAIZEN_COLS: list[tuple[str, float, str]] = [
    ("No.", 24, "L"),
    ("Title", 74, "L"),
    ("Plant", 38, "L"),
    ("Category", 24, "L"),
    ("Status", 26, "L"),
    ("Owner", 30, "L"),
    ("Target", 22, "L"),
    ("Saving / yr", 25, "R"),
    ("FT", 8, "C"),
]


def _kaizen_cols(labels: Mapping[str, str] | None) -> list[tuple[str, float, str]]:
    """_KAIZEN_COLS with the display-label override applied to the Plant header."""
    return [(_lbl(labels, "term.plant", h) if h == "Plant" else h, w, a) for h, w, a in _KAIZEN_COLS]


def render_kaizen_register_pdf(
    rows: list[dict[str, Any]],
    *,
    filter_summary: str,
    generated_by_name: str = "—",
    plant_label: str = "All plants in your scope",
    truncated: int = 0,
    labels: Mapping[str, str] | None = None,
) -> bytes:
    """The Kaizen register as a printable landscape table.

    Reuses `_Report` rather than forking it, for the same reason BRSR does: the
    latin-1 sanitisation, the page numbering and the CONFIDENTIAL footer are one
    implementation. A second copy drifts, and the one thing a register export
    must never do is silently mangle a saving figure.

    `filter_summary` is printed on the cover. That is not decoration — an export
    handed to somebody else is otherwise indistinguishable from the full
    register, and a filtered extract read as a complete one is how a programme
    gets reported as smaller than it is.
    """
    pdf = _Report("FINAL", "Kaizen register", "", doc_label="Kaizen Register")
    pdf.alias_nb_pages()
    # Landscape: nine columns of register data do not fit portrait without
    # truncating the title, which is the column a reader scans.
    pdf.add_page(orientation="L")

    pdf.set_font("Helvetica", "B", 15)
    pdf.set_text_color(*PURPLE)
    _para(pdf, 8, "Kaizen register", align="L")
    pdf.ln(1)

    pdf.set_text_color(0, 0, 0)
    _kv(pdf, "Scope", plant_label)
    _kv(pdf, "Filters", filter_summary)
    _kv(pdf, "Records", f"{len(rows)}")
    _kv(pdf, "Generated by", generated_by_name)
    _kv(pdf, "Generated at", _dt(datetime.now(timezone.utc).isoformat()))
    if truncated:
        pdf.set_text_color(*GOLD_INK)
        _kv(
            pdf,
            "Truncated",
            f"{truncated} further record(s) matched and are NOT in this export. "
            f"Narrow the filters or use the CSV export.",
        )
        pdf.set_text_color(0, 0, 0)
    pdf.ln(3)

    def header() -> None:
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_fill_color(*LIGHT)
        pdf.set_text_color(*PURPLE)
        for label, width, _align in _kaizen_cols(labels):
            pdf.cell(width, 7, label, border=0, ln=0, align="L", fill=True)
        pdf.ln(7)
        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Helvetica", "", 8)

    header()

    for i, r in enumerate(rows):
        # 190mm of usable height on landscape A4 after margins; re-print the
        # header on each new page so page four is still readable on its own.
        if pdf.get_y() > 180:
            pdf.add_page(orientation="L")
            header()
        if i % 2:
            pdf.set_fill_color(248, 249, 251)
        else:
            pdf.set_fill_color(255, 255, 255)

        values = [
            r.get("kaizenNo") or "—",
            _clip(r.get("title"), 58),
            _clip(r.get("siteName"), 28),
            _human(r.get("category")),
            _human(r.get("status")),
            _clip(r.get("ownerName"), 22) or "Unassigned",
            _dt(r.get("targetDate"), with_time=False) if r.get("targetDate") else "—",
            _money(r.get("saving"), r.get("currency")),
            # A tick only where the badge is EARNED by the record's own figures,
            # which is the same rule the screen applies. Printing the lane here
            # instead would reintroduce the defect this build removed.
            "Y" if r.get("fastTrack") else "",
        ]
        for (label, width, align), value in zip(_kaizen_cols(labels), values):
            pdf.cell(width, 6, str(value), border=0, ln=0, align=align, fill=True)
        pdf.ln(6)

    pdf.ln(4)
    pdf.set_font("Helvetica", "I", 7)
    pdf.set_text_color(*GREY)
    _para(
        pdf,
        4,
        "Saving shows the verified annual figure where one has been recorded and "
        "the estimate otherwise. FT marks records whose recorded investment and "
        "implementation window meet the fast-track thresholds — not the approval "
        "lane the record was submitted on.",
    )

    return bytes(pdf.output(dest="S"))


def _clip(value: Any, limit: int) -> str:
    """Truncate to fit a fixed-width column, with an ellipsis that says so."""
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _money(value: Any, currency: Any) -> str:
    # A missing figure prints as a dash, never as 0. A zero saving is a claim
    # somebody made; a blank is the absence of one, and the register's whole
    # credibility rests on the two staying distinguishable.
    if value is None:
        return "—"
    return f"{currency or ''} {float(value):,.0f}".strip()
