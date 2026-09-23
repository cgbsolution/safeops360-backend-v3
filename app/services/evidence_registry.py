"""Evidence Attachment entity registry (Stream B §5).

Maps an `entityType` string to everything the generic attachment router needs to
serve it safely: the SQLAlchemy model (to prove the parent exists + resolve its
plant), the permission codes to gate read vs write, and the allowed upload
categories. Adding a new attachable module (EAI SDS, Contractor certs, Training
certs, …) is one `EntitySpec` here — the router never changes.

This is where the shared capability earns its keep: the spec names four priority
modules (CAMS/Statutory, EAI, Contractor, Training); each is a registry line.
This pass wires CAMS first (highest compliance cost, spec §5.3).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.audit_compliance import (
    AuditCheckpointResponse,
    ComplianceAudit,
)
from app.models.business_excellence import BeKaizen, BeOpl, BePokaYoke
from app.models.cams import CamsFinding
from app.models.form_engine import FormRecord
from app.models.programme import ProgrammeCycle


@dataclass(frozen=True)
class EntitySpec:
    label: str
    model: type
    # Column on the model row holding the plant/site id for the permission
    # PermissionContext, or None for platform-scoped entities.
    plant_attr: str | None
    read_perm: str
    write_perm: str
    # Allowed per-entity `category` values for an upload.
    categories: frozenset[str]


REGISTRY: dict[str, EntitySpec] = {
    # ── CAMS audit-finding evidence (spec §5.3 #1 — CAMS/Statutory) ──────────
    # Attach the source document that substantiates a finding / its closure.
    "cams_finding": EntitySpec(
        label="Audit finding",
        model=CamsFinding,
        plant_attr="siteId",
        read_perm="CAMS.READ",
        write_perm="CAMS.FINDING_MANAGE",
        categories=frozenset(
            {"FINDING_EVIDENCE", "CLOSURE_EVIDENCE", "CERTIFICATE", "LICENSE", "REPORT", "OTHER"}
        ),
    ),
    # ── WP-26: audit-side evidence on the SAME platform layer ────────────────
    #
    # The audit engine stored evidence as raw storage paths in a JSON array
    # (`AuditCheckpointResponse.auditorEvidenceIds`) with no FK, no versioning,
    # no uploader and no soft-delete — which is why 15 photos across 2,503
    # checkpoints all pointed at 2 placeholder paths and nothing noticed (F-25,
    # F-26, F-54). Registering the audit entities here gives them the same
    # `Attachment` guarantees the inspection side already had, without a second
    # upload path to maintain.
    #
    # The legacy JSON array is NOT removed: existing rows still reference it and
    # the conduct screen still writes it. New evidence lands in `Attachment`;
    # the two are read together until a later migration folds the old paths in.
    "audit_checkpoint": EntitySpec(
        label="Audit checkpoint",
        model=AuditCheckpointResponse,
        plant_attr="plantId",
        read_perm="AUDIT_COMPLIANCE.READ",
        write_perm="AUDIT_COMPLIANCE.EXECUTE",
        categories=frozenset(
            {"FINDING_EVIDENCE", "CLOSURE_EVIDENCE", "OBSERVATION_PHOTO",
             "CERTIFICATE", "LICENSE", "OTHER"}
        ),
    ),
    # Engagement-level documents that belong to the audit rather than to any one
    # checkpoint: the signed report, meeting minutes, the auditee's response pack.
    "compliance_audit": EntitySpec(
        label="Audit",
        model=ComplianceAudit,
        plant_attr="plantId",
        read_perm="AUDIT_COMPLIANCE.READ",
        write_perm="AUDIT_COMPLIANCE.UPDATE",
        categories=frozenset(
            {"REPORT", "MEETING_MINUTES", "SIGNED_REPORT", "AUDITEE_RESPONSE",
             "CERTIFICATE", "CORRESPONDENCE", "OTHER"}
        ),
    ),
    # Programme-cycle documents: the approved programme document, the management
    # review minutes behind a ProgrammeReview, external-body schedules.
    "programme_cycle": EntitySpec(
        label="Programme cycle",
        model=ProgrammeCycle,
        plant_attr=None,  # a cycle spans sites; permission is platform-scoped
        read_perm="CAMS.READ",
        write_perm="CAMS.SCHEDULE",
        categories=frozenset(
            {"PROGRAMME_DOCUMENT", "REVIEW_MINUTES", "EXTERNAL_SCHEDULE",
             "CORRESPONDENCE", "OTHER"}
        ),
    ),
    # ── Form & Workflow Engine records ───────────────────────────────────────
    # A `file` / `signature` field on any form stores its bytes here rather than
    # in a per-form attachment table — which is why §3.2's proposed
    # `form_record_attachments` was not built. One registry line gives EVERY
    # form ever authored (Sustainability, Kaizen, OPL, and whatever a customer
    # builds in the Builder next year) versioning, soft-delete, an uploader and
    # the tamper-evident hash-chain, with no engine code per form.
    #
    # `category` is intentionally broad: the engine cannot know what categories
    # a form invented at authoring time, and the field's own `key` is the real
    # slot discriminator (passed as `slotKey`, which is what versions a re-upload
    # against the same field).
    "form_record": EntitySpec(
        label="Form record",
        model=FormRecord,
        plant_attr="siteId",
        # Coarse codes rather than the definition's own prefix: the generic
        # attachment router resolves permissions from this table alone and has
        # no definition in hand. The record-level plant scope still applies, and
        # the form's own endpoints remain gated by its specific prefix.
        read_perm="FORMS.READ",
        write_perm="FORMS.UPDATE",
        categories=frozenset(
            {"FORM_FIELD", "SUPPORTING_EVIDENCE", "SIGNATURE", "PHOTO",
             "CERTIFICATE", "REPORT", "OTHER"}
        ),
    ),
    # ── Business Excellence ──────────────────────────────────────────────────
    #
    # A Kaizen without a before-and-after photograph is a paragraph of prose
    # nobody can evaluate, and a Poka Yoke device record without a picture of
    # the fitted device is unverifiable at the machine. Both ride the shared
    # Attachment table rather than growing their own image columns.
    "be_kaizen": EntitySpec(
        label="Kaizen",
        model=BeKaizen,
        plant_attr="plantId",
        read_perm="KAIZEN.READ",
        write_perm="KAIZEN.UPDATE",
        categories=frozenset(
            {"BEFORE_PHOTO", "AFTER_PHOTO", "SUPPORTING_EVIDENCE", "SAVINGS_EVIDENCE",
             "DRAWING", "REPORT", "OTHER"}
        ),
    ),
    # The OPL's own visual content. `contentHtml` holds the text; the sketch,
    # photo or annotated diagram that makes it a ONE POINT lesson lives here —
    # deliberately not inlined as a base64 data URI, which is how a lesson
    # becomes a page nobody can load on plant wifi.
    "be_opl": EntitySpec(
        label="One Point Lesson",
        model=BeOpl,
        plant_attr="plantId",
        read_perm="OPL.READ",
        write_perm="OPL.UPDATE",
        categories=frozenset(
            {"LESSON_VISUAL", "PHOTO", "DIAGRAM", "SUPPORTING_EVIDENCE", "OTHER"}
        ),
    ),
    "be_poka_yoke": EntitySpec(
        label="Poka Yoke device",
        model=BePokaYoke,
        plant_attr="plantId",
        read_perm="POKAYOKE.READ",
        # VERIFY, not UPDATE: the person recording a periodic check needs to
        # attach the photograph that proves it, and they are not necessarily the
        # person entitled to edit the device record.
        write_perm="POKAYOKE.VERIFY",
        categories=frozenset(
            {"DEVICE_PHOTO", "BEFORE_PHOTO", "AFTER_PHOTO", "VERIFICATION_EVIDENCE",
             "DRAWING", "OTHER"}
        ),
    ),
    # ── Follow-ups (spec §5.3 #2-4) — each is a single line once wired: ──────
    #   "eai_entry":        EAI SDS sheets      → read EAI.READ  / write EAI.UPDATE
    #   "contractor":       insurance/comp certs→ read EPC.READ  / write EPC.UPDATE
    #   "training_record":  training certs      → read TRAINING.READ / write TRAINING.UPDATE
}


def get_spec(entity_type: str) -> EntitySpec | None:
    return REGISTRY.get(entity_type)


def supported_entities() -> list[str]:
    return sorted(REGISTRY)
