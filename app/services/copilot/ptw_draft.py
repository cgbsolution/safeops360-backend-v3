"""PTW drafting skill: the draft shape, patch validation and rule checks.

The New Permit form in the browser owns the draft. Each copilot turn the
browser sends its current draft; the copilot changes it only through
`update_ptw_draft` patches, which are validated here (shape, enums, and that
every id belongs to the permit's plant) before the browser applies them.

The rule check (`check_draft`) mirrors what `POST /api/ptw` enforces — the
same hazard union rules (`services.ptw_hazards`), the same competency gate —
so problems surface while drafting rather than as a 400 on submit.

Deliberately out of reach of the copilot:
  * the precaution checklist answers (YES / NO / NA). Those are the
    receiver's attestation that a control is physically in place; a model
    cannot make that statement, so patches cannot carry them;
  * submitting. The user presses Submit.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.permit import PermitHazardType
from app.services.ptw_hazards import (
    HAZARD_LABELS,
    binding_cap_hazard,
    competency_types_for,
    effective_hazards,
    required_controls,
    validity_cap_hours,
)

PermitTypeT = Literal[
    "HOT_WORK", "CONFINED_SPACE", "WORK_AT_HEIGHT", "EXCAVATION",
    "ELECTRICAL_LOTO", "LIFTING", "GENERAL_COLD",
]
HazardT = Literal[
    "HOT_WORK", "CONFINED_SPACE", "WORK_AT_HEIGHT", "FRAGILE_ROOF", "EXCAVATION",
    "ELECTRICAL_LOTO", "LIFTING", "CIVIL", "GENERAL",
]
CrewRoleT = Literal["OPERATOR", "HELPER", "SUPERVISOR", "TECHNICIAN", "WORKER", "CONTRACTOR"]
IsolationTypeT = Literal["ELECTRICAL", "MECHANICAL", "FLUID", "PNEUMATIC", "HYDRAULIC", "STEAM"]
WorkNatureT = Literal["INSPECTION", "REPAIR", "REPLACEMENT", "MODIFICATION"]

PERMIT_TYPE_LABELS: dict[str, str] = {
    "HOT_WORK": "Hot Work",
    "CONFINED_SPACE": "Confined Space Entry",
    "WORK_AT_HEIGHT": "Work at Height",
    "EXCAVATION": "Excavation",
    "ELECTRICAL_LOTO": "Electrical / LOTO",
    "LIFTING": "Lifting Operations",
    "GENERAL_COLD": "General Cold Work",
}

# Mirrors PPE_CATALOG / PPE_DEFAULTS / DEFAULT_GAS_PARAMS in the wizard
# (src/lib/ptw/wizard-config.ts). Codes are what the form stores.
PPE_CATALOG: dict[str, str] = {
    "helmet": "Safety Helmet",
    "shoes": "Safety Shoes",
    "goggles": "Safety Goggles",
    "gloves": "Safety Gloves",
    "welding_gloves": "Welding Gloves",
    "insulated_gloves": "Insulated Gloves",
    "fr_coverall": "Fire-Retardant Coverall",
    "face_shield": "Face Shield",
    "arc_flash_suit": "Arc Flash Suit",
    "harness": "Full-body Harness",
    "lanyard": "Energy-absorbing Lanyard",
    "scba": "SCBA / Air Line",
    "gas_monitor": "Personal Gas Monitor",
    "high_vis": "High-vis Vest",
    "ear_plugs": "Ear Plugs",
    "respirator": "Respirator",
}
PPE_DEFAULTS: dict[str, list[str]] = {
    "HOT_WORK": ["helmet", "shoes", "goggles", "fr_coverall", "welding_gloves", "face_shield"],
    "CONFINED_SPACE": ["helmet", "shoes", "harness", "scba", "gas_monitor"],
    "WORK_AT_HEIGHT": ["helmet", "shoes", "harness", "lanyard"],
    "EXCAVATION": ["helmet", "shoes", "high_vis"],
    "ELECTRICAL_LOTO": ["helmet", "shoes", "insulated_gloves", "arc_flash_suit"],
    "LIFTING": ["helmet", "shoes", "high_vis", "gloves"],
    "GENERAL_COLD": ["helmet", "shoes"],
}
DEFAULT_GAS_PARAMS: dict[str, list[dict[str, Any]]] = {
    "HOT_WORK": [
        {"parameter": "LEL", "lowLimit": 0, "highLimit": 10, "unit": "%"},
        {"parameter": "O2", "lowLimit": 19.5, "highLimit": 23.5, "unit": "%"},
    ],
    "CONFINED_SPACE": [
        {"parameter": "O2", "lowLimit": 19.5, "highLimit": 23.5, "unit": "%"},
        {"parameter": "LEL", "lowLimit": 0, "highLimit": 10, "unit": "%"},
        {"parameter": "CO", "lowLimit": 0, "highLimit": 35, "unit": "ppm"},
        {"parameter": "H2S", "lowLimit": 0, "highLimit": 10, "unit": "ppm"},
    ],
}
DEFAULT_GAS_REFRESH_MINUTES = {"HOT_WORK": 240, "CONFINED_SPACE": 120}

# "YYYY-MM-DDTHH:MM" — the wall-clock value of the form's datetime-local inputs.
_LOCAL_DT = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$"


# ─── Patch schema ───────────────────────────────────────────────────────


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CrewIn(_Strict):
    userId: str = Field(min_length=1, max_length=64)
    role: CrewRoleT = "WORKER"


class IsolationIn(_Strict):
    isolationType: IsolationTypeT
    description: str = Field(min_length=1, max_length=300)
    isolationPointTag: str = Field(min_length=1, max_length=80)
    lotoTagNumber: str | None = Field(default=None, max_length=80)


class ToolIn(_Strict):
    equipmentId: str | None = Field(default=None, max_length=64)
    freeTextDescription: str | None = Field(default=None, max_length=200)


class SubjectIn(_Strict):
    equipmentId: str = Field(min_length=1, max_length=64)
    workNature: WorkNatureT = "REPAIR"


class GasParamIn(_Strict):
    parameter: str = Field(min_length=1, max_length=24)
    lowLimit: float | None = None
    highLimit: float | None = None
    unit: str = Field(min_length=1, max_length=12)


class GasPlanIn(_Strict):
    refreshFrequencyMinutes: int = Field(default=120, ge=15, le=480)
    parametersToTest: list[GasParamIn] = Field(default_factory=list, max_length=12)
    instrumentSerial: str | None = Field(default=None, max_length=60)
    instrumentLastCalibrated: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class ChecklistAnswerIn(_Strict):
    """A suggested answer to one precaution item. Only YES or NA: a NO means
    "this control is not in place", which only someone on site can say. The
    user confirms every suggestion before the permit can be submitted."""

    itemId: str = Field(min_length=1, max_length=64)
    response: Literal["YES", "NA"]
    remark: str | None = Field(default=None, max_length=300)


class PtwDraftPatch(_Strict):
    """Fields the copilot may set. Omitted fields are left alone; an explicit
    null clears the field. Lists are replaced whole."""

    type: PermitTypeT | None = None
    extraHazards: list[HazardT] | None = Field(default=None, max_length=9)
    validFrom: str | None = Field(default=None, pattern=_LOCAL_DT)
    validTo: str | None = Field(default=None, pattern=_LOCAL_DT)
    plantId: str | None = Field(default=None, max_length=64)
    departmentId: str | None = Field(default=None, max_length=64)
    areaId: str | None = Field(default=None, max_length=64)
    specificLocation: str | None = Field(default=None, max_length=200)
    scopeOfWork: str | None = Field(default=None, max_length=2000)
    workOrderNumber: str | None = Field(default=None, max_length=60)
    contractorCompanyId: str | None = Field(default=None, max_length=64)
    issuerId: str | None = Field(default=None, max_length=64)
    receiverId: str | None = Field(default=None, max_length=64)
    workCrew: list[CrewIn] | None = Field(default=None, max_length=30)
    fireWatchPersonId: str | None = Field(default=None, max_length=64)
    standbyPersonId: str | None = Field(default=None, max_length=64)
    isolations: list[IsolationIn] | None = Field(default=None, max_length=20)
    requiredPpe: list[str] | None = Field(default=None, max_length=16)
    toolsEquipment: list[ToolIn] | None = Field(default=None, max_length=20)
    subjectEquipment: list[SubjectIn] | None = Field(default=None, max_length=20)
    gasTestPlan: GasPlanIn | None = None
    rescuePlan: str | None = Field(default=None, max_length=2000)
    weatherConditionsAtIssue: str | None = Field(default=None, max_length=120)
    windSpeedKmh: float | None = Field(default=None, ge=0, le=250)
    flraRequired: bool | None = None
    # Suggested precaution answers per hazard, for the user to verify on site.
    checklistSuggestions: dict[HazardT, list[ChecklistAnswerIn]] | None = None

    @field_validator("checklistSuggestions")
    @classmethod
    def _na_needs_reason(cls, v: dict[str, list[ChecklistAnswerIn]] | None) -> dict[str, list[ChecklistAnswerIn]] | None:
        for items in (v or {}).values():
            for a in items:
                if a.response == "NA" and not (a.remark or "").strip():
                    raise ValueError(f"item {a.itemId}: an NA answer needs a remark saying why it does not apply")
        return v

    @field_validator("requiredPpe")
    @classmethod
    def _ppe_codes(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        bad = [c for c in v if c not in PPE_CATALOG]
        if bad:
            raise ValueError(f"unknown PPE code(s) {bad}; use codes from get_ptw_reference")
        return list(dict.fromkeys(v))


def parse_patch(raw: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a model-supplied patch. Returns (patch-as-dict, error)."""
    try:
        model = PtwDraftPatch.model_validate(raw)
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()[:8]
        )
        return None, f"Invalid patch — {problems}"
    # Top-level "unset" means "leave alone", but nested rows keep their
    # defaults (a crew row without a role is a WORKER, not a role-less row).
    full = model.model_dump()
    patch = {k: full[k] for k in model.model_fields_set}
    if not patch:
        return None, "Empty patch — include at least one field to change."
    return patch, None


# ─── Working draft ──────────────────────────────────────────────────────

_DRAFT_KEYS = (set(PtwDraftPatch.model_fields) - {"checklistSuggestions"}) | {"checklist", "lotoLinked", "hiraLinked"}


def normalise_client_draft(raw: Any) -> dict[str, Any]:
    """The browser's draft is untrusted input — keep known keys, drop the rest.
    Values are re-validated wherever they matter (check_draft, references)."""
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if k in _DRAFT_KEYS}


def apply_patch(draft: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Merge a validated patch into the working draft, with the same side
    effects the form applies (type change → type defaults; plant change →
    area/department reset)."""
    out = dict(draft)
    if "type" in patch and patch["type"] and patch["type"] != draft.get("type"):
        t = patch["type"]
        out["requiredPpe"] = list(PPE_DEFAULTS.get(t, ["helmet", "shoes"]))
        if t in DEFAULT_GAS_PARAMS:
            out["gasTestPlan"] = {
                "refreshFrequencyMinutes": DEFAULT_GAS_REFRESH_MINUTES[t],
                "parametersToTest": [dict(p) for p in DEFAULT_GAS_PARAMS[t]],
            }
    if "plantId" in patch and patch["plantId"] != draft.get("plantId"):
        for k in ("areaId", "departmentId"):
            if k not in patch:
                out[k] = None
    out.update({k: v for k, v in patch.items() if k != "checklistSuggestions"})
    return out


# ─── Reference validation (ids must exist and belong to the plant) ──────


async def validate_references(
    db: AsyncSession, merged: dict[str, Any], patch: dict[str, Any], *, user_id: str
) -> list[str]:
    from app.models.epc import ContractorCompany
    from app.models.equipment import Equipment
    from app.models.masters import Department
    from app.models.plant import Area, Plant
    from app.models.user import User
    from app.services.permissions import PermissionContext, can

    errors: list[str] = []
    plant_id = merged.get("plantId")

    if "plantId" in patch and plant_id:
        if await db.get(Plant, plant_id) is None:
            errors.append(f"plantId {plant_id} does not exist")
        else:
            check = await can(db, user_id, "PTW.CREATE", PermissionContext(plant_id=plant_id))
            if not check.allowed:
                errors.append("You cannot raise permits at that plant (no PTW.CREATE there)")

    if patch.get("areaId"):
        area = await db.get(Area, patch["areaId"])
        if area is None or area.plantId != plant_id:
            errors.append("areaId is not an area of the permit's plant — use get_plant_options")

    if patch.get("departmentId"):
        dep = await db.get(Department, patch["departmentId"])
        if dep is None or getattr(dep, "plantId", None) != plant_id:
            errors.append("departmentId is not a department of the permit's plant")

    if patch.get("contractorCompanyId"):
        cc = await db.get(ContractorCompany, patch["contractorCompanyId"])
        if cc is None or getattr(cc, "status", "ACTIVE") != "ACTIVE":
            errors.append("contractorCompanyId is not an active contractor")

    user_ids: set[str] = set()
    for k in ("issuerId", "receiverId", "fireWatchPersonId", "standbyPersonId"):
        if patch.get(k):
            user_ids.add(patch[k])
    for c in patch.get("workCrew") or []:
        user_ids.add(c["userId"])
    if user_ids:
        found = set(
            (await db.execute(select(User.id).where(User.id.in_(list(user_ids))))).scalars().all()
        )
        missing = user_ids - found
        if missing:
            errors.append(f"unknown user id(s) {sorted(missing)} — use find_people")
    if patch.get("issuerId") and patch["issuerId"] == user_id:
        errors.append("The originator (current user) cannot be the issuer")

    eq_ids: set[str] = {t["equipmentId"] for t in patch.get("toolsEquipment") or [] if t.get("equipmentId")}
    eq_ids |= {s["equipmentId"] for s in patch.get("subjectEquipment") or []}
    if eq_ids:
        rows = (
            await db.execute(select(Equipment.id, Equipment.plantId).where(Equipment.id.in_(list(eq_ids))))
        ).all()
        ok = {r.id for r in rows if r.plantId == plant_id}
        bad = eq_ids - ok
        if bad:
            errors.append(f"equipment id(s) {sorted(bad)} are not equipment at the permit's plant")

    return errors


# ─── Rule check ─────────────────────────────────────────────────────────


def is_tbc(tag: Any) -> bool:
    """Placeholder tags the copilot writes when it proposes an isolation the
    user still has to tag. They block submission until replaced."""
    return isinstance(tag, str) and tag.strip().upper().startswith("TBC")


def _parse_local(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        return datetime.strptime(v[:16], "%Y-%m-%dT%H:%M")
    except ValueError:
        return None


def _hazard_list(draft: dict[str, Any]) -> list[str]:
    out = []
    for h in draft.get("extraHazards") or []:
        try:
            out.append(PermitHazardType(str(h)).value)
        except ValueError:
            continue
    return out


# ─── Plan-sanity alerts ─────────────────────────────────────────────────
# Mirror of src/lib/ptw/advisories.ts: plans that pass the rules but can't
# work in practice (a 10-minute hot work permit, a start that has passed,
# high-risk work starting before approvals can happen). Keep them in step.

MIN_WINDOW_MINUTES: dict[str, int] = {
    "HOT_WORK": 60, "CONFINED_SPACE": 60, "EXCAVATION": 60, "ELECTRICAL_LOTO": 30, "WORK_AT_HEIGHT": 30,
    "FRAGILE_ROOF": 30, "LIFTING": 30, "CIVIL": 30, "GENERAL": 15,
}
WINDOW_NEEDS: dict[str, str] = {
    "HOT_WORK": "a gas test before starting, the hot work itself, and a fire watch for at least 30 minutes after the hot work ends",
    "CONFINED_SPACE": "isolation, ventilation and a gas test before entry, the work itself, and a headcount and close-out on exit",
    "EXCAVATION": "locating buried services, shoring or benching the sides, the dig itself, and barricading or back-filling",
    "ELECTRICAL_LOTO": "isolation, lock-out and a zero-energy test before work, the work itself, and safe restoration",
    "WORK_AT_HEIGHT": "setting up access, checking anchors and harnesses, the work itself, and a safe descent",
    "FRAGILE_ROOF": "laying crawl boards and edge protection, the work itself, and removing them safely",
    "LIFTING": "the lift-plan briefing, rigging checks, setting the exclusion zone, and the lift itself",
    "CIVIL": "the pre-job briefing, the work itself, and making the area safe afterwards",
    "GENERAL": "the pre-job briefing, the work itself, and restoring the area",
}
_HIGH_RISK = {"HOT_WORK", "CONFINED_SPACE", "WORK_AT_HEIGHT", "FRAGILE_ROOF"}


def _fmt_duration(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} minute{'' if minutes == 1 else 's'}"
    h, m = divmod(minutes, 60)
    return f"{h} h {m} min" if m else f"{h} hour{'' if h == 1 else 's'}"


def plan_advisories(ptype: str | None, extra: list[str], valid_from: Any, valid_to: Any,
                    now_local: str | None) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    vf, vt = _parse_local(valid_from), _parse_local(valid_to)
    if ptype not in PERMIT_TYPE_LABELS or not vf or not vt or vt <= vf:
        return out
    hazards = [h.value for h in effective_hazards(ptype, extra)]
    controls = required_controls(ptype, extra)
    minutes = round((vt - vf).total_seconds() / 60)

    binding = max(hazards, key=lambda h: MIN_WINDOW_MINUTES.get(h, 15))
    need = MIN_WINDOW_MINUTES.get(binding, 15)
    if minutes < need:
        label = HAZARD_LABELS[PermitHazardType(binding)]
        end = vf + timedelta(minutes=need)
        out.append({
            "id": "window-too-short", "level": "warning",
            "title": f"Only {_fmt_duration(minutes)} for {label}",
            "message": (
                f"What can be done safely in {_fmt_duration(minutes)}? This window has to cover {WINDOW_NEEDS[binding]}. "
                f"Plan at least {_fmt_duration(need)} — for example {vf:%H:%M}–{end:%H:%M}."
            ),
        })

    now = _parse_local(now_local)
    if now:
        past_by = round((now - vf).total_seconds() / 60)
        if past_by > 15:
            out.append({
                "id": "start-in-past", "level": "warning", "title": "Start time has already passed",
                "message": f"Valid From is {vf:%H:%M}, {_fmt_duration(past_by)} ago. Set the time the crew will actually begin, after approval.",
            })
        lead = round((vf - now).total_seconds() / 60)
        if -15 <= lead < 30 and any(h in _HIGH_RISK for h in hazards):
            out.append({
                "id": "no-time-for-approval", "level": "warning",
                "title": "Starts now — no time for approval" if lead <= 0 else f"Starts in {_fmt_duration(lead)}",
                "message": (
                    "High-risk permits go through the issuer, the safety officer and often the plant head, then the "
                    "pre-start checks. Allow at least 30–60 minutes before the planned start."
                ),
            })

    t, night = vf, False
    while t < vt:
        if t.hour >= 22 or t.hour < 6:
            night = True
            break
        t += timedelta(minutes=30)
    if night and any(h in _HIGH_RISK for h in hazards):
        out.append({
            "id": "night-work", "level": "info", "title": "Part of the work is at night (22:00–06:00)",
            "message": "Confirm lighting, a supervisor on site, and that the fire / rescue team is available on the night shift.",
        })
    if minutes > 12 * 60 and any(h in _HIGH_RISK for h in hazards):
        extra_txt = (", the fire-watch rota" if "FIRE_WATCH" in controls else "") + (
            ", and a fresh gas test after any break" if "GAS_TEST" in controls else "")
        out.append({
            "id": "multi-shift", "level": "info", "title": f"{_fmt_duration(minutes)} spans more than one shift",
            "message": f"Plan the shift handover: who holds the permit on each shift{extra_txt}. Consider one permit per shift.",
        })
    return out


async def check_draft(
    db: AsyncSession, draft: dict[str, Any], *, user_id: str, now_local: str | None = None
) -> dict[str, Any]:
    """Section-by-section list of what still blocks submission, plus advisory
    warnings. Sections match the form's four steps."""
    job: list[str] = []
    people: list[str] = []
    controls: list[str] = []
    checklist: list[str] = []
    warnings: list[str] = []

    ptype = draft.get("type")
    if ptype not in PERMIT_TYPE_LABELS:
        job.append("Permit type not chosen")
        ptype = None
    extra = _hazard_list(draft)
    hazards = [h.value for h in effective_hazards(ptype, extra)] if ptype else []
    controls_needed = sorted(required_controls(ptype, extra)) if ptype else []
    cap = validity_cap_hours(ptype, extra) if ptype else None
    cap_by = binding_cap_hazard(ptype, extra) if ptype else None

    vf, vt = _parse_local(draft.get("validFrom")), _parse_local(draft.get("validTo"))
    hours: float | None = None
    if not vf or not vt:
        job.append("Validity window (valid from / valid to) not set")
    elif vt <= vf:
        job.append("Valid To must be after Valid From")
    else:
        hours = round((vt - vf).total_seconds() / 3600, 1)
        if cap is not None and hours > cap:
            job.append(
                f"Validity is {hours}h but the cap is {cap}h "
                f"(set by {HAZARD_LABELS.get(cap_by, cap_by.value) if cap_by else 'the hazards'})"
            )
        now = _parse_local(now_local)
        if now and vt < now:
            job.append("Valid To is in the past")

    if not draft.get("plantId"):
        job.append("Plant not set")
    if not draft.get("areaId"):
        job.append("Area not set")
    scope = (draft.get("scopeOfWork") or "").strip()
    if len(scope) < 10:
        job.append("Scope of work must describe the job (10+ characters)")

    issuer, receiver = draft.get("issuerId"), draft.get("receiverId")
    crew = [c for c in draft.get("workCrew") or [] if isinstance(c, dict) and c.get("userId")]
    if not issuer:
        people.append("Issuer not assigned")
    if not receiver:
        people.append("Receiver not assigned")
    if issuer and receiver and issuer == receiver:
        people.append("Issuer and receiver must be different people")
    if issuer and issuer == user_id:
        people.append("You (the originator) cannot also be the issuer")
    if "FIRE_WATCH" in controls_needed and not draft.get("fireWatchPersonId"):
        people.append("Fire watch person required for these hazards")
    if "STANDBY" in controls_needed:
        sb = draft.get("standbyPersonId")
        if not sb:
            people.append("Standby person required for these hazards")
        elif any(c["userId"] == sb for c in crew):
            people.append("Standby person cannot also be on the work crew")

    # Competency — the same gate create_permit applies, run early.
    if ptype and (receiver or crew):
        from app.services.competency import check_competency_for_permit_type

        holders: list[tuple[str, str]] = []
        if receiver:
            holders.append(("Receiver", receiver))
        holders += [(f"Crew member {i + 1}", c["userId"]) for i, c in enumerate(crew[:30])]
        for label, uid in holders:
            for type_code in competency_types_for(ptype, extra):
                try:
                    comp = await check_competency_for_permit_type(db, uid, type_code)
                except Exception:  # noqa: BLE001 — advisory; never break the draft check
                    continue
                if not comp.ok:
                    gaps = ", ".join(f"{b.programName} ({b.code.lower()})" for b in comp.blockers[:4])
                    people.append(f"{label} is not certified for {PERMIT_TYPE_LABELS.get(type_code, type_code)}: {gaps}")

    if ptype and ptype != "GENERAL_COLD":
        for i, iso in enumerate(draft.get("isolations") or []):
            if not isinstance(iso, dict) or not (
                iso.get("isolationType") and iso.get("description") and iso.get("isolationPointTag")
            ):
                controls.append(f"Isolation {i + 1} needs a type, description and point tag")
            elif is_tbc(iso.get("isolationPointTag")):
                controls.append(f"Isolation {i + 1} ({iso.get('description')}): enter the real point tag (still TBC)")
        if ptype == "ELECTRICAL_LOTO" and not draft.get("isolations") and not draft.get("lotoLinked"):
            warnings.append("Electrical / LOTO work with no isolations listed and no LOTO lockout linked")
    if not draft.get("requiredPpe"):
        controls.append("At least one PPE item is required")
    for i, s in enumerate(draft.get("subjectEquipment") or []):
        if not isinstance(s, dict) or not (s.get("equipmentId") and s.get("workNature")):
            controls.append(f"Subject equipment row {i + 1} needs equipment and work nature")
    if "GAS_TEST" in controls_needed:
        plan = draft.get("gasTestPlan") if isinstance(draft.get("gasTestPlan"), dict) else {}
        params = plan.get("parametersToTest") or []
        if not params:
            controls.append("Gas test plan needs at least one parameter")
        elif any(not (isinstance(p, dict) and p.get("parameter") and p.get("unit")) for p in params):
            controls.append("Each gas test parameter needs a name and unit")
        try:
            refresh = int((plan or {}).get("refreshFrequencyMinutes") or 0)
        except (TypeError, ValueError):
            refresh = 0
        if not 15 <= refresh <= 480:
            controls.append("Gas re-test frequency must be 15–480 minutes")
    if "RESCUE_PLAN" in controls_needed and not (draft.get("rescuePlan") or "").strip():
        controls.append("Rescue plan required for these hazards")
    try:
        wind = float(draft.get("windSpeedKmh") or 0)
    except (TypeError, ValueError):
        wind = 0
    if "HOT_WORK" in hazards and wind > 25:
        warnings.append("Wind above 25 km/h — outdoor hot work is not recommended")

    reported = draft.get("checklist") if isinstance(draft.get("checklist"), dict) else {}
    for h in hazards:
        st = reported.get(h) if isinstance(reported.get(h), dict) else None
        label = HAZARD_LABELS.get(PermitHazardType(h), h)
        if st is None:
            checklist.append(f"{label} precaution checklist not yet answered")
        elif st.get("blocker"):
            checklist.append(str(st["blocker"])[:200])

    alerts = plan_advisories(ptype, extra, draft.get("validFrom"), draft.get("validTo"), now_local)
    warnings = [f"{a['title']}: {a['message']}" for a in alerts] + warnings

    blocking = job + people + controls
    return {
        "permitType": ptype,
        "effectiveHazards": hazards,
        "requiredControls": controls_needed,
        "validityCapHours": cap,
        "validityHours": hours,
        "sections": {"job": job, "people": people, "controls": controls, "checklist": checklist},
        "warnings": warnings,
        "alerts": alerts,
        "readyForChecklist": not blocking,
        "readyToSubmit": not blocking and not checklist,
        "note": (
            "Checklist items are answered by the user on step 4 — the copilot never answers them."
            if checklist else None
        ),
    }


async def validate_checklist_suggestions(
    db: AsyncSession, merged: dict[str, Any], suggestions: dict[str, list[dict[str, Any]]]
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Check suggested answers against the live catalog: the hazard must be on
    the permit, the item must belong to it, and NA only where the item allows
    it. Returns (errors, checklist summary per hazard for the rule check)."""
    from app.services.ptw_annexures import load_catalog

    ptype = merged.get("type")
    in_force = (
        {h.value for h in effective_hazards(ptype, _hazard_list(merged))} if ptype in PERMIT_TYPE_LABELS else set()
    )
    errors: list[str] = []
    summary: dict[str, dict[str, Any]] = {}
    catalog = await load_catalog(db, [PermitHazardType(h) for h in suggestions])
    for h, answers in suggestions.items():
        label = HAZARD_LABELS.get(PermitHazardType(h), h)
        if h not in in_force:
            errors.append(f"{label} is not a hazard on this permit — add it to extraHazards first")
            continue
        items = {i.id: i for i in catalog.get(PermitHazardType(h), [])}
        answered: set[str] = set()
        for a in answers:
            item = items.get(a["itemId"])
            if item is None:
                errors.append(f"{label}: unknown checklist item {a['itemId']} — use get_ptw_reference with includeChecklist")
            elif a["response"] == "NA" and not item.allowsNA:
                errors.append(f"{label}: '{item.text[:70]}' cannot be answered NA")
            else:
                answered.add(item.id)
        left = [i for i in items.values() if i.isMandatory and i.id not in answered]
        summary[h] = {
            "total": len(items),
            "answered": len(answered),
            "blocker": (
                f"{label}: {len(left)} precaution{'s' if len(left) != 1 else ''} still unanswered"
                if left
                else f"{label}: {len(answered)} suggested answers waiting for the user's on-site confirmation"
            ),
        }
    return errors, summary


def reference_for(permit_type: str | None, extra_hazards: list[str] | None) -> dict[str, Any]:
    """Static rules + vocabularies the model needs to fill the form."""
    out: dict[str, Any] = {
        "permitTypes": PERMIT_TYPE_LABELS,
        "hazardTypes": {h.value: HAZARD_LABELS[h] for h in PermitHazardType},
        "ppeCatalog": PPE_CATALOG,
        "crewRoles": list(CrewRoleT.__args__),  # type: ignore[attr-defined]
        "isolationTypes": list(IsolationTypeT.__args__),  # type: ignore[attr-defined]
        "workNatures": list(WorkNatureT.__args__),  # type: ignore[attr-defined]
        "timeFormat": "validFrom/validTo are local wall-clock 'YYYY-MM-DDTHH:MM' (same clock as pageContext.nowLocal)",
    }
    if permit_type in PERMIT_TYPE_LABELS:
        extra = [h for h in (extra_hazards or []) if h in PermitHazardType.__members__]
        cap_by = binding_cap_hazard(permit_type, extra)
        out["forThisPermit"] = {
            "effectiveHazards": [h.value for h in effective_hazards(permit_type, extra)],
            "validityCapHours": validity_cap_hours(permit_type, extra),
            "capSetBy": HAZARD_LABELS.get(cap_by, cap_by.value),
            "requiredControls": sorted(required_controls(permit_type, extra)),
            "competencyTypesRequired": competency_types_for(permit_type, extra),
            "defaultPpe": PPE_DEFAULTS.get(permit_type, []),
            "defaultGasTest": (
                {
                    "refreshFrequencyMinutes": DEFAULT_GAS_REFRESH_MINUTES[permit_type],
                    "parametersToTest": DEFAULT_GAS_PARAMS[permit_type],
                }
                if permit_type in DEFAULT_GAS_PARAMS
                else None
            ),
        }
    return out


__all__ = [
    "DEFAULT_GAS_PARAMS",
    "PERMIT_TYPE_LABELS",
    "PPE_CATALOG",
    "PPE_DEFAULTS",
    "PtwDraftPatch",
    "apply_patch",
    "check_draft",
    "normalise_client_draft",
    "parse_patch",
    "reference_for",
    "validate_checklist_suggestions",
    "validate_references",
]
