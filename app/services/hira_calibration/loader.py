"""DB side of calibration v2: build the engine's input for one HIRA entry.

Reads the entry, its study matrix, the plant's evidence (incidents, near misses,
statutory inspections) and plant data (existing controls on sibling entries and
earlier versions, the statutory register, open CAPAs, the regulation map).

The per-hazard rating / control-link columns added by
prisma/apply-hira-calibration-ddl.ts are read with plain SQL, only after a
schema check, so this module works (and the app is unchanged) before the DDL
has been applied.
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.services.hira_calibration import risk_engine as eng
from app.services.hira_calibration import review_v2 as rv
from app.services.hira_calibration.config import MatrixRule, matrix_rule, table_exists

OPEN_CAPA_STATES = ("DRAFT", "SUBMITTED", "UNDER_RCA", "ACTIONS_PLANNED", "ACTIONS_IN_PROGRESS",
                    "PENDING_VERIFICATION")
PLANNED_STATUSES = ("PROPOSED", "APPROVED", "IN_PROGRESS")
# Equipment register categories / words per hazard family (statutory register).
EQUIPMENT_FAMILY = {
    "LIFTING": ("crane", "hoist", "lifting", "chain block", "sling", "winch"),
    "PRESSURE": ("pressure vessel", "air receiver", "boiler", "compressor", "pressure_vessel"),
    "VEHICLE": ("forklift", "mobile_equipment", "loader", "truck"),
    "ELECTRICAL": ("transformer", "panel", "switchgear", "substation"),
}

_SCHEMA: dict[str, Any] = {"ready": None, "at": 0.0}


async def calibration_schema_ready(db: AsyncSession) -> bool:
    """True once the calibration DDL has been applied (cached for 5 minutes)."""
    if _SCHEMA["ready"] is not None and time.monotonic() - _SCHEMA["at"] < 300:
        return bool(_SCHEMA["ready"])
    n = (await db.execute(text(
        "SELECT count(*) FROM information_schema.columns WHERE table_name = 'HiraEntryHazard' "
        "AND column_name IN ('likelihoodScore', 'severityScore')"))).scalar()
    ready = n == 2
    _SCHEMA.update(ready=ready, at=time.monotonic())
    return ready


def reset_schema_cache() -> None:
    _SCHEMA.update(ready=None, at=0.0)


def _words(t: str | None) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", (t or "").lower())}


async def _matrix(db: AsyncSession, matrix_id: str | None) -> tuple[Any, list[str]]:
    from app.models.hira import RiskMatrix  # noqa: PLC0415

    if not matrix_id:
        return None, []
    m = (await db.execute(select(RiskMatrix).where(RiskMatrix.id == matrix_id)
                          .options(selectinload(RiskMatrix.likelihoods)))).scalar_one_or_none()
    labels = [l.label for l in sorted(m.likelihoods, key=lambda x: x.score)] if m else []
    return m, labels


def _event_families(text_: str, category: str | None) -> set[str]:
    t = f" {(text_ or '').lower()} "
    fams = {fam for fam, kws in eng.FAMILY_KEYWORDS if any(k in t for k in kws)}
    if category:
        cat = category.lower().replace(" ", "_")
        if cat in eng.FAMILY_BY_CATEGORY:
            fams.add(eng.FAMILY_BY_CATEGORY[cat])
    return fams


async def _evidence(db: AsyncSession, *, plant_id: str | None, area_id: str | None, months: int,
                    equipment: list[Any], now: datetime) -> eng.Evidence:
    """Incidents and near misses in the entry's area within the window, counted
    per hazard family by what they describe; overdue statutory inspections of
    equipment the entry names count as control failures for that family."""
    from app.models.incident import Incident  # noqa: PLC0415
    from app.models.near_miss import NearMiss  # noqa: PLC0415

    ev = eng.Evidence(window_months=months)
    if not plant_id:
        return ev
    since, since12 = now - timedelta(days=30 * months), now - timedelta(days=365)
    events: list[tuple[datetime, str, set[str]]] = []
    if area_id:
        inc = (await db.execute(select(Incident.date, Incident.number, Incident.description,
                                       Incident.activityBeingPerformed).where(
            Incident.plantId == plant_id, Incident.areaId == area_id, Incident.date >= since))).all()
        nm = (await db.execute(select(NearMiss.date, NearMiss.number, NearMiss.description, NearMiss.activity,
                                      NearMiss.activityBeingPerformed, NearMiss.hazardCategory).where(
            NearMiss.plantId == plant_id, NearMiss.areaId == area_id, NearMiss.date >= since))).all()
        for d, num, desc, act in inc:
            events.append((d, f"incident {num}", _event_families(f"{desc} {act or ''}", None)))
        for d, num, desc, act, act2, cat in nm:
            events.append((d, f"near miss {num}", _event_families(f"{desc} {act or ''} {act2 or ''}", cat)))
    ev.events_window = len(events)
    ev.events_12m = sum(1 for d, _, _ in events if d and (d if d.tzinfo else d.replace(tzinfo=timezone.utc)) >= since12)
    if events:
        ev.items.append(f"{len(events)} incident(s) / near miss(es) in this area in the last {months} months")
    for d, label, fams in events:
        recent = bool(d and (d if d.tzinfo else d.replace(tzinfo=timezone.utc)) >= since12)
        for fam in fams:
            f = ev.by_family.setdefault(fam, {"window": 0, "12m": 0, "failures": [], "items": []})
            f["window"] += 1
            f["12m"] += int(recent)
            if len(f["items"]) < 3:
                f["items"].append(f"{label} ({d:%d %b %Y})" if d else label)
    for q in equipment:
        due = q.nextInspectionDue
        if due is not None and (due if due.tzinfo else due.replace(tzinfo=timezone.utc)) < now:
            msg = f"statutory inspection of {q.name} overdue since {due:%d %b %Y}"
            ev.control_failures.append(msg)
            ev.items.append(msg)
            fam = _equipment_family(q) or "GENERAL"
            f = ev.by_family.setdefault(fam, {"window": 0, "12m": 0, "failures": [], "items": []})
            f["failures"].append(msg)
            f["items"].append(msg)
    # Families with no matching event still get an (empty) entry, so the hazard
    # is judged on its own record rather than the whole area's.
    ev.by_family.setdefault("GENERAL", {"window": 0, "12m": 0, "failures": [], "items": []})
    return ev


def _equipment_family(e: Any) -> str | None:
    t = f"{e.name} {e.category or ''}".lower()
    for fam, kws in EQUIPMENT_FAMILY.items():
        if any(k in t for k in kws):
            return fam
    return None


async def build(db: AsyncSession, entry_id: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Everything evaluate() needs. Returns {"input", "rule", "evidence", "plant", "entry", "study"}."""
    from app.models.capa import Capa  # noqa: PLC0415
    from app.models.equipment import Equipment  # noqa: PLC0415
    from app.models.hira import HiraEntry, HiraEntryHazard, HiraStudy  # noqa: PLC0415
    from app.services.copilot import hira_review as v1  # noqa: PLC0415

    stmt = (select(HiraEntry).where(HiraEntry.id == entry_id)
            .options(selectinload(HiraEntry.study), selectinload(HiraEntry.hazards).selectinload(HiraEntryHazard.hazard),
                     selectinload(HiraEntry.existingControls), selectinload(HiraEntry.recommendedControls),
                     selectinload(HiraEntry.regulationRefs))
            .execution_options(populate_existing=True))
    e = (await db.execute(stmt)).scalar_one_or_none()
    if e is None:
        raise LookupError("HIRA entry not found")
    study: HiraStudy = e.study
    plant_id = study.plantId if study else None
    now = datetime.now(timezone.utc)
    ready = await calibration_schema_ready(db)

    matrix, labels = await _matrix(db, study.riskMatrixId if study else None)
    rule: MatrixRule = matrix_rule(matrix, cfg, labels)

    # per-hazard ratings and control links (new columns, only once the DDL is in)
    hz_extra: dict[str, dict[str, Any]] = {}
    ctl_links: dict[str, list[str]] = {}
    rec_extra: dict[str, dict[str, Any]] = {}
    if ready:
        rows = (await db.execute(text(
            'SELECT "hazardId", "likelihoodScore", "severityScore", "regulationSuggested" FROM "HiraEntryHazard" '
            'WHERE "entryId" = :e'), {"e": e.id})).all()
        hz_extra = {r[0]: {"l": r[1], "s": r[2], "suggested": r[3]} for r in rows}
        rows = (await db.execute(text('SELECT id, "appliesToHazardIds" FROM "HiraEntryControl" WHERE "entryId" = :e'),
                                 {"e": e.id})).all()
        ctl_links = {r[0]: r[1] for r in rows if r[1]}
        rows = (await db.execute(text(
            'SELECT id, "appliesToHazardIds", "ownerRole" FROM "HiraEntryRecommendedControl" WHERE "entryId" = :e'),
            {"e": e.id})).all()
        rec_extra = {r[0]: {"links": r[1], "ownerRole": r[2]} for r in rows}

    hazards = []
    regs: dict[str, tuple[str | None, str | None]] = {}
    entry_level_refs = bool(e.regulationRefs)
    for eh in sorted(e.hazards, key=lambda x: x.sortOrder or 0):
        hm = eh.hazard
        x = hz_extra.get(eh.hazardId, {})
        hazards.append(eng.Hazard(key=eh.hazardId, name=hm.name if hm else "Hazard", code=hm.code if hm else None,
                                  category=hm.category if hm else None,
                                  consequence=eh.consequence or eh.contextualDescription,
                                  likelihood=x.get("l"), severity=x.get("s")))
        ref = eh.regulationRef or ("entry-level" if entry_level_refs else None)
        regs[eh.hazardId] = (ref, eh.regulationSection)
    stored = bool(hazards) and all(h.likelihood and h.severity for h in hazards)

    controls = [eng.Control(key=c.id, hierarchy=c.hierarchy, description=c.description, effectiveness=c.effectiveness,
                            verification_method=c.verificationMethod, evidence_attached=bool(c.evidenceAttached),
                            hazard_keys=tuple(ctl_links[c.id]) if ctl_links.get(c.id) else None)
                for c in sorted(e.existingControls, key=lambda c: c.sortOrder or 0)]
    recommended = []
    for r in e.recommendedControls:
        x = rec_extra.get(r.id, {})
        links = tuple(x["links"]) if x.get("links") else None
        due = r.proposedImplementationDate.date().isoformat() if r.proposedImplementationDate else None
        if r.status == "IMPLEMENTED":
            controls.append(eng.Control(key=r.id, hierarchy=r.hierarchy, description=r.description,
                                        effectiveness="EFFECTIVE" if r.evidenceAttached else None,
                                        evidence_attached=bool(r.evidenceAttached), hazard_keys=links))
        elif r.status in PLANNED_STATUSES:
            controls.append(eng.Control(key=r.id, hierarchy=r.hierarchy, description=r.description, hazard_keys=links,
                                        planned=True, planned_date=due))
        recommended.append({"key": r.id, "hierarchy": r.hierarchy, "description": r.description,
                            "status": r.status, "ownerRole": x.get("ownerRole"), "proposedImplementationDate": due})

    src = v1.source_for(study.number if study else None, e.sequenceNumber)
    review_due = e.nextReviewDue or (study.nextScheduledReviewDate if study else None)
    entry_input = rv.EntryInput(
        entry_id=e.id, label=(f"S.No {e.sequenceNumber}" if src else f"#{e.sequenceNumber}"),
        activity=e.activityDescription or "", routine=e.routine, frequency=e.frequency, hazards=hazards,
        entry_l=e.initialLikelihoodScore, entry_s=e.initialSeverityScore,
        saved_residual=((e.residualLikelihoodScore, e.residualSeverityScore)
                        if e.residualLikelihoodScore and e.residualSeverityScore else None),
        # An imported single-rating sheet (QSD/24/F) rates the risk WITH the
        # existing controls: crediting them again would double count.
        rating_basis="WITH_CONTROLS" if src else "INHERENT",
        controls=controls, recommended=recommended, hazard_ratings_stored=stored,
        elimination_considered=bool(e.alarpFurtherControlsConsidered is not None and (
            (e.alarpFurtherControlsDescription or "").strip() or (e.alarpJustification or "").strip())),
        review_date_set=review_due is not None,
        review_overdue=bool(e.nextReviewDue and (e.nextReviewDue if e.nextReviewDue.tzinfo
                                                 else e.nextReviewDue.replace(tzinfo=timezone.utc)) < now),
        override_active=bool(e.unacceptableOverrideById and e.unacceptableOverrideExpiresAt and (
            e.unacceptableOverrideExpiresAt if e.unacceptableOverrideExpiresAt.tzinfo
            else e.unacceptableOverrideExpiresAt.replace(tzinfo=timezone.utc)) > now),
        hazard_regulations=regs, today=date.today(),
    )

    # ── statutory register: equipment this entry names ──
    families = {h.family for h in hazards}
    named = " ".join([e.activityDescription or "", *[str(x) for x in (e.equipmentUsed or [])]]).lower()
    eq_rows = []
    if plant_id:
        eq_rows = (await db.execute(select(Equipment).where(Equipment.plantId == plant_id,
                                                            Equipment.active.is_(True)))).scalars().all()
    matched, other_overdue = [], []
    for q in eq_rows:
        fam = _equipment_family(q)
        if fam not in families:
            continue
        name_words = _words(q.name) - {"bay", "the", "and", "unit"}
        if name_words and (len(name_words & _words(named)) >= max(2, len(name_words) // 2)
                           or (q.code and q.code.lower() in named)):
            matched.append(q)
        elif q.nextInspectionDue and (q.nextInspectionDue if q.nextInspectionDue.tzinfo
                                      else q.nextInspectionDue.replace(tzinfo=timezone.utc)) < now:
            other_overdue.append(q)

    evidence = await _evidence(db, plant_id=plant_id, area_id=e.areaId, months=cfg["likelihood"]["evidenceWindowMonths"],
                               equipment=matched, now=now)
    statutory_overdue: dict[str, list[str]] = {}
    for q in matched:
        if q.nextInspectionDue and (q.nextInspectionDue if q.nextInspectionDue.tzinfo
                                    else q.nextInspectionDue.replace(tzinfo=timezone.utc)) < now:
            statutory_overdue.setdefault(_equipment_family(q) or "GENERAL", []).append(q.name)

    # ── existing controls from plant data (R6) ──
    proposals: list[dict[str, Any]] = []
    for q in matched:
        last = f", last {q.lastInspectionDate:%d %b %Y}" if q.lastInspectionDate else ""
        nxt = f", next due {q.nextInspectionDue:%d %b %Y}" if q.nextInspectionDue else ""
        proposals.append({"hierarchy": "ADMINISTRATIVE",
                          "description": f"Statutory examination / inspection of {q.name}{last}{nxt}",
                          "source": "Equipment register", "verificationMethod": "Statutory register"})
    sib_rec: list[dict[str, Any]] = []
    if study is not None:
        scope = (or_(HiraEntry.studyId == e.studyId, HiraEntry.areaId == e.areaId) if e.areaId
                 else HiraEntry.studyId == e.studyId)
        sib_stmt = (select(HiraEntry).where(HiraEntry.isCurrentVersion.is_(True), HiraEntry.id != e.id)
                    .where(scope)
                    .options(selectinload(HiraEntry.hazards).selectinload(HiraEntryHazard.hazard),
                             selectinload(HiraEntry.existingControls), selectinload(HiraEntry.recommendedControls))
                    .limit(40))
        siblings = (await db.execute(sib_stmt)).scalars().all()
        for s in siblings:
            s_fams = {eng.hazard_family(h.hazard.code if h.hazard else None, h.hazard.category if h.hazard else None,
                                        h.consequence) for h in s.hazards}
            if not (s_fams & families):
                continue
            for c in s.existingControls:
                if eng.is_baseline(c.hierarchy, c.description, cfg):
                    continue
                if not (eng.control_families(c.description) & families) and not eng.is_generic_control(c.description):
                    continue
                if eng.is_generic_control(c.description) and not (eng.control_families(c.description) & families):
                    continue  # another activity's SOP is not this activity's SOP
                proposals.append({"hierarchy": c.hierarchy, "description": c.description,
                                  "source": f"Entry #{s.sequenceNumber} ({'same study' if s.studyId == e.studyId else 'same area'})",
                                  "verificationMethod": c.verificationMethod})
            if s.studyId == e.studyId:
                for r in s.recommendedControls:
                    sib_rec.append({"label": f"#{s.sequenceNumber}", "text": r.description})
    if e.parentVersionId:
        prev = (await db.execute(select(HiraEntry).where(HiraEntry.id == e.parentVersionId)
                                 .options(selectinload(HiraEntry.existingControls)))).scalar_one_or_none()
        for c in (prev.existingControls if prev else []):
            proposals.append({"hierarchy": c.hierarchy, "description": c.description,
                              "source": f"Earlier version v{prev.versionNumber}",
                              "verificationMethod": c.verificationMethod})

    # ── open CAPAs in the plant (link, don't re-raise) ──
    open_capas = []
    if plant_id:
        rows = (await db.execute(select(Capa.id, Capa.capaNumber, Capa.title, Capa.problemDescription).where(
            Capa.plantId == plant_id, Capa.state.in_(OPEN_CAPA_STATES)).limit(300))).all()
        open_capas = [{"id": r[0], "number": r[1], "text": f"{r[2] or ''}. {r[3] or ''}"} for r in rows]

    # ── regulation map ──
    rmap = list(rv.DEFAULT_REGULATION_MAP)
    if await table_exists(db, "HiraRegulationMap"):
        from app.models.hira_calibration import HiraRegulationMap  # noqa: PLC0415

        rows = (await db.execute(select(HiraRegulationMap).where(
            HiraRegulationMap.isActive.is_(True),
            or_(HiraRegulationMap.plantId.is_(None), HiraRegulationMap.plantId == plant_id))
            .order_by(HiraRegulationMap.plantId.is_(None), HiraRegulationMap.sortOrder))).scalars().all()
        if rows:
            rmap = [{"hazardCode": r.hazardCode, "hazardCategory": r.hazardCategory, "family": r.family,
                     "regulation": r.regulation, "section": r.section} for r in rows]

    # ── source-sheet checks (imported client HIRA) as advisories ──
    extra = []
    if src:
        f = v1._Findings()
        v1._sheet_checks(f, src, e.sequenceNumber)
        for it in f.items:
            if it["code"] in ("SEVERITY_LOW",):  # covered by the v2 severity rubric
                continue
            extra.append(rv._finding(f"SHEET_{it['code']}", rv.INFO if it["level"] in ("info", "suggestion")
                                     else rv.ADVISORY, it["title"],
                                     it["detail"] + (f" (QSD/24/F row {it['sheetRow']})" if it.get("sheetRow") else ""),
                                     it.get("fix")))
    for q in other_overdue[:2]:
        extra.append(rv._finding("STATUTORY_CHECK", rv.INFO, "Confirm the equipment used",
                                 f"The plant register shows the statutory inspection of {q.name} overdue since "
                                 f"{q.nextInspectionDue:%d %b %Y}.",
                                 "If this activity uses it, name it under equipment used: an overdue statutory "
                                 "examination is a documented control failure."))

    plant = rv.PlantData(existing_control_proposals=proposals, open_capas=open_capas, sibling_recommended=sib_rec,
                         statutory_overdue=statutory_overdue, regulation_map=rmap, extra_findings=extra)
    return {"input": entry_input, "rule": rule, "evidence": evidence, "plant": plant, "entry": e, "study": study,
            "schemaReady": ready}


async def build_for_draft(db: AsyncSession, *, study: Any, area_id: str | None, families: set[str],
                          cfg: dict[str, Any]) -> dict[str, Any]:
    """Rule, evidence and plant data for an entry that is not saved yet (a
    Copilot draft): the study's matrix, the area's events, open CAPAs, and the
    controls / recommendations already on entries of the same study."""
    from app.models.capa import Capa  # noqa: PLC0415
    from app.models.hira import HiraEntry, HiraEntryHazard  # noqa: PLC0415

    now = datetime.now(timezone.utc)
    matrix, labels = await _matrix(db, study.riskMatrixId)
    rule = matrix_rule(matrix, cfg, labels)
    evidence = await _evidence(db, plant_id=study.plantId, area_id=area_id or study.areaId,
                               months=cfg["likelihood"]["evidenceWindowMonths"], equipment=[], now=now)
    siblings = (await db.execute(select(HiraEntry).where(HiraEntry.studyId == study.id,
                                                          HiraEntry.isCurrentVersion.is_(True))
                                 .options(selectinload(HiraEntry.hazards).selectinload(HiraEntryHazard.hazard),
                                          selectinload(HiraEntry.existingControls),
                                          selectinload(HiraEntry.recommendedControls)).limit(60))).scalars().all()
    proposals, sib_rec = [], []
    for s in siblings:
        for c in s.existingControls:
            if eng.is_baseline(c.hierarchy, c.description, cfg) or not (eng.control_families(c.description) & families):
                continue
            proposals.append({"hierarchy": c.hierarchy, "description": c.description,
                              "source": f"Entry #{s.sequenceNumber} (same study)",
                              "verificationMethod": c.verificationMethod})
        sib_rec += [{"label": f"#{s.sequenceNumber}", "text": r.description} for r in s.recommendedControls]
    rows = (await db.execute(select(Capa.id, Capa.capaNumber, Capa.title, Capa.problemDescription).where(
        Capa.plantId == study.plantId, Capa.state.in_(OPEN_CAPA_STATES)).limit(300))).all()
    plant = rv.PlantData(existing_control_proposals=proposals,
                         open_capas=[{"id": r[0], "number": r[1], "text": f"{r[2] or ''}. {r[3] or ''}"} for r in rows],
                         sibling_recommended=sib_rec)
    return {"rule": rule, "evidence": evidence, "plant": plant}


__all__ = ["build", "build_for_draft", "calibration_schema_ready", "reset_schema_cache"]
