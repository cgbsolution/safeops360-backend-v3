"""Form & Workflow Engine — the form primitive (Part A §3).

Two tables. `FormDefinition` is the template (versioned, published, authored as
config today and by the no-code Builder later); `FormRecord` is one filled-in
instance of it. Everything else the engine needs already exists on the platform
and is REUSED rather than cloned:

  * approvals / tasks / history → the existing WorkflowDefinition / WorkflowStep /
    WorkflowInstance / WorkflowTask / WorkflowHistory engine in
    `app/services/workflow_engine.py`. A FormRecord submits into it exactly the
    way a Permit or an Observation does, so a form's approvals land in the SAME
    inbox every other module uses. Building a second set of workflow tables
    would have forked the platform's inbox and audit history in two.
  * attachments → the shared `Attachment` table via the evidence registry
    (`form_record` EntitySpec). No per-form attachment clone.
  * audit trail → the P1-1 tamper-evident hash-chain (`register_audited`).
  * RBAC → `app/services/permissions.py`, scoped by plant, with each definition
    naming its own permission prefix so a module gets its own codes without a
    line of engine code changing.

WHY VERSIONS ARE ROWS, NOT A CHILD TABLE
A published definition is immutable. Editing one inserts a new row with the same
`key` and `version + 1`; `FormRecord.formVersion` pins the version a record was
created against, so a record is always validated against the schema it was
authored under even after three more versions publish. That makes
(key, version) the natural primary identity and a separate version table
redundant.

WHY `computedJson` IS A SEPARATE COLUMN FROM `dataJson`
Calculated fields are derived server-side on every write and never read back
from the client payload. Keeping them out of `dataJson` means a client that
POSTs `{"co2e_emissions": 0}` cannot overwrite a computed disclosure figure —
the merge is one-way, and what the client sent is simply dropped. Storing them
in the same blob would have made that a validation rule that a later refactor
could quietly lose; here it is a property of the schema.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models._base import Base, IdMixin, SoftDeleteMixin

# ── Definition lifecycle ────────────────────────────────────────────────────
DEF_DRAFT = "DRAFT"
DEF_PUBLISHED = "PUBLISHED"
DEF_ARCHIVED = "ARCHIVED"
DEFINITION_STATUSES = (DEF_DRAFT, DEF_PUBLISHED, DEF_ARCHIVED)

# ── Record lifecycle ────────────────────────────────────────────────────────
# Deliberately a small, fixed set. The *interesting* states of a record live in
# its WorkflowInstance (which step, whose task, what SLA); this column is the
# coarse status a register screen filters on, kept in sync by the engine.
REC_DRAFT = "DRAFT"
REC_SUBMITTED = "SUBMITTED"
REC_IN_REVIEW = "IN_REVIEW"
REC_APPROVED = "APPROVED"
REC_REJECTED = "REJECTED"
REC_CLOSED = "CLOSED"
RECORD_STATUSES = (
    REC_DRAFT,
    REC_SUBMITTED,
    REC_IN_REVIEW,
    REC_APPROVED,
    REC_REJECTED,
    REC_CLOSED,
)
# A record in one of these is locked: its data is final and further writes are
# refused. §7 requires an approved sustainability period to be immutable.
RECORD_LOCKED_STATUSES = (REC_APPROVED, REC_REJECTED, REC_CLOSED)


class FormDefinition(Base, IdMixin, SoftDeleteMixin):
    """One version of one form template."""

    __tablename__ = "FormDefinition"

    # Stable slug shared across versions: 'kaizen', 'sustainability_energy'.
    key: Mapped[str] = mapped_column(String, nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String, nullable=False, default=DEF_DRAFT, index=True)

    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # Grouping only — 'SUSTAINABILITY', 'BUSINESS_EXCELLENCE'. Not a licence code.
    module: Mapped[str] = mapped_column(String, nullable=False, index=True)

    # The ordered field list. Shape is validated by services/form_engine/schema.py
    # on publish, so an unpublishable definition can never reach a record.
    schemaJson: Mapped[list | None] = mapped_column(JSONB)
    # Layout hints (sections, column spans). Never affects validation — a
    # renderer may ignore it entirely and the form still behaves identically.
    uiSchemaJson: Mapped[dict | None] = mapped_column(JSONB)

    # ── approval workflow (the EXISTING engine) ──
    # `workflowModule` is the value the workflow engine matches on
    # WorkflowDefinition.module; `workflowRecordType` selects among several
    # definitions for that module. NULL workflowModule = no approval, the record
    # is complete on submit. Two forms naming the SAME workflowModule share one
    # WorkflowDefinition — which is exactly what §8 asks of the four Business
    # Excellence registers.
    workflowModule: Mapped[str | None] = mapped_column(String, index=True)
    workflowRecordType: Mapped[str | None] = mapped_column(String)

    # 'KAIZEN-{YYYY}-{####}'. See services/form_engine/numbering.py.
    numberPattern: Mapped[str | None] = mapped_column(String)

    # WHERE this form's data physically lives. {"kind": "NATIVE"} stores rows in
    # FormRecord.dataJson. The other kinds bind a definition to a pre-existing
    # platform table (BRSR's environmental capture, ERM's RootCauseAnalysis) so
    # the engine can sit in FRONT of a data model it did not create instead of
    # standing up a duplicate one. Only NATIVE is implemented in this phase; the
    # column exists now so adding a binding is a resolver entry, not a migration
    # against tables that are already filed against.
    storageBinding: Mapped[dict | None] = mapped_column(JSONB)

    # {"plantIds": [...]} — which sites may use this form. NULL = every site.
    orgScope: Mapped[dict | None] = mapped_column(JSONB)

    # Permission codes are '<prefix>.READ' / '.CREATE' / '.UPDATE' / '.PUBLISH'.
    # Per-definition so the Sustainability forms can be gated by SUSTAINABILITY.*
    # while a Business Excellence register uses its own codes — without the
    # engine knowing either module exists.
    permissionPrefix: Mapped[str] = mapped_column(String, nullable=False, default="FORMS")

    createdById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )
    publishedById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    publishedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("key", "version", name="uq_FormDefinition_key_version"),
        Index("ix_FormDefinition_key_status", "key", "status"),
        Index("ix_FormDefinition_module_status", "module", "status"),
    )


class FormRecord(Base, IdMixin, SoftDeleteMixin):
    """One submitted instance of a FormDefinition version."""

    __tablename__ = "FormRecord"

    definitionId: Mapped[str] = mapped_column(
        ForeignKey("FormDefinition.id"), nullable=False, index=True
    )
    # Denormalised so the register screen can filter by form without a join, and
    # so a record still identifies its form after the definition is archived.
    definitionKey: Mapped[str] = mapped_column(String, nullable=False, index=True)
    formVersion: Mapped[int] = mapped_column(Integer, nullable=False)
    module: Mapped[str] = mapped_column(String, nullable=False, index=True)

    # House convention: never render a raw Plant cuid. `siteName` is frozen at
    # write time so a closed record keeps the site name it was filed under even
    # after the plant is renamed.
    siteId: Mapped[str] = mapped_column(String, nullable=False, index=True)
    siteName: Mapped[str | None] = mapped_column(String)
    areaId: Mapped[str | None] = mapped_column(String, index=True)

    # Client-supplied field values, validated against the PINNED version.
    dataJson: Mapped[dict | None] = mapped_column(JSONB)
    # Server-derived values. Never merged from the client payload. See the
    # module docstring for why this is a separate column.
    computedJson: Mapped[dict | None] = mapped_column(JSONB)

    status: Mapped[str] = mapped_column(String, nullable=False, default=REC_DRAFT, index=True)
    # 'KAIZEN-2026-0142'. Assigned on submit, not on draft creation — an
    # abandoned draft must not burn a number out of the register's sequence.
    referenceNo: Mapped[str | None] = mapped_column(String, index=True)

    # The existing engine's WorkflowInstance. NULL until submit, and permanently
    # NULL for a definition with no workflowModule.
    workflowInstanceId: Mapped[str | None] = mapped_column(String, index=True)

    createdById: Mapped[str] = mapped_column(ForeignKey("User.id"), nullable=False, index=True)
    createdAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updatedById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    updatedAt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False
    )
    submittedById: Mapped[str | None] = mapped_column(ForeignKey("User.id"))
    submittedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closedAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    definition: Mapped[FormDefinition] = relationship(lazy="raise")

    __table_args__ = (
        # Partial-free unique: two records may both have a NULL referenceNo
        # (drafts), but two SUBMITTED records may never share a number. Postgres
        # treats NULLs as distinct, which gives us exactly that for free.
        UniqueConstraint("definitionKey", "referenceNo", name="uq_FormRecord_key_reference"),
        # The register screen's default query: one form, newest first
        # (platform list-sort convention is createdAt DESC).
        Index("ix_FormRecord_key_created", "definitionKey", "createdAt"),
        Index("ix_FormRecord_site_status", "siteId", "status"),
        Index("ix_FormRecord_module_status", "module", "status"),
    )


__all__ = [
    "FormDefinition",
    "FormRecord",
    "DEF_DRAFT",
    "DEF_PUBLISHED",
    "DEF_ARCHIVED",
    "DEFINITION_STATUSES",
    "REC_DRAFT",
    "REC_SUBMITTED",
    "REC_IN_REVIEW",
    "REC_APPROVED",
    "REC_REJECTED",
    "REC_CLOSED",
    "RECORD_STATUSES",
    "RECORD_LOCKED_STATUSES",
]
