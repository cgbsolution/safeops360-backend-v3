"""PSSR rules: vocabularies, the default steel-plant template, and the go / no-go gate.

Everything here is pure (plain values in, plain values out, no DB) so the gate
is unit-tested offline in tests/test_pssr.py. The router loads rows, hands them
to these functions as `ItemState` / `SignState`, and persists the outcome.

The gate (spec, JSL Hisar "MOC & PSSR"):
  APPROVED_GO only when
    * every checklist item is answered (YES / NO / NA),
    * every NO is a punch item with an owner and a due date,
    * every category-A punch item (must close before start-up) is CLOSED, and
    * every required discipline has a current APPROVE.
  Any current REJECT makes the review NO_GO, with the rejecting comments as the
  reason. Category-B punch items (may close after start-up) do not hold the GO;
  the review is CLOSED once they are all closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

# ── vocabularies ─────────────────────────────────────────────────────────────
TRIGGERS = ("NEW_FACILITY", "MODIFICATION", "TURNAROUND", "RESTART_AFTER_SHUTDOWN", "OTHER")
STATUSES = ("DRAFT", "IN_PROGRESS", "PENDING_APPROVAL", "APPROVED_GO", "NO_GO", "CLOSED")
OPEN_STATUSES = ("DRAFT", "IN_PROGRESS", "PENDING_APPROVAL", "NO_GO")
RESPONSES = ("YES", "NO", "NA")
PUNCH_CATEGORIES = ("A", "B")
PUNCH_STATUSES = ("OPEN", "CLOSED")
DECISIONS = ("APPROVE", "REJECT")
DISCIPLINES = ("PROCESS", "MECHANICAL", "ELECTRICAL", "INSTRUMENTATION", "SAFETY", "OPERATIONS", "PLANT_HEAD")
# Always on the matrix, whatever the leader picks: no start-up without the
# safety function, the people who will run it, and the plant head.
MANDATORY_DISCIPLINES = ("SAFETY", "OPERATIONS", "PLANT_HEAD")

DISCIPLINE_LABEL = {
    "PROCESS": "Process", "MECHANICAL": "Mechanical", "ELECTRICAL": "Electrical",
    "INSTRUMENTATION": "Instrumentation", "SAFETY": "Safety", "OPERATIONS": "Operations",
    "PLANT_HEAD": "Plant Head",
}

# Responses can be changed only while the checklist is being worked.
EDITABLE_STATUSES = ("DRAFT", "IN_PROGRESS")
# Punch items can be worked (owner, due, close) until the review is closed.
PUNCH_WORK_STATUSES = ("DRAFT", "IN_PROGRESS", "PENDING_APPROVAL", "APPROVED_GO", "NO_GO")

# The trigger a MOC-started PSSR gets.
MOC_TRIGGER = "MODIFICATION"


# ── default steel-plant template ─────────────────────────────────────────────
# (section code, section title, [(question, discipline, default punch category, guidance)])
# The same content is seeded into PssrTemplate / PssrTemplateItem by
# safeops_360/prisma/apply-pssr-psi-ddl.ts (tests/test_pssr.py checks they agree).
DEFAULT_TEMPLATE_CODE = "STEEL_PLANT_DEFAULT"
DEFAULT_TEMPLATE_ID = "pssr-tpl-steel-default"
DEFAULT_TEMPLATE_NAME = "Steel plant PSSR (default)"

DEFAULT_TEMPLATE: list[tuple[str, str, list[tuple[str, str, str, str | None]]]] = [
    ("DESIGN_CONSTRUCTION", "Design & construction per specification", [
        ("Equipment installed per the approved design, drawings and specifications", "MECHANICAL", "A", "Walk down against the IFC drawings; note every field change."),
        ("Materials of construction verified against specification (PMI done for alloy / stainless piping and fittings)", "MECHANICAL", "A", None),
        ("Civil and structural work complete: foundations grouted, platforms, handrails and toe boards fitted", "MECHANICAL", "A", None),
        ("Deviations from the approved design captured and approved through MOC", "PROCESS", "A", None),
    ]),
    ("PID_ASBUILT", "P&ID / as-built drawings updated", [
        ("Red-lined P&IDs and layouts reflecting the installed system are available in the control room", "PROCESS", "A", None),
        ("Final as-built P&IDs, PFDs and layouts issued to the PSI library", "PROCESS", "B", "May follow start-up; track as a B punch with a due date."),
        ("Equipment datasheets, line list and instrument index updated", "PROCESS", "B", None),
    ]),
    ("OPERATING_PROCEDURES", "Operating procedures", [
        ("Start-up, normal operation, shutdown and emergency shutdown SOPs written or updated and approved", "OPERATIONS", "A", None),
        ("Safe operating limits and the consequences of deviation documented", "PROCESS", "A", None),
        ("Maintenance procedures and preventive maintenance schedule set up in the CMMS", "MECHANICAL", "B", None),
    ]),
    ("TRAINING", "Training done", [
        ("Operators trained on the new or modified procedures; training records available", "OPERATIONS", "A", None),
        ("Maintenance staff and contractors briefed on the change and its hazards", "OPERATIONS", "B", None),
        ("Emergency response team briefed on new hazards and response", "SAFETY", "A", None),
    ]),
    ("PHA_RECOMMENDATIONS", "PHA / HAZOP recommendations closed", [
        ("PHA / HAZOP / HIRA completed for the new or changed facility", "PROCESS", "A", None),
        ("All pre-start-up PHA / HAZOP recommendations closed, or formally deferred with risk acceptance", "PROCESS", "A", None),
        ("HIRA register updated for new or changed activities", "SAFETY", "B", None),
    ]),
    ("MOC_COMPLETE", "MOC complete", [
        ("MOC approved and every pre-start-up MOC action complete", "PROCESS", "A", None),
        ("Temporary changes carry an expiry date and a removal plan", "PROCESS", "A", None),
    ]),
    ("EQUIPMENT_INTEGRITY", "Equipment integrity & tests", [
        ("Pressure / leak / hydro tests done for vessels, piping and hydraulic systems; certificates available", "MECHANICAL", "A", None),
        ("Rotating equipment aligned, lubricated and no-load trial run completed", "MECHANICAL", "A", None),
        ("Cranes, hoists and lifting tackle load-tested and certified by a competent person", "MECHANICAL", "A", None),
        ("Refractory dry-out / heating curve completed for furnaces, ladles and tundishes where applicable", "MECHANICAL", "A", None),
        ("Water-cooled elements (panels, moulds, tuyeres) pressure-tested; cooling water flow and low-flow alarms proven", "MECHANICAL", "A", "Water in contact with molten metal is an explosion hazard."),
    ]),
    ("RELIEF_SAFETY", "Relief / safety systems tested", [
        ("Relief valves and rupture discs set, tested and tagged; relief paths unobstructed", "INSTRUMENTATION", "A", None),
        ("Emergency stops and emergency trip systems function-tested", "INSTRUMENTATION", "A", None),
        ("Fixed gas detection (CO, O2 deficiency, flammable gas) calibrated and alarming at the right locations", "INSTRUMENTATION", "A", None),
    ]),
    ("INTERLOCKS_ALARMS", "Interlocks & alarms", [
        ("Safety interlocks / SIFs proof-tested; every bypass removed and logged", "INSTRUMENTATION", "A", None),
        ("Alarm set points match the safe operating limits; alarms tested through to DCS / PLC", "INSTRUMENTATION", "A", None),
        ("Control logic changes backed up and documented", "INSTRUMENTATION", "B", None),
    ]),
    ("FIRE_EMERGENCY", "Fire protection & emergency response ready", [
        ("Fire detection and suppression (hydrants, sprinklers, extinguishers, cable gallery protection) in place and tested", "SAFETY", "A", None),
        ("Escape routes, emergency lighting, signage and assembly points available", "SAFETY", "A", None),
        ("On-site emergency plan updated for the new hazards (molten metal, gas leak, chemical spill)", "SAFETY", "B", None),
        ("Safety showers and eyewash stations working where chemicals are handled", "SAFETY", "A", None),
    ]),
    ("ELECTRICAL_CLASSIFICATION", "Electrical classification", [
        ("Hazardous area classification done and equipment Ex-rated for its zone", "ELECTRICAL", "A", None),
        ("Earthing, bonding and lightning protection tested; insulation resistance values recorded", "ELECTRICAL", "A", None),
        ("Protection relay settings tested; switchgear labelled; single-line diagram updated", "ELECTRICAL", "B", None),
    ]),
    ("LOTO_PTW", "LOTO / PTW readiness", [
        ("Isolation points identified, labelled and included in LOTO procedures", "OPERATIONS", "A", None),
        ("All work permits on the system closed; temporary blinds / spades removed and the blind list reconciled", "OPERATIONS", "A", None),
        ("Mechanical completion handed over from projects / maintenance to operations and signed", "OPERATIONS", "A", None),
    ]),
    ("HOUSEKEEPING_ACCESS", "Housekeeping & access", [
        ("Scaffolding, debris and temporary materials removed; area clean", "OPERATIONS", "B", None),
        ("Safe access and egress, machine guards, barricades, equipment nameplates and line markings in place", "SAFETY", "A", None),
        ("PPE requirements posted and the PPE available at the point of use", "SAFETY", "B", None),
    ]),
    ("ENVIRONMENTAL", "Environmental", [
        ("Emission controls (fume extraction, bag filters, dedusting) commissioned", "PROCESS", "A", None),
        ("Effluent, drainage, spill containment and waste disposal arrangements ready", "PROCESS", "B", None),
        ("Statutory consents (CTO, PESO, boiler, factory licence amendment) obtained where required", "SAFETY", "A", None),
    ]),
]


def default_template_rows() -> list[dict[str, Any]]:
    """The default template flattened to item rows (for copying into a review
    when no template has been seeded, and for the seed consistency test)."""
    out: list[dict[str, Any]] = []
    for s_idx, (_code, title, items) in enumerate(DEFAULT_TEMPLATE, start=1):
        for i_idx, (question, discipline, cat, guidance) in enumerate(items, start=1):
            out.append({
                "id": f"{DEFAULT_TEMPLATE_ID}-{s_idx:02d}-{i_idx:02d}",
                "section": title, "sectionOrder": s_idx, "itemOrder": i_idx, "question": question,
                "guidance": guidance, "discipline": discipline, "defaultPunchCategory": cat,
            })
    return out


# ── state the gate reads ─────────────────────────────────────────────────────
@dataclass
class ItemState:
    id: str
    question: str = ""
    response: str | None = None
    punchCategory: str | None = None
    punchStatus: str | None = None
    punchOwnerId: str | None = None
    punchDueDate: datetime | None = None


@dataclass
class SignState:
    discipline: str
    decision: str
    userId: str
    comment: str | None = None
    signedAt: datetime | None = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalise_disciplines(requested: Iterable[str] | None) -> list[str]:
    """Requested sign-off disciplines + the mandatory ones, in matrix order.
    Raises ValueError on an unknown discipline."""
    req = {d.strip().upper() for d in (requested or []) if d and d.strip()}
    bad = sorted(req - set(DISCIPLINES))
    if bad:
        raise ValueError(f"Unknown discipline(s): {', '.join(bad)}. Use {', '.join(DISCIPLINES)}")
    req |= set(MANDATORY_DISCIPLINES)
    return [d for d in DISCIPLINES if d in req]


def is_punch(item: ItemState) -> bool:
    return item.response == "NO"


def punch_fields_for_response(response: str | None, default_category: str) -> dict[str, Any]:
    """Punch columns implied by a new response. A NO opens a punch item with the
    template's default category; YES / NA / unanswered clears it."""
    if response == "NO":
        return {"punchCategory": default_category if default_category in PUNCH_CATEGORIES else "A", "punchStatus": "OPEN"}
    return {
        "punchCategory": None, "punchStatus": None, "punchOwnerId": None, "punchDueDate": None,
        "punchClosedAt": None, "punchClosedById": None, "punchClosureNote": None, "punchDescription": None,
    }


def checklist_summary(items: list[ItemState]) -> dict[str, int]:
    s = {"total": len(items), "answered": 0, "yes": 0, "no": 0, "na": 0, "unanswered": 0,
         "punchAOpen": 0, "punchAClosed": 0, "punchBOpen": 0, "punchBClosed": 0, "punchIncomplete": 0}
    for it in items:
        if it.response in RESPONSES:
            s["answered"] += 1
            s[it.response.lower()] += 1
        else:
            s["unanswered"] += 1
        if is_punch(it):
            key = f"punch{it.punchCategory or 'A'}{'Closed' if it.punchStatus == 'CLOSED' else 'Open'}"
            s[key] += 1
            if not punch_complete(it):
                s["punchIncomplete"] += 1
    return s


def punch_complete(item: ItemState) -> bool:
    """A NO is a valid punch item only with a category, an owner and a due date."""
    return bool(item.punchCategory in PUNCH_CATEGORIES and item.punchOwnerId and item.punchDueDate)


def _short(q: str, n: int = 60) -> str:
    q = (q or "").strip()
    return q if len(q) <= n else q[: n - 1] + "…"


def checklist_blockers(items: list[ItemState]) -> list[str]:
    """Reasons the checklist is not ready for sign-off."""
    out: list[str] = []
    if not items:
        return ["The checklist has no items."]
    unanswered = [i for i in items if i.response not in RESPONSES]
    if unanswered:
        out.append(f"{len(unanswered)} checklist item(s) unanswered.")
    incomplete = [i for i in items if is_punch(i) and not punch_complete(i)]
    if incomplete:
        out.append(
            f"{len(incomplete)} NO answer(s) are not complete punch items (need category, owner and due date): "
            + "; ".join(_short(i.question) for i in incomplete[:3]) + ("…" if len(incomplete) > 3 else "")
        )
    return out


def open_a_punch(items: list[ItemState]) -> list[ItemState]:
    return [i for i in items if is_punch(i) and (i.punchCategory or "A") == "A" and i.punchStatus != "CLOSED"]


def open_b_punch(items: list[ItemState]) -> list[ItemState]:
    return [i for i in items if is_punch(i) and i.punchCategory == "B" and i.punchStatus != "CLOSED"]


def current_signoffs(signoffs: list[SignState]) -> dict[str, SignState]:
    """Latest decision per discipline (callers pass only isCurrent rows; if two
    slip through, the later one wins)."""
    out: dict[str, SignState] = {}
    for s in sorted(signoffs, key=lambda x: x.signedAt or datetime.min.replace(tzinfo=timezone.utc)):
        out[s.discipline] = s
    return out


def signoff_matrix(required: list[str], signoffs: list[SignState]) -> list[dict[str, Any]]:
    cur = current_signoffs(signoffs)
    rows = []
    for d in DISCIPLINES:
        s = cur.get(d)
        if d not in required and s is None:
            continue
        rows.append({
            "discipline": d, "label": DISCIPLINE_LABEL[d], "required": d in required,
            "decision": s.decision if s else None, "userId": s.userId if s else None,
            "comment": s.comment if s else None, "signedAt": s.signedAt.isoformat() if s and s.signedAt else None,
        })
    return rows


def no_go_reason(signoffs: list[SignState]) -> str | None:
    rej = [s for s in current_signoffs(signoffs).values() if s.decision == "REJECT"]
    if not rej:
        return None
    return "; ".join(f"{DISCIPLINE_LABEL.get(s.discipline, s.discipline)}: {(s.comment or 'rejected').strip()}" for s in rej)


def gate(items: list[ItemState], signoffs: list[SignState], required: list[str]) -> dict[str, Any]:
    """The go / no-go evaluation. decision is GO, NO_GO or PENDING."""
    blockers = checklist_blockers(items)
    a_open = open_a_punch(items)
    if a_open:
        blockers.append(
            f"{len(a_open)} category-A punch item(s) still open (must close before start-up): "
            + "; ".join(_short(i.question) for i in a_open[:3]) + ("…" if len(a_open) > 3 else "")
        )
    cur = current_signoffs(signoffs)
    missing = [d for d in required if d not in cur or cur[d].decision != "APPROVE"]
    missing_unsigned = [d for d in missing if d not in cur]
    if missing_unsigned:
        blockers.append("Sign-off pending from: " + ", ".join(DISCIPLINE_LABEL.get(d, d) for d in missing_unsigned) + ".")
    reason = no_go_reason(signoffs)
    if reason:
        decision = "NO_GO"
    elif blockers:
        decision = "PENDING"
    else:
        decision = "GO"
    return {
        "decision": decision, "blockers": blockers, "noGoReason": reason,
        "openA": len(a_open), "openB": len(open_b_punch(items)),
        "missingSignoffs": missing, "summary": checklist_summary(items),
    }


def status_after_evaluation(status: str, g: dict[str, Any]) -> str:
    """Status move implied by a gate evaluation. Only a review awaiting
    approval moves: to NO_GO on any rejection, to APPROVED_GO when clear."""
    if status != "PENDING_APPROVAL":
        return status
    if g["decision"] == "NO_GO":
        return "NO_GO"
    if g["decision"] == "GO":
        return "APPROVED_GO"
    return status


def submit_blockers(status: str, items: list[ItemState]) -> list[str]:
    """Reasons a review cannot be sent for sign-off yet."""
    out: list[str] = []
    if status not in EDITABLE_STATUSES:
        out.append(f"Only a DRAFT or IN_PROGRESS review can be submitted (this one is {status}).")
    out += checklist_blockers(items)
    return out


def close_blockers(status: str, items: list[ItemState]) -> list[str]:
    out: list[str] = []
    if status != "APPROVED_GO":
        out.append(f"Only an APPROVED_GO review can be closed (this one is {status}).")
    b_open = open_b_punch(items)
    if b_open:
        out.append(f"{len(b_open)} category-B punch item(s) still open.")
    return out


def signoff_blockers(status: str, discipline: str, required: list[str], decision: str, comment: str | None) -> list[str]:
    out: list[str] = []
    if status != "PENDING_APPROVAL":
        out.append(f"Sign-off is recorded only while the review is PENDING_APPROVAL (this one is {status}).")
    if discipline not in DISCIPLINES:
        out.append(f"Unknown discipline '{discipline}'.")
    elif discipline not in required:
        out.append(f"{DISCIPLINE_LABEL[discipline]} is not on this review's sign-off matrix.")
    if decision not in DECISIONS:
        out.append("Decision must be APPROVE or REJECT.")
    elif decision == "REJECT" and not (comment and comment.strip()):
        out.append("A rejection needs a reason.")
    return out


def capa_blockers(item: ItemState, capa_id: str | None) -> list[str]:
    """Only an open category-B punch item spawns a CAPA. A-items must be closed
    before start-up, so a CAPA (90-day horizon) is the wrong tool for them."""
    out: list[str] = []
    if not is_punch(item):
        out.append("Only a NO answer (punch item) can raise a CAPA.")
    elif item.punchCategory != "B":
        out.append("Only category-B punch items raise CAPAs; category-A items must be closed before start-up.")
    elif item.punchStatus == "CLOSED":
        out.append("The punch item is already closed.")
    if capa_id:
        out.append("A CAPA has already been raised for this punch item.")
    return out


def can_move_punch_category(status: str) -> bool:
    """Re-categorising A <-> B changes what holds the start-up, so it is only
    allowed while the checklist is still being worked."""
    return status in EDITABLE_STATUSES


def can_reopen(status: str) -> bool:
    return status in ("PENDING_APPROVAL", "NO_GO")


def punch_overdue(item: ItemState, now: datetime | None = None) -> bool:
    if not is_punch(item) or item.punchStatus == "CLOSED" or item.punchDueDate is None:
        return False
    due = item.punchDueDate if item.punchDueDate.tzinfo else item.punchDueDate.replace(tzinfo=timezone.utc)
    return due < (now or utcnow())
