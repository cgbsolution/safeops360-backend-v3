"""PTW control center, insights and paper-permit scan (JSL Hisar PTW requirement:
"scan to read the doc & give us the realtime dashboard, analytics feature").

  GET  /api/ptw/control-center?plantId=        live permits by area + exceptions (PTW.READ)
  GET  /api/ptw/insights?plantId=&from=&to=    analytics over permits raised in a window (PTW.READ)
  POST /api/ptw/scan                           photos / PDF of a paper permit → wizard draft (PTW.CREATE)

⚠ MOUNT ORDER: app/routers/ptw.py owns `GET /api/ptw/{permit_id}`, which would
swallow `/api/ptw/control-center` and `/api/ptw/insights` as permit ids. This
router must be mounted BEFORE "ptw" in app/main.py `_ROUTERS` (same constraint
as observation_sla before observations).

Plant scope: reads use build_query_scope(PTW.READ) like routers/ims.py; a
requested plant outside the caller's scope is refused, not silently emptied.
No new tables: everything is derived on read (app/services/ptw_cc.py), and the
scan returns a draft only — nothing is written until the user submits the
ordinary New Permit form.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.user import User
from app.services import ptw_cc as svc
from app.services import ptw_scan as scan_svc
from app.services.access_scope import build_query_scope
from app.services.ai.anthropic_client import is_configured
from app.services.permissions import PermissionContext, can
from app.services.plant_directory import resolve_plant_names
from app.services.user_directory import resolve_user_directory

router = APIRouter(prefix="/api/ptw", tags=["ptw-control-center"])

READ, CREATE = "PTW.READ", "PTW.CREATE"


async def _require(db: AsyncSession, user: User, perm: str, plant_id: str | None = None) -> None:
    res = await can(db, user.id, perm, PermissionContext(plant_id=plant_id))
    if not res.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, res.reason or "Access denied")


async def _scoped_plants(db: AsyncSession, user: User, plant_id: str | None) -> list[str] | None:
    """None = every plant (ALL_PLANTS scope and no plant asked for)."""
    await _require(db, user, READ)
    scope = await build_query_scope(db, user.id, READ)
    if plant_id:
        if not scope.allows_plant(plant_id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Plant is outside your access scope")
        return [plant_id]
    return None if scope.all_plants else scope.plant_ids


async def _area_names(db: AsyncSession, ids) -> dict[str, tuple[str, str | None]]:
    from app.models.plant import Area  # noqa: PLC0415

    wanted = sorted({i for i in ids if i})
    if not wanted:
        return {}
    rows = (await db.execute(select(Area.id, Area.name, Area.plantId).where(Area.id.in_(wanted)))).all()
    return {r.id: (r.name, r.plantId) for r in rows}


async def _user_names(db: AsyncSession, ids) -> dict[str, str]:
    return {k: v.name for k, v in (await resolve_user_directory(db, ids)).items()}


# ── control center ───────────────────────────────────────────────────────────
@router.get("/control-center")
async def control_center(
    plantId: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    plants = await _scoped_plants(db, user, plantId)
    now = svc.utcnow()
    snaps, truncated = await svc.load_live_snaps(db, plants, now)
    closed = await svc.count_closed_since(db, plants, now - timedelta(hours=24))
    areas = await _area_names(db, [s.areaId for s in snaps])
    users = await _user_names(db, [i for s in snaps for i in (s.issuerId, s.receiverId)])
    plant_names = await resolve_plant_names(db, [s.plantId for s in snaps] + ([plantId] if plantId else []))
    payload = svc.build_payload(
        snaps, now=now, area_names=areas, user_names=users, plant_names=plant_names,
        closed_last_24h=closed, truncated=truncated,
    )
    return {**payload, "plantId": plantId, "plantName": plant_names.get(plantId or "")}


# ── insights ─────────────────────────────────────────────────────────────────
@router.get("/insights")
async def insights(
    plantId: str | None = Query(None),
    from_: date | None = Query(None, alias="from"),
    to: date | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    plants = await _scoped_plants(db, user, plantId)
    try:
        start, end, frm, to_d = svc.window_bounds(from_, to)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    now = svc.utcnow()
    data = await svc.load_insight_inputs(db, plants, start, end)
    shifts = await svc.load_shifts(db)
    out = svc.compute_insights(
        data["rows"], now=now, start=start, end=end, extensions=data["extensions"],
        suspensions=data["suspensions"], gas_readings=data["readings"], shifts=shifts,
    )

    # Names for the id-keyed breakdowns.
    areas = await _area_names(db, [r["key"] for r in out["byArea"] if r["key"] != "none"])
    for r in out["byArea"]:
        r["label"] = areas.get(r["key"], ("No area recorded" if r["key"] == "none" else "Unknown area", None))[0]
    people = await _user_names(db, [r["key"] for r in out["topIssuers"] + out["topReceivers"]])
    for r in out["topIssuers"] + out["topReceivers"]:
        r["label"] = people.get(r["key"], "Unknown user")
    cids = [r["key"][3:] for r in out["byContractor"] if r["key"].startswith("id:")]
    cnames: dict[str, str] = {}
    if cids:
        from app.models.epc import ContractorCompany  # noqa: PLC0415

        cnames = {i: n for i, n in (await db.execute(
            select(ContractorCompany.id, ContractorCompany.name).where(ContractorCompany.id.in_(cids))
        )).all()}
    for r in out["byContractor"]:
        k = r["key"]
        r["label"] = (cnames.get(k[3:], "Unknown contractor") if k.startswith("id:")
                      else f"{k[5:]} (not linked)" if k.startswith("text:") else "No contractor (own staff)")
    for r in out["byType"]:
        r["label"] = svc.label_type(r["key"])
    for r in out["byStatus"]:
        r["label"] = r["key"].replace("_", " ").title()

    plant_names = await resolve_plant_names(db, [plantId] if plantId else [])
    return {
        **out,
        "plantId": plantId, "plantName": plant_names.get(plantId or ""),
        "window": {"from": frm.isoformat(), "to": to_d.isoformat(), "start": start.isoformat(), "end": end.isoformat()},
        "truncated": data["truncated"],
        "generatedAt": now.isoformat(),
        "definitions": {
            "cohort": "Permits raised (created) inside the window. The wizard submits on create, so created = submitted.",
            "approvedAt": "Last of the issuer / safety / plant-head approval stamps; issue time when none was written.",
            "onTimeClosure": f"Closed after work completed, within {svc.ON_TIME_CLOSURE_GRACE_MIN} min of the final (post-extension) validity end.",
            "gasCompliance": "Initial test: a pre-entry reading or one within 15 min of activation. Refresh on time: taken no later than the previous reading's due time; a refresh that fell due before work ended with no later reading counts as missed.",
            "hourOfDay": "Planned start (validFrom) in plant local time.",
        },
    }


# ── scan a paper permit ──────────────────────────────────────────────────────
class ScanFileIn(BaseModel):
    name: str = Field("scan", max_length=200)
    mimeType: str | None = Field(None, max_length=100)
    # ~16 MB of base64 per file; the decoded size is re-checked per type.
    dataB64: str = Field(min_length=8, max_length=22_000_000)


class ScanIn(BaseModel):
    plantId: str = Field(min_length=1, max_length=64)
    files: list[ScanFileIn] = Field(min_length=1, max_length=scan_svc.MAX_IMAGES)


@router.post("/scan")
async def scan_paper_permit(
    body: ScanIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await _require(db, user, CREATE, body.plantId)
    if not is_configured():
        return {
            "ok": False, "reason": "ai_not_configured",
            "message": "Scanning a paper permit needs the AI reading service, which is not set up on this server. "
                       "Fill the permit form manually.",
        }
    try:
        files = scan_svc.decode_files([f.model_dump() for f in body.files])
        masters = await scan_svc.load_masters(db, body.plantId, user.id)
        if masters.plant_name is None:
            raise HTTPException(422, "Unknown plant")
        today = svc.utcnow().astimezone(svc.LOCAL_TZ).date().isoformat()
        raw, method = await scan_svc.extract(files, today=today, plant_name=masters.plant_name)
    except scan_svc.ScanError as e:
        if e.message == "AI_NOT_CONFIGURED":
            return {"ok": False, "reason": "ai_not_configured",
                    "message": "Scanning a paper permit needs the AI reading service, which is not set up on this server."}
        if e.status == 422:
            raise HTTPException(422, e.message) from e
        return {"ok": False, "reason": "read_failed", "message": e.message}
    ex = scan_svc.normalise_extraction(raw)
    if not ex.is_permit:
        return {"ok": False, "reason": "not_a_permit",
                "message": "This does not look like a permit-to-work form. Check the photo and try again."}
    result = scan_svc.build_draft(ex, masters)
    return {
        "ok": True,
        **result,
        "source": {"kind": "pdf" if method != "vision" else "images", "files": len(files),
                   "textOnly": method == "pdf_text"},
    }
