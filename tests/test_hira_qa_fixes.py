"""Regression tests for the HIRA ALARP functional-QA findings.

Covers the defects raised in "SafeOps360 — HIRA Module · Residual Risk & ALARP
— Functional Test Flow":

  §3.4/1  score rationale was optional everywhere — an entry could be created,
          submitted and approved with both rationales blank.
  §4.1/2  a recommended control saved with a blank description (the model's
          NOT NULL is satisfied by an empty string).
  §4.1/3  a recommended control in a committed status saved with no proposed
          implementation date.
  §4.2/1  a proposal with a responsible person raised no CAPA in CAPA Universal
          (gap G18 — the HIRA_CONTROL source type was seeded, the producer was
          never written).
  §7/2    editing an approved entry 500'd on the (entryId, versionNumber)
          unique key once the entry's counter caught up with an archived row.

Offline: the pure helpers and Pydantic validators need no DB, matching the
house style in test_hira_alarp.py. The CAPA producer is exercised against a
fake session that records what would be written.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.routers.hira import _missing_rationales, _require_rationales
from app.schemas.hira import HiraEntryRecommendedControlReplaceItem
from app.services import hira_capa


def _now():
    return datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────────────────
# §3.4 — score rationale is required documented information
# ─────────────────────────────────────────────────────────────────────


def _entry(**overrides):
    base = dict(
        initialLikelihoodRationale="Two near misses in 18 months.",
        initialSeverityRationale="Worst credible outcome is a fatality.",
        residualRiskLevel="MODERATE",
        residualLikelihoodRationale="Interlock reduces exposure.",
        residualSeverityRationale="Severity unchanged; consequence is the same.",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_complete_entry_has_no_missing_rationales():
    assert _missing_rationales(_entry()) == []


@pytest.mark.parametrize(
    "field,label",
    [
        ("initialLikelihoodRationale", "initial likelihood rationale"),
        ("initialSeverityRationale", "initial severity rationale"),
        ("residualLikelihoodRationale", "residual likelihood rationale"),
        ("residualSeverityRationale", "residual severity rationale"),
    ],
)
def test_blank_rationale_is_reported(field, label):
    assert _missing_rationales(_entry(**{field: ""})) == [label]


def test_whitespace_only_rationale_counts_as_blank():
    """The QA screenshot showed empty boxes; a spaces-only value must not pass
    either, or the rule is trivially defeated."""
    assert _missing_rationales(_entry(initialSeverityRationale="   ")) == [
        "initial severity rationale"
    ]


def test_residual_rationale_not_required_before_a_residual_is_scored():
    """An entry mid-assessment has no residual yet — demanding its rationale
    would make the initial assessment unsubmittable."""
    entry = _entry(
        residualRiskLevel=None,
        residualLikelihoodRationale=None,
        residualSeverityRationale=None,
    )
    assert _missing_rationales(entry) == []


def test_require_rationales_raises_422_naming_the_gaps():
    entry = _entry(initialLikelihoodRationale="", initialSeverityRationale="")
    with pytest.raises(HTTPException) as exc:
        _require_rationales(entry, "approve this entry")
    assert exc.value.status_code == 422
    assert "initial likelihood rationale" in exc.value.detail
    assert "initial severity rationale" in exc.value.detail
    assert "approve this entry" in exc.value.detail


def test_require_rationales_passes_a_complete_entry():
    _require_rationales(_entry(), "approve this entry")  # must not raise


# ─────────────────────────────────────────────────────────────────────
# §4.1 — recommended-control completeness
# ─────────────────────────────────────────────────────────────────────


def _control(**overrides):
    base = dict(
        hierarchy="ENGINEERING",
        description="Install fixed gas detection interlocked to the extract fan.",
        status="PROPOSED",
    )
    base.update(overrides)
    return base


def test_valid_proposal_is_accepted():
    item = HiraEntryRecommendedControlReplaceItem(**_control())
    assert item.description.startswith("Install fixed gas detection")
    assert item.hierarchy == "ENGINEERING"


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_description_is_rejected(blank):
    """§4.1/2 — the model column is NOT NULL but an empty string satisfies it,
    so a nameless proposal saved cleanly."""
    with pytest.raises(ValidationError) as exc:
        HiraEntryRecommendedControlReplaceItem(**_control(description=blank))
    assert "description is required" in str(exc.value)


def test_blank_hierarchy_is_rejected():
    with pytest.raises(ValidationError) as exc:
        HiraEntryRecommendedControlReplaceItem(**_control(hierarchy=""))
    assert "hierarchy is required" in str(exc.value)


def test_unknown_hierarchy_is_rejected():
    with pytest.raises(ValidationError):
        HiraEntryRecommendedControlReplaceItem(**_control(hierarchy="MAGIC"))


@pytest.mark.parametrize("status", ["APPROVED", "PLANNED", "IN_PROGRESS"])
def test_committed_status_requires_a_proposed_date(status):
    """§4.1/3 — a control being pursued is a commitment to a date."""
    with pytest.raises(ValidationError) as exc:
        HiraEntryRecommendedControlReplaceItem(**_control(status=status))
    assert "Proposed implementation date is required" in str(exc.value)


@pytest.mark.parametrize("status", ["APPROVED", "PLANNED", "IN_PROGRESS"])
def test_committed_status_passes_with_a_date(status):
    item = HiraEntryRecommendedControlReplaceItem(
        **_control(status=status, proposedImplementationDate=_now() + timedelta(days=30))
    )
    assert item.status == status


@pytest.mark.parametrize("status", ["PROPOSED", "IMPLEMENTED", "DEFERRED", "REJECTED"])
def test_uncommitted_status_does_not_require_a_date(status):
    """A proposal still under consideration legitimately has no date yet —
    over-enforcing here would block honest drafting."""
    item = HiraEntryRecommendedControlReplaceItem(**_control(status=status))
    assert item.proposedImplementationDate is None


def test_hierarchy_and_status_are_normalised():
    item = HiraEntryRecommendedControlReplaceItem(
        **_control(hierarchy="engineering", status="proposed")
    )
    assert item.hierarchy == "ENGINEERING"
    assert item.status == "PROPOSED"


def test_blank_responsible_id_normalises_to_none():
    """An empty string would be sent straight into a foreign-key column."""
    item = HiraEntryRecommendedControlReplaceItem(**_control(responsibleId="  "))
    assert item.responsibleId is None


# ─────────────────────────────────────────────────────────────────────
# §4.2 / G18 — recommended control → CAPA Universal
# ─────────────────────────────────────────────────────────────────────


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    """Minimal AsyncSession stand-in: returns pre-seeded CAPA rows for the
    lookup query and records anything added."""

    def __init__(self, existing=()):
        self._existing = list(existing)
        self.added = []

    async def execute(self, _stmt):
        return _FakeResult(self._existing)

    def add(self, obj):
        self.added.append(obj)


def _hira_entry():
    return SimpleNamespace(
        id="entry-1",
        studyId="study-1",
        sequenceNumber=1,
        activityDescription="Atmospheric testing and pre-entry checklist",
        residualRiskLevel="HIGH",
        residualRiskScore=9,
        targetRiskLevel="MODERATE",
    )


def _proposal(**overrides):
    base = dict(
        id="rc-1",
        hierarchy="ENGINEERING",
        description="Install continuous gas monitoring with automated interlock.",
        rationale="Removes reliance on human action.",
        estimatedCostBand="HIGH",
        proposedImplementationDate=_now() + timedelta(days=30),
        responsibleId="user-99",
        status="PROPOSED",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _fake_capa(**overrides):
    base = dict(
        id="capa-1",
        capaNumber="CAPA-S-2026-NW-001",
        sourceTypeCode="HIRA_CONTROL",
        sourceReferenceId="rc-1",
        state="ACTIONS_PLANNED",
        primaryOwnerUserId="user-99",
        closureTargetDate=None,
        sourceMetadata={"controlStatus": "PROPOSED"},
        stateChangedAt=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_owned_live_proposal_raises_a_capa(monkeypatch):
    """§4.2/1 — the whole point of G18."""
    captured = {}

    async def _fake_spawn(db, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id="capa-new", capaNumber="CAPA-S-2026-NW-007")

    monkeypatch.setattr(hira_capa, "spawn_capa", _fake_spawn)
    db = _FakeSession()
    result = await hira_capa.sync_recommended_control_capas(
        db, entry=_hira_entry(), plant_id="plant-1", controls=[_proposal()], actor_id="actor-1"
    )

    assert result["created"] == ["CAPA-S-2026-NW-007"]
    assert captured["source_code"] == "HIRA_CONTROL"
    assert captured["ref_id"] == "rc-1"
    # Owner is the named responsible person, not the person who saved the form.
    assert captured["owner_id"] == "user-99"
    # HIGH residual drives the CAPA severity.
    assert captured["severity"] == "HIGH"
    assert captured["metadata"]["hiraEntryId"] == "entry-1"


@pytest.mark.asyncio
async def test_unassigned_proposal_raises_no_capa(monkeypatch):
    """§4.2/4 — an unowned CAPA is worse than none: it lands in the register
    with nobody accountable and skews every open-action metric."""

    async def _fail(db, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("spawn_capa should not run for an unassigned proposal")

    monkeypatch.setattr(hira_capa, "spawn_capa", _fail)
    db = _FakeSession()
    result = await hira_capa.sync_recommended_control_capas(
        db,
        entry=_hira_entry(),
        plant_id="plant-1",
        controls=[_proposal(responsibleId=None)],
        actor_id="actor-1",
    )
    assert result["created"] == []


@pytest.mark.asyncio
async def test_rejected_proposal_raises_no_capa(monkeypatch):
    async def _fail(db, **kwargs):  # pragma: no cover
        raise AssertionError("spawn_capa should not run for a rejected proposal")

    monkeypatch.setattr(hira_capa, "spawn_capa", _fail)
    result = await hira_capa.sync_recommended_control_capas(
        _FakeSession(),
        entry=_hira_entry(),
        plant_id="plant-1",
        controls=[_proposal(status="REJECTED")],
        actor_id="actor-1",
    )
    assert result["created"] == []


@pytest.mark.asyncio
async def test_sync_is_idempotent(monkeypatch):
    """Saving Section 6 twice must not mint a second CAPA for the same
    proposal — the register would double-count the same action."""

    async def _fail(db, **kwargs):  # pragma: no cover
        raise AssertionError("spawn_capa should not run when a CAPA already exists")

    monkeypatch.setattr(hira_capa, "spawn_capa", _fail)
    db = _FakeSession(existing=[_fake_capa()])
    result = await hira_capa.sync_recommended_control_capas(
        db, entry=_hira_entry(), plant_id="plant-1", controls=[_proposal()], actor_id="actor-1"
    )
    assert result["created"] == []


@pytest.mark.asyncio
async def test_implemented_proposal_moves_capa_to_verification():
    """§4.2/3 — implemented is not closed. Closure stays with the CAPA engine,
    which owns verification and effectiveness."""
    capa = _fake_capa()
    db = _FakeSession(existing=[capa])
    result = await hira_capa.sync_recommended_control_capas(
        db,
        entry=_hira_entry(),
        plant_id="plant-1",
        controls=[_proposal(status="IMPLEMENTED")],
        actor_id="actor-1",
    )
    assert capa.state == "PENDING_VERIFICATION"
    assert capa.capaNumber in result["retired"]
    assert capa.sourceMetadata["controlStatus"] == "IMPLEMENTED"


@pytest.mark.asyncio
async def test_abandoned_proposal_cancels_its_capa():
    capa = _fake_capa()
    db = _FakeSession(existing=[capa])
    await hira_capa.sync_recommended_control_capas(
        db,
        entry=_hira_entry(),
        plant_id="plant-1",
        controls=[_proposal(status="REJECTED")],
        actor_id="actor-1",
    )
    assert capa.state == "CANCELLED"


@pytest.mark.asyncio
async def test_owner_reassignment_follows_through_to_the_capa():
    capa = _fake_capa(primaryOwnerUserId="user-11")
    db = _FakeSession(existing=[capa])
    await hira_capa.sync_recommended_control_capas(
        db,
        entry=_hira_entry(),
        plant_id="plant-1",
        controls=[_proposal(responsibleId="user-22")],
        actor_id="actor-1",
    )
    assert capa.primaryOwnerUserId == "user-22"


@pytest.mark.asyncio
async def test_closed_capa_is_never_re_driven():
    """A CAPA that has already been verified and closed must not be dragged
    back open by a later Section 6 edit."""
    capa = _fake_capa(state="CLOSED")
    db = _FakeSession(existing=[capa])
    await hira_capa.sync_recommended_control_capas(
        db,
        entry=_hira_entry(),
        plant_id="plant-1",
        controls=[_proposal(status="REJECTED")],
        actor_id="actor-1",
    )
    assert capa.state == "CLOSED"


@pytest.mark.asyncio
async def test_metadata_is_reassigned_not_mutated_in_place():
    """SQLAlchemy does not mark a JSON column dirty when its dict is mutated in
    place, so an in-place update silently no-ops on commit."""
    original = {"controlStatus": "PROPOSED"}
    capa = _fake_capa(sourceMetadata=original)
    db = _FakeSession(existing=[capa])
    await hira_capa.sync_recommended_control_capas(
        db,
        entry=_hira_entry(),
        plant_id="plant-1",
        controls=[_proposal(status="IMPLEMENTED")],
        actor_id="actor-1",
    )
    assert capa.sourceMetadata is not original
    assert original["controlStatus"] == "PROPOSED"


def test_due_days_floors_at_one_for_a_past_date():
    """A proposal whose target date has already passed must still produce a
    valid (immediately overdue) CAPA, not a negative closure target."""
    assert hira_capa._due_days(_now() - timedelta(days=40), 60) == 1


def test_due_days_falls_back_when_no_date_is_set():
    assert hira_capa._due_days(None, 60) == 60


# ─────────────────────────────────────────────────────────────────────
# §7/2 — version numbering must not collide on an approved entry
# ─────────────────────────────────────────────────────────────────────


class _MaxResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _VersionSession:
    def __init__(self, existing_max):
        self._existing_max = existing_max

    async def execute(self, _stmt):
        return _MaxResult(self._existing_max)


@pytest.mark.asyncio
async def test_archive_number_skips_an_already_used_version():
    """The live prod entry behind the QA failure had HiraVersion rows [1, 3]
    while entry.versionNumber was also 3, so filing under versionNumber hit the
    (entryId, versionNumber) unique key and 500'd every subsequent save."""
    from app.routers.hira import _archive_version_number

    entry = SimpleNamespace(id="entry-1", versionNumber=3)
    assert await _archive_version_number(_VersionSession(3), entry) == 4


@pytest.mark.asyncio
async def test_archive_number_self_heals_a_gapped_entry():
    from app.routers.hira import _archive_version_number

    entry = SimpleNamespace(id="entry-1", versionNumber=2)
    assert await _archive_version_number(_VersionSession(7), entry) == 8


@pytest.mark.asyncio
async def test_archive_number_uses_the_entry_counter_when_free():
    from app.routers.hira import _archive_version_number

    entry = SimpleNamespace(id="entry-1", versionNumber=4)
    assert await _archive_version_number(_VersionSession(3), entry) == 4


@pytest.mark.asyncio
async def test_archive_number_handles_an_entry_with_no_versions():
    from app.routers.hira import _archive_version_number

    entry = SimpleNamespace(id="entry-1", versionNumber=1)
    assert await _archive_version_number(_VersionSession(None), entry) == 1
