"""Assembles a filing-ready BRSR report and freezes it on filing.

Two modes, one assembler:

* **Live** — built from the current rows every time it is requested. What a
  DRAFT / DATA_COLLECTION / REVIEW cycle shows.
* **Frozen** — the snapshot written into `BrsrReportingCycle.snapshotJson` at
  the moment the cycle became FILED, returned verbatim thereafter.

The freeze is the point. A BRSR disclosure is filed with a regulator; if the
report re-derived itself from live data forever, correcting a source incident
six months later would silently change what the entity is on record as having
filed. The snapshot is hashed (SHA-256 over a canonical JSON encoding) so the
report screen can prove the document being downloaded is the one that was
approved — the same integrity pattern the CAMS audit report uses.

SafeOps360 does NOT submit to SEBI. The structured export exists so the entity
can file through whatever process it already uses; `filingReference` records
what they filed, after the fact.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.brsr import (
    CYCLE_FILED,
    INDICATOR_ESSENTIAL,
    INDICATOR_LEADERSHIP,
    MANUAL_ONLY_PRINCIPLES,
    PRINCIPLES,
    SECTION_A,
    SECTION_B,
    SECTION_C,
    BrsrIndicator,
    BrsrIndicatorValue,
    BrsrPrincipleResponse,
    BrsrReportingCycle,
)
from app.services import brsr_env

# Human-facing names for the nine principles, as SEBI states them. Held here
# rather than in the seed because they are report chrome, not data anyone edits.
PRINCIPLE_TITLES: dict[str, str] = {
    "P1": "Businesses should conduct and govern themselves with integrity, and in a "
          "manner that is Ethical, Transparent and Accountable",
    "P2": "Businesses should provide goods and services in a manner that is sustainable and safe",
    "P3": "Businesses should respect and promote the well-being of all employees, "
          "including those in their value chains",
    "P4": "Businesses should respect the interests of and be responsive to all its stakeholders",
    "P5": "Businesses should respect and promote human rights",
    "P6": "Businesses should respect and make efforts to protect and restore the environment",
    "P7": "Businesses, when engaging in influencing public and regulatory policy, should do so "
          "in a manner that is responsible and transparent",
    "P8": "Businesses should promote inclusive growth and equitable development",
    "P9": "Businesses should engage with and provide value to their consumers in a "
          "responsible manner",
}


def _value_of(v: BrsrIndicatorValue | None, indicator: BrsrIndicator):
    if v is None:
        return None
    if indicator.valueType == "NUMBER":
        return v.valueNumber
    if indicator.valueType == "BOOLEAN":
        return v.valueBoolean
    if indicator.valueType == "TABLE":
        return v.valueJson
    return v.valueText


def _indicator_block(indicator: BrsrIndicator, v: BrsrIndicatorValue | None) -> dict:
    """One disclosure line, with its provenance attached.

    Provenance travels INTO the report, not just the screen. An assurance
    provider reading the exported file has to be able to see which figures the
    platform derived and from how many records without opening the application.
    """
    return {
        "code": indicator.code,
        "label": indicator.label,
        "groupLabel": indicator.groupLabel,
        "indicatorClass": indicator.indicatorClass,
        "valueType": indicator.valueType,
        "unit": (v.unit if v and v.unit else indicator.unit),
        "isMandatory": indicator.isMandatory,
        "value": _value_of(v, indicator),
        "answered": v is not None and _value_of(v, indicator) is not None,
        "provenance": v.provenance if v else None,
        "notApplicableReason": v.notApplicableReason if v else None,
        "sourceModule": v.sourceModule if v else None,
        "derivationNote": v.derivationNote if v else None,
        "sourceRecordCount": v.sourceRecordCount if v else None,
        "isVerified": bool(v.isVerified) if v else False,
        "computedAt": v.computedAt.isoformat() if v and v.computedAt else None,
    }


async def assemble(db: AsyncSession, cycle: BrsrReportingCycle) -> dict:
    """Build the full Section A / B / C report from live rows."""
    indicators = sorted(
        (
            await db.execute(
                select(BrsrIndicator).where(BrsrIndicator.isActive.is_(True))
            )
        )
        .scalars()
        .all(),
        key=lambda i: (i.section, i.principle or "", i.displayOrder, i.code),
    )
    values = {
        v.indicatorCode: v
        for v in (
            await db.execute(
                select(BrsrIndicatorValue).where(BrsrIndicatorValue.cycleId == cycle.id)
            )
        ).scalars()
    }
    responses = {
        r.principle: r
        for r in (
            await db.execute(
                select(BrsrPrincipleResponse).where(BrsrPrincipleResponse.cycleId == cycle.id)
            )
        ).scalars()
    }

    section_a = [_indicator_block(i, values.get(i.code)) for i in indicators if i.section == SECTION_A]
    section_b = [_indicator_block(i, values.get(i.code)) for i in indicators if i.section == SECTION_B]

    section_c = []
    for principle in PRINCIPLES:
        p_inds = [i for i in indicators if i.section == SECTION_C and i.principle == principle]
        resp = responses.get(principle)
        section_c.append(
            {
                "principle": principle,
                "title": PRINCIPLE_TITLES[principle],
                "status": resp.status if resp else "NOT_STARTED",
                "narrative": resp.narrative if resp else None,
                "completionPct": resp.completionPct if resp else 0.0,
                "autoPopulatedPct": resp.autoPopulatedPct if resp else 0.0,
                # Carried into the report so a reader of the exported file knows
                # which principles the platform could never have sourced.
                "isPlatformSourced": principle not in MANUAL_ONLY_PRINCIPLES,
                "essentialIndicators": [
                    _indicator_block(i, values.get(i.code))
                    for i in p_inds
                    if i.indicatorClass == INDICATOR_ESSENTIAL
                ],
                "leadershipIndicators": [
                    _indicator_block(i, values.get(i.code))
                    for i in p_inds
                    if i.indicatorClass == INDICATOR_LEADERSHIP
                ],
                "reviewedAt": resp.reviewedAt.isoformat() if resp and resp.reviewedAt else None,
            }
        )

    totals = await brsr_env.load_totals(db, cycle.id)

    return {
        "meta": {
            "financialYear": cycle.financialYear,
            "periodStart": cycle.periodStart.isoformat(),
            "periodEnd": cycle.periodEnd.isoformat(),
            "status": cycle.status,
            "completionPct": cycle.completionPct,
            "autoPopulatedPct": cycle.autoPopulatedPct,
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "generatedBy": "SafeOps360 BRSR Reporting",
            # Stated in the document itself so nobody mistakes the export for a
            # regulatory submission.
            "filingNote": (
                "This report is prepared for filing through the entity's own SEBI filing "
                "process. SafeOps360 does not submit to SEBI."
            ),
        },
        "entity": {
            "name": cycle.entityName,
            "cin": cycle.cin,
            "stockExchangeCodes": cycle.stockExchangeCodes or [],
            "registeredOfficeAddress": cycle.registeredOfficeAddress,
            "contactName": cycle.contactName,
            "contactEmail": cycle.contactEmail,
            "contactPhone": cycle.contactPhone,
            "assuranceProvider": cycle.assuranceProvider,
            "assuranceType": cycle.assuranceType,
        },
        "sectionA": section_a,
        "sectionB": section_b,
        "sectionC": section_c,
        "environmentalSummary": {
            "sitesReporting": totals.siteCount,
            "energyTotalGj": totals.energyTotalGj,
            "energyRenewableGj": totals.energyRenewableGj,
            "energyNonRenewableGj": totals.energyNonRenewableGj,
            "waterWithdrawnKl": totals.waterWithdrawnKl,
            "waterDischargedKl": totals.waterDischargedKl,
            "waterConsumedKl": totals.waterConsumedKl,
            "waterRecycledKl": totals.waterRecycledKl,
            "scope1TCo2e": totals.scope1TCo2e,
            "scope2TCo2e": totals.scope2TCo2e,
            "scope3TCo2e": totals.scope3TCo2e,
            "wasteGeneratedT": totals.wasteGeneratedT,
            "wasteRecoveredT": totals.wasteRecoveredT,
            "wasteDisposedT": totals.wasteDisposedT,
            "wasteDivertedPct": totals.wasteDivertedPct,
            "turnoverInr": totals.turnoverInr,
            "unresolvedEmissionLines": totals.unresolvedEmissionLines,
        },
        "approval": {
            "approvedById": cycle.approvedById,
            "approvedAt": cycle.approvedAt.isoformat() if cycle.approvedAt else None,
            "filedById": cycle.filedById,
            "filedAt": cycle.filedAt.isoformat() if cycle.filedAt else None,
            "filingReference": cycle.filingReference,
        },
    }


def snapshot_hash(payload: dict) -> str:
    """SHA-256 over a canonical encoding of the report.

    `sort_keys` + a fixed separator is what makes this reproducible — without
    it, two encodings of the same content hash differently and the integrity
    check becomes noise. `generatedAt` is excluded for the same reason: it
    changes on every assembly and would make every hash unique regardless of
    content.
    """
    canonical = {k: v for k, v in payload.items()}
    meta = dict(canonical.get("meta") or {})
    meta.pop("generatedAt", None)
    canonical["meta"] = meta
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


async def get_report(db: AsyncSession, cycle: BrsrReportingCycle) -> dict:
    """The report for a cycle — frozen if filed, live otherwise."""
    if cycle.status == CYCLE_FILED and cycle.snapshotJson:
        payload = dict(cycle.snapshotJson)
        payload.setdefault("meta", {})["frozen"] = True
        payload["meta"]["snapshotHash"] = cycle.snapshotHash
        return payload
    payload = await assemble(db, cycle)
    payload["meta"]["frozen"] = False
    return payload


async def freeze(db: AsyncSession, cycle: BrsrReportingCycle) -> dict:
    """Assemble and store the immutable snapshot. Caller commits.

    Called on the transition to FILED and nowhere else. Re-freezing an already
    filed cycle would defeat the entire mechanism, so it refuses.
    """
    if cycle.snapshotJson is not None:
        raise ValueError(
            f"Cycle {cycle.financialYear} already carries a filed snapshot "
            f"(hash {cycle.snapshotHash}). A filed disclosure cannot be re-frozen."
        )
    payload = await assemble(db, cycle)
    cycle.snapshotJson = payload
    cycle.snapshotHash = snapshot_hash(payload)
    return payload


def flatten_for_export(payload: dict) -> list[dict]:
    """The report as flat rows — the structured data export.

    One row per indicator, provenance included. This is the shape a filing
    team pastes into their own template and an assurance provider samples from,
    which is why the derivation note travels with the figure rather than being
    left behind in the UI.
    """
    rows: list[dict] = []

    def _emit(section: str, principle: str | None, block: dict) -> None:
        rows.append(
            {
                "section": section,
                "principle": principle or "",
                "indicatorCode": block["code"],
                "indicatorClass": block.get("indicatorClass") or "",
                "groupLabel": block.get("groupLabel") or "",
                "label": block["label"],
                "value": (
                    json.dumps(block["value"], default=str)
                    if isinstance(block["value"], (dict, list))
                    else block["value"]
                ),
                "unit": block.get("unit") or "",
                "provenance": block.get("provenance") or "UNANSWERED",
                "sourceModule": block.get("sourceModule") or "",
                "sourceRecordCount": block.get("sourceRecordCount"),
                "derivationNote": block.get("derivationNote") or "",
                "isVerified": block.get("isVerified"),
                "notApplicableReason": block.get("notApplicableReason") or "",
            }
        )

    for block in payload.get("sectionA", []):
        _emit("A", None, block)
    for block in payload.get("sectionB", []):
        _emit("B", None, block)
    for p in payload.get("sectionC", []):
        for block in p.get("essentialIndicators", []):
            _emit("C", p["principle"], block)
        for block in p.get("leadershipIndicators", []):
            _emit("C", p["principle"], block)
    return rows


__all__ = [
    "PRINCIPLE_TITLES",
    "assemble",
    "flatten_for_export",
    "freeze",
    "get_report",
    "snapshot_hash",
]
