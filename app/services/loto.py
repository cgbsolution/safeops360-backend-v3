"""LOTO (Lockout/Tagout) service layer.

Everything that is a RULE rather than a route lives here, so the router stays a
thin transport shell and the same rule cannot be enforced two different ways on
two different endpoints.

The five rules worth reading before changing anything:

1. **Publish precondition.** A procedure cannot go active with zero isolation
   points or zero verification steps. Enforced in `publish_blockers()`, which the
   publish endpoint calls and the detail response also reports, so the button and
   the API agree.

2. **Material edits version, they do not overwrite.** `apply_body_edit()` writes
   an immutable `LotoProcedureVersion` snapshot, bumps `version`, and — if the
   procedure was live — moves it to `under_review`. v1's snapshot is never
   touched. `publishedVersionId` keeps pointing at the last APPROVED version, so
   a QR scan during the review window still resolves the approved sequence.

3. **The execution snapshot is frozen at start.** `start_execution()` copies the
   published body into `procedureVersionSnapshot`. Nothing afterwards re-reads
   the live procedure. A concurrent authoring edit is invisible to a crew
   already at the equipment — surfaced to supervisors as
   `procedureHasChangedSinceStart`, never applied.

4. **Group lockout confirmations are individual, at the API layer.**
   `confirm_lock()` / `confirm_unlock()` only ever move the CALLER's own
   participant row, and the status transitions are gated on ALL lock-holder rows.
   There is no bulk path. An `affected_employee` is notified, not locked on, and
   so is excluded from the gate — otherwise every group lockout would hang on a
   person with no lock to confirm.

5. **Closure is blocked, and says what is missing.** `execution_gate()` returns
   every outstanding participant and step by name. A silent close with an
   incomplete record is the exact failure the HIRA re-approval gap produced; it
   is not repeated here.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import Integer, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import set_committed_value

from app.models.loto import (
    LOCK_HOLDER_ROLES,
    OPEN_EXECUTION_STATUSES,
    LotoEnergySource,
    LotoExecution,
    LotoExecutionParticipant,
    LotoHardwareRequirement,
    LotoIsolationPoint,
    LotoProcedure,
    LotoProcedureVersion,
    LotoReviewLog,
    LotoVerificationRecord,
    LotoVerificationStep,
)

log = logging.getLogger("safeops360.loto")

#: A review inside this window is "due soon" — enough notice to schedule a
#: walk-down without the list crying wolf all year.
DUE_SOON_DAYS = 30


class LotoError(Exception):
    """Business-rule violation. The router maps this to a 409/422 with the
    message shown verbatim to the user, so messages must say what to DO."""

    def __init__(self, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    """Postgres columns come back naive under some drivers; comparing a naive to
    an aware datetime raises. Normalise before every comparison."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _add_months(base: datetime, months: int) -> datetime:
    """Month arithmetic without a hard dependency on dateutil (which is an
    optional import elsewhere in this codebase). Clamps the day so
    31 Jan + 1 month lands on 28/29 Feb rather than raising."""
    month_index = base.month - 1 + months
    year = base.year + month_index // 12
    month = month_index % 12 + 1
    # Last day of the target month, found by stepping back from the 1st of the
    # month after it.
    if month == 12:
        first_of_next = base.replace(year=year + 1, month=1, day=1)
    else:
        first_of_next = base.replace(year=year, month=month + 1, day=1)
    last_day = (first_of_next - timedelta(days=1)).day
    return base.replace(year=year, month=month, day=min(base.day, last_day))


# ═══════════════════════════════════════════════════════════════════════════
#  Numbering
# ═══════════════════════════════════════════════════════════════════════════


async def next_procedure_code(db: AsyncSession, site_id: str) -> str:
    """`LOTO-EQ-####`, per site, MAX(existing) + 1.

    Deliberately NOT `COUNT(*) + 1`. The code is unique per site among live rows,
    and a soft-delete leaves the row — so counting live rows re-proposes a code
    that still exists and the insert dies on the unique index. That exact bug
    (count+1 against a soft-deleting table) took Schedule Audit down and is
    documented on `audit_compliance._next_number`; it is not repeated here.

    `include_deleted=True` opts this one query out of the global soft-delete
    filter: uniqueness is a property of the table, not of what the caller may see.
    """
    tail = func.regexp_replace(LotoProcedure.procedureCode, r"^.*-", "")
    last = (
        await db.execute(
            select(func.max(cast(tail, Integer)))
            .where(LotoProcedure.siteId == site_id)
            .where(LotoProcedure.procedureCode.op("~")(r"-[0-9]+$"))
            .execution_options(include_deleted=True)
        )
    ).scalar() or 0
    return f"LOTO-EQ-{(last + 1):04d}"


async def next_execution_number(db: AsyncSession) -> str:
    """`LOTO-EX-{year}-####`. Same max+1 reasoning as above, scoped to the year."""
    year = _now().year
    prefix = f"LOTO-EX-{year}-"
    tail = func.regexp_replace(LotoExecution.number, r"^.*-", "")
    last = (
        await db.execute(
            select(func.max(cast(tail, Integer)))
            .where(LotoExecution.number.like(f"{prefix}%"))
            .execution_options(include_deleted=True)
        )
    ).scalar() or 0
    return f"{prefix}{(last + 1):04d}"


def new_qr_token() -> str:
    """URL-safe, unguessable, and long enough that the public field URL cannot be
    enumerated. Generated once and never regenerated — the token is printed on a
    label bolted to a machine, so rotating it would silently orphan the label."""
    return secrets.token_urlsafe(24)


# ═══════════════════════════════════════════════════════════════════════════
#  Procedure body — load, snapshot, replace
# ═══════════════════════════════════════════════════════════════════════════

_BODY_LOADS = (
    selectinload(LotoProcedure.energySources),
    selectinload(LotoProcedure.isolationPoints),
    selectinload(LotoProcedure.hardware),
    selectinload(LotoProcedure.verificationSteps),
)


async def load_procedure(
    db: AsyncSession, procedure_id: str, *, with_versions: bool = False
) -> LotoProcedure:
    """Load a procedure with its full body.

    ⚠ Uses `select()`, never `db.get(..., options=...)` — `db.get` silently
    DROPS eager-load options when the row is already in the identity map, which
    then raises MissingGreenlet on first access of a collection. That has bitten
    this codebase before (documented on the HIRA round-3 build).
    """
    opts = list(_BODY_LOADS)
    if with_versions:
        opts.append(selectinload(LotoProcedure.versions))
    proc = (
        await db.execute(
            select(LotoProcedure).options(*opts).where(LotoProcedure.id == procedure_id)
        )
    ).scalar_one_or_none()
    if proc is None or proc.isDeleted:
        raise LotoError("Procedure not found", status_code=404)
    return proc


def body_snapshot(proc: LotoProcedure) -> dict[str, Any]:
    """Serialise the live body into the JSON shape stored on a version row and
    copied into an execution.

    This is the ONLY producer of that shape. The QR view, the execution screen
    and the close-out report all consume it, so a field added here appears
    everywhere at once and a field renamed here cannot half-migrate.
    """
    return {
        "header": {
            "procedureCode": proc.procedureCode,
            "title": proc.title,
            "description": proc.description,
            "equipmentId": proc.equipmentId,
            "equipmentName": proc.equipmentName,
            "equipmentTag": proc.equipmentTag,
            "siteId": proc.siteId,
            "siteName": proc.siteName,
            "area": proc.area,
            "version": proc.version,
        },
        "energySources": [
            {
                "id": e.id,
                "sequence": e.sequence,
                "energyType": e.energyType,
                "magnitude": e.magnitude,
                "locationDescription": e.locationDescription,
            }
            for e in sorted(proc.energySources, key=lambda x: x.sequence)
        ],
        "isolationPoints": [
            {
                "id": p.id,
                "sequence": p.sequence,
                "energySourceId": p.energySourceId,
                "location": p.location,
                "isolationMethod": p.isolationMethod,
                "lockType": p.lockType,
                "verificationMethod": p.verificationMethod,
                "notes": p.notes,
            }
            for p in sorted(proc.isolationPoints, key=lambda x: x.sequence)
        ],
        "hardware": [
            {
                "id": h.id,
                "itemType": h.itemType,
                "description": h.description,
                "quantityRequired": h.quantityRequired,
            }
            for h in proc.hardware
        ],
        "verificationSteps": [
            {
                "id": s.id,
                "sequence": s.sequence,
                "stepText": s.stepText,
                "requiresPhoto": s.requiresPhoto,
                "requiresSignoff": s.requiresSignoff,
            }
            for s in sorted(proc.verificationSteps, key=lambda x: x.sequence)
        ],
    }


def publish_blockers(proc: LotoProcedure) -> list[str]:
    """Spec §2.1 — the publish precondition, as validation and not a UI hint.

    Returned as a list rather than a bool so the builder can show every reason at
    once, and so the API's refusal message names the same reasons the button did.
    """
    blockers: list[str] = []
    if not proc.isolationPoints:
        blockers.append(
            "Add at least one isolation point — a procedure with no isolation "
            "point does not isolate anything."
        )
    if not proc.verificationSteps:
        blockers.append(
            "Add at least one verification step — without one there is no "
            "recorded proof of zero energy."
        )
    if not (proc.title or "").strip():
        blockers.append("Give the procedure a title.")
    return blockers


def _reseq(items: list[Any]) -> None:
    """Renumber densely from 1, in list order. Runs on every body replace so a
    client that sends gaps, duplicates or no sequence at all still ends up with
    an unambiguous isolation order."""
    for i, item in enumerate(items, start=1):
        item.sequence = i


async def replace_body(
    db: AsyncSession,
    proc: LotoProcedure,
    *,
    energy_sources: list[Any] | None,
    isolation_points: list[Any] | None,
    hardware: list[Any] | None,
    verification_steps: list[Any] | None,
) -> bool:
    """Replace whichever collections were supplied. Returns True if anything in
    the MATERIAL set (isolation points, hardware, verification steps) moved.

    Rows are matched by id where the payload supplies one, so an edit that only
    reorders or retitles keeps the same row ids — which matters because a
    published version snapshot and any running execution reference those ids.
    """
    material = False

    # ── Energy sources ──
    # Not material on their own (they are descriptive), but isolation points
    # reference them, so they are rebuilt first and the id map handed downstream.
    id_map: dict[int, str] = {}
    if energy_sources is not None:
        existing = {e.id: e for e in proc.energySources}
        kept: list[LotoEnergySource] = []
        for idx, payload in enumerate(energy_sources):
            row = existing.pop(payload.id, None) if payload.id else None
            if row is None:
                row = LotoEnergySource(procedureId=proc.id, energyType=payload.energyType)
                db.add(row)
                await db.flush()  # need the generated id for the ref map below
            row.energyType = payload.energyType
            row.magnitude = payload.magnitude
            row.locationDescription = payload.locationDescription
            kept.append(row)
            id_map[idx] = row.id
        for orphan in existing.values():
            await db.delete(orphan)
        _reseq(kept)
        proc.energySources = kept
    else:
        for idx, e in enumerate(sorted(proc.energySources, key=lambda x: x.sequence)):
            id_map[idx] = e.id

    # ── Isolation points (MATERIAL) ──
    if isolation_points is not None:
        existing = {p.id: p for p in proc.isolationPoints}
        before = {
            (p.location, p.isolationMethod, p.lockType, p.verificationMethod, p.sequence)
            for p in proc.isolationPoints
        }
        kept_points: list[LotoIsolationPoint] = []
        for payload in isolation_points:
            row = existing.pop(payload.id, None) if payload.id else None
            if row is None:
                row = LotoIsolationPoint(
                    procedureId=proc.id,
                    location=payload.location,
                    isolationMethod=payload.isolationMethod,
                    sequence=0,
                )
                db.add(row)
            # An explicit id wins; otherwise resolve the in-payload index ref.
            source_id = payload.energySourceId
            if source_id is None and payload.energySourceRef is not None:
                source_id = id_map.get(payload.energySourceRef)
            row.energySourceId = source_id
            row.location = payload.location
            row.isolationMethod = payload.isolationMethod
            row.lockType = payload.lockType
            row.verificationMethod = payload.verificationMethod
            row.notes = payload.notes
            kept_points.append(row)
        for orphan in existing.values():
            await db.delete(orphan)
        _reseq(kept_points)
        proc.isolationPoints = kept_points
        after = {
            (p.location, p.isolationMethod, p.lockType, p.verificationMethod, p.sequence)
            for p in kept_points
        }
        material = material or (before != after)

    # ── Hardware (MATERIAL) ──
    if hardware is not None:
        existing = {h.id: h for h in proc.hardware}
        before_hw = {(h.itemType, h.description, h.quantityRequired) for h in proc.hardware}
        kept_hw: list[LotoHardwareRequirement] = []
        for payload in hardware:
            row = existing.pop(payload.id, None) if payload.id else None
            if row is None:
                row = LotoHardwareRequirement(procedureId=proc.id, itemType=payload.itemType)
                db.add(row)
            row.itemType = payload.itemType
            row.description = payload.description
            row.quantityRequired = payload.quantityRequired
            kept_hw.append(row)
        for orphan in existing.values():
            await db.delete(orphan)
        proc.hardware = kept_hw
        after_hw = {(h.itemType, h.description, h.quantityRequired) for h in kept_hw}
        material = material or (before_hw != after_hw)

    # ── Verification steps (MATERIAL) ──
    if verification_steps is not None:
        existing = {s.id: s for s in proc.verificationSteps}
        before_vs = {
            (s.stepText, s.requiresPhoto, s.requiresSignoff, s.sequence)
            for s in proc.verificationSteps
        }
        kept_vs: list[LotoVerificationStep] = []
        for payload in verification_steps:
            row = existing.pop(payload.id, None) if payload.id else None
            if row is None:
                row = LotoVerificationStep(
                    procedureId=proc.id, stepText=payload.stepText, sequence=0
                )
                db.add(row)
            row.stepText = payload.stepText
            row.requiresPhoto = payload.requiresPhoto
            row.requiresSignoff = payload.requiresSignoff
            kept_vs.append(row)
        for orphan in existing.values():
            await db.delete(orphan)
        _reseq(kept_vs)
        proc.verificationSteps = kept_vs
        after_vs = {
            (s.stepText, s.requiresPhoto, s.requiresSignoff, s.sequence) for s in kept_vs
        }
        material = material or (before_vs != after_vs)

    await db.flush()
    return material


async def record_version(
    db: AsyncSession,
    proc: LotoProcedure,
    *,
    actor_id: str,
    change_type: str,
    change_summary: str | None,
    publish: bool = False,
) -> LotoProcedureVersion:
    """Write an immutable snapshot at the procedure's CURRENT version number.

    Idempotent per (procedureId, version): if a row already exists for this
    version it is refreshed rather than duplicated, which keeps the DB's unique
    index from turning a double-submit into a 500.
    """
    existing = (
        await db.execute(
            select(LotoProcedureVersion)
            .where(LotoProcedureVersion.procedureId == proc.id)
            .where(LotoProcedureVersion.version == proc.version)
        )
    ).scalar_one_or_none()

    snapshot = body_snapshot(proc)
    if existing is not None:
        # Reassign, never mutate in place: an in-place edit of a JSON column is
        # invisible to SQLAlchemy's change detection and the commit silently
        # no-ops (the CAMS citation-provenance lesson).
        existing.snapshotJson = snapshot
        existing.changeType = change_type
        if change_summary:
            existing.changeSummary = change_summary
        version = existing
    else:
        version = LotoProcedureVersion(
            procedureId=proc.id,
            version=proc.version,
            snapshotJson=snapshot,
            changeType=change_type,
            changeSummary=change_summary,
            createdById=actor_id,
        )
        db.add(version)
        await db.flush()

    if publish:
        # Supersede whatever was published before — the old row stays, it just
        # stops being the one the field sees.
        if proc.publishedVersionId and proc.publishedVersionId != version.id:
            prior = await db.get(LotoProcedureVersion, proc.publishedVersionId)
            if prior is not None:
                prior.isPublished = False
                prior.supersededAt = _now()
        version.isPublished = True
        version.publishedAt = _now()
        version.publishedById = actor_id
        proc.publishedVersionId = version.id

    await db.flush()
    return version


async def apply_body_edit(
    db: AsyncSession,
    proc: LotoProcedure,
    *,
    actor_id: str,
    material: bool,
    change_summary: str | None,
) -> bool:
    """Post-edit bookkeeping. Returns True if the edit withdrew a live approval.

    Material edit to an ACTIVE procedure:
      • bump `version` (v1's snapshot is untouched — it stays queryable forever)
      • write the new version row, NOT published
      • move status → under_review, so it must be re-approved before going live

    `publishedVersionId` is deliberately left pointing at the last APPROVED
    version. That is what makes a QR scan during the review window resolve the
    approved sequence rather than an unreviewed one.
    """
    if not material:
        # Minor edit — title/description/cadence. No version, no re-approval.
        proc.updatedById = actor_id
        await db.flush()
        return False

    withdrew = proc.status == "active"
    proc.version += 1
    proc.updatedById = actor_id
    await record_version(
        db, proc, actor_id=actor_id, change_type="MATERIAL",
        change_summary=change_summary, publish=False,
    )
    if withdrew:
        proc.status = "under_review"
    await db.flush()
    return withdrew


def compute_next_review(proc: LotoProcedure, *, from_dt: datetime | None = None) -> datetime:
    base = from_dt or _now()
    return _add_months(base, proc.reviewFrequencyMonths or 12)


# ═══════════════════════════════════════════════════════════════════════════
#  Review cycle
# ═══════════════════════════════════════════════════════════════════════════


async def review_state(
    db: AsyncSession, procedure_ids: Iterable[str]
) -> dict[str, str | None]:
    """`{procedureId: pendingReviewId | None}` in one query.

    Batched because the library screen renders the overdue flag on every row and
    a per-row lookup would be N+1 against the list.
    """
    ids = [i for i in procedure_ids if i]
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(LotoReviewLog.procedureId, LotoReviewLog.id)
            .where(LotoReviewLog.procedureId.in_(ids))
            .where(LotoReviewLog.status == "pending")
        )
    ).all()
    return {r[0]: r[1] for r in rows}


def review_status_fields(
    proc: LotoProcedure, *, pending_review_id: str | None = None
) -> dict[str, Any]:
    """Everything the `ReviewStatus` block needs, computed in one place so the
    list and the detail can never disagree about whether a procedure has lapsed.

    A retired procedure is never "overdue" — it is out of service, and flagging
    it would bury the live ones that actually need a walk-down.
    """
    due = _aware(proc.nextReviewDueAt)
    now = _now()
    days: int | None = None
    overdue = False
    due_soon = False
    if due is not None and proc.status != "retired":
        days = (due - now).days
        overdue = due < now
        due_soon = not overdue and days is not None and days <= DUE_SOON_DAYS
    return {
        "nextReviewDueAt": proc.nextReviewDueAt,
        "lastReviewedAt": proc.lastReviewedAt,
        "lastReviewedById": proc.lastReviewedById,
        "isOverdue": overdue,
        "isDueSoon": due_soon,
        "daysUntilDue": days,
        "pendingReviewId": pending_review_id,
    }


async def run_review_scan(db: AsyncSession) -> dict[str, Any]:
    """Daily job: open a pending LotoReviewLog for every active procedure whose
    evaluation has fallen due, and notify the owner once.

    Idempotent twice over — the partial unique index allows at most one pending
    row per procedure, and `notifiedAt` dedupes the notification — so a scan that
    runs twice in a day creates nothing extra and does not double-email.

    Note this job never DECIDES anything. An unattended review leaves the pending
    row in place, and the procedure keeps rendering as overdue on the library
    screen until a human acts. Silence escalates; it does not clear.
    """
    now = _now()
    due = (
        await db.execute(
            select(LotoProcedure)
            .where(LotoProcedure.isDeleted.is_(False))
            .where(LotoProcedure.status == "active")
            .where(LotoProcedure.nextReviewDueAt.isnot(None))
            .where(LotoProcedure.nextReviewDueAt <= now)
        )
    ).scalars().all()

    existing_pending = await review_state(db, [p.id for p in due])

    created = 0
    notified = 0
    for proc in due:
        review_id = existing_pending.get(proc.id)
        if review_id is None:
            review = LotoReviewLog(
                procedureId=proc.id,
                status="pending",
                dueAt=proc.nextReviewDueAt or now,
            )
            db.add(review)
            await db.flush()
            created += 1
        else:
            review = await db.get(LotoReviewLog, review_id)
            if review is None:
                continue

        if review.notifiedAt is None:
            owner_id = proc.lastReviewedById or proc.createdById
            if owner_id:
                await _notify_review_due(db, proc, owner_id)
                notified += 1
            review.notifiedAt = now

    await db.flush()
    return {
        "evaluated": len(due),
        "created": created,
        "notified": notified,
        "summary": f"{len(due)} procedure(s) due; {created} review(s) opened, {notified} notified",
    }


async def _notify_review_due(db: AsyncSession, proc: LotoProcedure, owner_id: str) -> None:
    """In-app Notification row + best-effort email, via the mechanisms the ERM
    and MOC schedulers already use. No new notification channel is introduced —
    a new channel is a new thing to configure, monitor and forget."""
    from app.models.notification import Notification

    title = f"LOTO procedure review due — {proc.procedureCode}"
    body = (
        f"{proc.title} ({proc.equipmentName or proc.equipmentTag or 'equipment'}) "
        f"is due for its periodic evaluation. Review it to confirm the isolation "
        f"sequence still matches the plant."
    )
    db.add(
        Notification(
            userId=owner_id,
            type="LOTO_REVIEW_DUE",
            severity="WARNING",
            title=title,
            body=body,
            entityType="LotoProcedure",
            entityId=proc.id,
            linkUrl=f"/loto/{proc.id}",
        )
    )

    try:
        from app.models.user import User
        from app.services.notifications import send_email

        owner = await db.get(User, owner_id)
        if owner is not None and getattr(owner, "email", None):
            await send_email([owner.email], title, body)
    except Exception as e:  # noqa: BLE001
        # Best-effort by design: a mail outage must not stop the review row from
        # being created, or the cycle silently stops working when SMTP breaks.
        log.warning("LOTO review-due email failed for %s: %s", proc.procedureCode, e)


# ═══════════════════════════════════════════════════════════════════════════
#  Execution
# ═══════════════════════════════════════════════════════════════════════════


async def load_execution(db: AsyncSession, execution_id: str) -> LotoExecution:
    ex = (
        await db.execute(
            select(LotoExecution)
            .options(
                selectinload(LotoExecution.participants),
                selectinload(LotoExecution.verificationRecords),
            )
            .where(LotoExecution.id == execution_id)
        )
    ).scalar_one_or_none()
    if ex is None or ex.isDeleted:
        raise LotoError("Lockout record not found", status_code=404)
    return ex


def lock_holders(ex: LotoExecution) -> list[LotoExecutionParticipant]:
    """The participants whose confirmation actually gates a transition.

    Excludes `affected_employee`: an affected employee is someone whose job is
    disrupted by the lockout, notified under OSHA 1910.147(b) but never issued a
    lock. Including them would deadlock every group lockout on a confirmation
    that cannot be given.
    """
    return [p for p in ex.participants if p.participantRole in LOCK_HOLDER_ROLES]


def _snapshot_steps(ex: LotoExecution) -> list[dict[str, Any]]:
    """Verification steps FROM THE FROZEN SNAPSHOT — never the live procedure.
    This is the function that makes checklist item 5 of the spec true."""
    snap = ex.procedureVersionSnapshot or {}
    steps = snap.get("verificationSteps") or []
    return sorted(steps, key=lambda s: s.get("sequence", 0))


def execution_gate(ex: LotoExecution) -> dict[str, Any]:
    """Which transitions are available, and every reason they are not.

    All-at-once (never short-circuited) so the execution screen renders the full
    picture in one panel — the same shape as the PTW activation gate. Outstanding
    people and steps are named, because "someone hasn't confirmed" is not
    actionable when you are standing at a kiln at 6 a.m.
    """
    holders = lock_holders(ex)
    blockers: list[str] = []

    awaiting_lock = [
        p.userName or p.userId for p in holders if not p.lockAppliedConfirmed
    ]
    awaiting_unlock = [
        p.userName or p.userId for p in holders if not p.lockRemovedConfirmed
    ]

    steps = _snapshot_steps(ex)
    done_by_step = {r.stepId: r for r in ex.verificationRecords}
    outstanding_steps: list[str] = []
    for s in steps:
        rec = done_by_step.get(s.get("id"))
        label = f"Step {s.get('sequence')}: {(s.get('stepText') or '')[:60]}"
        if rec is None:
            outstanding_steps.append(label)
            continue
        if s.get("requiresSignoff") and not rec.signoff:
            outstanding_steps.append(f"{label} — sign-off missing")
        elif s.get("requiresPhoto") and not rec.photoUrl:
            outstanding_steps.append(f"{label} — photo missing")

    all_locked = bool(holders) and not awaiting_lock
    all_verified = not outstanding_steps
    all_unlocked = bool(holders) and not awaiting_unlock

    status = ex.status
    terminal = status in {"closed", "aborted"}

    can_apply = (not terminal) and status == "locks_applied" and bool(awaiting_lock)
    can_verify = (not terminal) and status in {"locks_applied", "verified"} and all_locked
    can_start_work = (not terminal) and status == "verified"
    # Locks come off from work_in_progress OR straight from verified — work can
    # legitimately be called off after verification without ever starting.
    can_remove = (
        (not terminal)
        and status in {"verified", "work_in_progress", "locks_removed"}
        and bool(awaiting_unlock)
    )
    can_close = (not terminal) and status == "locks_removed" and all_unlocked and all_verified

    # ── Closure blockers, stated specifically (spec §2.9) ──
    if not terminal:
        if not holders:
            blockers.append(
                "No lock holder is enrolled on this lockout — add at least one "
                "authorised person."
            )
        if awaiting_lock:
            blockers.append(
                "Waiting on lock application from: " + ", ".join(awaiting_lock)
            )
        if outstanding_steps:
            blockers.append(
                "Zero-energy verification incomplete — "
                + "; ".join(outstanding_steps[:5])
                + (f" (+{len(outstanding_steps) - 5} more)" if len(outstanding_steps) > 5 else "")
            )
        if status in {"locks_removed"} and awaiting_unlock:
            blockers.append(
                "Waiting on lock removal from: " + ", ".join(awaiting_unlock)
            )
        elif awaiting_unlock and status in {"verified", "work_in_progress"}:
            blockers.append(
                "Locks still on equipment — outstanding removal from: "
                + ", ".join(awaiting_unlock)
            )

    return {
        "canApplyLocks": can_apply,
        "canVerify": can_verify,
        "canStartWork": can_start_work,
        "canRemoveLocks": can_remove,
        "canClose": can_close,
        "blockers": blockers,
        "awaitingLockConfirmation": awaiting_lock,
        "awaitingUnlockConfirmation": awaiting_unlock,
        "outstandingVerificationSteps": outstanding_steps,
    }


async def start_execution(
    db: AsyncSession,
    *,
    proc: LotoProcedure,
    actor_id: str,
    actor_name: str | None,
    is_group: bool,
    participants: list[Any],
    ptw_id: str | None,
    ptw_number: str | None,
) -> LotoExecution:
    """Open a lockout against the PUBLISHED version of a procedure.

    The published version — not the live body. If a material edit is sitting
    unapproved, the crew locks out against the last approved sequence, which is
    the only sequence anyone has signed off on.
    """
    if proc.status != "active":
        raise LotoError(
            f"Procedure {proc.procedureCode} is {proc.status.replace('_', ' ')}, not active. "
            "Only a published, active procedure can be locked out against."
        )
    if not proc.publishedVersionId:
        raise LotoError(
            f"Procedure {proc.procedureCode} has never been published — publish it first."
        )

    version = await db.get(LotoProcedureVersion, proc.publishedVersionId)
    if version is None:
        raise LotoError(
            "The published version of this procedure is missing. Re-publish it "
            "before starting a lockout.",
            status_code=500,
        )

    ex = LotoExecution(
        number=await next_execution_number(db),
        procedureId=proc.id,
        procedureVersionId=version.id,
        # THE FREEZE. Copied here, once, and never refreshed. dict(...) so the
        # execution never shares a mutable object with the version row.
        procedureVersionSnapshot=dict(version.snapshotJson or {}),
        snapshotVersion=version.version,
        ptwId=ptw_id,
        ptwNumber=ptw_number,
        siteId=proc.siteId,
        siteName=proc.siteName,
        initiatedById=actor_id,
        initiatedByName=actor_name,
        isGroupLockout=is_group,
        status="locks_applied",
    )
    db.add(ex)
    await db.flush()

    rows = list(participants)
    if not rows:
        # Solo lockout — the initiator is the primary authorised person.
        built = [
            LotoExecutionParticipant(
                executionId=ex.id,
                userId=actor_id,
                userName=actor_name,
                participantRole="primary_authorized",
                assignedIsolationPointIds=[
                    p["id"] for p in (ex.procedureVersionSnapshot.get("isolationPoints") or [])
                ],
            )
        ]
    else:
        seen: set[str] = set()
        built = []
        for p in rows:
            if p.userId in seen:
                raise LotoError(
                    "The same person is listed twice on this lockout. Each person "
                    "appears once and confirms their own lock."
                )
            seen.add(p.userId)
            built.append(
                LotoExecutionParticipant(
                    executionId=ex.id,
                    userId=p.userId,
                    participantRole=p.participantRole,
                    assignedIsolationPointIds=list(p.assignedIsolationPointIds or []),
                    lockTagNumber=p.lockTagNumber,
                    notes=p.notes,
                )
            )

    if not [r for r in built if r.participantRole in LOCK_HOLDER_ROLES]:
        raise LotoError(
            "A lockout needs at least one lock holder. Every listed person is an "
            "affected employee, who is notified rather than issued a lock."
        )

    # ⚠ Add the children individually and PUBLISH the collections with
    # `set_committed_value` — never `ex.participants = [...]`.
    #
    # `ex` is persistent (we just flushed it) and both collections cascade
    # delete-orphan, so ASSIGNING to one makes SQLAlchemy first LOAD the
    # existing contents to work out what became an orphan. That load is lazy
    # I/O in an async session, which raises MissingGreenlet and surfaced as a
    # blanket 500 on "Start lockout".
    #
    # set_committed_value states the fact without any I/O: this row is brand
    # new, so its collections are known-empty and these are their contents.
    for row in built:
        db.add(row)
    set_committed_value(ex, "participants", built)
    set_committed_value(ex, "verificationRecords", [])

    await db.flush()
    return ex


def _own_row(ex: LotoExecution, user_id: str, participant_id: str | None) -> LotoExecutionParticipant:
    """Resolve the caller's OWN participant row.

    `participantId` is accepted so the mobile client can be explicit, but it is
    cross-checked against the authenticated user and a mismatch is refused. This
    is the API-layer half of "no single action removes another person's lock" —
    the UI half is not trusted.
    """
    row = next((p for p in ex.participants if p.userId == user_id), None)
    if row is None:
        raise LotoError(
            "You are not enrolled on this lockout, so you have no lock to confirm. "
            "Ask the person who started it to add you.",
            status_code=403,
        )
    if participant_id and participant_id != row.id:
        raise LotoError(
            "You can only confirm your own lock. Each person on a group lockout "
            "confirms individually.",
            status_code=403,
        )
    if row.participantRole not in LOCK_HOLDER_ROLES:
        raise LotoError(
            "You are recorded as an affected employee on this lockout — you are "
            "notified of it, but hold no lock to confirm.",
            status_code=409,
        )
    return row


async def confirm_lock(
    db: AsyncSession,
    ex: LotoExecution,
    *,
    user_id: str,
    participant_id: str | None,
    lock_tag_number: str | None,
    notes: str | None,
) -> LotoExecutionParticipant:
    if ex.status in {"closed", "aborted"}:
        raise LotoError("This lockout is already finished.")
    if ex.status != "locks_applied":
        raise LotoError(
            f"Locks have already been accounted for on this lockout (status: "
            f"{ex.status.replace('_', ' ')})."
        )

    row = _own_row(ex, user_id, participant_id)
    if row.lockAppliedConfirmed:
        raise LotoError("You have already confirmed your lock on this equipment.")

    row.lockAppliedConfirmed = True
    row.lockAppliedAt = _now()
    if lock_tag_number:
        row.lockTagNumber = lock_tag_number
    if notes:
        row.notes = notes
    await db.flush()
    return row


async def confirm_unlock(
    db: AsyncSession,
    ex: LotoExecution,
    *,
    user_id: str,
    participant_id: str | None,
    notes: str | None,
) -> LotoExecutionParticipant:
    """Reverse of confirm_lock, and deliberately its mirror image.

    In particular there is no "remove all" counterpart, and no supervisor
    override: a supervisor who could clear another person's lock in one action
    is the failure mode lockout procedure exists to prevent. If a lock genuinely
    must come off without its owner, that is an ABORT with a recorded reason —
    a visible exception, not a silent one.
    """
    if ex.status in {"closed", "aborted"}:
        raise LotoError("This lockout is already finished.")
    if ex.status not in {"verified", "work_in_progress", "locks_removed"}:
        raise LotoError(
            "Zero-energy verification has to be completed before locks come off "
            f"(status: {ex.status.replace('_', ' ')})."
        )

    row = _own_row(ex, user_id, participant_id)
    if not row.lockAppliedConfirmed:
        raise LotoError(
            "You never confirmed applying a lock, so there is nothing to remove."
        )
    if row.lockRemovedConfirmed:
        raise LotoError("You have already confirmed removing your lock.")

    row.lockRemovedConfirmed = True
    row.lockRemovedAt = _now()
    if notes:
        row.notes = notes

    # Status advances ONLY when every lock holder has confirmed individually.
    if all(p.lockRemovedConfirmed for p in lock_holders(ex)):
        ex.status = "locks_removed"
        ex.locksRemovedAt = _now()
    await db.flush()
    return row


async def record_verification(
    db: AsyncSession,
    ex: LotoExecution,
    *,
    records: list[Any],
    user_id: str,
    user_name: str | None,
) -> list[LotoVerificationRecord]:
    """Complete one or more zero-energy verification steps.

    Steps are validated against the FROZEN snapshot, so a step deleted from the
    live procedure mid-job is still completable here, and a step ADDED to the
    live procedure mid-job is not silently demanded of a crew that never saw it.
    """
    if ex.status in {"closed", "aborted"}:
        raise LotoError("This lockout is already finished.")
    if ex.status not in {"locks_applied", "verified"}:
        raise LotoError(
            f"Verification does not apply at this stage (status: "
            f"{ex.status.replace('_', ' ')})."
        )

    holders = lock_holders(ex)
    unconfirmed = [p.userName or p.userId for p in holders if not p.lockAppliedConfirmed]
    if unconfirmed:
        raise LotoError(
            "Every lock must be on the equipment before zero-energy verification. "
            "Outstanding: " + ", ".join(unconfirmed)
        )

    steps_by_id = {s["id"]: s for s in _snapshot_steps(ex) if s.get("id")}
    existing = {r.stepId: r for r in ex.verificationRecords}
    written: list[LotoVerificationRecord] = []

    for payload in records:
        step = steps_by_id.get(payload.stepId)
        if step is None:
            raise LotoError(
                f"Step {payload.stepId} is not part of the procedure version this "
                f"lockout is running (v{ex.snapshotVersion}).",
                status_code=422,
            )
        if step.get("requiresSignoff") and not payload.signoff:
            raise LotoError(
                f"Step {step.get('sequence')} requires a sign-off.", status_code=422
            )
        if step.get("requiresPhoto") and not payload.photoUrl:
            raise LotoError(
                f"Step {step.get('sequence')} requires a photo.", status_code=422
            )

        row = existing.get(payload.stepId)
        if row is None:
            row = LotoVerificationRecord(
                executionId=ex.id,
                stepId=payload.stepId,
                sequence=step.get("sequence", 0),
                stepText=step.get("stepText"),
                completedById=user_id,
                completedByName=user_name,
            )
            db.add(row)
            ex.verificationRecords.append(row)
        row.completedById = user_id
        row.completedByName = user_name
        row.completedAt = _now()
        row.signoff = payload.signoff
        row.photoUrl = payload.photoUrl
        row.notes = payload.notes
        written.append(row)

    await db.flush()

    # Promote to `verified` only when the whole checklist is satisfied — the gate
    # is the single authority on "whole", so this cannot drift from what closure
    # will later demand.
    gate = execution_gate(ex)
    if not gate["outstandingVerificationSteps"] and ex.status == "locks_applied":
        ex.status = "verified"
        await db.flush()
    return written


async def start_work(db: AsyncSession, ex: LotoExecution) -> LotoExecution:
    if ex.status != "verified":
        raise LotoError(
            "Zero-energy verification must be complete before work starts "
            f"(status: {ex.status.replace('_', ' ')})."
        )
    ex.status = "work_in_progress"
    ex.workStartedAt = _now()
    await db.flush()
    return ex


async def close_execution(
    db: AsyncSession, ex: LotoExecution, *, user_id: str, notes: str | None
) -> LotoExecution:
    """Final sign-off. Refuses, with specifics, on an incomplete record.

    This is spec §2.9 and checklist items 3/4: a lockout cannot be closed while a
    participant has not confirmed, or a verification step is outstanding. The
    refusal names WHO and WHAT so the closer can go and fix it, rather than
    receiving a flat "cannot close".
    """
    if ex.status == "closed":
        raise LotoError("This lockout is already closed.")
    if ex.status == "aborted":
        raise LotoError("This lockout was aborted and cannot be closed.")

    gate = execution_gate(ex)
    if not gate["canClose"]:
        detail = gate["blockers"] or ["The lockout record is incomplete."]
        raise LotoError(
            "This lockout cannot be closed yet:\n• " + "\n• ".join(detail)
        )

    ex.status = "closed"
    ex.closedById = user_id
    ex.closedAt = _now()
    ex.closureNotes = notes
    await db.flush()
    return ex


async def abort_execution(
    db: AsyncSession, ex: LotoExecution, *, user_id: str, reason: str
) -> LotoExecution:
    """The recorded exception path.

    Exists precisely so that "we had to cut a lock off" is a visible, attributed
    event with a reason attached, instead of pressure to build a silent override
    into the normal close path.
    """
    if ex.status in {"closed", "aborted"}:
        raise LotoError("This lockout is already finished.")
    ex.status = "aborted"
    ex.abortedById = user_id
    ex.abortedAt = _now()
    ex.abortReason = reason
    await db.flush()
    return ex


async def procedure_changed_since_start(db: AsyncSession, ex: LotoExecution) -> bool:
    """Has the library moved on under a running job?

    Informational ONLY. It never changes what the crew is following — that is
    the frozen snapshot — but a supervisor should know the procedure was edited
    mid-lockout.
    """
    proc = await db.get(LotoProcedure, ex.procedureId)
    if proc is None:
        return False
    return proc.version != ex.snapshotVersion


# ═══════════════════════════════════════════════════════════════════════════
#  PTW cross-reference (spec §6)
# ═══════════════════════════════════════════════════════════════════════════


async def open_execution_for_permit(
    db: AsyncSession, permit_id: str
) -> LotoExecution | None:
    """The linked execution, if it is still holding locks.

    THE single source of truth for the PTW closure gate. `workflow_engine` calls
    this rather than re-deriving "open" from a status list of its own, so the two
    modules cannot end up disagreeing about what open means.
    """
    from app.models.permit import Permit

    permit = await db.get(Permit, permit_id)
    if permit is None or not getattr(permit, "lotoExecutionId", None):
        return None
    ex = await db.get(LotoExecution, permit.lotoExecutionId)
    if ex is None or ex.isDeleted:
        return None
    return ex if ex.status in OPEN_EXECUTION_STATUSES else None


async def permit_closure_blocker(db: AsyncSession, permit_id: str) -> str | None:
    """Message to show, or None if the permit is free to close."""
    ex = await open_execution_for_permit(db, permit_id)
    if ex is None:
        return None
    return (
        f"Lockout {ex.number} linked to this permit is still open "
        f"({ex.status.replace('_', ' ')}). Close the lockout before closing the "
        f"permit — equipment cannot be handed back while locks are on it."
    )


__all__ = [
    "LotoError",
    "next_procedure_code",
    "next_execution_number",
    "new_qr_token",
    "load_procedure",
    "body_snapshot",
    "publish_blockers",
    "replace_body",
    "record_version",
    "apply_body_edit",
    "compute_next_review",
    "review_state",
    "review_status_fields",
    "run_review_scan",
    "load_execution",
    "lock_holders",
    "execution_gate",
    "start_execution",
    "confirm_lock",
    "confirm_unlock",
    "record_verification",
    "start_work",
    "close_execution",
    "abort_execution",
    "procedure_changed_since_start",
    "open_execution_for_permit",
    "permit_closure_blocker",
    "DUE_SOON_DAYS",
]
