"""External-system integrations (JSL Hisar: "integrate with JSL SAS portal").
Mounts at /api/integrations.

STATUS: awaiting the JSL SAS API specification. The connector is configuration:
an admin sets URL, auth (by environment-variable NAME), and the field mapping
on /configuration/integrations. Until a base URL is set, or while dry-run is on,
outbound rows are SKIPPED with the would-be payload so the mapping can be shown.

Admin (user-authenticated):
  GET    /meta                         vocabularies, SafeOps field list, template
  GET    /endpoints                    list with log counts
  POST   /endpoints                    create (INTEGRATION.MANAGE)
  GET    /endpoints/{id}
  PUT    /endpoints/{id}               update (INTEGRATION.MANAGE)
  DELETE /endpoints/{id}               soft delete (INTEGRATION.MANAGE)
  POST   /endpoints/{id}/test          reachability + credentials check
  POST   /endpoints/{id}/preview       payload for one observation (nothing sent)
  POST   /endpoints/{id}/push          enqueue one observation now
  POST   /endpoints/{id}/sync-now      change capture + delivery for this endpoint
  GET    /logs                         sync log with filters
  GET    /logs/{id}                    one row with its payload
  POST   /logs/{id}/retry              re-queue a FAILED / SKIPPED row
  POST   /logs/retry-failed            re-queue every FAILED row (optionally per endpoint)

Inbound (machine-authenticated, no user session):
  POST   /{code}/observations          API key from the endpoint's env var, compared
                                       in constant time; idempotent on externalId.
  External systems call the BACKEND URL directly; the Next.js /api proxy only
  forwards a user's session token, not an API key.

RBAC (safeops_360/prisma/apply-integrations-permissions.ts): INTEGRATION.READ,
INTEGRATION.MANAGE, admins only. Not plant-scoped: an endpoint is plant-wide
configuration. Mounted ungated like ims; per-endpoint RBAC is the gate.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.integration import IntegrationEndpoint, IntegrationSyncLog
from app.models.user import User
from app.services import integrations as svc
from app.services.permissions import PermissionContext, can

router = APIRouter(prefix="/api/integrations", tags=["integrations"])

READ, MANAGE = "INTEGRATION.READ", "INTEGRATION.MANAGE"
AWAITING_SPEC = "Awaiting JSL SAS API specification"


async def _require(db: AsyncSession, user: User, perm: str) -> None:
    res = await can(db, user.id, perm, PermissionContext())
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or "Access denied")


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


async def _get_endpoint(db: AsyncSession, eid: str) -> IntegrationEndpoint:
    ep = await db.get(IntegrationEndpoint, eid)
    if ep is None or ep.isDeleted:
        raise HTTPException(404, "Integration endpoint not found")
    return ep


def _endpoint_out(ep: IntegrationEndpoint, counts: dict[str, int] | None = None) -> dict[str, Any]:
    return {
        "id": ep.id, "code": ep.code, "name": ep.name, "description": ep.description, "entityType": ep.entityType,
        "direction": ep.direction, "baseUrl": ep.baseUrl, "path": ep.path, "method": ep.method,
        "authType": ep.authType, "authHeaderName": ep.authHeaderName, "secretEnvVar": ep.secretEnvVar,
        "inboundSecretEnvVar": ep.inboundSecretEnvVar, "headers": ep.headers or {},
        "fieldMapping": ep.fieldMapping or [], "triggerEvents": ep.triggerEvents or list(svc.TRIGGER_EVENTS),
        "plantIds": ep.plantIds or [], "responseIdPath": ep.responseIdPath, "timeoutSeconds": ep.timeoutSeconds,
        "serviceUserId": ep.serviceUserId, "defaultPlantId": ep.defaultPlantId,
        "enabled": ep.enabled, "dryRun": ep.dryRun,
        # Whether the named env vars resolve on THIS server; the values never leave it.
        "secretConfigured": bool(svc.resolve_secret(ep.secretEnvVar)),
        "inboundKeyConfigured": bool(svc.resolve_secret(ep.inboundSecretEnvVar)),
        "targetUrl": svc.target_url(ep.baseUrl, ep.path),
        "inboundPath": f"/api/integrations/{ep.code}/observations" if svc.is_inbound(ep) else None,
        "awaitingSpec": not (ep.baseUrl or "").strip(),
        "statusNote": AWAITING_SPEC if not (ep.baseUrl or "").strip() else None,
        "cursorAt": _iso(ep.cursorAt), "lastTestAt": _iso(ep.lastTestAt), "lastTestOk": ep.lastTestOk,
        "lastTestMessage": ep.lastTestMessage, "createdAt": _iso(ep.createdAt), "updatedAt": _iso(ep.updatedAt),
        "logCounts": counts or {},
    }


def _log_out(r: IntegrationSyncLog, codes: dict[str, str], with_payload: bool = False) -> dict[str, Any]:
    out = {
        "id": r.id, "endpointId": r.endpointId, "endpointCode": codes.get(r.endpointId), "entityType": r.entityType,
        "entityId": r.entityId, "entityRef": r.entityRef, "eventType": r.eventType, "entityStatus": r.entityStatus,
        "direction": r.direction, "status": r.status, "attempts": r.attempts, "nextAttemptAt": _iso(r.nextAttemptAt),
        "lastAttemptAt": _iso(r.lastAttemptAt), "httpStatus": r.httpStatus, "error": r.error,
        "externalId": r.externalId, "sentAt": _iso(r.sentAt), "createdAt": _iso(r.createdAt),
        "retrying": r.status == "FAILED" and r.nextAttemptAt is not None,
    }
    if with_payload:
        out["payload"] = r.payload
        out["responseExcerpt"] = r.responseExcerpt
    return out


async def _counts(db: AsyncSession, ids: list[str]) -> dict[str, dict[str, int]]:
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(IntegrationSyncLog.endpointId, IntegrationSyncLog.status, func.count())
            .where(IntegrationSyncLog.endpointId.in_(ids))
            .group_by(IntegrationSyncLog.endpointId, IntegrationSyncLog.status)
        )
    ).all()
    out: dict[str, dict[str, int]] = {}
    for eid, st, n in rows:
        out.setdefault(eid, {})[st] = int(n)
    return out


# ── meta ─────────────────────────────────────────────────────────────────────
@router.get("/meta")
async def meta(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, READ)
    return {
        "directions": svc.DIRECTIONS, "methods": svc.METHODS, "authTypes": svc.AUTH_TYPES,
        "logStatuses": svc.LOG_STATUSES, "logDirections": svc.LOG_DIRECTIONS, "triggerEvents": svc.TRIGGER_EVENTS,
        "transforms": [{"code": t, "help": svc.TRANSFORM_HELP[t]} for t in svc.TRANSFORMS],
        "mappingDirections": svc.MAPPING_DIRECTIONS,
        "safeopsFields": [{"code": k, "label": v["label"], "inbound": v["in"]} for k, v in svc.SAFEOPS_FIELDS.items()],
        "templates": {
            "JSL_SAS": {
                "code": "JSL_SAS", "name": "JSL SAS portal", "direction": "BOTH", "method": "POST",
                "authType": "API_KEY", "authHeaderName": "X-API-Key",
                "secretEnvVar": "INTEGRATION_JSL_SAS_KEY", "inboundSecretEnvVar": "INTEGRATION_JSL_SAS_INBOUND_KEY",
                "fieldMapping": svc.JSL_SAS_TEMPLATE_MAPPING, "triggerEvents": list(svc.TRIGGER_EVENTS),
                "description": f"{AWAITING_SPEC}. Field names on the SAS side are placeholders until JSL publishes the API.",
            }
        },
        "maxAttempts": svc.MAX_ATTEMPTS,
        "backoffMinutes": [svc.backoff_seconds(i) // 60 for i in range(1, svc.MAX_ATTEMPTS)],
        "awaitingSpecNote": AWAITING_SPEC,
    }


# ── endpoints CRUD ───────────────────────────────────────────────────────────
class EndpointIn(BaseModel):
    code: str = Field(min_length=2, max_length=40)
    name: str = Field(min_length=2, max_length=120)
    description: str | None = Field(None, max_length=2000)
    direction: str = "OUTBOUND"
    baseUrl: str | None = Field(None, max_length=500)
    path: str | None = Field(None, max_length=300)
    method: str = "POST"
    authType: str = "NONE"
    authHeaderName: str | None = None
    secretEnvVar: str | None = None
    inboundSecretEnvVar: str | None = None
    headers: dict[str, str] = {}
    fieldMapping: list[dict[str, Any]] = []
    triggerEvents: list[str] = list(svc.TRIGGER_EVENTS)
    plantIds: list[str] = []
    responseIdPath: str | None = Field(None, max_length=120)
    timeoutSeconds: int = 15
    serviceUserId: str | None = None
    defaultPlantId: str | None = None
    enabled: bool = False
    dryRun: bool = True


def _clean(body: EndpointIn) -> dict[str, Any]:
    d = body.model_dump()
    d["code"] = d["code"].strip().upper()
    for k in ("baseUrl", "path", "authHeaderName", "secretEnvVar", "inboundSecretEnvVar", "responseIdPath",
              "serviceUserId", "defaultPlantId", "description"):
        if isinstance(d.get(k), str):
            d[k] = d[k].strip() or None
    d["fieldMapping"] = [
        {k: v for k, v in {
            "source": str(r.get("source") or "").strip(), "target": str(r.get("target") or "").strip(),
            "transform": str(r.get("transform") or "none").strip(), "arg": (str(r.get("arg")).strip() if r.get("arg") not in (None, "") else None),
            "direction": str(r.get("direction") or "BOTH").strip().upper(),
        }.items() if v is not None}
        for r in d["fieldMapping"]
    ]
    return d


async def _validate(db: AsyncSession, d: dict[str, Any]) -> None:
    errs = svc.validate_config(d)
    if d.get("direction") in ("INBOUND", "BOTH"):
        if not d.get("serviceUserId"):
            errs.append("Inbound endpoints need a service user: observations received from the external system are created under that account (it must hold OBSERVATION.CREATE).")
        elif await db.get(User, d["serviceUserId"]) is None:
            errs.append("The service user does not exist.")
    if errs:
        raise HTTPException(422, " ".join(errs))


@router.get("/endpoints")
async def list_endpoints(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, READ)
    rows = (
        await db.execute(select(IntegrationEndpoint).where(IntegrationEndpoint.isDeleted.is_(False)).order_by(IntegrationEndpoint.code))
    ).scalars().all()
    counts = await _counts(db, [r.id for r in rows])
    return {"items": [_endpoint_out(r, counts.get(r.id)) for r in rows], "total": len(rows)}


@router.post("/endpoints", status_code=201)
async def create_endpoint(body: EndpointIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, MANAGE)
    d = _clean(body)
    await _validate(db, d)
    clash = await db.scalar(select(IntegrationEndpoint.id).where(IntegrationEndpoint.code == d["code"]))
    if clash:
        raise HTTPException(409, f"An endpoint with code {d['code']} already exists")
    ep = IntegrationEndpoint(**d, entityType="OBSERVATION", createdBy=user.id, updatedBy=user.id)
    if ep.enabled:
        ep.cursorAt = svc.utcnow()
    db.add(ep)
    await db.flush()
    await db.refresh(ep)
    return _endpoint_out(ep)


@router.get("/endpoints/{eid}")
async def get_endpoint(eid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, READ)
    ep = await _get_endpoint(db, eid)
    return _endpoint_out(ep, (await _counts(db, [ep.id])).get(ep.id))


@router.put("/endpoints/{eid}")
async def update_endpoint(eid: str, body: EndpointIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, MANAGE)
    ep = await _get_endpoint(db, eid)
    d = _clean(body)
    if d["code"] != ep.code:
        raise HTTPException(422, "The code identifies the endpoint (and its inbound URL) and cannot be changed")
    await _validate(db, d)
    was_enabled = ep.enabled
    for k, v in d.items():
        setattr(ep, k, v)
    if ep.enabled and not was_enabled and ep.cursorAt is None:
        ep.cursorAt = svc.utcnow()
    ep.updatedBy = user.id
    await db.flush()
    await db.refresh(ep)
    return _endpoint_out(ep, (await _counts(db, [ep.id])).get(ep.id))


@router.delete("/endpoints/{eid}")
async def delete_endpoint(eid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, MANAGE)
    ep = await _get_endpoint(db, eid)
    ep.isDeleted = True
    ep.enabled = False
    # Free the code for re-use; the sync log keeps the id.
    ep.code = f"{ep.code}__DELETED_{ep.id[:8]}"
    ep.updatedBy = user.id
    await db.flush()
    return {"ok": True}


# ── test / preview / push / sync ─────────────────────────────────────────────
@router.post("/endpoints/{eid}/test")
async def test_endpoint(eid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, MANAGE)
    ep = await _get_endpoint(db, eid)
    ok, message, code = await svc.test_connection(ep)
    ep.lastTestAt = svc.utcnow()
    ep.lastTestOk = ok
    ep.lastTestMessage = message
    await db.flush()
    return {"ok": ok, "message": message, "httpStatus": code, "testedAt": _iso(ep.lastTestAt)}


class RecordIn(BaseModel):
    observationId: str | None = None
    eventType: str = svc.EVENT_CREATED


async def _pick_observation(db: AsyncSession, observation_id: str | None):
    from app.models.observation import Observation

    if observation_id:
        obs = await db.get(Observation, observation_id)
        if obs is None:
            obs = (await db.execute(select(Observation).where(Observation.number == observation_id).limit(1))).scalar_one_or_none()
    else:
        obs = (await db.execute(select(Observation).order_by(Observation.createdAt.desc()).limit(1))).scalar_one_or_none()
    if obs is None:
        raise HTTPException(404, "Observation not found")
    return obs


@router.post("/endpoints/{eid}/preview")
async def preview(eid: str, body: RecordIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, READ)
    ep = await _get_endpoint(db, eid)
    obs = await _pick_observation(db, body.observationId)
    if body.eventType not in svc.TRIGGER_EVENTS:
        raise HTTPException(422, f"eventType must be one of {', '.join(svc.TRIGGER_EVENTS)}")
    payload = await svc.preview_payload(db, ep, obs, body.eventType)
    return {
        "observationId": obs.id, "observationNumber": obs.number, "method": ep.method,
        "targetUrl": svc.target_url(ep.baseUrl, ep.path), "payload": payload,
        "wouldSkip": svc.skip_reason(ep), "headers": sorted(svc.outbound_headers(ep).keys()),
    }


@router.post("/endpoints/{eid}/push")
async def push_one(eid: str, body: RecordIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, MANAGE)
    ep = await _get_endpoint(db, eid)
    if not svc.is_outbound(ep):
        raise HTTPException(409, "This endpoint is inbound only")
    obs = await _pick_observation(db, body.observationId)
    row = await svc.enqueue_manual(db, ep, obs)
    return _log_out(row, {ep.id: ep.code}, with_payload=True)


@router.post("/endpoints/{eid}/sync-now")
async def sync_now(eid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, MANAGE)
    ep = await _get_endpoint(db, eid)
    captured = await svc.capture_changes(db, ep)
    delivered = await svc.deliver_due(db, endpoint_id=ep.id)
    return {"captured": captured, "delivered": delivered}


# ── sync log ─────────────────────────────────────────────────────────────────
@router.get("/logs")
async def list_logs(
    endpointId: str | None = Query(None), status_: str | None = Query(None, alias="status"),
    direction: str | None = Query(None), q: str | None = Query(None), limit: int = Query(200, ge=1, le=1000),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, READ)
    stmt = select(IntegrationSyncLog)
    if endpointId:
        stmt = stmt.where(IntegrationSyncLog.endpointId == endpointId)
    if status_:
        if status_ not in svc.LOG_STATUSES:
            raise HTTPException(422, f"status must be one of {', '.join(svc.LOG_STATUSES)}")
        stmt = stmt.where(IntegrationSyncLog.status == status_)
    if direction:
        stmt = stmt.where(IntegrationSyncLog.direction == direction)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(IntegrationSyncLog.entityRef.ilike(like), IntegrationSyncLog.externalId.ilike(like),
                              IntegrationSyncLog.entityId == q.strip()))
    rows = (await db.execute(stmt.order_by(IntegrationSyncLog.createdAt.desc()).limit(limit))).scalars().all()
    codes = dict((await db.execute(select(IntegrationEndpoint.id, IntegrationEndpoint.code))).all())
    return {"items": [_log_out(r, codes) for r in rows], "total": len(rows)}


@router.get("/logs/{lid}")
async def get_log(lid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, READ)
    r = await db.get(IntegrationSyncLog, lid)
    if r is None:
        raise HTTPException(404, "Log entry not found")
    codes = dict((await db.execute(select(IntegrationEndpoint.id, IntegrationEndpoint.code))).all())
    return _log_out(r, codes, with_payload=True)


def _requeue(r: IntegrationSyncLog) -> None:
    r.status = "PENDING"
    r.attempts = 0
    r.nextAttemptAt = None
    r.error = None


@router.post("/logs/{lid}/retry")
async def retry_log(lid: str, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, MANAGE)
    r = await db.get(IntegrationSyncLog, lid)
    if r is None:
        raise HTTPException(404, "Log entry not found")
    if r.direction != "OUTBOUND" or r.status not in ("FAILED", "SKIPPED"):
        raise HTTPException(409, "Only a FAILED or SKIPPED outbound row can be retried")
    _requeue(r)
    await db.flush()
    codes = dict((await db.execute(select(IntegrationEndpoint.id, IntegrationEndpoint.code))).all())
    return _log_out(r, codes, with_payload=True)


class RetryAllIn(BaseModel):
    endpointId: str | None = None


@router.post("/logs/retry-failed")
async def retry_failed(body: RetryAllIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await _require(db, user, MANAGE)
    stmt = select(IntegrationSyncLog).where(IntegrationSyncLog.direction == "OUTBOUND").where(IntegrationSyncLog.status == "FAILED")
    if body.endpointId:
        stmt = stmt.where(IntegrationSyncLog.endpointId == body.endpointId)
    rows = (await db.execute(stmt.limit(1000))).scalars().all()
    for r in rows:
        _requeue(r)
    await db.flush()
    return {"requeued": len(rows)}


# ── inbound ──────────────────────────────────────────────────────────────────
def _presented_key(request: Request) -> str | None:
    key = request.headers.get("x-api-key")
    if key:
        return key.strip()
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


@router.post("/{code}/observations")
async def inbound_observation(code: str, request: Request, db: AsyncSession = Depends(get_db)) -> Any:
    """Create an observation from an external system. Idempotent: the same
    externalId returns the observation created the first time (200)."""
    from app.core.audit_context import set_system_actor
    from app.models.observation import Observation
    from app.models.observation_severity import OVERRIDE_SOURCE_CAPTURE_CONVERSION
    from app.routers.observations import create_observation
    from app.schemas.observation import ObservationCreate

    ep = (
        await db.execute(
            select(IntegrationEndpoint).where(IntegrationEndpoint.code == code.strip().upper())
            .where(IntegrationEndpoint.isDeleted.is_(False))
        )
    ).scalar_one_or_none()
    # One answer for "no such endpoint", "not inbound" and "wrong key", so the
    # URL space cannot be probed for configured integrations.
    if ep is None or not svc.is_inbound(ep) or not svc.api_key_matches(_presented_key(request), svc.resolve_secret(ep.inboundSecretEnvVar)):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")
    if not ep.enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "This integration is disabled")
    set_system_actor(f"integration:{ep.code}")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "Body must be JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "Body must be a JSON object")

    fields = svc.map_inbound(body, ep.fieldMapping)
    ext = str(fields.get("externalId") or "").strip()

    def fail(message: str, code_: int = 422) -> JSONResponse:
        db.add(IntegrationSyncLog(
            endpointId=ep.id, entityType="OBSERVATION", direction="INBOUND", status="FAILED", attempts=1,
            eventType=svc.EVENT_INBOUND, externalId=ext or None, payload=body, error=message[:2000],
            lastAttemptAt=svc.utcnow(),
        ))
        return JSONResponse(status_code=code_, content={"detail": message, "externalId": ext or None})

    if not ext:
        return fail("externalId is required (map the external record id onto the SafeOps field 'externalId').")
    existing = await svc.find_received(db, ep.id, ext)
    if existing is not None:
        return JSONResponse(status_code=200, content={
            "duplicate": True, "observationId": existing.entityId, "number": existing.entityRef, "externalId": ext,
        })
    service_user = await db.get(User, ep.serviceUserId) if ep.serviceUserId else None
    if service_user is None:
        return fail("The endpoint has no valid service user configured.", 503)

    try:
        kwargs = await svc.build_inbound_create(db, ep, fields)
        payload = ObservationCreate(
            **kwargs,
            severityOverrideReason=f"Severity as recorded in {ep.name} (external ref {ext}).",
            severityOverrideSource=OVERRIDE_SOURCE_CAPTURE_CONVERSION,
        )
    except svc.InboundError as e:
        return fail(str(e))
    except ValidationError as e:
        first = e.errors()[0] if e.errors() else {}
        return fail(f"{'.'.join(str(x) for x in first.get('loc', []))}: {first.get('msg', 'invalid value')}")

    try:
        async with db.begin_nested():
            row = IntegrationSyncLog(
                endpointId=ep.id, entityType="OBSERVATION", direction="INBOUND", status="RECEIVED", attempts=1,
                eventType=svc.EVENT_INBOUND, externalId=ext, payload=body, dedupeKey=svc.inbound_dedupe_key(ext),
                lastAttemptAt=svc.utcnow(),
            )
            db.add(row)
            await db.flush()
            created = await create_observation(payload, service_user, db)
            row.entityId, row.entityRef, row.entityStatus = created.id, created.number, str(getattr(created.status, "value", created.status))
            email = str(fields.get("observerEmail") or "").strip().lower()
            if email:
                reporter = (await db.execute(select(User).where(func.lower(User.email) == email).limit(1))).scalar_one_or_none()
                if reporter is not None and reporter.id != service_user.id:
                    obs = await db.get(Observation, created.id)
                    if obs is not None:
                        obs.observerId = reporter.id
            await db.flush()
    except IntegrityError:
        # A concurrent delivery of the same externalId won the race.
        existing = await svc.find_received(db, ep.id, ext)
        return JSONResponse(status_code=200, content={
            "duplicate": True, "observationId": existing.entityId if existing else None,
            "number": existing.entityRef if existing else None, "externalId": ext,
        })
    except HTTPException as e:
        return fail(str(e.detail), 422 if e.status_code < 500 else e.status_code)
    return JSONResponse(status_code=201, content={
        "duplicate": False, "observationId": created.id, "number": created.number, "externalId": ext,
    })
