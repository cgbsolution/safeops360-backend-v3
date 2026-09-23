"""Per-flow analytics API.

`GET /api/analytics/{flow}` returns the trend, distribution, ageing, SLA and
ownership analytics for one operational flow, plus the existing deterministic
insight cards as its narrative layer — so the charts and the sentence above them
are always computed from the same records by the same kind of rule.

Unversioned, matching every other router on this backend.

Plant scope is fail-closed: the caller's accessible-plant list is resolved from
their permissions and pushed into the query. A user who can read two plants sees
two plants' analytics, and an explicit `?plant=` outside that set returns
nothing rather than everything.
"""

from __future__ import annotations

import logging

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.user import User
from app.services.analytics import FLOW_KEYS, SPECS, compute_flow, get_spec

router = APIRouter(prefix="/api/analytics", tags=["analytics"])
log = logging.getLogger("safeops360.analytics")

# flow key → (permission code used for plant-scope resolution, insight engine
# module key). The insight key differs from the flow key in two places, which is
# why this is explicit rather than derived.
_FLOW_PERMISSION: dict[str, str] = {
    "observation": "OBSERVATION.READ",
    "incident": "INCIDENT.READ",
    "nearmiss": "NEAR_MISS.READ",
    "capa": "CAPA.READ",
    "hira": "HIRA.READ",
    "eai": "EAI.READ",
    "moc": "MOC.READ",
    "risk": "ERM.READ",
    "inspection": "INSPECTION.READ",
    "training": "TRAINING.READ",
    "audit": "CAMS.ANALYTICS",
    "ptw": "PTW.READ",
    "loto": "LOTO.READ",
}
_INSIGHT_MODULE: dict[str, str] = {
    "observation": "observation",
    "incident": "incident",
    "nearmiss": "nearmiss",
    "capa": "capa",
    "hira": "hira",
    "eai": "eai",
    "moc": "moc",
    "risk": "combined-risk",
    # No Tier-1 insight rules exist for these three — see SUPPORTED_MODULES in
    # app/services/insights/engine.py. Mapped to None rather than omitted so the
    # gap is explicit in code and the router can say so on the wire, instead of
    # the rail silently rendering empty and reading as "nothing to report".
    "inspection": None,
    "training": None,
    "audit": None,
    "ptw": None,
    "loto": None,
}


@router.get("/flows")
async def list_flows(user: User = Depends(get_current_user)) -> dict:  # noqa: ARG001 — auth gate only
    return {
        "flows": [
            {"key": s.key, "label": s.label, "href": s.href, "analyticsHref": f"{s.href}/analytics"}
            for s in SPECS.values()
        ]
    }


@router.get("/summary")
async def all_flow_summaries(
    plant: str | None = Query(default=None),
    months: int = Query(default=12, ge=3, le=36),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Headline numbers for every flow the caller can see — the analytics hub.

    A flow the user lacks permission for is OMITTED, not zeroed: showing "0 open
    incidents" to someone who simply cannot read incidents is a false statement
    about the business, and the hub is exactly where that would be believed.
    """
    from app.services.permissions import get_accessible_plants_for

    out = []
    for key, spec in SPECS.items():
        try:
            allowed = await get_accessible_plants_for(db, user.id, _FLOW_PERMISSION[key])
        except Exception:  # noqa: BLE001 — fail closed, per flow
            continue
        if allowed == [] or (plant and allowed is not None and plant not in allowed):
            continue
        try:
            d = await compute_flow(db, spec, plant=plant, plants_allowed=allowed, months=months)
        except Exception as e:  # noqa: BLE001 — one bad flow must not blank the hub
            log.warning("flow summary failed for %s: %s", key, e)
            continue
        out.append({
            "flow": key,
            "label": spec.label,
            "href": spec.href,
            "analyticsHref": f"/{key}",  # rewritten by the frontend route map
            "recordCount": d["recordCount"],
            "hasClosureData": d["hasClosureData"],
            "hasTargetDates": d["hasTargetDates"],
            "summary": d["summary"],
            "sla": d["sla"],
            "trend": d["trend"],
        })
    return {"flows": out, "months": months, "plant": plant}


def _parse_date(v: str | None, field: str) -> datetime | None:
    """ISO date or datetime → naive UTC. A malformed value is a 422, never a
    silent fall-back to the default window: a filter the reader can see in the
    URL but that the server quietly ignored produces a screen that is wrong in
    a way nobody can spot."""
    if not v:
        return None
    try:
        d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError as e:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"`{field}` must be an ISO date (YYYY-MM-DD), got {v!r}.",
        ) from e
    return d.replace(tzinfo=None) if d.tzinfo else d


# Beyond this many accessible plants, fanning the insight engine out per plant
# is no longer a cheap way to scope it, and the rail is suppressed instead.
_INSIGHT_FANOUT_CAP = 6

# The insight engine's own severity vocabulary, most severe first.
_INSIGHT_SEV_RANK = {"critical": 0, "high": 1, "watch": 2, "info": 3}


async def _scoped_insights(
    db: AsyncSession, module: str, *, plant: str | None, allowed: list[str] | None
) -> tuple[list, bool, str | None]:
    """The narrative cards, honestly scoped to the caller's accessible plants.

    `app.services.insights.compute` narrows on an EXPLICIT `plant=` and on
    nothing else — with no plant it aggregates every plant in the database. The
    register screens call it with the user's selected plant, so this never
    showed; this route calls it with `plant=None` whenever the reader has not
    picked a site, and a reader entitled to two plants was being handed a
    headline counting all twenty-two, naming records (INC-ISL-005,
    INC-CCS-004 …) at plants they cannot open. That is both a false number and a
    disclosure, sitting directly above a correctly-scoped chart.

    Rather than change a service eight register screens depend on, this route
    scopes at its own boundary: fan the engine out over the accessible plants
    and merge. Above `_INSIGHT_FANOUT_CAP` plants that stops being cheap, and
    the rail is SUPPRESSED with a reason rather than silently widened —
    unscoped is not an acceptable fallback for a narrative that names records.

    Returns ([(siteId, card)], scoped, suppression_reason). The site travels
    WITH the card so the UI can join cross-module signals to the finding they
    belong beside, rather than parsing it back out of a composite id.
    """
    from app.services.insights import compute as compute_insights

    if plant or allowed is None:
        # Either the reader picked one site, or their role is unrestricted —
        # in both cases the single call is already correctly scoped.
        res = await compute_insights(db, module, plant=plant)
        return [(plant, c) for c in res.bar], True, None

    if len(allowed) > _INSIGHT_FANOUT_CAP:
        return [], False, (
            f"Findings are not shown here because your access spans "
            f"{len(allowed)} sites — pick a site above to see them."
        )

    # Per-plant cards share a rule slug, so they must NOT be deduplicated by id.
    # Doing that keeps whichever plant happened to be first in the list and
    # silently discards the rest — a two-site reader would be shown "8
    # investigations stalled" when the real figure across their sites is 18.
    # The cards are kept whole and prefixed with the site code instead, so two
    # cards of the same kind are distinguishable at a glance.
    from app.models.plant import Plant

    codes = dict(
        (await db.execute(select(Plant.id, Plant.code).where(Plant.id.in_(allowed)))).all()
    )

    merged: list = []
    seen: set[tuple[str, str]] = set()
    for pid in allowed:
        res = await compute_insights(db, module, plant=pid)
        code = codes.get(pid)
        for card in res.bar:
            key = (str(card.id), pid)
            if key in seen:
                continue
            seen.add(key)
            merged.append((
                pid,
                card.model_copy(
                    update={
                        "id": f"{card.id}:{pid}",
                        "headline": f"{code} · {card.headline}" if code else card.headline,
                    }
                )
                if hasattr(card, "model_copy")
                else card,
            ))
    merged.sort(key=lambda pc: _INSIGHT_SEV_RANK.get(str(pc[1].severity).lower(), 9))
    # More than the rail shows at rest: it folds the tail behind "+N more", and
    # trimming here instead would hide an entire site's finding rather than
    # merely collapse it.
    return merged[:9], True, None


@router.get("/{flow}")
async def flow_analytics(
    flow: str,
    plant: str | None = Query(default=None, description="Restrict to one plant id"),
    months: int = Query(default=12, ge=3, le=36),
    severity: list[str] | None = Query(
        default=None,
        description="Repeatable. Values of the flow's severity dimension; "
                    "`__NOT_SET__` selects records where it is blank.",
    ),
    date_from: str | None = Query(
        default=None, alias="from", description="ISO date; overrides `months`."
    ),
    date_to: str | None = Query(default=None, alias="to", description="ISO date."),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    spec = get_spec(flow)
    if spec is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"No analytics for flow '{flow}'. Supported: {', '.join(FLOW_KEYS)}",
        )

    from app.services.permissions import get_accessible_plants_for

    try:
        # None means "every plant" (an unrestricted role); a list restricts.
        allowed = await get_accessible_plants_for(db, user.id, _FLOW_PERMISSION[flow])
    except Exception as e:  # noqa: BLE001
        # Fail CLOSED. If scope cannot be resolved we must not fall through to
        # an unscoped aggregate — that is precisely how a cross-plant leak ships.
        log.warning("plant scope resolution failed for %s/%s: %s", user.id, flow, e)
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Could not resolve your plant access for this flow."
        ) from e

    # An empty list means the permission is absent entirely (per
    # get_accessible_plants_for's contract), which is a 403, not an empty chart.
    # Rendering "0 records" to someone who simply lacks the permission reads as
    # "there is nothing here" — a false statement about the business.
    if allowed == []:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"You do not hold {_FLOW_PERMISSION[flow]}, so {spec.label} analytics are not available to you.",
        )
    if plant and allowed is not None and plant not in allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You do not have access to that plant.")

    data = await compute_flow(
        db, spec,
        plant=plant,
        plants_allowed=allowed,
        months=months,
        severities=severity or None,
        date_from=_parse_date(date_from, "from"),
        date_to=_parse_date(date_to, "to"),
    )

    # Narrative layer: the same insight cards the register shows, so the
    # analytics page never contradicts the list it links into. Guarded — a
    # failure here must not take the charts down with it.
    #
    # `insightsScope` declares exactly which of the caller's filters the
    # narrative actually honours, and it is not decoration. The insight engine
    # is shared with eight register screens and narrows on plant only; it has no
    # severity filter, and it computes over its own fixed recency window rather
    # than the analytics window. Without this declaration, filtering to the four
    # CRITICAL/HIGH incidents left the rail still announcing "41 investigations
    # stalled >30d" over the top of a four-record chart — the narrative and the
    # numbers disagreeing, which is the one thing this layer promises cannot
    # happen. The UI renders a qualifier instead of quietly presenting the
    # unfiltered sentence as though it described the filtered set.
    data["insights"] = []
    insight_module = _INSIGHT_MODULE.get(flow)
    data["insightsScope"] = {
        "plant": True,        # honoured — see _scoped_insights
        "severity": False,    # the insight engine has no severity filter
        "window": False,      # it uses its own recency window, not `months`
        "suppressedReason": None,
        # True when this flow has NO Tier-1 rule set at all, as opposed to
        # having one that found nothing. The screen must render those two
        # states differently: "no rules exist for this module yet" is a product
        # gap, "the rules found nothing" is a result.
        "engineAvailable": insight_module is not None,
    }
    if insight_module is None:
        data["insightsScope"]["suppressedReason"] = (
            f"No Tier-1 insight rules exist for {spec.label} yet, so this rail has no "
            f"source. This is a coverage gap, not an all-clear."
        )
        return data
    try:
        cards, scoped, reason = await _scoped_insights(
            db, insight_module, plant=plant, allowed=allowed
        )
        data["insightsScope"]["plant"] = scoped
        data["insightsScope"]["suppressedReason"] = reason
        data["insights"] = [
            {
                "id": i.id, "kind": i.kind, "severity": i.severity, "headline": i.headline,
                "evidence": i.evidence, "suggestedAction": i.suggestedAction,
                "confidence": i.confidence, "recordRefs": i.recordRefs,
                "siteId": pid,
            }
            for pid, i in cards
        ]
    except Exception as e:  # noqa: BLE001
        log.warning("insight overlay failed for %s: %s", flow, e)

    return data
