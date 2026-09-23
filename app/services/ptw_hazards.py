"""PTW hazard taxonomy — the rules that turn a multi-hazard permit into a
single authorisation.

Background
──────────
Until now a permit carried exactly ONE `Permit.type`, which drove three
separate things at once: which workflow definition ran, how long the permit
could be valid, and which controls (gas test / fire watch / standby / rescue)
were mandatory. That is fine for a single-hazard job and wrong for a real one:
replacing a pipe spool on a rack is height + hot work + electrical isolation,
and issuing three permits for it produces three validity clocks, three
closures and no single answer to "what is this crew authorised to do right
now?".

The industry answer — and the shape of the Page Industries form
(PIL/EHSD/CL/038-R2) this module is modelled on — is a base permit with
hazard ANNEXURES attached. The permit stays one record with one number, one
window and one closure; each attached hazard brings its own precaution
checklist and its own control requirements.

What this module owns
─────────────────────
Everything derived from "which hazards are on this permit":

  • `effective_hazards()`   base type + attached annexures, de-duplicated
  • `workflow_type_for()`   which seeded workflow definition runs (the UNION
                            chain — the most stringent one wins)
  • `validity_cap_hours()`  the tightest cap across all hazards
  • `required_controls()`   the union of mandatory controls
  • `competency_types_for()` every permit type the crew must be certified for

Design note — why routing rather than new workflow steps
────────────────────────────────────────────────────────
`workflow_engine.initiate()` resolves a definition by `record_data["type"]`.
So a cold-work permit carrying a hot-work annexure simply routes to the
already-seeded "PTW — Hot Work (high-risk)" chain. No new workflow steps, no
re-seed of `seed_workflows.py` (which is destructive — it deletes and
recreates definitions, orphaning in-flight instances), and no change to the
engine's step machinery.

The consequence, and it is the intended one: the site EHS officer signs ONCE
for the whole permit. There is no per-annexure approval step. The single
`Safety Officer Approval` step in the resolved chain is the certification of
every attached checklist — which is exactly what the paper form does, where
the same four signatories sign the permit rather than each checklist.
"""

from __future__ import annotations

# `PermitHazardType` / `PrecautionResponse` live in the model layer next to
# the tables that persist them, so the dependency here stays one-way
# (services → models) and the annexure models can map the enums without
# importing a service.
from app.models.permit import PermitHazardType, PermitType, PrecautionResponse

__all__ = [
    "PermitHazardType",
    "PrecautionResponse",
    "HAZARD_LABELS",
    "hazard_for_base_type",
    "effective_hazards",
    "workflow_type_for",
    "validity_cap_hours",
    "binding_cap_hazard",
    "required_controls",
    "competency_types_for",
]


# ── Base permit type → the hazard annexure it implies ──────────────────────
# The base type ALWAYS contributes its own hazard, so a Hot Work permit
# always carries the hot-work checklist even if the originator ticked nothing
# else. GENERAL_COLD maps to GENERAL — the form's "General Work" checklist.
_BASE_TO_HAZARD: dict[str, PermitHazardType] = {
    PermitType.HOT_WORK.value: PermitHazardType.HOT_WORK,
    PermitType.CONFINED_SPACE.value: PermitHazardType.CONFINED_SPACE,
    PermitType.WORK_AT_HEIGHT.value: PermitHazardType.WORK_AT_HEIGHT,
    PermitType.EXCAVATION.value: PermitHazardType.EXCAVATION,
    PermitType.ELECTRICAL_LOTO.value: PermitHazardType.ELECTRICAL_LOTO,
    PermitType.LIFTING.value: PermitHazardType.LIFTING,
    PermitType.GENERAL_COLD.value: PermitHazardType.GENERAL,
}

# ── Hazard → the workflow/competency permit type it behaves as ─────────────
# FRAGILE_ROOF and CIVIL have no workflow definition of their own. Rather
# than seed two new chains (and re-run the destructive workflow seed), each
# borrows the chain of the type it is operationally equivalent to:
#   FRAGILE_ROOF → WORK_AT_HEIGHT  (it is a fall-from-height exposure)
#   CIVIL, GENERAL → GENERAL_COLD  (the base cold-work chain)
_HAZARD_TO_PERMIT_TYPE: dict[PermitHazardType, PermitType] = {
    PermitHazardType.HOT_WORK: PermitType.HOT_WORK,
    PermitHazardType.CONFINED_SPACE: PermitType.CONFINED_SPACE,
    PermitHazardType.WORK_AT_HEIGHT: PermitType.WORK_AT_HEIGHT,
    PermitHazardType.FRAGILE_ROOF: PermitType.WORK_AT_HEIGHT,
    PermitHazardType.EXCAVATION: PermitType.EXCAVATION,
    PermitHazardType.ELECTRICAL_LOTO: PermitType.ELECTRICAL_LOTO,
    PermitHazardType.LIFTING: PermitType.LIFTING,
    PermitHazardType.CIVIL: PermitType.GENERAL_COLD,
    PermitHazardType.GENERAL: PermitType.GENERAL_COLD,
}

# ── Risk rank — higher wins when choosing the union approval chain ─────────
# Ordering rationale: confined space and hot work are the two types this
# codebase already caps at 24h and routes through Safety Officer + Plant
# Head; fall-from-height follows; the cold end sits last. Only the ORDER
# matters, never the absolute numbers.
_RISK_RANK: dict[PermitHazardType, int] = {
    PermitHazardType.CONFINED_SPACE: 100,
    PermitHazardType.HOT_WORK: 90,
    PermitHazardType.WORK_AT_HEIGHT: 80,
    PermitHazardType.FRAGILE_ROOF: 75,
    PermitHazardType.EXCAVATION: 70,
    PermitHazardType.ELECTRICAL_LOTO: 60,
    PermitHazardType.LIFTING: 50,
    PermitHazardType.CIVIL: 20,
    PermitHazardType.GENERAL: 10,
}

# ── Validity caps (hours) ──────────────────────────────────────────────────
# The first seven mirror the caps the wizard and `create_permit` already
# enforce, so single-hazard permits behave exactly as before. FRAGILE_ROOF is
# new and is capped at 24h: it is a high-consequence fall exposure whose
# controls (crawl boards, edge protection, nets) degrade across shifts and
# want re-authorisation daily. CIVIL is ordinary cold work at 72h.
_VALIDITY_CAP_HOURS: dict[PermitHazardType, int] = {
    PermitHazardType.HOT_WORK: 24,
    PermitHazardType.CONFINED_SPACE: 24,
    PermitHazardType.FRAGILE_ROOF: 24,
    PermitHazardType.WORK_AT_HEIGHT: 72,
    PermitHazardType.EXCAVATION: 72,
    PermitHazardType.ELECTRICAL_LOTO: 72,
    PermitHazardType.LIFTING: 72,
    PermitHazardType.CIVIL: 72,
    PermitHazardType.GENERAL: 72,
}

# ── Controls each hazard makes mandatory ───────────────────────────────────
# Taken from the rules the wizard already applies per type, extended to the
# two new hazards. The permit takes the UNION: a cold-work permit with a
# hot-work annexure needs a fire watch, because the hot work needs one.
_CONTROLS: dict[PermitHazardType, frozenset[str]] = {
    PermitHazardType.HOT_WORK: frozenset({"GAS_TEST", "FIRE_WATCH"}),
    PermitHazardType.CONFINED_SPACE: frozenset({"GAS_TEST", "STANDBY", "RESCUE_PLAN"}),
    PermitHazardType.WORK_AT_HEIGHT: frozenset({"RESCUE_PLAN"}),
    PermitHazardType.FRAGILE_ROOF: frozenset({"RESCUE_PLAN"}),
    PermitHazardType.LIFTING: frozenset({"STANDBY"}),
    PermitHazardType.EXCAVATION: frozenset(),
    PermitHazardType.ELECTRICAL_LOTO: frozenset(),
    PermitHazardType.CIVIL: frozenset(),
    PermitHazardType.GENERAL: frozenset(),
}

# Human labels — used in API payloads and blocker messages so the UI and the
# error text can never drift apart.
HAZARD_LABELS: dict[PermitHazardType, str] = {
    PermitHazardType.HOT_WORK: "Hot Work",
    PermitHazardType.CONFINED_SPACE: "Confined Space",
    PermitHazardType.WORK_AT_HEIGHT: "Height Work",
    PermitHazardType.FRAGILE_ROOF: "Work on Fragile Roof",
    PermitHazardType.EXCAVATION: "Excavation Work",
    PermitHazardType.ELECTRICAL_LOTO: "Electrical / LOTO",
    PermitHazardType.LIFTING: "Lifting Operations",
    PermitHazardType.CIVIL: "Civil Work",
    PermitHazardType.GENERAL: "General Work",
}


def hazard_for_base_type(base: PermitType | str) -> PermitHazardType:
    """The annexure a base permit type always contributes."""
    key = base.value if isinstance(base, PermitType) else str(base)
    return _BASE_TO_HAZARD.get(key, PermitHazardType.GENERAL)


def effective_hazards(
    base: PermitType | str,
    attached: list[PermitHazardType | str] | None = None,
) -> list[PermitHazardType]:
    """Every hazard in force on this permit: the base type's own hazard plus
    whatever annexures were attached, de-duplicated and ordered most-severe
    first so UI lists and blocker messages read sensibly."""
    out: set[PermitHazardType] = {hazard_for_base_type(base)}
    for h in attached or []:
        try:
            out.add(h if isinstance(h, PermitHazardType) else PermitHazardType(str(h)))
        except ValueError:
            # Unknown hazard string — ignore rather than 500. The API layer
            # validates the input; this keeps a bad legacy row readable.
            continue
    return sorted(out, key=lambda h: -_RISK_RANK.get(h, 0))


def workflow_type_for(
    base: PermitType | str,
    attached: list[PermitHazardType | str] | None = None,
) -> PermitType:
    """The permit type whose seeded workflow definition should run — the
    UNION chain, i.e. the most stringent one among all hazards in force.

    This is what gets passed as `record_data["type"]` to
    `workflow_engine.initiate`. `Permit.type` itself is NOT changed: it stays
    the base type the originator chose, so registers, analytics group-bys and
    the permit-number prefix all keep their existing meaning.
    """
    hazards = effective_hazards(base, attached)
    top = max(hazards, key=lambda h: _RISK_RANK.get(h, 0))
    return _HAZARD_TO_PERMIT_TYPE.get(top, PermitType.GENERAL_COLD)


def validity_cap_hours(
    base: PermitType | str,
    attached: list[PermitHazardType | str] | None = None,
) -> int:
    """Tightest validity cap across every hazard in force.

    A 72h height job that also involves hot work is capped at 24h — the hot
    work is what expires, and there is no way to expire half a permit.
    """
    hazards = effective_hazards(base, attached)
    return min(_VALIDITY_CAP_HOURS.get(h, 72) for h in hazards)


def binding_cap_hazard(
    base: PermitType | str,
    attached: list[PermitHazardType | str] | None = None,
) -> PermitHazardType:
    """Which hazard is responsible for the cap — so the 400 can say *why*
    ("24h cap set by the Hot Work annexure") instead of just refusing."""
    hazards = effective_hazards(base, attached)
    return min(hazards, key=lambda h: (_VALIDITY_CAP_HOURS.get(h, 72), -_RISK_RANK.get(h, 0)))


def required_controls(
    base: PermitType | str,
    attached: list[PermitHazardType | str] | None = None,
) -> set[str]:
    """Union of mandatory controls: GAS_TEST, FIRE_WATCH, STANDBY, RESCUE_PLAN."""
    hazards = effective_hazards(base, attached)
    out: set[str] = set()
    for h in hazards:
        out |= _CONTROLS.get(h, frozenset())
    return out


def competency_types_for(
    base: PermitType | str,
    attached: list[PermitHazardType | str] | None = None,
) -> list[str]:
    """Every `PermitType` value the receiver and crew must be certified for.

    A permit covering height + hot work needs a holder certified for BOTH —
    checking only the base type would let an uncertified person hold the
    hot-work half of the job. Returned as plain strings because that is what
    `competency.check_competency_for_permit_type` takes.
    """
    hazards = effective_hazards(base, attached)
    seen: list[str] = []
    for h in hazards:
        code = _HAZARD_TO_PERMIT_TYPE.get(h, PermitType.GENERAL_COLD).value
        if code not in seen:
            seen.append(code)
    return seen
