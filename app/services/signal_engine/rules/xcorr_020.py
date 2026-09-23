"""XCORR-020 — cross-module reference integrity (spec §3, rule 20).

Checks the references Postgres CANNOT check for us. A declared foreign key is
already enforced by the database, so it is never interesting here. What is
interesting is the platform's polymorphic pointers — `Capa.sourceReferenceId`
discriminated by `sourceTypeCode`, `GoldenThreadLink.targetId` discriminated by
`targetType`, `Attachment.entityId` discriminated by `entityType`. Those are
plain strings with no constraint behind them, so they rot in silence: the CAPA
still lists, the golden thread still renders, and the row it points at is gone.

Two properties make this rule safe to leave running against a live tenant:

* **It never invents coverage it does not have.** The discriminator values are
  read from the database at run time, not assumed from code. A code the registry
  has never heard of is reported as unmapped COVERAGE in the run log — never as
  a defect. A rule that shouted "broken!" at every code it did not recognise
  would be worse than no rule.
* **Multi-table targets are honoured.** An AUDIT_INTERNAL CAPA legitimately
  points at a CamsFinding or an AuditCheckpointResponse depending on which
  engine raised it; a reference is dangling only when it resolves in none of
  its candidate tables.

Audience is engineering (spec §3): DATA_QUALITY signals are excluded from the
executive Daily Brief and surface only on the admin panel.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.signal_engine import data_access as da
from app.services.signal_engine.base import (
    EvidenceRef,
    RuleContext,
    SignalCandidate,
    SignalRuleImpl,
)


def _a(noun: str) -> str:
    """Indefinite article. These narratives are read by clients and auditors;
    "a audit finding" undermines the whole claim that the deterministic template
    is publishable as-is."""
    return "an" if noun[:1].lower() in "aeiou" else "a"


@dataclass(frozen=True)
class RefCheck:
    key: str
    label: str                       # human phrasing of the SOURCE side
    source_module: str
    source_table: str
    source_id_col: str
    source_ref_col: str | None
    target_tables: tuple[str, ...]
    target_label: str                # human phrasing of the TARGET side
    discriminator_col: str | None = None
    discriminator_value: str | None = None


# ── CAPA polymorphic source (verified against the code paths that write it) ──
# Each entry: sourceTypeCode → (candidate target tables, human label).
_CAPA_SOURCE_TARGETS: dict[str, tuple[tuple[str, ...], str]] = {
    "SAFETY_INCIDENT": (("Incident",), "incident"),
    "SAFETY_OBSERVATION": (("Observation",), "observation"),
    "NEAR_MISS": (("NearMiss",), "near miss"),
    "ENTERPRISE_RCA": (("RootCauseAnalysis",), "root cause analysis"),
    "RISK_TREATMENT": (("EnterpriseRisk",), "enterprise risk"),
    "INSPECTION_FINDING": (("InspectionFinding",), "inspection finding"),
    "MOC_ACTION": (("ChangeRequest",), "change request"),
    "KAIZEN_INITIATIVE": (("KaizenPost",), "kaizen post"),
    "HIRA_CONTROL": (("HiraEntryRecommendedControl", "HiraEntryControl"), "HIRA control"),
    # The two audit engines are unified at the CAPA layer but not at the record
    # layer — see the note in the module docstring.
    "AUDIT_INTERNAL": (("CamsFinding", "AuditCheckpointResponse", "AuditFinding"), "audit finding"),
    "AUDIT_EXTERNAL": (("CamsFinding", "AuditCheckpointResponse", "AuditFinding"), "audit finding"),
    "AUDIT_REGULATORY": (("CamsFinding", "AuditCheckpointResponse", "AuditFinding"), "audit finding"),
    "BC_EXERCISE": (("ExerciseFinding",), "business-continuity exercise finding"),
    "COMPLIANCE": (("ComplianceTask",), "compliance task"),
    "CONTROL_DEFICIENCY": (("ControlDeficiency",), "control deficiency"),
    # Culture CAPAs are spawned from two different records depending on which
    # screen raised them (a behaviour observation or a leadership walk).
    "SAFETY_CULTURE": (("Observation", "LeadershipWalk"), "culture record"),
}

# Codes whose `sourceReferenceId` is NOT a table row id. Checking these would
# manufacture a permanent false positive — and a data-quality rule that cries
# wolf every night is worse than no rule.
#
# VENDOR_RISK is the live example: its reference is the id of an element inside
# `VendorAssessment.findings`, a JSON array, so there is no table for the id to
# resolve against. That is itself a modelling weakness worth writing down, but
# it is a Stream 2+ finding about the vendor module, not an integrity defect
# this rule can honestly report.
_NON_TABLE_REFERENCES: dict[str, str] = {
    "VENDOR_RISK": "references an element inside VendorAssessment.findings (JSON), not a table row",
}

# ── Golden-thread targets (app/services/golden_thread.py) ────────────────────
# `audit_checkpoint` carries two candidates because the writer falls back to the
# audit's own id when the ad-hoc checkpoint helper returns no checkpoint;
# `training_assignment` points at a TrainingRegistration, not TrainingAssignment.
_GOLDEN_THREAD_TARGETS: dict[str, tuple[tuple[str, ...], str]] = {
    "risk_register": (("EnterpriseRisk",), "enterprise risk"),
    "capa": (("Capa",), "CAPA"),
    "training_assignment": (("TrainingRegistration", "TrainingAssignment"), "training registration"),
    "audit_checkpoint": (("AuditCheckpointResponse", "ComplianceAudit"), "audit checkpoint"),
}


class Xcorr020ReferenceIntegrity(SignalRuleImpl):
    code = "XCORR-020"
    name = "Cross-module reference integrity"
    description = (
        "Flags unenforced cross-module references (polymorphic source pointers, "
        "golden-thread targets, generic attachment links) that resolve to a "
        "missing or soft-deleted record. Engineering audience, not safety."
    )
    rule_class = "DATA_QUALITY"
    category = "DATA_QUALITY"
    default_severity = "INFO"
    source_modules = ("CAPA", "INCIDENT", "CAMS_AUDIT", "ERM", "TRAINING", "PLATFORM")
    window_days = 0  # whole-dataset state, not a rolling window
    default_thresholds: dict[str, Any] = {
        # Below this many broken references, the finding is a single bad row for
        # someone to fix by hand, not a pattern worth a signal.
        "minBroken": 1,
        "sampleSize": 10,
    }

    async def evaluate(self, ctx: RuleContext) -> list[SignalCandidate]:
        t = ctx.thresholds
        min_broken = int(t["minBroken"])
        sample = int(t["sampleSize"])

        checks, unmapped = await self._build_checks(ctx)
        if unmapped:
            ctx.notes["unmappedReferenceCodes"] = unmapped
        ctx.notes["checksRun"] = len(checks)

        out: list[SignalCandidate] = []
        for chk in checks:
            missing, deleted, samples = await da.check_reference_integrity(
                ctx.db,
                source_table=chk.source_table,
                source_id_col=chk.source_id_col,
                source_ref_col=chk.source_ref_col,
                target_tables=chk.target_tables,
                discriminator_col=chk.discriminator_col,
                discriminator_value=chk.discriminator_value,
                limit=sample,
            )
            broken = missing + deleted
            if broken < min_broken:
                continue
            out.append(SignalCandidate(
                signalKey=f"REF::{chk.key}",
                severity=self.default_severity,
                confidence=self._confidence(missing, deleted),
                facts={
                    "checkKey": chk.key,
                    "sourceModule": chk.source_module,
                    "sourceLabel": chk.label,
                    "sourceTable": chk.source_table,
                    "sourceColumn": chk.source_id_col,
                    "targetLabel": chk.target_label,
                    "targetTables": list(chk.target_tables),
                    "missing": missing,
                    "softDeleted": deleted,
                    "broken": broken,
                },
                evidence=[
                    EvidenceRef(
                        sourceModule=chk.source_module,
                        sourceRecordId=d.source_id,
                        sourceRecordRef=d.source_ref,
                        weight=round(1.0 / max(len(samples), 1), 3),
                        snapshot={
                            "targetId": d.target_id,
                            "kind": d.kind,
                            "targetLabel": chk.target_label,
                        },
                    )
                    for d in samples
                ],
                windowStart=ctx.now,
                windowEnd=ctx.now,
            ))
        return out

    async def _build_checks(self, ctx: RuleContext) -> tuple[list[RefCheck], list[str]]:
        """Discover the discriminator values this deployment actually holds and
        build one check per mapped value. Unmapped values are returned, not
        guessed at."""
        checks: list[RefCheck] = []
        unmapped: list[str] = []

        excluded: list[str] = []
        for code in await da.distinct_values(ctx.db, "Capa", "sourceTypeCode"):
            if code in _NON_TABLE_REFERENCES:
                excluded.append(f"Capa.sourceTypeCode={code} — {_NON_TABLE_REFERENCES[code]}")
                continue
            target = _CAPA_SOURCE_TARGETS.get(code)
            if target is None:
                unmapped.append(f"Capa.sourceTypeCode={code}")
                continue
            tables, label = target
            checks.append(RefCheck(
                key=f"Capa.sourceReferenceId:{code}",
                label=f"CAPAs raised from {_a(label)} {label}",
                source_module="CAPA",
                source_table="Capa",
                source_id_col="sourceReferenceId",
                source_ref_col="capaNumber",
                target_tables=tables,
                target_label=label,
                discriminator_col="sourceTypeCode",
                discriminator_value=code,
            ))

        for tt in await da.distinct_values(ctx.db, "GoldenThreadLink", "targetType"):
            target = _GOLDEN_THREAD_TARGETS.get(tt)
            if target is None:
                unmapped.append(f"GoldenThreadLink.targetType={tt}")
                continue
            tables, label = target
            checks.append(RefCheck(
                key=f"GoldenThreadLink.targetId:{tt}",
                label=f"golden-thread links to {_a(label)} {label}",
                source_module="INCIDENT",
                source_table="GoldenThreadLink",
                source_id_col="targetId",
                source_ref_col="targetRef",
                target_tables=tables,
                target_label=label,
                discriminator_col="targetType",
                discriminator_value=tt,
            ))

        # The evidence registry already maps entityType → model for the generic
        # attachment router; reusing it means a newly-attachable module gains
        # integrity checking with no edit here.
        try:
            from app.services.evidence_registry import REGISTRY

            for entity_type in await da.distinct_values(ctx.db, "Attachment", "entityType"):
                spec = REGISTRY.get(entity_type)
                if spec is None:
                    unmapped.append(f"Attachment.entityType={entity_type}")
                    continue
                checks.append(RefCheck(
                    key=f"Attachment.entityId:{entity_type}",
                    label=f"evidence files attached to {_a(spec.label)} {spec.label.lower()}",
                    source_module="PLATFORM",
                    source_table="Attachment",
                    source_id_col="entityId",
                    source_ref_col="fileName",
                    target_tables=(spec.model.__tablename__,),
                    target_label=spec.label.lower(),
                    discriminator_col="entityType",
                    discriminator_value=entity_type,
                ))
        except Exception as e:  # noqa: BLE001 — a registry import problem must not fail the rule
            ctx.notes["attachmentChecksSkipped"] = str(e)[:200]

        if excluded:
            ctx.notes["excludedReferences"] = sorted(set(excluded))
        return checks, sorted(set(unmapped))

    # A dangling reference is an observed fact, not an inference — the row
    # genuinely does not resolve — so confidence floors at 0.5 rather than
    # scaling from zero. Above that: MISSING is stronger evidence of a defect
    # than SOFT_DELETED (which can be legitimate withdrawn history), and a
    # larger break count is stronger evidence of a systemic cause than a
    # one-off.
    @staticmethod
    def _confidence(missing: int, deleted: int) -> float:
        broken = missing + deleted
        if broken == 0:
            return 0.0
        purity = missing / broken
        volume = min(broken / 25.0, 1.0)
        return round(0.5 + 0.3 * purity + 0.2 * volume, 3)

    def render_narrative(self, c: SignalCandidate) -> str:
        f = c.facts
        broken, missing, deleted = f["broken"], f["missing"], f["softDeleted"]
        target = f["targetLabel"]
        # With a single cause the count is already in the opening clause, so
        # repeating it ("3 … : 3 point at …") reads like a fault in the tool.
        single = not (missing and deleted)
        subject = ("it points" if broken == 1 else "they point") if single else None
        parts = []
        if missing:
            parts.append(
                f"{subject if single else f'{missing} point'} at "
                f"{_a(target)} {target} that no longer exists"
            )
        if deleted:
            parts.append(
                f"{deleted} at one that has been deleted"
                if missing else
                f"{subject} at {_a(target)} {target} that has been deleted"
            )
        return (
            f"{f['sourceModule']} — {broken} of the {f['sourceLabel']} "
            f"{'has' if broken == 1 else 'have'} a source reference that does not "
            f"resolve: {', '.join(parts)}. A record whose source cannot be resolved "
            f"cannot be traced back to why it was raised — the first question an "
            f"auditor asks about it."
        )

    def render_action(self, c: SignalCandidate) -> str:
        f = c.facts
        return (
            f"Re-point or annotate each listed row, then check the delete path on "
            f"{f['targetLabel']}: a reference that dangles means deletion is neither "
            f"cascading to nor blocking on its dependants. Until that path is fixed "
            f"the count will keep climbing."
        )


__all__ = ["Xcorr020ReferenceIntegrity"]
