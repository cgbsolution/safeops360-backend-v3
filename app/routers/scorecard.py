"""EHS Scorecard API.

`GET /api/scorecard` returns the payload the dashboard renders.
`GET /api/scorecard/export.pdf|.pptx` return that SAME payload rendered to a
document — built by calling `build_payload()` with the identical arguments, so an
export cannot drift from the screen it was taken from.

Plant scope is fail-closed, as on every other analytics surface: a scorecard is
exactly the artefact someone forwards outside their own site.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.scorecard import DEFAULT_TENANT
from app.models.user import User
from app.services.scorecard.payload import build_payload

router = APIRouter(prefix="/api/scorecard", tags=["scorecard"])
log = logging.getLogger("safeops360.scorecard")

_PERMISSION = "INCIDENT.READ"  # the scorecard's narrowest constituent module


async def _scope(db: AsyncSession, user: User) -> list[str] | None:
    """The caller's accessible plants, or None for an unrestricted role."""
    from app.services.permissions import get_accessible_plants_for

    try:
        allowed = await get_accessible_plants_for(db, user.id, _PERMISSION)
    except Exception as e:  # noqa: BLE001
        # Fail CLOSED. Falling through to an unscoped aggregate is how a
        # cross-site leak ships, and a scorecard is a document people forward.
        log.warning("scorecard scope resolution failed for %s: %s", user.id, e)
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Could not resolve your site access."
        ) from e
    if allowed == []:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You do not hold the permissions the EHS Scorecard is built from.",
        )
    return allowed


async def _export_labels(db: AsyncSession, payload: dict) -> dict[str, str]:
    """Display-label overrides for the plants the document covers.

    One site → that site's profile. Several → only if they all share one
    profile (labels_for_plants keeps defaults for a mixed scope).
    """
    from app.services.display_labels import labels_for_plant, labels_for_plants

    if payload.get("site"):
        return await labels_for_plant(db, payload["site"])
    return await labels_for_plants(db, [s.get("siteId") for s in payload.get("bySite") or []])


async def _payload(
    db: AsyncSession, user: User, site: str | None, grain: str, periods: int
) -> dict:
    allowed = await _scope(db, user)
    if site and allowed is not None and site not in allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You do not have access to that site.")
    if grain not in ("month", "quarter"):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "grain must be 'month' or 'quarter'"
        )
    return await build_payload(
        db, tenant=DEFAULT_TENANT, site_id=site, grain=grain,
        periods=periods, plants_allowed=allowed,
    )


@router.get("")
async def scorecard(
    site: str | None = Query(default=None, description="Restrict to one site id"),
    grain: str = Query(default="month", description="month | quarter"),
    periods: int = Query(default=12, ge=1, le=36),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await _payload(db, user, site, grain, periods)


def _filename(payload: dict, ext: str) -> str:
    site = (payload.get("siteName") or "All sites").split(" — ")[0].replace(" ", "-")
    periods = payload.get("periods") or []
    return f"EHS-Scorecard_{site}_{periods[-1] if periods else 'no-data'}.{ext}"


@router.get("/export.pdf")
async def export_pdf(
    site: str | None = Query(default=None),
    grain: str = Query(default="month"),
    periods: int = Query(default=12, ge=1, le=36),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """The scorecard as a PDF — same payload, same filters, as the screen."""
    from app.services.scorecard.export_pdf import render_scorecard_pdf

    payload = await _payload(db, user, site, grain, periods)
    name = _filename(payload, "pdf")
    return Response(
        content=render_scorecard_pdf(payload, await _export_labels(db, payload)),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
    )


@router.get("/export.pptx")
async def export_pptx(
    site: str | None = Query(default=None),
    grain: str = Query(default="month"),
    periods: int = Query(default=12, ge=1, le=36),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """The scorecard as a PowerPoint deck — same payload, same filters."""
    from app.services.scorecard.export_pptx import render_scorecard_pptx

    payload = await _payload(db, user, site, grain, periods)
    name = _filename(payload, "pptx")
    return Response(
        content=render_scorecard_pptx(payload, await _export_labels(db, payload)),
        media_type=(
            "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        ),
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
    )


@router.post("/rollup")
async def run_rollup_now(
    months: int = Query(default=18, ge=1, le=60),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Recompute the stored rollups. Admin-triggered; the nightly job does the same.

    Not scoped to the caller's plants: a partial rollup would leave the stored
    history internally inconsistent, and this writes a derived table rather than
    returning anyone's data.
    """
    from app.services.permissions import get_user_role_codes
    from app.services.scorecard.rollup import run_rollup

    roles = set(await get_user_role_codes(db, user.id))
    if not roles & {"SYSTEM_ADMIN", "SUPER_ADMIN", "ADMIN", "PLATFORM_ADMIN", "CORPORATE_HSE"}:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Only an administrator can trigger a scorecard rollup."
        )
    return await run_rollup(db, months=months)
