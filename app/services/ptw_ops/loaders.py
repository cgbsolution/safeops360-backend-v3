"""Async loaders → Snapshot + Extras for one plant.

Round trips per refresh: (1) a cached schema probe, (2) the permit query with
LATERAL sub-selects, (3) one plant-context row of JSON sub-selects, (4) the
user-name lookup. Supabase RTT from India is ~40 ms, so this is what keeps the
payload inside its 500 ms p95 budget at 10k permits — N widget queries would not.

Tables/columns that only exist after prisma/apply-ptw-control-center-ddl.ts
are probed first and left out of the SQL when missing, so the control center
still works (impairments/exceptions empty, default rules) on a database where
the DDL has not been run.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ptw_cc import load_shifts
from app.services.ptw_ops import defaults, steps
from app.services.ptw_ops.payload import Extras
from app.services.ptw_ops.shifts import range_window
from app.services.ptw_ops.types import (
    ImpairmentRuleView,
    ImpairmentView,
    LayoutView,
    PermitView,
    SimopsRuleView,
    Snapshot,
    ZoneView,
)

TENANT = "default"
PERMIT_LIMIT = 3000
_SCHEMA_TTL = 60.0
_schema_cache: tuple[float, dict[str, bool]] | None = None

NEW_TABLES = ("PlantLayout", "ZoneGuidance", "Impairment", "ImpairmentRule", "SimopsRule", "PermitControl",
              "PermitEvent", "PtwException", "PtwShiftBrief", "PtwKioskToken")


def _utc(d: Any) -> datetime | None:
    if d is None:
        return None
    if isinstance(d, str):
        d = datetime.fromisoformat(d.replace("Z", "+00:00"))
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


async def schema(db: AsyncSession, *, fresh: bool = False) -> dict[str, bool]:
    """Which control-center tables/columns exist. Cached for a minute."""
    global _schema_cache
    if not fresh and _schema_cache and time.monotonic() - _schema_cache[0] < _SCHEMA_TTL:
        return _schema_cache[1]
    rows = (await db.execute(text(
        """SELECT table_name, column_name FROM information_schema.columns
           WHERE table_schema = current_schema()
             AND (table_name = ANY(:tables)
                  OR (table_name = 'Area' AND column_name IN ('geometry','sortOrder'))
                  OR (table_name = 'PermitCrewMember' AND column_name IN ('checkedInAt','checkedOutAt')))"""
    ), {"tables": list(NEW_TABLES)})).all()
    have_tables = {r[0] for r in rows}
    have_cols = {f"{r[0]}.{r[1]}" for r in rows}
    out = {t: t in have_tables for t in NEW_TABLES}
    out["Area.geometry"] = "Area.geometry" in have_cols
    out["Area.sortOrder"] = "Area.sortOrder" in have_cols
    out["PermitCrewMember.checkedInAt"] = "PermitCrewMember.checkedInAt" in have_cols
    out["ready"] = all(out[t] for t in NEW_TABLES) and out["Area.geometry"]
    _schema_cache = (time.monotonic(), out)
    return out


def reset_schema_cache() -> None:
    global _schema_cache
    _schema_cache = None


# ── permits ──────────────────────────────────────────────────────────────────
def _permit_sql(s: dict[str, bool]) -> str:
    checked_in = (
        'count(*) FILTER (WHERE c."removedAt" IS NULL AND c."checkedInAt" IS NOT NULL AND c."checkedOutAt" IS NULL)'
        if s["PermitCrewMember.checkedInAt"] else "0"
    )
    ev_join = (
        'LEFT JOIN LATERAL (SELECT ev."createdAt" AS step_at, ev."toStep" AS to_step FROM "PermitEvent" ev '
        'WHERE ev."permitId" = p.id ORDER BY ev."createdAt" DESC LIMIT 1) ev ON true'
        if s["PermitEvent"] else
        "LEFT JOIN LATERAL (SELECT NULL::timestamptz AS step_at, NULL::text AS to_step) ev ON true"
    )
    ctl_join = (
        'LEFT JOIN LATERAL (SELECT array_agg(pc."controlKey") AS controls FROM "PermitControl" pc '
        'WHERE pc."permitId" = p.id) ctl ON true'
        if s["PermitControl"] else "LEFT JOIN LATERAL (SELECT NULL::text[] AS controls) ctl ON true"
    )
    # Only schema flags are interpolated — never request input.
    return f"""
SELECT p.id, p.number, p.type::text AS type, p.status::text AS status, p."plantId", p."areaId",
       p."validFrom", p."validTo", p."createdAt", p."issuerId", p."receiverId", p."contractorCompanyId",
       p."contractorName", p."closureType", p."closedAt", p."activatedAt", p."workCompletedAt",
       p."cancelledAt", p."suspendedReason", p."suspendedAt", p."issuedAt", p."siteVerifiedAt",
       p."gasTestRequired", p."flraRequired", p."currentActiveFlraId", p."fireWatchPersonId",
       wi."currentStepName" AS step_name, ws."slaHours" AS step_sla_h,
       tk."assignedToId" AS task_assignee, tk."assignedAt" AS task_assigned_at,
       cr.crew, cr.checked_in,
       gr."recordedAt" AS gas_at, gr."refreshDueBy" AS gas_due, gr."isExceedance" AS gas_exc,
       EXISTS (SELECT 1 FROM "PermitGasTestPlan" g WHERE g."permitId" = p.id) AS has_gas_plan,
       iso.iso_total, iso.iso_unverified,
       lo.status AS loto_status,
       ev.step_at, ev.to_step,
       ctl.controls,
       ce.cert_min, ce.cert_exp, ce.cert_exp7,
       la.last_act
FROM "Permit" p
LEFT JOIN "WorkflowInstance" wi ON wi.module = 'PTW' AND wi."recordId" = p.id
LEFT JOIN "WorkflowStep" ws ON ws.id = wi."currentStepId"
LEFT JOIN LATERAL (
    SELECT t."assignedToId", t."assignedAt" FROM "WorkflowTask" t
    WHERE t."instanceId" = wi.id AND t.status IN ('PENDING','OVERDUE','ESCALATED')
    ORDER BY t."assignedAt" DESC LIMIT 1) tk ON true
LEFT JOIN LATERAL (
    SELECT count(*) FILTER (WHERE c."removedAt" IS NULL) AS crew, {checked_in} AS checked_in
    FROM "PermitCrewMember" c WHERE c."permitId" = p.id) cr ON true
LEFT JOIN LATERAL (
    SELECT r."recordedAt", r."refreshDueBy", r."isExceedance" FROM "PermitGasTestReading" r
    WHERE r."permitId" = p.id ORDER BY r."recordedAt" DESC LIMIT 1) gr ON true
LEFT JOIN LATERAL (
    SELECT count(*) AS iso_total, count(*) FILTER (WHERE i."isolationVerifiedAt" IS NULL) AS iso_unverified
    FROM "PermitIsolation" i WHERE i."permitId" = p.id AND i."restoredAt" IS NULL) iso ON true
LEFT JOIN LATERAL (
    SELECT le.status FROM "LotoExecution" le
    WHERE le.id = p."lotoExecutionId" OR le."ptwId" = p.id
    ORDER BY le."createdAt" DESC LIMIT 1) lo ON true
{ev_join}
{ctl_join}
LEFT JOIN LATERAL (
    SELECT min(x.vt) AS cert_min,
           count(*) FILTER (WHERE x.vt <= CAST(:now AS timestamptz) + interval '1 day') AS cert_exp,
           count(*) FILTER (WHERE x.vt <= CAST(:now AS timestamptz) + interval '7 day') AS cert_exp7
    FROM (
        SELECT DISTINCT ON (tc."userId", tc."programId") tc."validTo" AS vt
        FROM "PermitCrewMember" c2
        JOIN "TrainingCertificate" tc ON tc."userId" = c2."userId" AND tc.status = 'ACTIVE'
        JOIN "TrainingProgram" tp ON tp.id = tc."programId"
             AND p.type::text = ANY(tp."isMandatoryForPermitTypes")
        WHERE c2."permitId" = p.id AND c2."removedAt" IS NULL
          AND p.status::text IN ('ISSUED','ACTIVE','SUSPENDED')
        ORDER BY tc."userId", tc."programId", tc."validTo" DESC NULLS FIRST
    ) x WHERE x.vt IS NOT NULL) ce ON true
LEFT JOIN LATERAL (
    SELECT GREATEST(
        (SELECT max(e."capturedAt") FROM "PermitActionEvidence" e WHERE e."permitId" = p.id),
        (SELECT max(cm."createdAt") FROM "PermitComment" cm WHERE cm."permitId" = p.id)) AS last_act
    ) la ON p.status::text = 'ACTIVE'
WHERE p."plantId" = :plant AND p."isDeleted" = false
  AND (
        (p.status::text NOT IN ('DRAFT','CLOSED','CANCELLED','REJECTED') AND p."closedAt" IS NULL)
     OR p."closedAt" >= CAST(:wstart AS timestamptz)
     OR (p.status::text <> 'DRAFT' AND p."validTo" >= CAST(:wstart AS timestamptz) AND p."validFrom" < CAST(:wend AS timestamptz))
  )
ORDER BY p."validTo" ASC
LIMIT {PERMIT_LIMIT}
"""  # noqa: S608


def _step_entered(r: Any, key: str) -> datetime | None:
    """When the permit entered its current step: the newest PermitEvent if it is
    for this step, else the best timestamp the permit row already carries."""
    if r.to_step == key and r.step_at is not None:
        return _utc(r.step_at)
    if key in steps.APPROVAL_STEPS:
        return _utc(r.task_assigned_at) or _utc(r.createdAt)
    if key == steps.AWAITING_ACCEPT:
        return _utc(r.issuedAt) or _utc(r.task_assigned_at)
    if key == steps.ACTIVE:
        return _utc(r.activatedAt)
    if key == steps.SUSPENDED:
        return _utc(r.suspendedAt)
    if key == steps.CLOSURE_PENDING:
        return _utc(r.siteVerifiedAt) or _utc(r.workCompletedAt)
    return _utc(r.step_at)


async def load_permits(db: AsyncSession, s: dict[str, bool], plant_id: str, now: datetime,
                       wstart: datetime, wend: datetime) -> list[PermitView]:
    rows = (await db.execute(text(_permit_sql(s)), {
        "plant": plant_id, "now": now, "wstart": wstart, "wend": wend,
    })).all()
    out = []
    for r in rows:
        controls = set(r.controls or [])
        if r.fireWatchPersonId:
            controls.add("fire_watch")
        p = PermitView(
            id=r.id, number=r.number, type=r.type, status=r.status, plantId=r.plantId, areaId=r.areaId,
            validFrom=_utc(r.validFrom), validTo=_utc(r.validTo), createdAt=_utc(r.createdAt),
            workflowStepName=r.step_name, pendingAssigneeId=r.task_assignee,
            stepSlaMin=int(r.step_sla_h) * 60 if r.step_sla_h is not None else None, issuerId=r.issuerId,
            receiverId=r.receiverId, contractorCompanyId=r.contractorCompanyId, contractorName=r.contractorName,
            closureType=r.closureType, closedAt=_utc(r.closedAt), activatedAt=_utc(r.activatedAt),
            workCompletedAt=_utc(r.workCompletedAt), cancelledAt=_utc(r.cancelledAt),
            suspendedReason=r.suspendedReason, crewCount=int(r.crew or 0), crewCheckedIn=int(r.checked_in or 0),
            gasRequired=bool(r.gasTestRequired or r.has_gas_plan), lastGasAt=_utc(r.gas_at),
            gasRefreshDueBy=_utc(r.gas_due), lastGasExceedance=bool(r.gas_exc),
            isolationsTotal=int(r.iso_total or 0), isolationsUnverified=int(r.iso_unverified or 0),
            lotoExecutionStatus=r.loto_status, flraRequired=bool(r.flraRequired),
            hasActiveFlra=bool(r.currentActiveFlraId), satisfiedControls=controls,
            crewCertMinExpiry=_utc(r.cert_min), crewCertExpiringCount=int(r.cert_exp or 0),
            crewCertExpiring7d=int(r.cert_exp7 or 0),
        )
        p.stepEnteredAt = _step_entered(r, p.step)
        acts = [d for d in (_utc(r.last_act), _utc(r.gas_at), _utc(r.step_at)) if d]
        p.lastActivityAt = max(acts) if acts else None
        out.append(p)
    return out


# ── plant context ────────────────────────────────────────────────────────────
def _context_sql(s: dict[str, bool]) -> str:
    geo = ', a.geometry' if s["Area.geometry"] else ', NULL::jsonb AS geometry'
    srt = ', a."sortOrder"' if s["Area.sortOrder"] else ', NULL::int AS "sortOrder"'
    null_json = "NULL::json"
    layout = (
        '(SELECT row_to_json(l) FROM (SELECT "storagePath", "imageUrl", "widthM", "heightM" '
        'FROM "PlantLayout" WHERE "plantId" = :plant AND "tenantId" = :tenant) l)'
        if s["PlantLayout"] else null_json
    )
    impairments = (
        """(SELECT coalesce(json_agg(i), '[]'::json) FROM (
             SELECT id, code, "plantId", "areaId", "systemType", "systemName", reason, status, "startedAt",
                    "plannedRestoreAt", "restoredAt", precautions, "approvedById", "createdById",
                    "escalationCount", "lastEscalatedAt"
             FROM "Impairment" WHERE "plantId" = :plant AND "tenantId" = :tenant
               AND (status IN ('requested','approved','active') OR "restoredAt" >= CAST(:wstart AS timestamptz))) i)"""
        if s["Impairment"] else "'[]'::json"
    )
    imp_rules = (
        """(SELECT coalesce(json_agg(r), '[]'::json) FROM (
             SELECT id, "plantId", "systemType", "permitType", effect, "requiredControls", message
             FROM "ImpairmentRule" WHERE "isActive" AND "tenantId" = :tenant
               AND ("plantId" IS NULL OR "plantId" = :plant)) r)"""
        if s["ImpairmentRule"] else "'[]'::json"
    )
    simops_rules = (
        """(SELECT coalesce(json_agg(r), '[]'::json) FROM (
             SELECT id, "plantId", "permitTypeA", "permitTypeB", "minDistanceM", "sameArea", severity
             FROM "SimopsRule" WHERE "isActive" AND "tenantId" = :tenant
               AND ("plantId" IS NULL OR "plantId" = :plant)) r)"""
        if s["SimopsRule"] else "'[]'::json"
    )
    open_ex = (
        """(SELECT coalesce(json_agg(e), '[]'::json) FROM (
             SELECT id, "dedupeKey", "openedAt", "ownerUserId", "acknowledgedAt"
             FROM "PtwException" WHERE "plantId" = :plant AND "tenantId" = :tenant AND "resolvedAt" IS NULL) e)"""
        if s["PtwException"] else "'[]'::json"
    )
    brief = (
        """(SELECT row_to_json(b) FROM (SELECT text, "generatedAt", shift FROM "PtwShiftBrief"
             WHERE "plantId" = :plant AND "tenantId" = :tenant ORDER BY "generatedAt" DESC LIMIT 1) b)"""
        if s["PtwShiftBrief"] else null_json
    )
    guidance = (
        """(SELECT coalesce(json_object_agg(g."areaId", g."updatedAt"), '{}'::json)
             FROM "ZoneGuidance" g WHERE g."plantId" = :plant AND g."tenantId" = :tenant)"""
        if s["ZoneGuidance"] else "'{}'::json"
    )
    return f"""
SELECT
  (SELECT row_to_json(pl) FROM (SELECT name, location FROM "Plant" WHERE id = :plant) pl) AS plant,
  (SELECT coalesce(json_agg(a), '[]'::json) FROM (
      SELECT a.id, a.name, a."ownerUserId" {geo} {srt} FROM "Area" a WHERE a."plantId" = :plant) a) AS areas,
  {layout} AS layout,
  {impairments} AS impairments,
  {imp_rules} AS imp_rules,
  {simops_rules} AS simops_rules,
  {open_ex} AS open_ex,
  {brief} AS brief,
  {guidance} AS guidance,
  (SELECT coalesce(json_agg(x), '[]'::json) FROM (
      SELECT ws.name, ws."slaHours" FROM "WorkflowStep" ws
      JOIN "WorkflowDefinition" wd ON wd.id = ws."definitionId"
      WHERE wd.module = 'PTW' AND wd."isActive" AND ws."slaHours" IS NOT NULL) x) AS slas,
  (SELECT count(*) FROM "Permit" p WHERE p."plantId" = :plant AND p."isDeleted" = false
      AND p."activatedAt" IS NOT NULL AND p."activatedAt" <= CAST(:baseline AS timestamptz)
      AND coalesce(p."workCompletedAt", p."closedAt", p."cancelledAt", p."autoExpiredAt", p."expiredAt",
                   'infinity'::timestamptz) > CAST(:baseline AS timestamptz)) AS baseline,
  (SELECT count(*) FROM "NearMiss" n WHERE n."plantId" = :plant AND n."createdAt" >= CAST(:d30 AS timestamptz)) AS nm30,
  (SELECT count(*) FROM "Observation" o WHERE o."plantId" = :plant AND o."createdAt" >= CAST(:d30 AS timestamptz)) AS obs30,
  (SELECT count(*) FROM "Incident" i WHERE i."plantId" = :plant AND i."isDeleted" = false
      AND coalesce(i."occurredAt", i.date) >= CAST(:d30 AS timestamptz)) AS inc30,
  (SELECT count(*) FROM "Incident" i WHERE i."plantId" = :plant AND i."isDeleted" = false
      AND i.type::text = 'LTI' AND coalesce(i."occurredAt", i.date) >= CAST(:d30 AS timestamptz)) AS lti30,
  (SELECT count(*) FROM "Capa" c WHERE c."plantId" = :plant AND c."isDeleted" = false
      AND c."closedAt" IS NULL) AS capa_open,
  (SELECT json_agg(n ORDER BY wk DESC) FROM (
      SELECT wk, count(p.id) AS n FROM generate_series(0, 11) wk
      LEFT JOIN "Permit" p ON p."plantId" = :plant AND p."isDeleted" = false AND p.status::text <> 'DRAFT'
           AND p."createdAt" >= CAST(:now AS timestamptz) - (wk + 1) * interval '7 day'
           AND p."createdAt" <  CAST(:now AS timestamptz) - wk * interval '7 day'
      GROUP BY wk) w) AS weekly,
  (SELECT json_build_object(
      'expired', count(*) FILTER (WHERE p.status::text = 'EXPIRED' OR p."autoExpiredAt" IS NOT NULL
                                   OR p."expiredAt" IS NOT NULL),
      'total', count(*))
   FROM "Permit" p WHERE p."plantId" = :plant AND p."isDeleted" = false AND p.status::text <> 'DRAFT'
      AND p."createdAt" >= CAST(:now AS timestamptz) - interval '84 day') AS expiry,
  (SELECT coalesce(json_agg(c), '[]'::json) FROM (
      SELECT cc.id, cc.name, cc.status, cc.score FROM "ContractorCompany" cc
      WHERE cc.id IN (SELECT DISTINCT p."contractorCompanyId" FROM "Permit" p
                      WHERE p."plantId" = :plant AND p."isDeleted" = false
                        AND p.status::text IN ('ISSUED','ACTIVE','SUSPENDED'))) c) AS companies
"""  # noqa: S608


async def load_names(db: AsyncSession, ids: set[str]) -> dict[str, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    rows = (await db.execute(text('SELECT id, name FROM "User" WHERE id = ANY(:ids)'), {"ids": list(ids)})).all()
    return {r[0]: r[1] for r in rows}


def _sla_map(rows: list[dict[str, Any]]) -> dict[str, int | None]:
    out: dict[str, int] = {}
    for r in rows or []:
        key = steps.approval_substep(r.get("name"))
        if "accept" in (r.get("name") or "").lower():
            key = steps.AWAITING_ACCEPT
        elif "clos" in (r.get("name") or "").lower():
            key = steps.CLOSURE_PENDING
        mins = int(r["slaHours"]) * 60
        out[key] = min(out.get(key, mins), mins)
    return out


async def load(
    db: AsyncSession,
    *,
    plant_id: str,
    now: datetime | None = None,
    area: str | None = None,
    shift: str | None = None,
    rng: str = "today",
) -> tuple[Snapshot, Extras]:
    now = now or datetime.now(timezone.utc)
    s = await schema(db)
    shifts = await load_shifts(db)
    wstart, wend = range_window(now, rng, shift, shifts)
    permits = await load_permits(db, s, plant_id, now, wstart, wend)

    ctx = (await db.execute(text(_context_sql(s)), {
        "plant": plant_id, "tenant": TENANT, "now": now, "wstart": wstart,
        "baseline": now - timedelta(hours=24), "d30": now - timedelta(days=30),
    })).mappings().one()

    zones = [
        ZoneView(id=a["id"], name=a["name"], polygon=list(a.get("geometry") or []),
                 sortOrder=a.get("sortOrder"), ownerUserId=a.get("ownerUserId"))
        for a in (ctx["areas"] or [])
    ]
    lay = ctx["layout"] or {}
    image_url = lay.get("imageUrl")
    if lay.get("storagePath"):
        try:
            from app.services.storage import create_signed_download_url, is_storage_configured
            if is_storage_configured():
                image_url = create_signed_download_url(lay["storagePath"]).get("signedUrl") or image_url
        except Exception:  # noqa: BLE001 — a broken bucket must not take down the board
            import logging
            logging.getLogger("safeops360.ptw_ops").warning("layout image URL could not be signed", exc_info=True)
    layout = LayoutView(widthM=lay.get("widthM"), heightM=lay.get("heightM"), imageUrl=image_url)

    impairments = [
        ImpairmentView(
            id=i["id"], code=i["code"], plantId=i["plantId"], areaId=i.get("areaId"), systemType=i["systemType"],
            systemName=i["systemName"], reason=i["reason"], status=i["status"], startedAt=_utc(i["startedAt"]),
            plannedRestoreAt=_utc(i.get("plannedRestoreAt")), restoredAt=_utc(i.get("restoredAt")),
            precautions=list(i.get("precautions") or []), approvedById=i.get("approvedById"),
            createdById=i.get("createdById"), escalationCount=int(i.get("escalationCount") or 0),
            lastEscalatedAt=_utc(i.get("lastEscalatedAt")),
        )
        for i in (ctx["impairments"] or [])
    ]
    imp_rules = [
        ImpairmentRuleView(id=r["id"], systemType=r["systemType"], permitType=r["permitType"], effect=r["effect"],
                           requiredControls=list(r.get("requiredControls") or []), message=r.get("message"),
                           plantId=r.get("plantId"))
        for r in (ctx["imp_rules"] or [])
    ] or defaults.impairment_rule_views()
    simops_rules = [
        SimopsRuleView(id=r["id"], permitTypeA=r["permitTypeA"], permitTypeB=r["permitTypeB"],
                       minDistanceM=r.get("minDistanceM"), sameArea=bool(r.get("sameArea")),
                       severity=r.get("severity") or "critical", plantId=r.get("plantId"))
        for r in (ctx["simops_rules"] or [])
    ] or defaults.simops_rule_views()

    owners = {z.id: z.ownerUserId for z in zones}
    name_ids = {p.receiverId for p in permits} | {p.issuerId for p in permits} | {p.pendingAssigneeId for p in permits}
    name_ids |= set(owners.values()) | {i.approvedById for i in impairments} | {i.createdById for i in impairments}
    name_ids |= {e.get("ownerUserId") for e in (ctx["open_ex"] or [])}
    names = await load_names(db, {n for n in name_ids if n})
    for p in permits:
        p.receiverName = names.get(p.receiverId or "")
    for z in zones:
        z.ownerName = names.get(z.ownerUserId or "")
    for i in impairments:
        i.approvedByName = names.get(i.approvedById or "")

    snap = Snapshot(
        now=now, plantId=plant_id, permits=permits, impairments=impairments, impairmentRules=imp_rules,
        simopsRules=simops_rules, zones=zones, layout=layout, stepSlaMin=_sla_map(ctx["slas"]),
        shifts=shifts, areaOwners=owners,
    )
    plant = ctx["plant"] or {}
    weekly = list(ctx["weekly"] or [])
    persisted = {}
    for e in ctx["open_ex"] or []:
        persisted[e["dedupeKey"]] = {**e, "openedAt": _utc(e.get("openedAt")),
                                     "acknowledgedAt": _utc(e.get("acknowledgedAt"))}
    brief = ctx["brief"]
    extras = Extras(
        windowStart=wstart, windowEnd=wend, rangeKey=rng, shiftCode=shift, areaFilter=area,
        plantName=plant.get("name"), plantLocation=plant.get("location"),
        areaNames={z.id: z.name for z in zones}, userNames=names, persisted=persisted,
        brief=({"text": brief["text"], "generatedAt": _utc(brief["generatedAt"]).isoformat(),
                "shift": brief.get("shift")} if brief else None),
        signals={
            "nearMisses30d": int(ctx["nm30"] or 0), "observations30d": int(ctx["obs30"] or 0),
            "incidents30d": int(ctx["inc30"] or 0), "lti30d": int(ctx["lti30"] or 0),
            "capaOpen": int(ctx["capa_open"] or 0), "weekly": [int(n or 0) for n in weekly],
            "expiryRate": dict(ctx["expiry"] or {"expired": 0, "total": 0}),
        },
        contractorCompanies={c["id"]: c for c in (ctx["companies"] or [])},
        activeBaseline=int(ctx["baseline"] or 0),
        cardsExpiring7d=sum(p.crewCertExpiring7d for p in permits if p.status in ("ISSUED", "ACTIVE", "SUSPENDED")),
        guidanceUpdated={k: str(v) for k, v in (ctx["guidance"] or {}).items()},
        tablesReady=bool(s.get("ready")),
    )
    return snap, extras
