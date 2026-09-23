"""Form & Workflow Engine router. Mounted at /api/forms.

  Meta
    GET    /api/forms/meta                       — field types, operators, functions

  Definitions (the template)
    GET    /api/forms/definitions                — list (module/status filters)
    POST   /api/forms/definitions                — create v1 draft
    GET    /api/forms/definitions/{key}          — the published version (or latest)
    GET    /api/forms/definitions/{key}/versions — every version
    PATCH  /api/forms/definitions/{id}           — edit a DRAFT
    POST   /api/forms/definitions/{id}/publish   — DRAFT → PUBLISHED (runs the gate)
    POST   /api/forms/definitions/{key}/new-version — PUBLISHED → a fresh draft
    POST   /api/forms/definitions/{id}/archive   — withdraw from use
    POST   /api/forms/definitions/validate       — dry-run the gate (Builder preview)

  Records (the instance)
    GET    /api/forms/records                    — register list, plant-scoped
    POST   /api/forms/records                    — create draft (optionally submit)
    GET    /api/forms/records/{id}               — detail + the PINNED definition
    PATCH  /api/forms/records/{id}               — edit a draft
    POST   /api/forms/records/{id}/submit        — into the platform workflow engine
    DELETE /api/forms/records/{id}               — soft-delete (governed)

PERMISSIONS ARE PER-DEFINITION, NOT PER-ROUTER.
Each definition names a `permissionPrefix`, and every endpoint checks
'<prefix>.READ' / '.CREATE' / '.UPDATE' / '.PUBLISH' / '.DELETE'. That is what
lets Sustainability ship gated by SUSTAINABILITY.* and a Business Excellence
register by its own codes without the engine knowing either module exists — the
§11 rule that a client requirement must be expressible as configuration.

Plant scoping is fail-closed on every list via access_scope.build_query_scope
and re-checked per record on read and write, matching LOTO/HIRA.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.core.soft_delete import soft_delete
from app.models.form_engine import (
    DEF_ARCHIVED,
    DEF_DRAFT,
    DEF_PUBLISHED,
    REC_DRAFT,
    FormDefinition,
    FormRecord,
)
from app.models.plant import Plant
from app.models.user import User
from app.schemas.form_engine import (
    DefinitionCreate,
    DefinitionOut,
    DefinitionSummary,
    DefinitionUpdate,
    RecordCreate,
    RecordDetail,
    RecordListOut,
    RecordOut,
    RecordUpdate,
    SubmitOut,
)
from app.services.access_scope import build_query_scope
from app.services.form_engine.binding import BindingError
from app.services.form_engine.binding import normalise as normalise_binding
from app.services.form_engine.formula import FUNCTIONS
from app.services.form_engine.numbering import PatternError
from app.services.form_engine.records import (
    RecordError,
    apply_data,
)
from app.services.form_engine.records import (
    submit as submit_record,
)
from app.services.form_engine.schema import (
    CONDITION_OPERATORS,
    FIELD_TYPES,
    SchemaError,
    validate_definition,
)
from app.services.form_engine.validation import ValidationError, required_but_missing
from app.services.permissions import PermissionContext, can

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/forms", tags=["forms"])


# ── permission helpers ──────────────────────────────────────────────────────


async def _require(
    db: AsyncSession,
    user: User,
    definition: FormDefinition,
    verb: str,
    *,
    plant_id: str | None = None,
) -> None:
    code = f"{definition.permissionPrefix}.{verb}"
    result = await can(db, user.id, code, PermissionContext(plant_id=plant_id))
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or f"Requires {code}")


async def _require_any_authoring(db: AsyncSession, user: User, prefix: str, verb: str) -> None:
    """For endpoints that act before a definition exists (create) — the prefix
    comes from the payload, so it is checked directly."""
    code = f"{prefix}.{verb}"
    result = await can(db, user.id, code, PermissionContext())
    if not result.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, result.reason or f"Requires {code}")


def _check_org_scope(definition: FormDefinition, site_id: str) -> None:
    scope = definition.orgScope or {}
    allowed = scope.get("plantIds")
    if isinstance(allowed, list) and allowed and site_id not in allowed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"'{definition.title}' is not enabled for this site.",
        )


async def _load_definition_or_404(db: AsyncSession, definition_id: str) -> FormDefinition:
    d = await db.get(FormDefinition, definition_id)
    if d is None or d.isDeleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Form definition not found")
    return d


async def _published(db: AsyncSession, key: str) -> FormDefinition | None:
    """The live version of a form: the highest PUBLISHED version.

    Highest rather than 'the one publish() last touched' — publishing v3 while
    v2 is live must not leave two rows both claiming to be current, and taking
    the max makes that impossible to get wrong from the data alone.
    """
    return (
        await db.execute(
            select(FormDefinition)
            .where(FormDefinition.key == key, FormDefinition.status == DEF_PUBLISHED)
            .order_by(FormDefinition.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


# ── meta ────────────────────────────────────────────────────────────────────


@router.get("/meta")
async def meta(
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Everything the Builder palette and the config author need, served from
    the engine itself so the UI can never offer a capability the engine lacks
    (§6.3)."""
    from app.models.workflow import WorkflowDefinition as WfDef

    # The attachable workflows, grouped by the `module` value a definition binds
    # to. Grouped rather than listed one-row-per-definition because that IS the
    # sharing model: several WorkflowDefinitions may exist for one module
    # (selected by recordType), and several FORMS naming the same module all
    # run on it. That is how the four Business Excellence registers share a
    # single workflow — so the picker offers modules, not definitions.
    rows = (
        await db.execute(
            select(WfDef.module, WfDef.recordType, WfDef.name)
            .where(WfDef.isActive.is_(True))
            .order_by(WfDef.module, WfDef.recordType)
        )
    ).all()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for module_code, record_type, name in rows:
        grouped.setdefault(module_code, []).append({"recordType": record_type, "name": name})

    return {
        "fieldTypes": sorted(FIELD_TYPES),
        "conditionOperators": sorted(CONDITION_OPERATORS),
        "formulaFunctions": sorted(FUNCTIONS),
        "numberPatternTokens": ["{YYYY}", "{YY}", "{MM}", "{SITE}", "{####}"],
        "definitionStatuses": [DEF_DRAFT, DEF_PUBLISHED, DEF_ARCHIVED],
        "storageBindings": ["NATIVE"],
        "workflowModules": [
            {"module": m, "definitions": defs} for m, defs in sorted(grouped.items())
        ],
    }


# ── definitions ─────────────────────────────────────────────────────────────


@router.get("/definitions", response_model=list[DefinitionSummary])
async def list_definitions(
    module: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[DefinitionSummary]:
    stmt = select(FormDefinition).where(FormDefinition.isDeleted.is_(False))
    if module:
        stmt = stmt.where(FormDefinition.module == module)
    if status_filter:
        stmt = stmt.where(FormDefinition.status == status_filter)
    rows = (await db.execute(stmt.order_by(FormDefinition.updatedAt.desc()))).scalars().all()

    visible = [
        d for d in rows
        if (await can(db, user.id, f"{d.permissionPrefix}.READ", PermissionContext())).allowed
    ]
    if not visible:
        return []

    counts = dict(
        (
            await db.execute(
                select(FormRecord.definitionKey, func.count(FormRecord.id))
                .where(FormRecord.definitionKey.in_([d.key for d in visible]))
                .group_by(FormRecord.definitionKey)
            )
        ).all()
    )
    out: list[DefinitionSummary] = []
    for d in visible:
        s = DefinitionSummary.model_validate(d)
        s.recordCount = int(counts.get(d.key, 0))
        out.append(s)
    return out


@router.post("/definitions", response_model=DefinitionOut, status_code=201)
async def create_definition(
    body: DefinitionCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FormDefinition:
    await _require_any_authoring(db, user, body.permissionPrefix, "PUBLISH")

    existing = (
        await db.execute(select(FormDefinition.id).where(FormDefinition.key == body.key).limit(1))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"A form with key '{body.key}' already exists. Use /new-version to revise it.",
        )

    binding = _normalise_binding_or_422(body.storageBinding)
    d = FormDefinition(
        key=body.key,
        version=1,
        status=DEF_DRAFT,
        title=body.title,
        description=body.description,
        module=body.module,
        schemaJson=body.schemaJson,
        uiSchemaJson=body.uiSchemaJson,
        workflowModule=body.workflowModule,
        workflowRecordType=body.workflowRecordType,
        numberPattern=body.numberPattern,
        storageBinding=binding,
        orgScope=body.orgScope,
        permissionPrefix=body.permissionPrefix,
        createdById=user.id,
    )
    db.add(d)
    await db.commit()
    await db.refresh(d)
    return d


def _normalise_binding_or_422(raw: Any) -> dict[str, Any]:
    try:
        return normalise_binding(raw)
    except BindingError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e


@router.post("/definitions/validate")
async def validate_only(
    body: dict[str, Any],
    _: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Dry-run the publish gate. The Builder calls this on every change so an
    author sees the same errors publishing would raise, before publishing."""
    try:
        validate_definition(body.get("schemaJson"), number_pattern=body.get("numberPattern"))
    except (SchemaError, PatternError) as e:
        return {"ok": False, "errors": str(e).split(" | ")}
    return {"ok": True, "errors": []}


@router.get("/definitions/{key}", response_model=DefinitionOut)
async def get_definition(
    key: str,
    version: int | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FormDefinition:
    if version is not None:
        d = (
            await db.execute(
                select(FormDefinition).where(
                    FormDefinition.key == key, FormDefinition.version == version
                )
            )
        ).scalar_one_or_none()
    else:
        d = await _published(db, key)
        if d is None:
            # No published version yet — fall back to the latest draft so the
            # Builder can open a form that has never been published.
            d = (
                await db.execute(
                    select(FormDefinition)
                    .where(FormDefinition.key == key, FormDefinition.isDeleted.is_(False))
                    .order_by(FormDefinition.version.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
    if d is None or d.isDeleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Form definition not found")
    await _require(db, user, d, "READ")
    return d


@router.get("/definitions/{key}/versions", response_model=list[DefinitionOut])
async def list_versions(
    key: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[FormDefinition]:
    rows = (
        await db.execute(
            select(FormDefinition)
            .where(FormDefinition.key == key, FormDefinition.isDeleted.is_(False))
            .order_by(FormDefinition.version.desc())
        )
    ).scalars().all()
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Form definition not found")
    await _require(db, user, rows[0], "READ")
    return list(rows)


@router.patch("/definitions/{definition_id}", response_model=DefinitionOut)
async def update_definition(
    definition_id: str,
    body: DefinitionUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FormDefinition:
    d = await _load_definition_or_404(db, definition_id)
    await _require(db, user, d, "PUBLISH")
    if d.status != DEF_DRAFT:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"v{d.version} is {d.status.lower()} and is immutable. "
            "POST /definitions/{key}/new-version to revise it.",
        )
    patch = body.model_dump(exclude_unset=True)
    if "storageBinding" in patch:
        patch["storageBinding"] = _normalise_binding_or_422(patch["storageBinding"])
    for k, v in patch.items():
        setattr(d, k, v)
    await db.commit()
    await db.refresh(d)
    return d


@router.post("/definitions/{definition_id}/publish", response_model=DefinitionOut)
async def publish_definition(
    definition_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FormDefinition:
    """DRAFT → PUBLISHED. The gate that runs here is the SAME one the Builder's
    preview calls and the same one a config-seeded definition passes, which is
    what makes §10's "identical behaviour" structural rather than aspirational."""
    d = await _load_definition_or_404(db, definition_id)
    await _require(db, user, d, "PUBLISH")
    if d.status != DEF_DRAFT:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Only a draft can be published (this is {d.status}).")

    try:
        validate_definition(d.schemaJson, number_pattern=d.numberPattern)
    except (SchemaError, PatternError) as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e

    # A binding that cannot be executed must not become live: records against it
    # would fail one by one at write time instead of failing once, here.
    try:
        from app.services.form_engine.binding import resolve as resolve_binding

        resolve_binding(d.storageBinding)
    except BindingError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e

    if d.workflowModule:
        from app.models.workflow import WorkflowDefinition as WfDef

        wf = (
            await db.execute(
                select(WfDef.id).where(WfDef.module == d.workflowModule, WfDef.isActive.is_(True)).limit(1)
            )
        ).scalar_one_or_none()
        if wf is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"No active workflow definition exists for module '{d.workflowModule}'. "
                "Publishing would make every submission fail at the approval step.",
            )

    # Supersede the version this one replaces. Without it two versions would sit
    # PUBLISHED and _published()'s max would be the only thing keeping them apart.
    prior = await _published(db, d.key)
    if prior is not None and prior.id != d.id:
        prior.status = DEF_ARCHIVED

    d.status = DEF_PUBLISHED
    d.publishedById = user.id
    d.publishedAt = func.now()
    await db.commit()
    await db.refresh(d)
    return d


@router.post("/definitions/{key}/new-version", response_model=DefinitionOut, status_code=201)
async def new_version(
    key: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FormDefinition:
    """Clone the latest version into a fresh DRAFT. Records already created keep
    pointing at the version they pinned, so revising a live form never disturbs
    a record that is mid-approval."""
    latest = (
        await db.execute(
            select(FormDefinition)
            .where(FormDefinition.key == key, FormDefinition.isDeleted.is_(False))
            .order_by(FormDefinition.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Form definition not found")
    await _require(db, user, latest, "PUBLISH")
    if latest.status == DEF_DRAFT:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"v{latest.version} is already an open draft — edit it instead of branching a second one.",
        )

    d = FormDefinition(
        key=latest.key,
        version=latest.version + 1,
        status=DEF_DRAFT,
        title=latest.title,
        description=latest.description,
        module=latest.module,
        schemaJson=latest.schemaJson,
        uiSchemaJson=latest.uiSchemaJson,
        workflowModule=latest.workflowModule,
        workflowRecordType=latest.workflowRecordType,
        numberPattern=latest.numberPattern,
        storageBinding=latest.storageBinding,
        orgScope=latest.orgScope,
        permissionPrefix=latest.permissionPrefix,
        createdById=user.id,
    )
    db.add(d)
    await db.commit()
    await db.refresh(d)
    return d


@router.post("/definitions/{definition_id}/archive", response_model=DefinitionOut)
async def archive_definition(
    definition_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FormDefinition:
    d = await _load_definition_or_404(db, definition_id)
    await _require(db, user, d, "PUBLISH")
    d.status = DEF_ARCHIVED
    await db.commit()
    await db.refresh(d)
    return d


# ── records ─────────────────────────────────────────────────────────────────


@router.get("/records", response_model=RecordListOut)
async def list_records(
    definitionKey: str | None = None,
    module: str | None = None,
    status_filter: str | None = Query(default=None, alias="status"),
    siteId: str | None = None,
    limit: int = Query(default=50, le=200),
    offset: int = 0,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RecordListOut:
    # Each definition carries its OWN permission prefix, so plant scope has to be
    # resolved per prefix and the results ORed — not resolved once against
    # FORMS.READ. A Sustainability user holding SUSTAINABILITY.READ but no
    # FORMS.* grant would otherwise get a fail-closed empty scope and see an
    # empty register with no error, which reads as "no data" rather than
    # "no permission". `definitionKey` is just the one-prefix case of the same
    # thing.
    if definitionKey:
        d = await _published(db, definitionKey) or (
            await db.execute(
                select(FormDefinition)
                .where(FormDefinition.key == definitionKey)
                .order_by(FormDefinition.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if d is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Form definition not found")
        await _require(db, user, d, "READ")
        keys_by_prefix: dict[str, set[str]] = {d.permissionPrefix: {d.key}}
    else:
        defs = (
            await db.execute(select(FormDefinition).where(FormDefinition.isDeleted.is_(False)))
        ).scalars().all()
        keys_by_prefix = {}
        for d in defs:
            if (await can(db, user.id, f"{d.permissionPrefix}.READ", PermissionContext())).allowed:
                keys_by_prefix.setdefault(d.permissionPrefix, set()).add(d.key)
        if not keys_by_prefix:
            return RecordListOut(items=[], total=0)

    # Fail-closed plant scoping, same as every other register — one clause per
    # prefix, each pairing that prefix's forms with the plants the caller may
    # read them in.
    clauses = []
    for prefix, keys in keys_by_prefix.items():
        scope = await build_query_scope(db, user.id, f"{prefix}.READ")
        if scope.all_plants:
            clauses.append(FormRecord.definitionKey.in_(keys))
        elif scope.plant_ids:
            clauses.append(
                and_(
                    FormRecord.definitionKey.in_(keys),
                    FormRecord.siteId.in_(scope.plant_ids),
                )
            )
        # No plants on this prefix → contributes nothing, deliberately.
    if not clauses:
        return RecordListOut(items=[], total=0)

    stmt = select(FormRecord).where(FormRecord.isDeleted.is_(False)).where(or_(*clauses))
    if module:
        stmt = stmt.where(FormRecord.module == module)
    if status_filter:
        stmt = stmt.where(FormRecord.status == status_filter)
    if siteId:
        stmt = stmt.where(FormRecord.siteId == siteId)

    total = (
        await db.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    rows = (
        await db.execute(
            # Platform list-sort convention: newest first.
            stmt.order_by(FormRecord.createdAt.desc()).offset(offset).limit(limit)
        )
    ).scalars().all()
    return RecordListOut(items=[RecordOut.model_validate(r) for r in rows], total=int(total))


@router.post("/records", response_model=RecordDetail, status_code=201)
async def create_record(
    body: RecordCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RecordDetail:
    d = await _published(db, body.definitionKey)
    if d is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"No published version of form '{body.definitionKey}'.",
        )
    await _require(db, user, d, "CREATE", plant_id=body.siteId)
    _check_org_scope(d, body.siteId)

    plant = await db.get(Plant, body.siteId)
    if plant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Site not found")

    rec = FormRecord(
        definitionId=d.id,
        definitionKey=d.key,
        formVersion=d.version,
        module=d.module,
        siteId=plant.id,
        siteName=plant.name,
        areaId=body.areaId,
        status=REC_DRAFT,
        createdById=user.id,
    )
    try:
        apply_data(rec, d, body.data, require_all=False)
    except ValidationError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=_verr(e)) from e
    except (RecordError, BindingError) as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e

    db.add(rec)
    await db.flush()

    if body.submit:
        await _do_submit(db, rec, d, user, plant)

    await db.commit()
    await db.refresh(rec)
    return _detail(rec, d)


@router.get("/records/{record_id}", response_model=RecordDetail)
async def get_record(
    record_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RecordDetail:
    rec = await db.get(FormRecord, record_id)
    if rec is None or rec.isDeleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Record not found")
    # The PINNED version, not the latest — the renderer must draw the form this
    # record was created under.
    d = await db.get(FormDefinition, rec.definitionId)
    if d is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "The form definition for this record is missing.")
    await _require(db, user, d, "READ", plant_id=rec.siteId)
    return _detail(rec, d)


@router.patch("/records/{record_id}", response_model=RecordDetail)
async def update_record(
    record_id: str,
    body: RecordUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RecordDetail:
    rec = await db.get(FormRecord, record_id)
    if rec is None or rec.isDeleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Record not found")
    d = await db.get(FormDefinition, rec.definitionId)
    if d is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "The form definition for this record is missing.")
    await _require(db, user, d, "UPDATE", plant_id=rec.siteId)

    if body.areaId is not None:
        rec.areaId = body.areaId
    if body.data is not None:
        try:
            apply_data(rec, d, body.data, require_all=False)
        except ValidationError as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=_verr(e)) from e
        except (RecordError, BindingError) as e:
            raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    rec.updatedById = user.id
    await db.commit()
    await db.refresh(rec)
    return _detail(rec, d)


@router.post("/records/{record_id}/submit", response_model=SubmitOut)
async def submit_endpoint(
    record_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SubmitOut:
    rec = await db.get(FormRecord, record_id)
    if rec is None or rec.isDeleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Record not found")
    d = await db.get(FormDefinition, rec.definitionId)
    if d is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "The form definition for this record is missing.")
    await _require(db, user, d, "UPDATE", plant_id=rec.siteId)

    plant = await db.get(Plant, rec.siteId)
    await _do_submit(db, rec, d, user, plant)
    await db.commit()
    await db.refresh(rec)
    return SubmitOut(
        recordId=rec.id,
        referenceNo=rec.referenceNo,
        status=rec.status,  # type: ignore[arg-type]
        workflowInstanceId=rec.workflowInstanceId,
    )


@router.delete("/records/{record_id}", status_code=204)
async def delete_record(
    record_id: str,
    reason: str = Query(min_length=10),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    rec = await db.get(FormRecord, record_id)
    if rec is None or rec.isDeleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Record not found")
    d = await db.get(FormDefinition, rec.definitionId)
    if d is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "The form definition for this record is missing.")
    await _require(db, user, d, "DELETE", plant_id=rec.siteId)
    soft_delete(rec, user.id, reason)
    await db.commit()


# ── shared bits ─────────────────────────────────────────────────────────────


async def _do_submit(
    db: AsyncSession, rec: FormRecord, d: FormDefinition, user: User, plant: Plant | None
) -> None:
    from app.services.workflow_engine import WorkflowError

    try:
        await submit_record(
            db, record=rec, definition=d, actor_id=user.id, site_code=getattr(plant, "code", None)
        )
    except ValidationError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=_verr(e)) from e
    except (RecordError, BindingError) as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    except WorkflowError as e:
        # The record must NOT be left SUBMITTED with no instance — that is a
        # record nobody owns and no inbox shows. Rolling back to draft keeps it
        # with its author, who can resubmit once the workflow is configured.
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Approval workflow could not be started: {e}",
        ) from e


def _verr(e: ValidationError) -> Any:
    return {"message": "The form has validation errors.", "errors": e.errors}


def _detail(rec: FormRecord, d: FormDefinition) -> RecordDetail:
    detail = RecordDetail.model_validate(
        {
            **RecordOut.model_validate(rec).model_dump(),
            "definition": DefinitionOut.model_validate(d).model_dump(),
            "missingRequired": required_but_missing(d.schemaJson, rec.dataJson),
            "canEdit": rec.status == REC_DRAFT,
        }
    )
    return detail
