"""Signal Engine — shared read-only data access.

Deliberately NOT a second query layer. The correlation rules of Streams 2/3
call the existing module services and the existing insight helpers
(`app.services.insights.common`) for record-level reads; what lives here is the
part those layers genuinely do not have — schema-level introspection for the
data-quality rules, and the tenant/site scope resolution every rule shares.

Everything here is read-only. Nothing writes, nothing calls out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean as SABoolean
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Identifiers reach raw SQL below (there is no bind-parameter form for a column
# name). They originate in SQLAlchemy metadata, not user input, but they are
# still validated — an interpolation path with no guard is how the next person's
# "just add a config option" becomes an injection.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def q(ident: str) -> str:
    """Quote a validated SQL identifier."""
    if not _IDENT_RE.match(ident):
        raise ValueError(f"unsafe SQL identifier: {ident!r}")
    return f'"{ident}"'


# ── Module attribution ───────────────────────────────────────────────────────
# A table's owning module is derived from the python model file that declares
# it (`cls.__module__`), so this map never drifts as tables are added — a new
# table in app/models/hira.py is HIRA on the day it is written.
MODULE_BY_MODEL_FILE: dict[str, str] = {
    "observation": "OBSERVATION",
    "observation_severity": "OBSERVATION",
    "observation_sla": "OBSERVATION",
    "near_miss": "NEAR_MISS",
    "near_miss_children": "NEAR_MISS",
    "incident": "INCIDENT",
    "incident_intel": "INCIDENT",
    "permit": "PTW",
    "flra": "PTW",
    "gas_test": "PTW",
    "hira": "HIRA",
    "cams": "CAMS_AUDIT",
    "cams_completion": "CAMS_AUDIT",
    "audit_compliance": "CAMS_AUDIT",
    "assurance": "CAMS_AUDIT",
    "programme": "CAMS_AUDIT",
    "moc": "MOC",
    "capa": "CAPA",
    "training": "TRAINING",
    "training_engine": "TRAINING",
    "competency_matrix": "TRAINING",
    "loto": "LOTO",
    "eai": "EAI",
    "erm": "ERM",
    "erm_p2": "ERM",
    "erm_p3": "ERM",
    "erm_t3": "ERM",
    "rca": "RCA",
    "safety_culture": "SAFETY_CULTURE",
    "ppe": "PPE",
    "epc": "EPC",
    "fire_safety": "FIRE",
    "capture": "CAPTURE",
    "form_engine": "FORMS",
    "factory": "FACILITIES",
    "factory_ext": "FACILITIES",
    "brsr": "BRSR",
    "inspection_finding": "INSPECTION",
    "equipment": "INSPECTION",
    "kaizen": "KAIZEN",
    "sci": "SCI",
    "scr": "SCR",
    "manhours": "MANHOURS",
}

# Tables the engine never scans for data quality:
#   • its own tables (a signal about the signal store is noise),
#   • append-only infrastructure (audit chain, job runs, outbox events),
#   • masters/lookup tables (sparse by design — a lookup row legitimately
#     fills two of nine columns).
SKIP_TABLES: frozenset[str] = frozenset({
    "Signal", "SignalEvidence", "SignalRule", "SignalRuleOverride", "SignalRunLog",
    "InsightSnapshot", "AuditLog", "JobRun", "DomainEvent", "Notification",
    "AgentInvocation", "AgentToolCall", "AgentPrompt",
    "_prisma_migrations",
})

# Columns skipped by the zero-row detector. Each is legitimately empty on a
# healthy dataset, so flagging it would train the reader to ignore the panel —
# which is the failure mode this rule exists to prevent.
SKIP_COLUMN_NAMES: frozenset[str] = frozenset({
    "id", "createdAt", "updatedAt",
    "isDeleted", "deletedAt", "deletedBy", "deletionReason",
    "cancelledAt", "cancelledBy", "cancellationReason",
    "rejectedAt", "rejectedBy", "rejectionReason",
    "reversedAt", "reversedBy",
    "archivedAt", "supersededAt", "supersededBy",
    "error", "errorDetail", "lastError", "errorCount",
})
SKIP_COLUMN_PREFIXES: tuple[str, ...] = ("deleted", "cancelled", "rejected", "reversed", "superseded")

# Column names that make a good human ref for evidence (house rule: render a
# record ref, never a raw cuid).
_REF_EXACT = ("code", "number", "title", "name", "reference")
_REF_SUFFIX = ("Number", "Ref", "Code")


@dataclass(frozen=True)
class TableSpec:
    table: str
    module: str
    columns: tuple[str, ...]          # scannable columns (skips applied)
    all_columns: frozenset[str]
    # Boolean columns need a different test for "populated" — see the note on
    # _POPULATED_SQL below. Carried separately so the rule can also word its
    # narrative correctly ("never set true" vs "never written").
    bool_columns: frozenset[str]
    ref_column: str | None
    has_created_at: bool


def _ref_column(cols: list[str]) -> str | None:
    for c in cols:
        if c in _REF_EXACT:
            return c
    for c in cols:
        if any(c.endswith(s) for s in _REF_SUFFIX) and not c.endswith("Id"):
            return c
    for c in ("title", "name", "summary"):
        if c in cols:
            return c
    return None


def _load_all_models() -> None:
    """Import EVERY module under app.models so Base.metadata is complete.

    `import app.models` is NOT sufficient and trusting it was a real bug: the
    package's `__init__` re-exports most models but not all, and the omissions
    are invisible — `moc`, `loto`, `rca`, `training_engine`, `fire_safety`,
    `capture`, `form_engine`, `kaizen`, `inspection_finding` and others are
    absent, which silently excluded 58 tables from the scan. A data-quality rule
    that quietly under-scans is the precise failure mode it exists to catch, so
    this walks the package instead of trusting the re-export list.
    """
    import importlib
    import pkgutil

    import app.models as models_pkg

    for m in pkgutil.iter_modules(models_pkg.__path__):
        if m.name.startswith("_"):
            continue
        try:
            importlib.import_module(f"app.models.{m.name}")
        except Exception:  # noqa: BLE001 — one unimportable model must not blind the whole scan
            continue


def scannable_tables(modules: set[str] | None = None) -> list[TableSpec]:
    """Every mapped table attributable to a business module, with its scannable
    columns."""
    _load_all_models()

    from app.core.db import Base

    by_table: dict[str, str] = {}
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        file = cls.__module__.rsplit(".", 1)[-1]
        module = MODULE_BY_MODEL_FILE.get(file)
        if module:
            by_table.setdefault(cls.__tablename__, module)

    out: list[TableSpec] = []
    for name, tbl in Base.metadata.tables.items():
        module = by_table.get(name)
        if module is None or name in SKIP_TABLES:
            continue
        if modules and module not in modules:
            continue
        all_cols = [c.name for c in tbl.columns]
        scannable = tuple(
            c for c in all_cols
            if c not in SKIP_COLUMN_NAMES and not c.startswith(SKIP_COLUMN_PREFIXES)
        )
        if not scannable:
            continue
        bools = frozenset(
            c.name for c in tbl.columns
            if c.name in scannable and isinstance(c.type, SABoolean)
        )
        out.append(TableSpec(
            table=name,
            module=module,
            columns=scannable,
            all_columns=frozenset(all_cols),
            bool_columns=bools,
            ref_column=_ref_column(all_cols),
            has_created_at="createdAt" in all_cols,
        ))
    return sorted(out, key=lambda t: (t.module, t.table))


# ── Population scanning ──────────────────────────────────────────────────────
# "Populated" is deliberately stricter than NOT NULL. A field written as `''`,
# `'{}'` or `'[]'` by a form that collects nothing is exactly as dead as a NULL
# one, and the manual diagnostics this rule automates always counted it that way.
_POPULATED_SQL = (
    'count(*) FILTER (WHERE {col} IS NOT NULL '
    "AND btrim({col}::text) NOT IN ('', '{{}}', '[]', 'null')) AS {alias}"
)
# Booleans need their own test, and getting this wrong would have made the whole
# rule miss its headline case. `HiraHazard.requiresPermit` is NOT NULL DEFAULT
# false: every row "has a value", so a NULL/empty test scores it 100% populated
# while the truth is that no user has ever ticked it — 0 of 167. For a flag,
# "populated" means SET, i.e. true at least once.
_POPULATED_BOOL_SQL = "count(*) FILTER (WHERE {col} IS TRUE) AS {alias}"


@dataclass
class TablePopulation:
    table: str
    total: int
    populated: dict[str, int]
    oldest_row: datetime | None


async def live_columns(db: AsyncSession) -> dict[str, frozenset[str]]:
    """table → the columns the DATABASE actually has.

    The scanner builds its column list from SQLAlchemy metadata, which is the
    developer's intent, not the deployed reality. On this platform those diverge:
    hand-DDL is applied per module and a model can carry a column the database
    has never been given. `KaizenPost` maps five such columns, and because
    SQLAlchemy names every mapped column in its projection, that made XCORR-019
    raise `UndefinedColumnError` and — through the runner's rollback cascade —
    take the entire nightly run down with it, every night, unnoticed.

    So every generated projection is intersected with this. The drift itself is
    not swallowed: it is a finding, reported by XDQ-021.
    """
    rows = (
        await db.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public'"
            )
        )
    ).all()
    out: dict[str, set[str]] = {}
    for t, c in rows:
        out.setdefault(t, set()).add(c)
    return {t: frozenset(c) for t, c in out.items()}


@dataclass(frozen=True)
class SchemaDrift:
    """A mapped table/column the database does not have."""

    table: str
    module: str
    missing_columns: tuple[str, ...]
    table_missing: bool
    row_count: int


async def schema_drift(db: AsyncSession) -> list[SchemaDrift]:
    """Mapped tables and columns absent from the database.

    Severity is not cosmetic here: a mapped column the database lacks breaks
    EVERY query against that entity, not just the ones that read the column,
    because SQLAlchemy projects all mapped columns. The screen 500s and the
    cause is invisible from the UI.
    """
    live = await live_columns(db)
    specs = {s.table: s for s in scannable_tables()}
    out: list[SchemaDrift] = []
    for table, spec in specs.items():
        if table not in live:
            out.append(SchemaDrift(table, spec.module, (), True, 0))
            continue
        gap = tuple(sorted(spec.all_columns - live[table]))
        if gap:
            n = (await db.execute(text(f"SELECT count(*) FROM {q(table)}"))).scalar() or 0  # noqa: S608
            out.append(SchemaDrift(table, spec.module, gap, False, int(n)))
    return sorted(out, key=lambda d: (not d.table_missing, -d.row_count, d.table))


async def scan_population(db: AsyncSession, spec: TableSpec) -> TablePopulation | None:
    """One query per table: total rows, populated count per scannable column,
    and the oldest row (the age gate's evidence). Returns None if the table is
    absent from this database — an unapplied module must not fail a whole run."""
    if not await table_exists(db, spec.table):
        return None

    # Only project columns the database actually has. Without this one line the
    # rule crashes on the first drifted table and, via the runner, kills the run.
    live = (await live_columns(db)).get(spec.table, frozenset())
    cols = tuple(c for c in spec.columns if c in live)
    if not cols:
        return None

    aliases = {c: f"c{i}" for i, c in enumerate(cols)}
    parts = [
        (_POPULATED_BOOL_SQL if c in spec.bool_columns else _POPULATED_SQL).format(col=q(c), alias=a)
        for c, a in aliases.items()
    ]
    has_created = spec.has_created_at and "createdAt" in live
    oldest = f", min({q('createdAt')}) AS oldest" if has_created else ""
    sql = f'SELECT count(*) AS total, {", ".join(parts)}{oldest} FROM {q(spec.table)}'  # noqa: S608 — identifiers validated by q()
    row = (await db.execute(text(sql))).mappings().one()
    return TablePopulation(
        table=spec.table,
        total=int(row["total"] or 0),
        populated={c: int(row[a] or 0) for c, a in aliases.items()},
        oldest_row=row.get("oldest") if has_created else None,
    )


async def sample_ids_missing(
    db: AsyncSession, spec: TableSpec, column: str, *, limit: int = 5
) -> list[tuple[str, str | None]]:
    """(id, ref) for a few rows where `column` is unpopulated — the evidence a
    reviewer clicks through to. Ordered newest-first where possible: a recent
    row proves the gap is current, not a legacy backfill artefact."""
    ref = q(spec.ref_column) if spec.ref_column else "NULL"
    order = f" ORDER BY {q('createdAt')} DESC" if spec.has_created_at else ""
    if column in spec.bool_columns:
        missing = f"{q(column)} IS NOT TRUE"
    else:
        missing = f"{q(column)} IS NULL OR btrim({q(column)}::text) IN ('', '{{}}', '[]', 'null')"
    sql = (
        f'SELECT {q("id")} AS id, {ref} AS ref FROM {q(spec.table)} '  # noqa: S608 — identifiers validated by q()
        f"WHERE {missing}"
        f"{order} LIMIT :lim"
    )
    rows = (await db.execute(text(sql), {"lim": limit})).mappings().all()
    return [(str(r["id"]), (str(r["ref"]) if r["ref"] is not None else None)) for r in rows]


async def table_exists(db: AsyncSession, table: str) -> bool:
    return bool(
        (
            await db.execute(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name=:t"
                ),
                {"t": table},
            )
        ).first()
    )


async def column_exists(db: AsyncSession, table: str, column: str) -> bool:
    return bool(
        (
            await db.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name=:t AND column_name=:c"
                ),
                {"t": table, "c": column},
            )
        ).first()
    )


# ── Reference integrity ──────────────────────────────────────────────────────
@dataclass
class DanglingRef:
    source_id: str
    source_ref: str | None
    target_id: str
    kind: str  # "MISSING" | "SOFT_DELETED"


async def check_reference_integrity(
    db: AsyncSession,
    *,
    source_table: str,
    source_id_col: str,
    source_ref_col: str | None,
    target_tables: tuple[str, ...],
    discriminator_col: str | None = None,
    discriminator_value: str | None = None,
    limit: int = 25,
) -> tuple[int, int, list[DanglingRef]]:
    """(missing, soft_deleted, samples) for one unenforced reference.

    Only UNENFORCED references are worth checking: a declared foreign key is
    already guaranteed by Postgres, so the interesting cases are the string
    columns that hold an id without a constraint — polymorphic source pointers,
    golden-thread targets, generic entity links. Those are precisely where a
    reference rots silently.

    `target_tables` is a tuple because several of this platform's polymorphic
    pointers are legitimately satisfied by more than one table. An AUDIT_INTERNAL
    CAPA, for instance, points at a `CamsFinding` when it came from the CAMS
    engine and at an `AuditCheckpointResponse` when it came from the
    ComplianceAudit engine — checking against either alone would report every
    CAPA from the other engine as broken. A reference is dangling only when it
    resolves in NONE of the candidate tables.

    A soft-deleted target is reported separately from a missing one because the
    remedies differ: a missing target is data loss, a soft-deleted one is a
    record still pointing at something a user consciously withdrew.
    """
    if not await table_exists(db, source_table):
        return (0, 0, [])
    live_targets = [t for t in target_tables if await table_exists(db, t)]
    if not live_targets:
        return (0, 0, [])

    branches = []
    for t in live_targets:
        soft = "COALESCE(" + q("isDeleted") + ", false)" if await column_exists(db, t, "isDeleted") else "false"
        branches.append(f'SELECT {q("id")} AS id, {soft} AS del FROM {q(t)}')  # noqa: S608 — identifiers validated by q()
    targets_cte = " UNION ALL ".join(branches)

    where = [f"s.{q(source_id_col)} IS NOT NULL", f"btrim(s.{q(source_id_col)}) <> ''"]
    params: dict[str, Any] = {"lim": limit}
    if discriminator_col and discriminator_value is not None:
        where.append(f"s.{q(discriminator_col)} = :disc")
        params["disc"] = discriminator_value
    # The source itself must be live — a soft-deleted CAPA pointing at a
    # soft-deleted incident is consistent history, not an integrity defect.
    if await column_exists(db, source_table, "isDeleted"):
        where.append(f"COALESCE(s.{q('isDeleted')}, false) = false")
    where_sql = " AND ".join(where)

    ref_sel = f"s.{q(source_ref_col)}" if source_ref_col else "NULL"
    # A reference is fine if ANY candidate target holds a live row for it.
    base = (
        f"WITH targets AS ({targets_cte}), "  # noqa: S608 — identifiers validated by q()
        f"joined AS ("
        f"  SELECT s.{q('id')} AS sid, {ref_sel} AS sref, s.{q(source_id_col)} AS tid,"
        f"         count(t.id) AS n_found,"
        f"         COALESCE(bool_or(NOT t.del), false) AS has_live"
        f"  FROM {q(source_table)} s LEFT JOIN targets t ON t.id = s.{q(source_id_col)}"
        f"  WHERE {where_sql}"
        f"  GROUP BY 1, 2, 3"
        f"), "
        f"classified AS ("
        f"  SELECT sid, sref, tid,"
        f"         CASE WHEN n_found = 0 THEN 'MISSING' ELSE 'SOFT_DELETED' END AS kind"
        f"  FROM joined WHERE has_live = false"
        f")"
    )

    counts = (
        await db.execute(text(f"{base} SELECT kind, count(*) AS n FROM classified GROUP BY 1"), params)  # noqa: S608
    ).mappings().all()
    missing = next((int(r["n"]) for r in counts if r["kind"] == "MISSING"), 0)
    deleted = next((int(r["n"]) for r in counts if r["kind"] == "SOFT_DELETED"), 0)
    if not (missing or deleted):
        return (0, 0, [])

    samples = (
        await db.execute(text(f"{base} SELECT * FROM classified LIMIT :lim"), params)  # noqa: S608
    ).mappings().all()
    return (
        missing,
        deleted,
        [
            DanglingRef(
                source_id=str(r["sid"]),
                source_ref=(str(r["sref"]) if r["sref"] is not None else None),
                target_id=str(r["tid"]),
                kind=str(r["kind"]),
            )
            for r in samples
        ],
    )


async def distinct_values(db: AsyncSession, table: str, column: str, *, limit: int = 200) -> list[str]:
    """Distinct non-null values of a discriminator column.

    XCORR-020 uses this to discover which polymorphic codes a deployment
    ACTUALLY holds rather than assuming the set from code — a code the registry
    has never heard of is reported as unmapped coverage, never as a defect."""
    if not await table_exists(db, table) or not await column_exists(db, table, column):
        return []
    rows = (
        await db.execute(
            text(
                f"SELECT DISTINCT {q(column)} AS v FROM {q(table)} "  # noqa: S608 — identifiers validated by q()
                f"WHERE {q(column)} IS NOT NULL LIMIT :lim"
            ),
            {"lim": limit},
        )
    ).mappings().all()
    return sorted(str(r["v"]) for r in rows)


__all__ = [
    "DanglingRef",
    "MODULE_BY_MODEL_FILE",
    "TablePopulation",
    "TableSpec",
    "check_reference_integrity",
    "column_exists",
    "distinct_values",
    "q",
    "sample_ids_missing",
    "scan_population",
    "scannable_tables",
    "table_exists",
]
