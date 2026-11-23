"""Contractor Safety Management service: rules, scoring and aggregates.

Two halves, like services/ims.py:

* Pure helpers (questionnaire scoring, workflow moves, the work-order
  activation gate, the worker clearance checks, evaluation KPI maths, sanction
  rules). Plain values in, plain values out, no DB, so tests/test_csm.py runs
  them offline.
* Async aggregates over other modules' tables (Contractor 360, KPI auto-fill,
  hub, expiry list). They count and list; they never copy another module's
  data into CSM tables, except the evaluation's frozen KPI snapshot, which is
  the point of an evaluation (what the numbers were when it was signed).

ContractorCompany vocabulary is the EPC one and is left as it is:
  prequalificationStatus  not_applied | approved | conditionally_approved | rejected
  status                  ACTIVE | SUSPENDED | BLACKLISTED (| TERMINATED, legacy)
CSM maps its own decisions onto those values (PQ_TO_EPC) so the existing EPC
gate and mobilisation checks keep working unchanged.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

# ── vocabularies ─────────────────────────────────────────────────────────────
ANSWER_TYPES = ("YES_NO", "SCALE_1_5", "NUMBER", "TEXT", "DOCUMENT_REQUIRED")
TEMPLATE_STATUSES = ("DRAFT", "ACTIVE", "RETIRED")

PQ_STATUSES = ("DRAFT", "SUBMITTED", "HSE_REVIEW", "APPROVAL", "QUALIFIED", "CONDITIONAL", "REJECTED", "WITHDRAWN")
PQ_OPEN = ("DRAFT", "SUBMITTED", "HSE_REVIEW", "APPROVAL")
PQ_DECISIONS = ("QUALIFIED", "CONDITIONAL", "REJECTED")
PQ_TO_EPC = {"QUALIFIED": "approved", "CONDITIONAL": "conditionally_approved", "REJECTED": "rejected"}
EPC_QUALIFIED = ("approved", "conditionally_approved")

_PQ_MOVES = {
    "DRAFT": {"SUBMITTED", "WITHDRAWN"},
    "SUBMITTED": {"HSE_REVIEW", "DRAFT", "WITHDRAWN"},
    "HSE_REVIEW": {"APPROVAL", "DRAFT"},
    "APPROVAL": {"QUALIFIED", "CONDITIONAL", "REJECTED", "HSE_REVIEW"},
}

DOC_TYPES = (
    "HSE_POLICY", "LABOUR_LICENCE", "PF_REGISTRATION", "ESIC_REGISTRATION", "WC_INSURANCE", "ISO_45001",
    "ISO_14001", "TRAINING_RECORD", "EQUIPMENT_FITNESS", "SAFETY_OFFICER_QUALIFICATION", "GST_PAN",
    "HSE_PLAN", "OTHER",
)
# One live document per company for these; a new upload supersedes the old one.
SINGLE_INSTANCE_DOCS = {
    "HSE_POLICY", "LABOUR_LICENCE", "PF_REGISTRATION", "ESIC_REGISTRATION", "WC_INSURANCE", "ISO_45001",
    "ISO_14001", "GST_PAN",
}
DOC_MIME = {"application/pdf", "image/jpeg", "image/png", "image/webp", "image/heic"}
DOC_MAX_BYTES = 10 * 1024 * 1024

COMPANY_STATUSES = ("ACTIVE", "SUSPENDED", "BLACKLISTED")
BLOCKED_COMPANY_STATUSES = ("BLACKLISTED", "SUSPENDED")

WORK_TYPES = ("MAINTENANCE", "AMC", "SHUTDOWN", "PROJECT", "SERVICE", "OTHER")
HIGH_RISK_ACTIVITIES = (
    "WORK_AT_HEIGHT", "HOT_WORK", "CONFINED_SPACE", "ELECTRICAL", "LIFTING", "EXCAVATION", "MOLTEN_METAL",
    "GAS_LINE", "CHEMICAL", "RADIATION", "DEMOLITION",
)
SAFETY_CHECKLIST = (
    ("hsePlanApproved", "Contractor HSE plan reviewed and approved"),
    ("supervisorRatioOk", "Supervisor ratio meets the requirement (at least 1 per 15 workers)"),
    ("ppeVerified", "PPE for the job verified (incl. task-specific PPE)"),
    ("toolsFitnessVerified", "Tools, tackles and equipment fitness certificates verified"),
    ("penaltyClausesAcknowledged", "Safety penalty clauses acknowledged by the contractor"),
)
SUPERVISOR_RATIO_MAX = 15
WO_STATUSES = ("DRAFT", "ACTIVE", "SUSPENDED", "COMPLETED", "CANCELLED")
_WO_MOVES = {
    "DRAFT": {"ACTIVE", "CANCELLED"},
    "ACTIVE": {"SUSPENDED", "COMPLETED"},
    "SUSPENDED": {"ACTIVE", "COMPLETED", "CANCELLED"},
    "COMPLETED": set(),
    "CANCELLED": set(),
}
ROSTER_STATUSES = ("PROPOSED", "CLEARED", "BLOCKED", "RELEASED")
INDUCTION_VALIDITY_DAYS = 365

EVAL_TYPES = ("MONTHLY", "QUARTERLY", "CONTRACT_END")
EVAL_STATUSES = ("DRAFT", "SUBMITTED", "SIGNED_OFF")
# Auto KPIs (60 % of the score). Penalty KPIs: 100 - perEvent x count, floor 0.
KPI_DEFS = (
    {"code": "LTI_FATALITY", "label": "Lost-time injuries and fatalities", "weight": 15, "perEvent": 60},
    {"code": "INCIDENTS", "label": "Other incidents (first aid, MTC, RWC, fire, property damage)", "weight": 10, "perEvent": 20},
    {"code": "NEAR_MISSES", "label": "Near misses involving the contractor", "weight": 5, "perEvent": 10},
    {"code": "UNSAFE_OBSERVATIONS", "label": "Unsafe acts / conditions observed against the contractor", "weight": 10, "perEvent": 5},
    {"code": "SHOW_CAUSE", "label": "Show cause notices issued", "weight": 8, "perEvent": 25},
    {"code": "PERMIT_VIOLATIONS", "label": "Permit violations (permits suspended)", "weight": 7, "perEvent": 25},
    {"code": "TBT_PARTICIPATION", "label": "Toolbox talks held vs working days", "weight": 5},
)
# Manual criteria (40 %), scored 1-5 by the evaluator (1 = 20 %, 5 = 100 %).
MANUAL_CRITERIA = (
    {"code": "HOUSEKEEPING", "label": "Housekeeping at the work site", "weight": 10},
    {"code": "SUPERVISION", "label": "Supervision and control of the workforce", "weight": 10},
    {"code": "COOPERATION", "label": "Cooperation and response to HSE instructions", "weight": 10},
    {"code": "DOCUMENTATION", "label": "Quality of documentation (permits, registers, records)", "weight": 10},
)
RATING_BANDS = ((85, "EXCELLENT"), (70, "GOOD"), (55, "AVERAGE"), (40, "POOR"), (0, "UNACCEPTABLE"))
# A fatality in the period caps the final score below the POOR line, whatever
# the other KPIs say: no contractor with a fatality is rated acceptable.
FATALITY_SCORE_CAP = 39.0

SANCTION_ACTIONS = ("SUSPEND", "BLACKLIST", "REINSTATE")
SANCTION_REASONS = (
    "FATALITY", "SERIOUS_INCIDENT", "REPEATED_VIOLATIONS", "POOR_EVALUATION", "STATUTORY_NON_COMPLIANCE",
    "FALSE_DOCUMENTS", "CORRECTIVE_ACTIONS_COMPLETED", "SUSPENSION_PERIOD_OVER", "OTHER",
)
SANCTION_STATUSES = ("PENDING_APPROVAL", "APPROVED", "REJECTED")
EXPIRY_WINDOW_DAYS = 30


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _num(v: Any) -> float | None:
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def add_months(d: datetime, months: int) -> datetime:
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return d.replace(year=y, month=m, day=day)


# ── questionnaire scoring ────────────────────────────────────────────────────
def is_scored(q: dict) -> bool:
    return q.get("answerType") != "TEXT" and float(q.get("weight") or 0) > 0


def question_fraction(q: dict, value: Any, valid_doc_types: set[str]) -> float | None:
    """Auto score of one question as a 0..1 fraction. None = not scored.

    YES_NO       preferred answer (default YES) = 1, other = 0
    SCALE_1_5    1 -> 0, 5 -> 1, linear
    NUMBER       first band whose `max` is >= the value (ascending; max None =
                 catch-all). No bands = full marks for a number given.
    DOCUMENT_REQUIRED  1 when the company holds a live (not expired) document
                 of the question's docType, else 0. The answer is irrelevant:
                 the document is the evidence.
    TEXT         never scored (evaluator may read it, not weigh it).
    Unanswered scored questions score 0.
    """
    if not is_scored(q):
        return None
    t = q.get("answerType")
    if t == "DOCUMENT_REQUIRED":
        return 1.0 if q.get("docType") in valid_doc_types else 0.0
    if value is None or value == "":
        return 0.0
    if t == "YES_NO":
        v = str(value).strip().upper()
        if v not in ("YES", "NO"):
            return 0.0
        return 1.0 if v == (q.get("preferredAnswer") or "YES").upper() else 0.0
    n = _num(value)
    if n is None:
        return 0.0
    if t == "SCALE_1_5":
        if n < 1 or n > 5:
            return 0.0
        return round((n - 1) / 4, 4)
    if t == "NUMBER":
        bands = q.get("bands") or []
        if not bands:
            return 1.0
        for b in bands:
            mx = b.get("max")
            if mx is None or n <= float(mx):
                return max(0.0, min(1.0, float(b.get("score") or 0)))
        return 0.0
    return 0.0


def score_application(
    sections: list[dict], answers: dict, overrides: dict, valid_doc_types: set[str]
) -> dict[str, Any]:
    """Weighted score of an application, auto and after evaluator overrides.

    Section score = sum(qWeight x qFraction) / sum(qWeight) over its scored
    questions. Overall = sum(sectionWeight x sectionScore) / sum(sectionWeight)
    over sections that have scored questions. 0..100, one decimal.
    A critical question whose effective fraction is 0 is a critical failure.
    """
    answers = answers or {}
    overrides = overrides or {}
    out_sections: list[dict] = []
    tot_w = tot_auto = tot_final = 0.0
    critical: list[dict] = []
    unanswered: list[str] = []
    missing_docs: list[str] = []
    for s in sections or []:
        num_a = num_f = den = 0.0
        rows: list[dict] = []
        for q in s.get("questions") or []:
            code = q["code"]
            val = (answers.get(code) or {}).get("value")
            auto = question_fraction(q, val, valid_doc_types)
            final = auto
            ov = overrides.get(code)
            if auto is not None and ov and ov.get("score") is not None:
                final = max(0.0, min(1.0, float(ov["score"])))
            t = q.get("answerType")
            answered = val not in (None, "")
            if t in ("YES_NO", "SCALE_1_5", "NUMBER", "TEXT") and q.get("required", is_scored(q)) and not answered:
                unanswered.append(code)
            if t == "DOCUMENT_REQUIRED" and q.get("docType") not in valid_doc_types:
                missing_docs.append(code)
            if auto is not None:
                w = float(q.get("weight") or 0)
                num_a += w * auto
                num_f += w * (final or 0)
                den += w
                if q.get("critical") and (final or 0) <= 0:
                    critical.append({"code": code, "text": q.get("text"), "section": s.get("title")})
            rows.append({
                "code": code, "auto": auto, "final": final, "overridden": final != auto,
                "answered": answered, "weight": q.get("weight") or 0, "critical": bool(q.get("critical")),
            })
        s_auto = round(100 * num_a / den, 1) if den else None
        s_final = round(100 * num_f / den, 1) if den else None
        sw = float(s.get("weight") or 0)
        if den and sw > 0:
            tot_w += sw
            tot_auto += sw * (num_a / den)
            tot_final += sw * (num_f / den)
        out_sections.append({"code": s.get("code"), "title": s.get("title"), "weight": sw,
                             "autoScore": s_auto, "finalScore": s_final, "questions": rows})
    return {
        "sections": out_sections,
        "autoScore": round(100 * tot_auto / tot_w, 1) if tot_w else None,
        "finalScore": round(100 * tot_final / tot_w, 1) if tot_w else None,
        "criticalFailures": critical,
        "unanswered": unanswered,
        "missingDocuments": missing_docs,
    }


def recommend(final_score: float | None, critical_failures: list, pass_score: float, conditional_score: float) -> str | None:
    """Auto recommendation. A critical failure caps it at CONDITIONAL."""
    if final_score is None:
        return None
    if final_score >= pass_score and not critical_failures:
        return "QUALIFIED"
    if final_score >= conditional_score:
        return "CONDITIONAL"
    return "REJECTED"


def normalise_answer(q: dict, value: Any) -> tuple[Any, str | None]:
    """(stored value, error). Empty clears the answer."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, None
    t = q.get("answerType")
    if t == "YES_NO":
        v = str(value).strip().upper()
        return (v, None) if v in ("YES", "NO") else (None, f"{q['code']}: answer YES or NO")
    if t == "SCALE_1_5":
        n = _num(value)
        if n is None or n != int(n) or not 1 <= n <= 5:
            return None, f"{q['code']}: pick a value from 1 to 5"
        return int(n), None
    if t == "NUMBER":
        n = _num(value)
        if n is None or n < 0:
            return None, f"{q['code']}: enter a number (0 or more)"
        return n, None
    if t in ("TEXT", "DOCUMENT_REQUIRED"):
        return str(value).strip()[:4000], None
    return None, f"{q['code']}: unknown answer type"


def find_question(sections: list[dict], code: str) -> dict | None:
    for s in sections or []:
        for q in s.get("questions") or []:
            if q.get("code") == code:
                return q
    return None


def template_problems(sections: list[dict]) -> list[str]:
    """Validation of an edited template. Empty list = valid."""
    out: list[str] = []
    if not sections:
        return ["A template needs at least one section."]
    seen_s: set[str] = set()
    seen_q: set[str] = set()
    total_w = 0.0
    for s in sections:
        sc = (s.get("code") or "").strip()
        if not sc or not (s.get("title") or "").strip():
            out.append("Every section needs a code and a title.")
        if sc in seen_s:
            out.append(f"Duplicate section code {sc}.")
        seen_s.add(sc)
        w = _num(s.get("weight"))
        if w is None or w < 0:
            out.append(f"Section {sc}: weight must be a number >= 0.")
        else:
            total_w += w
        qs = s.get("questions") or []
        if not qs:
            out.append(f"Section {sc} has no questions.")
        for q in qs:
            qc = (q.get("code") or "").strip()
            if not qc or not (q.get("text") or "").strip():
                out.append(f"Section {sc}: every question needs a code and text.")
            if qc in seen_q:
                out.append(f"Duplicate question code {qc}.")
            seen_q.add(qc)
            if q.get("answerType") not in ANSWER_TYPES:
                out.append(f"Question {qc}: answer type must be one of {', '.join(ANSWER_TYPES)}.")
            qw = _num(q.get("weight"))
            if qw is None or qw < 0:
                out.append(f"Question {qc}: weight must be a number >= 0.")
            if q.get("answerType") == "DOCUMENT_REQUIRED" and q.get("docType") not in DOC_TYPES:
                out.append(f"Question {qc}: pick the document type it requires.")
            for b in q.get("bands") or []:
                if _num(b.get("score")) is None or not 0 <= float(b["score"]) <= 1:
                    out.append(f"Question {qc}: band scores must be between 0 and 1.")
                    break
    if total_w <= 0:
        out.append("Section weights add up to zero; nothing would be scored.")
    return out


# ── prequalification workflow ────────────────────────────────────────────────
def can_move_pq(current: str, target: str) -> bool:
    return target in _PQ_MOVES.get(current, set())


def submit_blockers(*, status: str, unanswered: list[str]) -> list[str]:
    out: list[str] = []
    if status != "DRAFT":
        out.append(f"Only a draft application can be submitted (this one is {status}).")
    if unanswered:
        out.append(f"{len(unanswered)} required question(s) are not answered: {', '.join(unanswered[:8])}"
                   + ("…" if len(unanswered) > 8 else ""))
    return out


def decision_blockers(
    *, status: str, decision: str, conditions: str | None, notes: str | None,
    recommendation: str | None, recommended_by: str | None, decided_by: str,
) -> list[str]:
    """Plant-head decision rules. The recommender may not approve their own
    recommendation (segregation of duties), and a decision that departs from
    the HSE recommendation must say why."""
    out: list[str] = []
    if status != "APPROVAL":
        out.append(f"Only an application awaiting approval can be decided (this one is {status}).")
    if decision not in PQ_DECISIONS:
        out.append(f"Decision must be one of {', '.join(PQ_DECISIONS)}.")
    if decision == "CONDITIONAL" and not (conditions and conditions.strip()):
        out.append("State the conditions for a conditional qualification.")
    if decision == "REJECTED" and not (notes and notes.strip()):
        out.append("Give the reason for rejection.")
    if recommendation and decision in PQ_DECISIONS and decision != recommendation and not (notes and notes.strip()):
        out.append(f"The decision differs from the HSE recommendation ({recommendation}); record why.")
    if recommended_by and recommended_by == decided_by:
        out.append("The HSE reviewer who recommended this application cannot also approve it.")
    return out


def validity_for(decision: str, decided_at: datetime, validity_months: int, conditional_months: int) -> datetime | None:
    if decision == "QUALIFIED":
        return add_months(decided_at, validity_months)
    if decision == "CONDITIONAL":
        return add_months(decided_at, conditional_months)
    return None


def company_block_reason(status: str | None) -> str | None:
    """Why a contractor company may not work at all, or None. Used by the EPC
    gate, the work-order activation gate and the roster clearance."""
    s = (status or "ACTIVE").upper()
    if s in BLOCKED_COMPANY_STATUSES:
        return f"Contractor company is {s.lower()}"
    return None


def effective_qualification(epc_status: str | None, valid_until: datetime | None, now: datetime | None = None) -> str:
    """QUALIFIED | CONDITIONAL | EXPIRED | REJECTED | NOT_QUALIFIED, from the
    ContractorCompany prequalification fields (the shared source of truth)."""
    now = now or utcnow()
    s = (epc_status or "not_applied").lower()
    if s in EPC_QUALIFIED:
        vu = aware(valid_until)
        if vu is not None and vu < now:
            return "EXPIRED"
        return "QUALIFIED" if s == "approved" else "CONDITIONAL"
    if s == "rejected":
        return "REJECTED"
    return "NOT_QUALIFIED"


def expiry_state(d: datetime | None, now: datetime | None = None, window_days: int = EXPIRY_WINDOW_DAYS) -> str | None:
    if d is None:
        return None
    now = now or utcnow()
    d = aware(d)
    if d < now:
        return "EXPIRED"
    if d < now + timedelta(days=window_days):
        return "DUE"
    return "OK"


# ── work orders ──────────────────────────────────────────────────────────────
def can_move_wo(current: str, target: str) -> bool:
    return target in _WO_MOVES.get(current, set())


def activation_blockers(
    *, company_status: str | None, qualification: str, checklist: dict, start: datetime, end: datetime,
    engineer_id: str | None, manpower: int, supervisors: int | None, now: datetime | None = None,
) -> list[str]:
    """A work order goes ACTIVE only when every item here is clear."""
    now = now or utcnow()
    out: list[str] = []
    block = company_block_reason(company_status)
    if block:
        out.append(f"{block}: it cannot be given work.")
    if qualification not in ("QUALIFIED", "CONDITIONAL"):
        out.append(f"Contractor is not prequalified ({qualification.replace('_', ' ').lower()}).")
    missing = [label for key, label in SAFETY_CHECKLIST if not (checklist or {}).get(key)]
    if missing:
        out.append("Safety requirements not confirmed: " + "; ".join(missing) + ".")
    if aware(end) <= aware(start):
        out.append("End date must be after the start date.")
    elif aware(end) < now:
        out.append("The work order end date has already passed.")
    if not engineer_id:
        out.append("Assign an engineer-in-charge.")
    if supervisors is not None and supervisors > 0 and manpower > 0 and manpower / supervisors > SUPERVISOR_RATIO_MAX:
        out.append(f"Supervisor ratio 1:{manpower / supervisors:.0f} is worse than 1:{SUPERVISOR_RATIO_MAX}.")
    if supervisors == 0 and manpower > 0:
        out.append("No supervisors planned for the workforce.")
    return out


def worker_clearance(
    *, wo_status: str, wo_end: datetime, company_status: str | None, qualification: str,
    medical_valid_until: datetime | None, worker_status: str | None, roster_check: dict,
    induction_valid_until: datetime | None, same_company: bool, now: datetime | None = None,
) -> dict[str, Any]:
    """Plant gate clearance for one roster worker (the EPC gate's checks,
    applied to a plant work order). Returns {checks, overall, passValidUntil}."""
    now = now or utcnow()
    checks: dict[str, dict] = {}

    def put(key: str, ok: bool, detail: str, warn: bool = False) -> None:
        checks[key] = {"result": "pass" if ok and not warn else ("warn" if ok else "fail"), "detail": detail}

    put("work_order", wo_status == "ACTIVE", f"Work order is {wo_status.lower()}")
    block = company_block_reason(company_status)
    put("contractor_status", block is None, block or "Contractor company is active")
    put("prequalification", qualification in ("QUALIFIED", "CONDITIONAL"),
        f"Prequalification: {qualification.replace('_', ' ').lower()}")
    put("company_match", same_company, "Worker belongs to the work order's contractor" if same_company
        else "Worker belongs to a different contractor")
    med = aware(medical_valid_until)
    if med is None:
        put("medical_fitness", False, "No medical fitness certificate on record")
    else:
        put("medical_fitness", med > now, f"Medical fitness valid until {med.date().isoformat()}"
            if med > now else f"Medical fitness expired on {med.date().isoformat()}")
    ws = (worker_status or "active").lower()
    put("worker_status", ws == "active", f"Worker status is '{ws}'")
    checks["safety_roster"] = {"result": roster_check.get("result", "pass"), "detail": roster_check.get("detail") or "No safety hold"}
    ind = aware(induction_valid_until)
    if ind is None:
        put("plant_induction", False, "No plant induction recorded for this work order")
    elif ind < now:
        put("plant_induction", False, f"Plant induction expired on {ind.date().isoformat()}")
    else:
        days = (ind - now).days
        put("plant_induction", True, f"Plant induction valid until {ind.date().isoformat()}", warn=days <= 14)

    fails = [k for k, c in checks.items() if c["result"] == "fail"]
    warns = [k for k, c in checks.items() if c["result"] == "warn"]
    overall = "not_cleared" if fails else ("cleared_with_warnings" if warns else "cleared")
    pass_until = None
    if not fails:
        candidates = [aware(wo_end), med, ind]
        pass_until = min(d for d in candidates if d is not None)
    return {"checks": checks, "overall": overall, "blocking": fails, "warnings": warns,
            "passValidUntil": pass_until.isoformat() if pass_until else None}


# ── evaluations ──────────────────────────────────────────────────────────────
def working_days(start: datetime, end: datetime) -> int:
    """Mon-Sat days in [start, end] (a steel plant's contractors work six days)."""
    s, e = aware(start).date(), aware(end).date()
    if e < s:
        return 0
    n = 0
    d: date = s
    while d <= e:
        if d.weekday() < 6:
            n += 1
        d += timedelta(days=1)
    return n


def evaluation_kpis(counts: dict, expected_tbt: int) -> list[dict]:
    """Auto-filled KPI rows from source counts. score None = not measured
    (excluded from the weighting, not scored as zero)."""
    rows: list[dict] = []
    lti = int(counts.get("lti", 0))
    fat = int(counts.get("fatalities", 0))
    for d in KPI_DEFS:
        code = d["code"]
        if code == "LTI_FATALITY":
            value = lti + fat
            score = 0.0 if fat else max(0.0, 100.0 - d["perEvent"] * value)
        elif code == "TBT_PARTICIPATION":
            value = int(counts.get("tbt", 0))
            score = None if expected_tbt <= 0 else round(min(100.0, 100.0 * value / expected_tbt), 1)
        else:
            key = {"INCIDENTS": "incidents", "NEAR_MISSES": "nearMisses", "UNSAFE_OBSERVATIONS": "unsafeObservations",
                   "SHOW_CAUSE": "showCause", "PERMIT_VIOLATIONS": "permitViolations"}[code]
            value = int(counts.get(key, 0))
            score = max(0.0, 100.0 - d["perEvent"] * value)
        rows.append({"code": code, "label": d["label"], "weight": d["weight"], "value": value,
                     "score": score, "target": expected_tbt if code == "TBT_PARTICIPATION" else 0})
    return rows


def rating_band(score: float | None) -> str | None:
    if score is None:
        return None
    for floor, band in RATING_BANDS:
        if score >= floor:
            return band
    return "UNACCEPTABLE"


def evaluation_score(kpis: list[dict], manual: dict, *, fatalities: int = 0) -> dict[str, Any]:
    """Auto (KPIs), manual (criteria 1-5 -> x20) and final weighted score."""
    a_num = a_den = 0.0
    for k in kpis:
        if k.get("score") is None:
            continue
        a_num += k["weight"] * k["score"]
        a_den += k["weight"]
    m_num = m_den = 0.0
    for c in MANUAL_CRITERIA:
        s = _num((manual or {}).get(c["code"], {}).get("score"))
        if s is None or not 1 <= s <= 5:
            continue
        m_num += c["weight"] * s * 20
        m_den += c["weight"]
    auto = round(a_num / a_den, 1) if a_den else None
    man = round(m_num / m_den, 1) if m_den else None
    final = round((a_num + m_num) / (a_den + m_den), 1) if (a_den + m_den) else None
    capped = False
    if final is not None and fatalities > 0 and final > FATALITY_SCORE_CAP:
        final, capped = FATALITY_SCORE_CAP, True
    return {"autoScore": auto, "manualScore": man, "finalScore": final, "ratingBand": rating_band(final),
            "fatalityCapApplied": capped}


def eval_submit_blockers(*, status: str, manual: dict) -> list[str]:
    out: list[str] = []
    if status != "DRAFT":
        out.append(f"Only a draft evaluation can be submitted (this one is {status}).")
    missing = [c["label"] for c in MANUAL_CRITERIA
               if not (_num((manual or {}).get(c["code"], {}).get("score")) or 0) in (1, 2, 3, 4, 5)]
    if missing:
        out.append("Score every manual criterion (1-5): " + "; ".join(missing) + ".")
    return out


def eval_signoff_blockers(*, status: str, evaluator_id: str | None, signer_id: str) -> list[str]:
    out: list[str] = []
    if status != "SUBMITTED":
        out.append(f"Only a submitted evaluation can be signed off (this one is {status}).")
    if evaluator_id and evaluator_id == signer_id:
        out.append("The evaluator cannot sign off their own evaluation.")
    return out


def previous_period(eval_type: str, ref: datetime | None = None) -> tuple[datetime, datetime]:
    """Default window: the previous calendar month, or the previous Indian-FY
    quarter (Apr-Jun, Jul-Sep, Oct-Dec, Jan-Mar). [start, end] inclusive days."""
    ref = aware(ref or utcnow())
    first = ref.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if eval_type == "QUARTERLY":
        q_start_month = 4 + ((ref.month - 4) // 3) * 3 if ref.month >= 4 else 1
        cur_q = first.replace(month=q_start_month)
        start = add_months(cur_q, -3)
        end = cur_q - timedelta(microseconds=1)
        return start, end
    start = add_months(first, -1)
    return start, first - timedelta(microseconds=1)


# ── sanctions ────────────────────────────────────────────────────────────────
def sanction_target_status(action: str) -> str:
    return {"SUSPEND": "SUSPENDED", "BLACKLIST": "BLACKLISTED", "REINSTATE": "ACTIVE"}[action]


def sanction_request_blockers(*, action: str, current_status: str | None, reason: str | None, pending: bool) -> list[str]:
    out: list[str] = []
    cur = (current_status or "ACTIVE").upper()
    if action not in SANCTION_ACTIONS:
        out.append(f"Action must be one of {', '.join(SANCTION_ACTIONS)}.")
        return out
    if pending:
        out.append("A sanction request for this contractor is already awaiting approval.")
    if action == "SUSPEND" and cur in ("SUSPENDED", "BLACKLISTED"):
        out.append(f"Contractor is already {cur.lower()}.")
    if action == "BLACKLIST" and cur == "BLACKLISTED":
        out.append("Contractor is already blacklisted.")
    if action == "REINSTATE" and cur not in ("SUSPENDED", "BLACKLISTED"):
        out.append("Only a suspended or blacklisted contractor can be reinstated.")
    if not (reason and len(reason.strip()) >= 10):
        out.append("Give the reason (at least 10 characters).")
    return out


def sanction_decision_blockers(*, status: str, requested_by: str, decided_by: str) -> list[str]:
    out: list[str] = []
    if status != "PENDING_APPROVAL":
        out.append(f"This request is already {status.lower().replace('_', ' ')}.")
    if requested_by == decided_by:
        out.append("The person who requested the sanction cannot approve it.")
    return out


# ── default steel-plant prequalification template ────────────────────────────
def _q(code, text, t, weight, **kw) -> dict:
    return {"code": code, "text": text, "answerType": t, "weight": weight, **kw}


DEFAULT_TEMPLATE: dict[str, Any] = {
    "code": "PLANT_CONTRACTOR_PQ",
    "name": "Plant contractor HSE prequalification",
    "description": "Steel-plant prequalification for maintenance, AMC, shutdown and project contractors.",
    "passScore": 70, "conditionalScore": 55, "validityMonths": 12, "conditionalValidityMonths": 6,
    "sections": [
        {"code": "POLICY", "title": "HSE policy and leadership", "weight": 10, "questions": [
            _q("HSE_POLICY_EXISTS", "Do you have a written HSE policy signed by your top management?", "YES_NO", 3),
            _q("HSE_POLICY_DOC", "Upload the signed HSE policy.", "DOCUMENT_REQUIRED", 3, docType="HSE_POLICY"),
            _q("HSE_OBJECTIVES", "How are HSE objectives set and reviewed? (1 = none, 5 = annual targets reviewed by management)", "SCALE_1_5", 2),
            _q("MGMT_REVIEW", "Does top management review HSE performance at least every quarter?", "YES_NO", 2),
        ]},
        {"code": "ORGANISATION", "title": "Organisation and safety officer", "weight": 12, "questions": [
            _q("SAFETY_OFFICER", "Do you employ a full-time qualified safety officer (diploma in industrial safety)?", "YES_NO", 4, critical=True),
            _q("SAFETY_OFFICER_DETAILS", "Name, qualification and phone number of the safety officer.", "TEXT", 0, required=True),
            _q("SAFETY_OFFICER_CERT", "Upload the safety officer's qualification certificate.", "DOCUMENT_REQUIRED", 2, docType="SAFETY_OFFICER_QUALIFICATION"),
            _q("SUPERVISOR_RATIO", "How many workers does one supervisor control on a typical job?", "NUMBER", 2,
               bands=[{"max": 10, "score": 1}, {"max": 15, "score": 0.7}, {"max": 25, "score": 0.3}, {"max": None, "score": 0}]),
            _q("SAFETY_COMMITTEE", "Do you hold a monthly safety meeting with worker representatives?", "YES_NO", 2),
            _q("HSE_ROLES", "How clearly are HSE roles defined for site in-charges and supervisors? (1 = not defined, 5 = written and trained)", "SCALE_1_5", 2),
        ]},
        {"code": "PERFORMANCE", "title": "Safety performance, last 3 years", "weight": 20, "questions": [
            _q("MANHOURS_3Y", "Total man-hours worked in the last 3 years.", "NUMBER", 0, required=True),
            _q("LTIFR_3Y", "Average LTIFR for the last 3 years (lost-time injuries x 1,000,000 / man-hours).", "NUMBER", 8,
               bands=[{"max": 0.5, "score": 1}, {"max": 1.0, "score": 0.8}, {"max": 2.0, "score": 0.5},
                      {"max": 4.0, "score": 0.2}, {"max": None, "score": 0}]),
            _q("FATALITIES_3Y", "Number of fatalities in the last 3 years.", "NUMBER", 6, critical=True,
               bands=[{"max": 0, "score": 1}, {"max": None, "score": 0}]),
            _q("LTI_3Y", "Number of lost-time injuries in the last 3 years.", "NUMBER", 2,
               bands=[{"max": 0, "score": 1}, {"max": 2, "score": 0.6}, {"max": 5, "score": 0.3}, {"max": None, "score": 0}]),
            _q("STATUTORY_NOTICES", "Prohibitory or improvement notices received from authorities in the last 3 years.", "NUMBER", 2,
               bands=[{"max": 0, "score": 1}, {"max": 1, "score": 0.5}, {"max": None, "score": 0}]),
            _q("INCIDENT_REPORTING", "How mature is your incident reporting and investigation? (1 = none, 5 = all incidents incl. near misses investigated with CAPA)", "SCALE_1_5", 2),
        ]},
        {"code": "STATUTORY", "title": "Statutory compliance", "weight": 22, "questions": [
            _q("LABOUR_LICENCE", "Valid labour licence under the Contract Labour (R&A) Act.", "DOCUMENT_REQUIRED", 5, critical=True, docType="LABOUR_LICENCE"),
            _q("PF_REG", "EPF registration and the latest remittance challan.", "DOCUMENT_REQUIRED", 4, critical=True, docType="PF_REGISTRATION"),
            _q("ESIC_REG", "ESIC registration and the latest contribution challan.", "DOCUMENT_REQUIRED", 4, critical=True, docType="ESIC_REGISTRATION"),
            _q("WC_INSURANCE", "Workmen's compensation / employee's compensation insurance covering every deployed worker.", "DOCUMENT_REQUIRED", 5, critical=True, docType="WC_INSURANCE"),
            _q("MIN_WAGES", "Are wages paid at or above notified minimum wages, by bank transfer?", "YES_NO", 2),
            _q("STATUTORY_REGISTERS", "Do you maintain the statutory registers (workmen, wages, attendance, accidents)?", "YES_NO", 2),
        ]},
        {"code": "CERTIFICATION", "title": "Management system certification", "weight": 8, "questions": [
            _q("ISO_45001", "ISO 45001 certificate (current).", "DOCUMENT_REQUIRED", 5, docType="ISO_45001"),
            _q("OTHER_ISO", "Are you certified to ISO 14001 or ISO 9001?", "YES_NO", 3),
        ]},
        {"code": "TRAINING", "title": "Training and competence", "weight": 10, "questions": [
            _q("INDUCTION", "Does every worker receive an HSE induction before deployment?", "YES_NO", 3),
            _q("TRAINING_SYSTEM", "How mature is your training system? (1 = none, 5 = needs analysis, calendar, records and effectiveness checks)", "SCALE_1_5", 3),
            _q("SKILLED_CERTS", "Are specialised workers certified (welders, riggers, licensed electricians, crane operators)?", "YES_NO", 2),
            _q("TRAINING_RECORDS", "Upload training records for the last 12 months.", "DOCUMENT_REQUIRED", 2, docType="TRAINING_RECORD"),
        ]},
        {"code": "PPE", "title": "Personal protective equipment", "weight": 6, "questions": [
            _q("PPE_ISSUE", "Is PPE issued free of cost to every worker, with issue records?", "YES_NO", 3),
            _q("PPE_STANDARD", "Is the PPE you issue BIS or CE certified?", "YES_NO", 2),
            _q("PPE_SPECIAL", "Availability of task-specific PPE for steel-plant hazards (aluminised suits, full-body harness, SCBA). (1 = none, 5 = all, inspected)", "SCALE_1_5", 1),
        ]},
        {"code": "EQUIPMENT", "title": "Tools and equipment fitness", "weight": 7, "questions": [
            _q("EQUIP_REGISTER", "Do you keep an inventory of tools, tackles and equipment with inspection dates?", "YES_NO", 2),
            _q("EQUIP_FITNESS", "Third-party fitness certificates for lifting tools & tackles, cranes and pressure equipment (Factories Act s.28 / s.29).", "DOCUMENT_REQUIRED", 3, docType="EQUIPMENT_FITNESS"),
            _q("ELECTRICAL_TOOLS", "Are portable electrical tools inspected every month and protected by ELCB / RCCB?", "YES_NO", 2),
        ]},
        {"code": "SUBCONTRACTING", "title": "Subcontracting control", "weight": 5, "questions": [
            _q("SUBCONTRACTS", "Do you intend to subcontract any part of the work?", "YES_NO", 1, preferredAnswer="NO"),
            _q("SUB_CONTROL", "How do you control subcontractor HSE? (1 = no control, 5 = same prequalification, supervision and audits as your own workforce)", "SCALE_1_5", 4),
        ]},
    ],
}


# ── async aggregates ─────────────────────────────────────────────────────────
def _ev(v: Any) -> Any:
    return v.value if hasattr(v, "value") else v


def _iso(d: datetime | None) -> str | None:
    return aware(d).isoformat() if d else None


async def valid_doc_types(db: AsyncSession, company_id: str, now: datetime | None = None) -> set[str]:
    from app.models.csm import CsmDocument

    now = now or utcnow()
    rows = (
        await db.execute(
            select(CsmDocument.docType, CsmDocument.expiresOn)
            .where(CsmDocument.contractorCompanyId == company_id)
            .where(CsmDocument.isDeleted.is_(False)).where(CsmDocument.status == "ACTIVE")
        )
    ).all()
    return {t for t, exp in rows if exp is None or aware(exp) >= now}


async def kpi_counts(db: AsyncSession, *, company_id: str, plant_id: str, start: datetime, end: datetime) -> dict[str, int]:
    """Source counts for one contractor at one plant over [start, end]."""
    from app.models.ims import ShowCauseNotice, ToolboxTalk
    from app.models.incident import Incident, IncidentPerson
    from app.models.near_miss import NearMiss
    from app.models.observation import Observation, ObservationType
    from app.models.permit import Permit, PermitSuspension

    inc_rows = (
        await db.execute(
            select(Incident.id, Incident.type)
            .where(Incident.id.in_(select(IncidentPerson.incidentId).where(IncidentPerson.contractorCompanyId == company_id)))
            .where(Incident.plantId == plant_id)
            .where(Incident.isDeleted.is_(False)).where(Incident.date >= start).where(Incident.date <= end)
        )
    ).all()
    types = [str(_ev(t)) for _id, t in inc_rows]
    lti = sum(1 for t in types if t == "LTI")
    fat = sum(1 for t in types if t == "FATALITY")
    other = sum(1 for t in types if t not in ("LTI", "FATALITY", "HIPO_NEAR_MISS"))

    async def cnt(stmt) -> int:
        return int((await db.execute(stmt)).scalar() or 0)

    nm = await cnt(
        select(func.count()).select_from(NearMiss).where(NearMiss.contractorCompanyId == company_id)
        .where(NearMiss.plantId == plant_id).where(NearMiss.date >= start).where(NearMiss.date <= end)
    )
    obs = await cnt(
        select(func.count()).select_from(Observation).where(Observation.contractorCompanyId == company_id)
        .where(Observation.plantId == plant_id).where(Observation.date >= start).where(Observation.date <= end)
        .where(Observation.type.in_([ObservationType.UNSAFE_ACT, ObservationType.UNSAFE_CONDITION]))
    )
    scn = await cnt(
        select(func.count()).select_from(ShowCauseNotice).where(ShowCauseNotice.contractorCompanyId == company_id)
        .where(ShowCauseNotice.plantId == plant_id).where(ShowCauseNotice.isDeleted.is_(False))
        .where(ShowCauseNotice.issuedAt >= start).where(ShowCauseNotice.issuedAt <= end)
    )
    pv = await cnt(
        select(func.count()).select_from(PermitSuspension).join(Permit, Permit.id == PermitSuspension.permitId)
        .where(Permit.contractorCompanyId == company_id).where(Permit.plantId == plant_id)
        .where(Permit.isDeleted.is_(False))
        .where(PermitSuspension.suspendedAt >= start).where(PermitSuspension.suspendedAt <= end)
    )
    tbt_count, tbt_people = (
        await db.execute(
            select(func.count(), func.coalesce(func.sum(ToolboxTalk.contractorCount), 0))
            .where(ToolboxTalk.contractorCompanyId == company_id).where(ToolboxTalk.plantId == plant_id)
            .where(ToolboxTalk.isDeleted.is_(False))
            .where(ToolboxTalk.conductedAt >= start).where(ToolboxTalk.conductedAt <= end)
        )
    ).one()
    return {"lti": lti, "fatalities": fat, "incidents": other, "nearMisses": nm, "unsafeObservations": obs,
            "showCause": scn, "permitViolations": pv, "tbt": int(tbt_count), "tbtParticipants": int(tbt_people)}


async def contractor_360(
    db: AsyncSession, *, company_id: str, plant_ids: list[str] | None, days: int = 365
) -> dict[str, Any]:
    """Execution-monitoring view of one contractor across the caller's plants."""
    from app.models.csm import CsmEvaluation, CsmSanction, CsmWorkOrder, CsmWorkOrderWorker
    from app.models.ims import ShowCauseNotice, ToolboxTalk
    from app.models.incident import Incident, IncidentPerson
    from app.models.near_miss import NearMiss
    from app.models.observation import Observation
    from app.models.permit import Permit

    now = utcnow()
    since = now - timedelta(days=days)

    def plant(col):
        return True if plant_ids is None else col.in_(plant_ids or ["__none__"])

    obs_rows = (
        await db.execute(
            select(Observation).where(Observation.contractorCompanyId == company_id).where(plant(Observation.plantId))
            .where(Observation.date >= since).order_by(Observation.date.desc()).limit(200)
        )
    ).scalars().all()
    obs_by_type: dict[str, int] = {}
    for o in obs_rows:
        k = str(_ev(o.type))
        obs_by_type[k] = obs_by_type.get(k, 0) + 1

    nm_rows = (
        await db.execute(
            select(NearMiss).where(NearMiss.contractorCompanyId == company_id).where(plant(NearMiss.plantId))
            .where(NearMiss.date >= since).order_by(NearMiss.date.desc()).limit(200)
        )
    ).scalars().all()

    # IN-subquery, not JOIN + DISTINCT: Incident carries `json` columns, which
    # Postgres cannot compare, so SELECT DISTINCT over the full row fails there.
    involved = select(IncidentPerson.incidentId).where(IncidentPerson.contractorCompanyId == company_id)
    inc_rows = (
        await db.execute(
            select(Incident).where(Incident.id.in_(involved)).where(plant(Incident.plantId))
            .where(Incident.isDeleted.is_(False)).where(Incident.date >= since)
            .order_by(Incident.date.desc()).limit(200)
        )
    ).scalars().all()
    inc_by_type: dict[str, int] = {}
    for i in inc_rows:
        k = str(_ev(i.type))
        inc_by_type[k] = inc_by_type.get(k, 0) + 1

    permit_rows = (
        await db.execute(
            select(Permit).where(Permit.contractorCompanyId == company_id).where(plant(Permit.plantId))
            .where(Permit.isDeleted.is_(False)).where(Permit.validFrom >= since)
            .order_by(Permit.validFrom.desc()).limit(200)
        )
    ).scalars().all()
    live_permit = {"ISSUED", "ACTIVE", "SUSPENDED"}

    scn_rows = (
        await db.execute(
            select(ShowCauseNotice).where(ShowCauseNotice.contractorCompanyId == company_id)
            .where(plant(ShowCauseNotice.plantId)).where(ShowCauseNotice.isDeleted.is_(False))
            .where(ShowCauseNotice.issuedAt >= since).order_by(ShowCauseNotice.issuedAt.desc()).limit(200)
        )
    ).scalars().all()

    tbt_rows = (
        await db.execute(
            select(ToolboxTalk).where(ToolboxTalk.contractorCompanyId == company_id).where(plant(ToolboxTalk.plantId))
            .where(ToolboxTalk.isDeleted.is_(False)).where(ToolboxTalk.conductedAt >= since)
            .order_by(ToolboxTalk.conductedAt.desc()).limit(200)
        )
    ).scalars().all()

    wo_rows = (
        await db.execute(
            select(CsmWorkOrder).where(CsmWorkOrder.contractorCompanyId == company_id)
            .where(plant(CsmWorkOrder.plantId)).where(CsmWorkOrder.isDeleted.is_(False))
            .order_by(CsmWorkOrder.startDate.desc()).limit(100)
        )
    ).scalars().all()
    active_wo_ids = [w.id for w in wo_rows if w.status == "ACTIVE"]
    on_site = int((
        await db.execute(
            select(func.count()).select_from(CsmWorkOrderWorker)
            .where(CsmWorkOrderWorker.workOrderId.in_(active_wo_ids or ["__none__"]))
            .where(CsmWorkOrderWorker.status == "CLEARED")
        )
    ).scalar() or 0)

    evals = (
        await db.execute(
            select(CsmEvaluation).where(CsmEvaluation.contractorCompanyId == company_id)
            .where(plant(CsmEvaluation.plantId)).where(CsmEvaluation.isDeleted.is_(False))
            .order_by(CsmEvaluation.periodEnd.asc()).limit(60)
        )
    ).scalars().all()
    sanctions = (
        await db.execute(
            select(CsmSanction).where(CsmSanction.contractorCompanyId == company_id)
            .order_by(CsmSanction.requestedAt.desc()).limit(50)
        )
    ).scalars().all()

    return {
        "windowDays": days,
        "observations": {
            "total": len(obs_rows), "byType": obs_by_type,
            "open": sum(1 for o in obs_rows if str(_ev(o.status)) != "CLOSED"),
            "recent": [{"id": o.id, "number": o.number, "date": _iso(o.date), "type": str(_ev(o.type)),
                        "severity": str(_ev(o.severity)), "status": str(_ev(o.status)), "plantId": o.plantId}
                       for o in obs_rows[:10]],
        },
        "nearMisses": {
            "total": len(nm_rows),
            "recent": [{"id": n.id, "number": n.number, "date": _iso(n.date), "severity": str(_ev(n.potentialSeverity)),
                        "status": str(_ev(n.status)), "plantId": n.plantId} for n in nm_rows[:10]],
        },
        "incidents": {
            "total": len(inc_rows), "byType": inc_by_type,
            "recent": [{"id": i.id, "number": i.number, "date": _iso(i.date), "type": str(_ev(i.type)),
                        "status": str(_ev(i.status)), "plantId": i.plantId} for i in inc_rows[:10]],
        },
        "permits": {
            "total": len(permit_rows), "live": sum(1 for p in permit_rows if str(_ev(p.status)) in live_permit),
            "recent": [{"id": p.id, "number": p.number, "type": str(_ev(p.type)), "status": str(_ev(p.status)),
                        "validFrom": _iso(p.validFrom), "plantId": p.plantId} for p in permit_rows[:10]],
        },
        "showCause": {
            "total": len(scn_rows), "open": sum(1 for s in scn_rows if s.status != "CLOSED"),
            "recent": [{"id": s.id, "noticeNo": s.noticeNo, "issuedAt": _iso(s.issuedAt), "violationCategory": s.violationCategory,
                        "status": s.status, "decision": s.decision} for s in scn_rows[:10]],
        },
        "toolboxTalks": {
            "total": len(tbt_rows), "participants": sum(t.contractorCount or 0 for t in tbt_rows),
            "recent": [{"id": t.id, "talkNo": t.talkNo, "topic": t.topic, "conductedAt": _iso(t.conductedAt),
                        "contractorCount": t.contractorCount} for t in tbt_rows[:10]],
        },
        "workOrders": {
            "active": len(active_wo_ids), "total": len(wo_rows), "workersOnSite": on_site,
            "items": [{"id": w.id, "workOrderNo": w.workOrderNo, "title": w.title, "status": w.status, "plantId": w.plantId,
                       "startDate": _iso(w.startDate), "endDate": _iso(w.endDate), "estimatedManpower": w.estimatedManpower}
                      for w in wo_rows[:20]],
        },
        "scoreTrend": [{"evaluationId": e.id, "evaluationNo": e.evaluationNo, "periodEnd": _iso(e.periodEnd),
                        "evaluationType": e.evaluationType, "finalScore": e.finalScore, "ratingBand": e.ratingBand,
                        "status": e.status} for e in evals if e.finalScore is not None],
        "sanctions": [{"id": s.id, "sanctionNo": s.sanctionNo, "action": s.action, "status": s.status,
                       "reasonCategory": s.reasonCategory, "reason": s.reason, "requestedAt": _iso(s.requestedAt),
                       "decidedAt": _iso(s.decidedAt), "effectiveUntil": _iso(s.effectiveUntil), "plantId": s.plantId,
                       "requestedById": s.requestedById, "decidedById": s.decidedById, "decisionNotes": s.decisionNotes,
                       "evidence": s.evidence or []}
                      for s in sanctions],
    }


async def expiring(db: AsyncSession, *, plant_ids: list[str] | None, company_ids: list[str] | None,
                   days: int = EXPIRY_WINDOW_DAYS) -> dict[str, Any]:
    """Prequalifications and documents expired or due within `days`."""
    from app.models.csm import CsmDocument, CsmPrequalification
    from app.models.epc import ContractorCompany

    now = utcnow()
    horizon = now + timedelta(days=days)
    pq_stmt = (
        select(CsmPrequalification).where(CsmPrequalification.isDeleted.is_(False))
        .where(CsmPrequalification.isCurrent.is_(True))
        .where(CsmPrequalification.status.in_(("QUALIFIED", "CONDITIONAL")))
        .where(CsmPrequalification.validUntil.is_not(None)).where(CsmPrequalification.validUntil < horizon)
    )
    if plant_ids is not None:
        pq_stmt = pq_stmt.where(CsmPrequalification.plantId.in_(plant_ids or ["__none__"]))
    pqs = (await db.execute(pq_stmt.order_by(CsmPrequalification.validUntil.asc()).limit(500))).scalars().all()

    doc_stmt = (
        select(CsmDocument).where(CsmDocument.isDeleted.is_(False)).where(CsmDocument.status == "ACTIVE")
        .where(CsmDocument.expiresOn.is_not(None)).where(CsmDocument.expiresOn < horizon)
    )
    if company_ids is not None:
        doc_stmt = doc_stmt.where(CsmDocument.contractorCompanyId.in_(company_ids or ["__none__"]))
    docs = (await db.execute(doc_stmt.order_by(CsmDocument.expiresOn.asc()).limit(500))).scalars().all()

    ids = {p.contractorCompanyId for p in pqs} | {d.contractorCompanyId for d in docs}
    names = {}
    if ids:
        names = {c.id: c.name for c in (await db.execute(select(ContractorCompany).where(ContractorCompany.id.in_(ids)))).scalars()}
    # A renewal already in progress is shown, so nobody starts a second one.
    open_renewals = set()
    if pqs:
        open_renewals = set((
            await db.execute(
                select(CsmPrequalification.contractorCompanyId).where(CsmPrequalification.isDeleted.is_(False))
                .where(CsmPrequalification.status.in_(PQ_OPEN))
                .where(CsmPrequalification.contractorCompanyId.in_({p.contractorCompanyId for p in pqs}))
            )
        ).scalars().all())
    return {
        "windowDays": days,
        "prequalifications": [
            {"id": p.id, "applicationNo": p.applicationNo, "contractorCompanyId": p.contractorCompanyId,
             "contractorName": names.get(p.contractorCompanyId), "plantId": p.plantId, "status": p.status,
             "validUntil": _iso(p.validUntil), "state": expiry_state(p.validUntil, now, days),
             "renewalInProgress": p.contractorCompanyId in open_renewals}
            for p in pqs
        ],
        "documents": [
            {"id": d.id, "contractorCompanyId": d.contractorCompanyId, "contractorName": names.get(d.contractorCompanyId),
             "docType": d.docType, "title": d.title, "fileName": d.fileName, "expiresOn": _iso(d.expiresOn),
             "state": expiry_state(d.expiresOn, now, days)}
            for d in docs
        ],
    }


async def hub(db: AsyncSession, *, plant_ids: list[str] | None) -> dict[str, Any]:
    """CSM hub: pipeline by stage, qualification KPIs, approvals waiting."""
    from app.models.csm import CsmEvaluation, CsmPrequalification, CsmSanction, CsmWorkOrder, CsmWorkOrderWorker

    def plant(col):
        return True if plant_ids is None else col.in_(plant_ids or ["__none__"])

    now = utcnow()
    pq_rows = (
        await db.execute(
            select(CsmPrequalification.status, func.count()).where(CsmPrequalification.isDeleted.is_(False))
            .where(plant(CsmPrequalification.plantId)).group_by(CsmPrequalification.status)
        )
    ).all()
    pipeline = {s: 0 for s in PQ_STATUSES}
    for s, n in pq_rows:
        pipeline[s] = int(n)
    current = (
        await db.execute(
            select(CsmPrequalification.status, CsmPrequalification.validUntil)
            .where(CsmPrequalification.isDeleted.is_(False)).where(plant(CsmPrequalification.plantId))
            .where(CsmPrequalification.isCurrent.is_(True))
        )
    ).all()
    qualified = sum(1 for s, vu in current if s == "QUALIFIED" and (vu is None or aware(vu) >= now))
    conditional = sum(1 for s, vu in current if s == "CONDITIONAL" and (vu is None or aware(vu) >= now))
    expired = sum(1 for s, vu in current if s in ("QUALIFIED", "CONDITIONAL") and vu is not None and aware(vu) < now)

    wo_rows = (
        await db.execute(
            select(CsmWorkOrder.status, func.count()).where(CsmWorkOrder.isDeleted.is_(False))
            .where(plant(CsmWorkOrder.plantId)).group_by(CsmWorkOrder.status)
        )
    ).all()
    wo = {s: 0 for s in WO_STATUSES}
    for s, n in wo_rows:
        wo[s] = int(n)
    on_site = int((
        await db.execute(
            select(func.count()).select_from(CsmWorkOrderWorker).join(CsmWorkOrder, CsmWorkOrder.id == CsmWorkOrderWorker.workOrderId)
            .where(CsmWorkOrder.status == "ACTIVE").where(plant(CsmWorkOrder.plantId))
            .where(CsmWorkOrderWorker.status == "CLEARED")
        )
    ).scalar() or 0)

    q_start = now - timedelta(days=90)
    ev = (
        await db.execute(
            select(CsmEvaluation.status, CsmEvaluation.finalScore, CsmEvaluation.ratingBand)
            .where(CsmEvaluation.isDeleted.is_(False)).where(plant(CsmEvaluation.plantId))
            .where(CsmEvaluation.periodEnd >= q_start)
        )
    ).all()
    signed = [r for r in ev if r[0] == "SIGNED_OFF" and r[1] is not None]
    bands: dict[str, int] = {}
    for r in signed:
        bands[r[2]] = bands.get(r[2], 0) + 1

    pending_sanctions = int((
        await db.execute(
            select(func.count()).select_from(CsmSanction).where(CsmSanction.status == "PENDING_APPROVAL")
            .where(plant(CsmSanction.plantId))
        )
    ).scalar() or 0)
    sanctioned = (
        await db.execute(
            select(CsmSanction.contractorCompanyId, CsmSanction.action).where(CsmSanction.status == "APPROVED")
            .where(plant(CsmSanction.plantId)).order_by(CsmSanction.decidedAt.asc())
        )
    ).all()
    latest: dict[str, str] = {}
    for cid, action in sanctioned:
        latest[cid] = action
    return {
        "pipeline": pipeline,
        "qualification": {"qualified": qualified, "conditional": conditional, "expired": expired,
                          "suspended": sum(1 for a in latest.values() if a == "SUSPEND"),
                          "blacklisted": sum(1 for a in latest.values() if a == "BLACKLIST")},
        "workOrders": {**wo, "workersOnSite": on_site},
        "evaluations": {"last90Days": len(ev), "signedOff": len(signed),
                        "averageScore": round(sum(r[1] for r in signed) / len(signed), 1) if signed else None,
                        "byBand": bands, "awaitingSignOff": sum(1 for r in ev if r[0] == "SUBMITTED")},
        "approvals": {"prequalifications": pipeline.get("APPROVAL", 0), "reviews": pipeline.get("SUBMITTED", 0) + pipeline.get("HSE_REVIEW", 0),
                      "sanctions": pending_sanctions},
    }

