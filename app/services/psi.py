"""PSI library + chemical register + Chem advisor: pure rules.

No DB access here (the router loads rows and passes plain values), so the
register flags, the PSI completeness view and the advisor's grounding /
citation shaping are unit-tested offline in tests/test_psi.py.

Advisor contract (POST /api/psi/advisor/ask):
  * The facts are the plant's own HazardousMaterial rows (+ the linked SDS
    document metadata) and the rule-based compatibility matrix
    (services/psi_compat.py). The model is told to answer from those facts and
    to mark anything else as general guidance.
  * Citations are the register records the answer used. A record id the model
    returns that was not in the facts it was given is dropped, never shown.
  * When the AI is not configured or fails, the endpoint still answers: the
    register facts and the compatibility result, composed by `fallback_answer`.
  * Model name, cost and latency are never returned (logged server-side only).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

# ── PSI document vocabulary ──────────────────────────────────────────────────
DOC_TYPES = (
    "PID", "PFD", "DESIGN_BASIS", "RELIEF_SYSTEM_DESIGN", "ELECTRICAL_AREA_CLASSIFICATION",
    "MATERIAL_AND_ENERGY_BALANCE", "EQUIPMENT_DATASHEET", "SAFE_OPERATING_LIMITS", "SDS", "OTHER",
)
DOC_TYPE_LABEL = {
    "PID": "P&ID", "PFD": "PFD", "DESIGN_BASIS": "Design basis", "RELIEF_SYSTEM_DESIGN": "Relief system design",
    "ELECTRICAL_AREA_CLASSIFICATION": "Electrical area classification",
    "MATERIAL_AND_ENERGY_BALANCE": "Material & energy balance", "EQUIPMENT_DATASHEET": "Equipment datasheet",
    "SAFE_OPERATING_LIMITS": "Safe operating limits", "SDS": "Safety data sheet (SDS)", "OTHER": "Other",
}
DOC_STATUSES = ("CURRENT", "SUPERSEDED")
# What a process unit's PSI set should hold (OSHA 1910.119(d) / IS 18001-style PSI
# elements). SDSs are per chemical and tracked in the chemical register instead.
REQUIRED_UNIT_DOC_TYPES = (
    "PID", "PFD", "DESIGN_BASIS", "RELIEF_SYSTEM_DESIGN", "ELECTRICAL_AREA_CLASSIFICATION",
    "MATERIAL_AND_ENERGY_BALANCE", "EQUIPMENT_DATASHEET", "SAFE_OPERATING_LIMITS",
)
UNASSIGNED_UNIT = "(No unit / area)"

# An SDS older than this with no explicit review date is treated as due for
# review (common supplier / REACH practice; MSIHC requires it to be current).
SDS_REVIEW_YEARS = 3
DUE_SOON_DAYS = 30

SDS_STATUSES = ("CURRENT", "DUE_SOON", "EXPIRED", "MISSING")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(d: datetime | None) -> datetime | None:
    if d is None:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def effective_review_due(review_due: datetime | None, issued: datetime | None) -> datetime | None:
    if review_due:
        return _aware(review_due)
    if issued:
        return _aware(issued) + timedelta(days=365 * SDS_REVIEW_YEARS)
    return None


def sds_status(sds_doc: dict[str, Any] | None, now: datetime | None = None) -> str:
    """Status of a chemical's SDS from its CURRENT SDS document (or None).
    A document without an uploaded file does not count as an SDS on file."""
    now = now or utcnow()
    if not sds_doc or not sds_doc.get("hasFile"):
        return "MISSING"
    due = effective_review_due(sds_doc.get("reviewDue"), sds_doc.get("issuedDate"))
    if due is None:
        return "CURRENT"
    if due < now:
        return "EXPIRED"
    if due < now + timedelta(days=DUE_SOON_DAYS):
        return "DUE_SOON"
    return "CURRENT"


def over_max(quantity: float | None, max_qty: float | None) -> bool:
    return bool(max_qty is not None and max_qty > 0 and (quantity or 0) > max_qty)


def utilisation(quantity: float | None, max_qty: float | None) -> float | None:
    if not max_qty or max_qty <= 0:
        return None
    return round(100.0 * (quantity or 0) / max_qty, 1)


def storage_label(building: str | None, room: str | None) -> str | None:
    parts = [p.strip() for p in (building, room) if p and p.strip()]
    return " / ".join(parts) or None


def doc_review_overdue(status: str, review_due: datetime | None, now: datetime | None = None) -> bool:
    return status == "CURRENT" and review_due is not None and _aware(review_due) < (now or utcnow())


def completeness(docs: Iterable[dict[str, Any]], units: Iterable[str] = (), now: datetime | None = None) -> list[dict[str, Any]]:
    """Per unit/area: which required PSI doc types have a CURRENT document.
    `docs` are dicts with unitArea, docType, status, reviewDue, hasFile, docNo, revision, id.
    `units` adds units that have no documents yet (shown at 0%)."""
    now = now or utcnow()
    by_unit: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for u in units:
        if u and u.strip():
            by_unit.setdefault(u.strip(), {})
    for d in docs:
        if d.get("status") != "CURRENT" or d.get("docType") == "SDS":
            continue
        unit = (d.get("unitArea") or "").strip() or UNASSIGNED_UNIT
        by_unit.setdefault(unit, {}).setdefault(d["docType"], []).append(d)
    out = []
    for unit in sorted(by_unit, key=lambda u: (u == UNASSIGNED_UNIT, u.lower())):
        types = by_unit[unit]
        cells = []
        for t in REQUIRED_UNIT_DOC_TYPES:
            ds = types.get(t, [])
            present = bool(ds)
            cells.append({
                "docType": t, "label": DOC_TYPE_LABEL[t], "present": present,
                "count": len(ds), "withFile": sum(1 for d in ds if d.get("hasFile")),
                "reviewOverdue": any(doc_review_overdue("CURRENT", d.get("reviewDue"), now) for d in ds),
                "docs": [{"id": d.get("id"), "docNo": d.get("docNo"), "revision": d.get("revision")} for d in ds[:5]],
            })
        have = sum(1 for c in cells if c["present"])
        out.append({
            "unitArea": unit, "required": len(REQUIRED_UNIT_DOC_TYPES), "present": have,
            "percent": round(100.0 * have / len(REQUIRED_UNIT_DOC_TYPES)),
            "missing": [c["docType"] for c in cells if not c["present"]],
            "reviewOverdue": sum(1 for c in cells if c["reviewOverdue"]),
            "otherDocs": sum(len(v) for k, v in types.items() if k not in REQUIRED_UNIT_DOC_TYPES),
            "cells": cells,
        })
    return out


def revision_blockers(existing_revisions: Iterable[str], revision: str) -> list[str]:
    rev = (revision or "").strip()
    if not rev:
        return ["Revision is required."]
    if rev.lower() in {r.strip().lower() for r in existing_revisions if r}:
        return [f"Revision '{rev}' of this document already exists."]
    return []


# ── Chem advisor ─────────────────────────────────────────────────────────────
ADVISOR_DISCLAIMER = (
    "Screening guidance from the plant chemical register and rule tables. Verify against the current SDS "
    "for the exact product before acting, and follow the site emergency plan in an emergency."
)
MAX_CHEMICALS = 8

TOPICS = {
    "storage": r"stor|keep|warehouse|location|where",
    "segregation": r"segregat|compatib|together|mix|next to|alongside|near|incompat",
    "ppe": r"\bppe\b|glove|goggle|respirat|apron|face shield|protect|wear",
    "first_aid": r"first aid|splash|eye|skin|inhal|swallow|ingest|exposure symptom",
    "spill": r"spill|leak|release|drain|clean ?up|absorb",
    "fire": r"fire|extinguish|foam|powder|\bco2\b|water jet|burn",
    "exposure_limits": r"\btlv\b|\bpel\b|\bstel\b|\btwa\b|exposure limit|\bidlh\b|\boel\b|ppm|mg/m",
    "quantity": r"quantit|how much|stock|inventory|max",
}


def detect_topics(question: str) -> list[str]:
    q = (question or "").lower()
    return [t for t, pat in TOPICS.items() if re.search(pat, q)] or ["general"]


def _tokens(s: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9%-]+", (s or "").lower()) if t]


# Plant shorthand -> a word in the register name.
ALIASES = {
    "hf": "hydrofluoric", "hcl": "hydrochloric", "hno3": "nitric", "h2so4": "sulphuric", "sulfuric": "sulphuric",
    "naoh": "hydroxide", "caustic": "hydroxide", "koh": "hydroxide", "h2o2": "peroxide", "o2": "oxygen",
    "n2": "nitrogen", "ar": "argon", "nh3": "ammonia", "hsd": "diesel", "lpg": "lpg", "dissolved": "acetylene",
    "da": "acetylene", "co": "monoxide", "nacn": "cyanide", "hypo": "hypochlorite",
}

_STOP = {"acid", "solution", "gas", "oil", "liquid", "sodium", "calcium", "potassium", "the", "and", "of", "grade", "industrial"}


def match_chemicals(question: str, chems: list[dict[str, Any]], limit: int = MAX_CHEMICALS) -> list[str]:
    """Register rows the question names: by CAS number, full name, or a
    distinctive name token (e.g. 'nitric' matches 'Nitric acid 68%')."""
    q = (question or "").lower()
    qtok = set(_tokens(q))
    qtok |= {ALIASES[t] for t in list(qtok) if t in ALIASES}
    scored: list[tuple[int, str]] = []
    for c in chems:
        score = 0
        cas = (c.get("casNumber") or "").strip().lower()
        name = (c.get("chemicalName") or "").strip().lower()
        if cas and cas in q:
            score += 100
        if name and name in q:
            score += 50
        distinctive = [t for t in _tokens(name) if t not in _STOP and len(t) >= 3 and not t.endswith("%")]
        score += 10 * sum(1 for t in distinctive if t in qtok)
        if score:
            scored.append((score, c["id"]))
    scored.sort(key=lambda x: -x[0])
    return [cid for _s, cid in scored[:limit]]


def chem_fact(row: dict[str, Any], groups: list[str], sds_doc: dict[str, Any] | None) -> dict[str, Any]:
    """The register record the advisor may rely on (and cite)."""
    return {
        "id": row["id"], "chemicalName": row.get("chemicalName"), "casNumber": row.get("casNumber"),
        "plantName": row.get("plantName"), "facilityName": row.get("facilityName"),
        "ghsSignalWord": row.get("ghsSignalWord"), "ghsHazardClasses": row.get("ghsHazardClasses") or [],
        "ghsPictograms": row.get("ghsPictograms") or [], "hazardGroups": groups,
        "quantityStored": row.get("quantityStored"), "unit": row.get("unit"), "maxAllowableQty": row.get("maxAllowableQty"),
        "storage": storage_label(row.get("storageBuilding"), row.get("storageRoom")),
        "containerType": row.get("containerType"),
        "secondaryContainment": bool(row.get("secondaryContainmentPresent")),
        "ventilation": bool(row.get("ventilationAvailable")),
        "ppeRequired": row.get("ppeRequired") or [],
        "incompatibleSubstances": row.get("incompatibleSubstances") or [],
        "spillKitLocation": row.get("spillKitLocation"), "emergencyContact": row.get("emergencyContact"),
        "sdsReference": row.get("sdsDocId"), "sdsVersion": row.get("sdsVersion"),
        "sds": ({"docNo": sds_doc.get("docNo"), "revision": sds_doc.get("revision"), "status": sds_doc.get("sdsStatus"),
                 "reviewDue": sds_doc.get("reviewDueIso")} if sds_doc else None),
    }


ADVISOR_SYSTEM = (
    "You are the chemical safety advisor for a steel plant's process safety team. Answer the user's question about "
    "chemicals held at the plant.\n"
    "Rules:\n"
    "1. The REGISTER FACTS are authoritative for this plant (where it is stored, how much, PPE, spill kit, listed "
    "incompatibilities). Use them first and do not contradict them.\n"
    "2. The COMPATIBILITY RESULT comes from the plant's rule tables. Quote its verdict for any pair; never upgrade an "
    "INCOMPATIBLE or CAUTION pair to compatible.\n"
    "3. For information the register does not hold (first aid, fire-fighting media, exposure limits such as TLV/PEL), "
    "you may give standard guidance for that substance, but say clearly it is general guidance to verify against SDS "
    "sections 4, 5 and 8.\n"
    "4. If the question names a chemical that is not in the register facts, say it is not in the plant register.\n"
    "5. Be concise and practical: short paragraphs or bullet lines, no more than about 250 words.\n"
    "Reply with JSON only: {\"answer\": string, \"usedRecordIds\": [ids of register facts you relied on], "
    "\"generalGuidance\": boolean (true if any part is not from the register), \"followUps\": [up to 3 short follow-up questions]}"
)


def build_advisor_prompt(question: str, facts: list[dict[str, Any]], compat: dict[str, Any] | None, plant_name: str | None) -> str:
    import json

    compact_compat = None
    if compat and compat.get("pairs"):
        compact_compat = [
            {"pair": [p["aName"], p["bName"]], "verdict": p["verdict"], "reasons": [r["reason"] for r in p["reasons"]],
             "advice": [r["advice"] for r in p["reasons"]]}
            for p in compat["pairs"]
        ]
    payload = {
        "plant": plant_name, "question": question.strip()[:1500],
        "registerFacts": facts, "compatibilityResult": compact_compat,
    }
    return json.dumps(payload, default=str, ensure_ascii=False)


def _fmt_qty(f: dict[str, Any]) -> str | None:
    if f.get("quantityStored") is None:
        return None
    q = f"{f['quantityStored']:g} {f.get('unit') or ''}".strip()
    if f.get("maxAllowableQty"):
        q += f" (max {f['maxAllowableQty']:g} {f.get('unit') or ''})".rstrip()
    return q


def fallback_answer(question: str, facts: list[dict[str, Any]], compat: dict[str, Any] | None) -> str:
    """Rule-based answer when the AI is unavailable: register facts for the
    topics the question asks about, plus the compatibility verdicts."""
    if not facts:
        return ("None of the chemicals in your question were found in the plant chemical register. "
                "Pick chemicals from the register (quick-pick) or check the spelling / CAS number.")
    topics = detect_topics(question)
    lines: list[str] = []
    if compat and compat.get("pairs"):
        lines.append("Compatibility (rule tables):")
        for p in compat["pairs"]:
            reason = p["reasons"][0]["reason"] if p["reasons"] else ""
            lines.append(f"- {p['aName']} + {p['bName']}: {p['verdict']}. {reason}")
    show_all = "general" in topics
    for f in facts:
        bits: list[str] = []
        if show_all or "storage" in topics or "segregation" in topics:
            if f.get("storage"):
                bits.append(f"stored at {f['storage']}")
            bits.append("secondary containment " + ("present" if f.get("secondaryContainment") else "NOT recorded"))
        if show_all or "quantity" in topics or "storage" in topics:
            q = _fmt_qty(f)
            if q:
                bits.append(f"quantity {q}")
        if show_all or "ppe" in topics or "spill" in topics or "first_aid" in topics:
            bits.append("PPE: " + (", ".join(f["ppeRequired"]) if f.get("ppeRequired") else "not recorded"))
        if show_all or "spill" in topics:
            bits.append(f"spill kit: {f.get('spillKitLocation') or 'not recorded'}")
            if f.get("emergencyContact"):
                bits.append(f"emergency contact: {f['emergencyContact']}")
        if show_all or "segregation" in topics:
            if f.get("incompatibleSubstances"):
                bits.append("listed incompatibles: " + ", ".join(f["incompatibleSubstances"]))
        hz = ", ".join(f.get("ghsHazardClasses") or []) or "GHS class not recorded"
        head = f"{f['chemicalName']}" + (f" (CAS {f['casNumber']})" if f.get("casNumber") else "")
        lines.append(f"{head}: {f.get('ghsSignalWord') or 'no signal word'}; {hz}" + ("; " + "; ".join(bits) if bits else "") + ".")
    gaps = [t for t in topics if t in ("first_aid", "fire", "exposure_limits")]
    if gaps:
        names = {"first_aid": "first aid (SDS section 4)", "fire": "fire-fighting media (SDS section 5)",
                 "exposure_limits": "exposure limits (SDS section 8)"}
        sds_refs = [f"{f['chemicalName']}: {f['sds']['docNo']} rev {f['sds']['revision']}" for f in facts if f.get("sds")]
        lines.append(
            "The register does not hold " + ", ".join(names[g] for g in gaps) + ". Refer to the SDS"
            + (f" ({'; '.join(sds_refs)})" if sds_refs else " (no SDS file is on record for these chemicals)") + "."
        )
    return "\n".join(lines)


def shape_advisor_response(
    *, question: str, facts: list[dict[str, Any]], compat: dict[str, Any] | None,
    ai: dict[str, Any] | None, ai_configured: bool, unmatched: list[str] | None = None,
) -> dict[str, Any]:
    """Final advisor payload. Grounding guard: citations only ever name records
    that were in `facts`; an unusable AI reply falls back to the rule-based answer."""
    fact_ids = {f["id"] for f in facts}
    answer = None
    used: list[str] = []
    dropped = 0
    general = False
    follow_ups: list[str] = []
    if isinstance(ai, dict) and isinstance(ai.get("answer"), str) and ai["answer"].strip():
        answer = ai["answer"].strip()
        raw_ids = ai.get("usedRecordIds") if isinstance(ai.get("usedRecordIds"), list) else []
        for rid in raw_ids:
            if isinstance(rid, str) and rid in fact_ids:
                if rid not in used:
                    used.append(rid)
            else:
                dropped += 1
        general = bool(ai.get("generalGuidance"))
        follow_ups = [s.strip() for s in (ai.get("followUps") or []) if isinstance(s, str) and s.strip()][:3]
    ai_used = answer is not None
    if not ai_used:
        answer = fallback_answer(question, facts, compat)
        used = [f["id"] for f in facts]
    elif not used:
        # The model answered without naming records; it was given only these facts.
        used = [f["id"] for f in facts]
    by_id = {f["id"]: f for f in facts}
    citations = [
        {"id": rid, "chemicalName": by_id[rid]["chemicalName"], "casNumber": by_id[rid].get("casNumber"),
         "storage": by_id[rid].get("storage"), "facilityName": by_id[rid].get("facilityName"),
         "sds": by_id[rid].get("sds")}
        for rid in used
    ]
    mode = "AI" if ai_used else ("RULES_ONLY_AI_UNAVAILABLE" if not ai_configured else "RULES_ONLY_AI_FAILED")
    return {
        "question": question,
        "answer": answer,
        "mode": mode,
        "aiUsed": ai_used,
        "generalGuidance": general,
        "citations": citations,
        "droppedCitations": dropped,
        "compatibility": compat,
        "topics": detect_topics(question),
        "unmatched": unmatched or [],
        "followUps": follow_ups,
        "disclaimer": ADVISOR_DISCLAIMER,
    }
