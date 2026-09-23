"""Precaution-checklist completeness — one shared verdict, two callers.

Shaped like `ptw_closure_gate` and `loto.permit_closure_blocker`: a pure-ish
function returning the REASON a permit may not proceed, called both by the
API that enforces it and by the endpoint the UI reads. The panel can
therefore never say "clear" about something the API is about to refuse.

The rule, in one sentence: every mandatory precaution line on every attached
annexure must be answered, YES or a justified NA — a NO means the control is
not in place and the permit does not get raised.

Where it is enforced
────────────────────
The single hard gate is the EHS officer's permit-wide CERTIFICATION — the
Safety Officer step, or the Issuer step on chains that have no Safety Officer
(cold work). See `workflow_engine.approve`. That is the same point at which
the paper form is signed.

Deliberately NOT enforced at two other places:

  • `POST /api/ptw` (create). It is a published API the mobile app uses
    without sending `hazards`; gating create would 400 every one of those
    calls the moment the catalog is seeded. A checklist is also legitimately
    completable after raising — that is what the detail page's annexure panel
    is for. The web wizard validates client-side anyway.
  • Receiver acceptance. By then the EHS officer has already certified, and
    blocking a crew standing at the worksite over a checkbox someone else owns
    is how gates get worked around.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.permit import (
    Permit,
    PermitHazardAnnexure,
    PermitHazardType,
    PermitPrecautionItem,
    PermitPrecautionResponse,
    PrecautionResponse,
)
from app.services.ptw_hazards import HAZARD_LABELS


@dataclass
class AnnexureVerdict:
    """Per-annexure completeness detail — what the UI renders per tab."""

    hazardType: str
    label: str
    isPrimary: bool
    totalItems: int
    mandatoryItems: int
    answered: int
    unanswered: list[str] = field(default_factory=list)   # item texts
    refused: list[str] = field(default_factory=list)      # answered NO
    naWithoutRemark: list[str] = field(default_factory=list)
    complete: bool = False


@dataclass
class CompletenessVerdict:
    """Permit-wide verdict. `blocker` is None when everything is satisfied."""

    complete: bool
    blocker: str | None
    annexures: list[AnnexureVerdict] = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def load_catalog(
    db: AsyncSession, hazards: list[PermitHazardType] | None = None
) -> dict[PermitHazardType, list[PermitPrecautionItem]]:
    """Active catalog items grouped by hazard, in display order.

    Retired items (`isActive = False`) are excluded here on purpose: they must
    never be added to a NEW permit, but `evaluate` still reads them through
    the recorded responses so an old permit renders as it was answered.
    """
    stmt = select(PermitPrecautionItem).where(PermitPrecautionItem.isActive.is_(True))
    if hazards:
        stmt = stmt.where(PermitPrecautionItem.hazardType.in_(list(hazards)))
    rows = (await db.execute(stmt.order_by(PermitPrecautionItem.sequence))).scalars().all()
    grouped: dict[PermitHazardType, list[PermitPrecautionItem]] = {}
    for r in rows:
        grouped.setdefault(r.hazardType, []).append(r)
    return grouped


def evaluate_annexure(
    annexure: PermitHazardAnnexure,
    catalog: list[PermitPrecautionItem],
) -> AnnexureVerdict:
    """Verdict for ONE annexure against the catalog it should have answered.

    Items are keyed by id, and the union of (catalog ∪ answered) is walked —
    so an item retired after the permit was raised still counts as answered
    rather than silently vanishing from the denominator.
    """
    by_item: dict[str, PermitPrecautionResponse] = {
        r.itemId: r for r in (annexure.responses or [])
    }
    verdict = AnnexureVerdict(
        hazardType=annexure.hazardType.value,
        label=HAZARD_LABELS.get(annexure.hazardType, annexure.hazardType.value),
        isPrimary=annexure.isPrimary,
        totalItems=len(catalog),
        mandatoryItems=sum(1 for i in catalog if i.isMandatory),
        answered=0,
    )

    for item in catalog:
        resp = by_item.get(item.id)
        if resp is None:
            if item.isMandatory:
                verdict.unanswered.append(item.text)
            continue
        verdict.answered += 1
        if resp.response == PrecautionResponse.NO and item.isMandatory:
            verdict.refused.append(item.text)
        elif resp.response == PrecautionResponse.NA:
            # NA must be justified, and only where the catalog permits it.
            if not item.allowsNA:
                verdict.refused.append(item.text)
            elif not (resp.remark and resp.remark.strip()):
                verdict.naWithoutRemark.append(item.text)

    verdict.complete = not (
        verdict.unanswered or verdict.refused or verdict.naWithoutRemark
    )
    return verdict


def _summarise(items: list[str], limit: int = 3) -> str:
    """First few offending lines, truncated — a blocker message has to fit in
    a toast, and the panel shows the full list anyway."""
    shown = [t if len(t) <= 90 else t[:87] + "…" for t in items[:limit]]
    rest = len(items) - len(shown)
    out = "; ".join(shown)
    return f"{out} (+{rest} more)" if rest > 0 else out


async def evaluate(db: AsyncSession, permit_id: str) -> CompletenessVerdict:
    """Permit-wide checklist verdict. Safe to call on a permit with no
    annexures (legacy rows predating this build) — those come back complete,
    so nothing that is already in flight is retro-blocked."""
    annexures = (
        (
            await db.execute(
                select(PermitHazardAnnexure)
                .where(PermitHazardAnnexure.permitId == permit_id)
                .options(selectinload(PermitHazardAnnexure.responses))
            )
        )
        .scalars()
        .all()
    )
    if not annexures:
        return CompletenessVerdict(complete=True, blocker=None, annexures=[])

    catalog = await load_catalog(db, [a.hazardType for a in annexures])

    results = [evaluate_annexure(a, catalog.get(a.hazardType, [])) for a in annexures]
    results.sort(key=lambda v: (not v.isPrimary, v.label))

    problems: list[str] = []
    for v in results:
        if v.unanswered:
            problems.append(f"{v.label}: {len(v.unanswered)} unanswered — {_summarise(v.unanswered)}")
        if v.refused:
            problems.append(
                f"{v.label}: precaution not in place — {_summarise(v.refused)}. "
                "Put the control in place or remove the hazard from this permit."
            )
        if v.naWithoutRemark:
            problems.append(
                f"{v.label}: 'Not applicable' needs a reason — {_summarise(v.naWithoutRemark)}"
            )

    return CompletenessVerdict(
        complete=not problems,
        blocker="\n• ".join(["Precaution checklist incomplete:", *problems]) if problems else None,
        annexures=results,
    )


async def stamp_annexure_completion(
    db: AsyncSession, permit_id: str, user_id: str | None
) -> None:
    """Stamp `completedAt` on each annexure whose checklist now passes, and
    clear it on any that no longer does.

    Called after responses are written. Idempotent, and never touches
    `Permit.precautionsCertifiedAt` — that is the EHS officer's single
    permit-wide signature and belongs to the approval step alone.
    """
    annexures = (
        (
            await db.execute(
                select(PermitHazardAnnexure)
                .where(PermitHazardAnnexure.permitId == permit_id)
                .options(selectinload(PermitHazardAnnexure.responses))
            )
        )
        .scalars()
        .all()
    )
    if not annexures:
        return
    catalog = await load_catalog(db, [a.hazardType for a in annexures])
    for a in annexures:
        ok = evaluate_annexure(a, catalog.get(a.hazardType, [])).complete
        if ok and a.completedAt is None:
            a.completedAt = _now()
            a.completedById = user_id
        elif not ok and a.completedAt is not None:
            # An edit that breaks a previously-complete annexure must retract
            # the completion, not leave a stale green tick behind it.
            a.completedAt = None
            a.completedById = None
    await db.flush()


async def certification_blocker(db: AsyncSession, permit: Permit) -> str | None:
    """Reason the EHS officer's permit-wide certification cannot be given.

    Called from the workflow engine at the Safety Officer step. Returns None
    when the permit may be certified.
    """
    verdict = await evaluate(db, permit.id)
    if verdict.complete:
        return None
    return (
        f"{verdict.blocker}\n\n"
        "Open the permit → Hazard Annexures panel. Certifying this permit "
        "signs for every attached checklist at once."
    )
