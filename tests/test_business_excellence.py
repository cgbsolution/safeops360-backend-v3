"""Tests for the Business Excellence registers (Phase 1).

Offline: the state machine, the derivations and the Pydantic validators need no
DB, matching the house style in test_hira_qa_fixes.py. The two functions that do
touch a session (audience resolution and the workflow bridge) are exercised
against a fake session that records what would be written.

The cases here are chosen for the mistakes this platform has actually made
before: numbering that collides after a soft delete, a rejected record left
stranded at its pre-rejection status, an overdue flag that fires on a closed
record, and a percentage that reports 0% when the truth is "nobody was asked".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.models.business_excellence import (
    KAIZEN_OPEN_STATUSES,
    KAIZEN_STATUSES,
    VERIFICATION_INTERVAL_DAYS,
)
from app.schemas.business_excellence import (
    BypassRequest,
    BypassRestore,
    KaizenCreate,
    OplCreate,
    PokaYokeCreate,
    VerificationCreate,
)
from app.services import business_excellence as be


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# Kaizen state machine
# ─────────────────────────────────────────────────────────────────────────────
def _kaizen(**kw) -> SimpleNamespace:
    base = dict(
        id="k1",
        status="DRAFT",
        lane="STANDARD",
        targetDate=None,
        createdById="u-author",
        verifiedAnnualSaving=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_every_status_in_the_vocabulary_has_a_transition_entry():
    """A status missing from the table silently strands a record: the detail
    screen renders no buttons and nobody can tell whether that is correct."""
    missing = [s for s in KAIZEN_STATUSES if s not in be.KAIZEN_TRANSITIONS]
    assert missing == [], f"statuses with no transition entry: {missing}"


def test_transitions_only_target_real_statuses():
    for source, targets in be.KAIZEN_TRANSITIONS.items():
        for target in targets:
            assert target in KAIZEN_STATUSES, f"{source} → unknown status {target}"


def test_terminal_statuses_offer_nothing():
    for terminal in ("CLOSED", "REJECTED"):
        assert be.allowed_kaizen_actions(_kaizen(status=terminal), {"KAIZEN.APPROVE"}) == []


def test_actions_are_gated_on_the_caller_permission_not_just_status():
    k = _kaizen(status="SUBMITTED")
    assert be.allowed_kaizen_actions(k, set()) == []
    assert "SCREENED" in be.allowed_kaizen_actions(k, {"KAIZEN.APPROVE"})


def test_fast_track_lane_can_approve_straight_out_of_submitted():
    """The Suggestion Scheme lane exists so a small idea does not wait for a
    committee. A STANDARD record at the same status must NOT get the shortcut."""
    fast = _kaizen(status="SUBMITTED", lane="FAST_TRACK")
    standard = _kaizen(status="SUBMITTED", lane="STANDARD")
    assert "APPROVED" in be.allowed_kaizen_actions(fast, {"KAIZEN.APPROVE"})
    assert "APPROVED" not in be.allowed_kaizen_actions(standard, {"KAIZEN.APPROVE"})


def test_fast_track_shortcut_still_needs_the_approve_permission():
    fast = _kaizen(status="SUBMITTED", lane="FAST_TRACK")
    assert be.allowed_kaizen_actions(fast, {"KAIZEN.UPDATE"}) == []


def test_transition_permitted_agrees_with_allowed_actions():
    """The API gate and the button list must be the same answer — a client that
    is offered an action the gate refuses produces a button that 403s."""
    k = _kaizen(status="IMPLEMENTED")
    granted = {"KAIZEN.VERIFY"}
    for target in KAIZEN_STATUSES:
        assert be.kaizen_transition_permitted(k, target, granted) == (
            target in be.allowed_kaizen_actions(k, granted)
        )


# ─────────────────────────────────────────────────────────────────────────────
# Overdue derivation
# ─────────────────────────────────────────────────────────────────────────────
def test_overdue_only_applies_to_records_still_owing_work():
    past = _now() - timedelta(days=5)
    assert be.is_kaizen_overdue(_kaizen(status="IN_IMPLEMENTATION", targetDate=past)) is True
    # A finished or abandoned record is never overdue however old its target —
    # an overdue flag on a closed row is noise that teaches people to ignore
    # the column.
    for done in ("IMPLEMENTED", "VERIFIED", "CLOSED", "REJECTED", "PARKED"):
        assert be.is_kaizen_overdue(_kaizen(status=done, targetDate=past)) is False


def test_no_target_date_is_never_overdue():
    assert be.is_kaizen_overdue(_kaizen(status="APPROVED", targetDate=None)) is False


def test_overdue_tolerates_a_naive_datetime_from_the_db():
    """Rows written before a column was timezone-aware come back naive, and a
    naive/aware comparison raises TypeError mid-request."""
    naive_past = datetime.now() - timedelta(days=3)
    assert be.is_kaizen_overdue(_kaizen(status="APPROVED", targetDate=naive_past)) is True


def test_open_statuses_are_a_subset_of_the_vocabulary():
    assert set(KAIZEN_OPEN_STATUSES) <= set(KAIZEN_STATUSES)


# ─────────────────────────────────────────────────────────────────────────────
# Poka Yoke verification cycle
# ─────────────────────────────────────────────────────────────────────────────
def _device(**kw) -> SimpleNamespace:
    base = dict(
        id="d1",
        status="ACTIVE",
        isBypassed=False,
        nextVerificationDueAt=None,
        verificationFrequency="MONTHLY",
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.parametrize("freq,days", sorted(VERIFICATION_INTERVAL_DAYS.items()))
def test_next_due_matches_the_declared_interval(freq: str, days: int):
    start = _now()
    assert be.next_verification_due(freq, from_dt=start) == start + timedelta(days=days)


def test_unknown_frequency_degrades_to_monthly_rather_than_raising():
    """A device with a typo'd cadence should still land on somebody's list."""
    start = _now()
    assert be.next_verification_due("FORTNIGHTLY", from_dt=start) == start + timedelta(days=30)


def test_verification_overdue_only_for_devices_that_owe_a_check():
    past = _now() - timedelta(days=2)
    for live in ("VERIFIED", "ACTIVE", "DEGRADED"):
        assert be.is_verification_overdue(_device(status=live, nextVerificationDueAt=past)) is True
    # A proposed or retired device has no verification obligation, so flagging
    # it overdue would inflate the count the register's banner claims.
    for idle in ("PROPOSED", "APPROVED", "INSTALLED", "RETIRED", "REJECTED"):
        assert be.is_verification_overdue(_device(status=idle, nextVerificationDueAt=past)) is False


def test_bypassed_device_offers_restore_not_a_second_bypass():
    actions = be.allowed_poka_yoke_actions(
        _device(status="ACTIVE", isBypassed=True), {"POKAYOKE.UPDATE"}
    )
    assert "RESTORE" in actions
    assert "BYPASS" not in actions


def test_only_a_live_unbypassed_device_can_be_bypassed():
    assert "BYPASS" in be.allowed_poka_yoke_actions(
        _device(status="ACTIVE", isBypassed=False), {"POKAYOKE.UPDATE"}
    )
    assert "BYPASS" not in be.allowed_poka_yoke_actions(
        _device(status="PROPOSED", isBypassed=False), {"POKAYOKE.UPDATE"}
    )


# ─────────────────────────────────────────────────────────────────────────────
# Poka Yoke — the bypass log
#
# These exist because the previous implementation lost data on a HAPPY path: it
# ended a bypass by NULLing the timestamp and both user references on the device
# row, so a device bypassed and restored ten times kept one reason string and no
# dates. Nothing failed, nothing logged, and the register looked correct.
# ─────────────────────────────────────────────────────────────────────────────
def _bypass(**kw) -> SimpleNamespace:
    base = dict(
        id="b1",
        deviceId="d1",
        bypassedById="u-1",
        bypassedAt=_now() - timedelta(hours=5),
        reason="Sensor awaiting a part.",
        approvedById=None,
        statusAtBypass="ACTIVE",
        restoredAt=None,
        restoredById=None,
        restoreNote=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_open_bypass_overrides_the_displayed_status():
    """A device somebody has switched off must not read as Active.

    The badge is the only thing most people look at, and 'Active' over a
    bypassed device is a screen stating something untrue.
    """
    d = _device(status="ACTIVE", isBypassed=True)
    assert be.poka_yoke_display_status(d) == "BYPASSED"


def test_the_lifecycle_status_is_left_alone_underneath():
    """The derived status must not be written back into `status`.

    That column drives the workflow engine and the verification obligation. If
    'BYPASSED' were stored there, 'what was this device before somebody
    switched it off?' would be unrecoverable — the exact loss the bypass log
    exists to undo.
    """
    d = _device(status="ACTIVE", isBypassed=True)
    be.poka_yoke_display_status(d)
    assert d.status == "ACTIVE"


def test_a_withdrawn_device_is_never_displayed_as_bypassed():
    """RETIRED and REJECTED outrank a stale flag.

    A retired device has nothing to protect, so showing it as bypassed would
    put it on a tile that means 'somebody has to go and switch this back on'.
    """
    for withdrawn in ("RETIRED", "REJECTED"):
        d = _device(status=withdrawn, isBypassed=True)
        assert be.poka_yoke_display_status(d) == withdrawn


def test_display_status_prefers_the_caller_s_answer_over_the_cached_flag():
    """The register resolves open episodes in bulk and passes the answer in.

    The flag on the device row is a cache; the log is the record of truth. When
    the two disagree the caller's answer must win, or a stale flag would
    outvote the table that replaced it.
    """
    stale = _device(status="ACTIVE", isBypassed=False)
    assert be.poka_yoke_display_status(stale, has_open_bypass=True) == "BYPASSED"

    also_stale = _device(status="ACTIVE", isBypassed=True)
    assert be.poka_yoke_display_status(also_stale, has_open_bypass=False) == "ACTIVE"


def test_an_unbypassed_device_shows_its_own_status():
    for live in ("PROPOSED", "APPROVED", "INSTALLED", "VERIFIED", "ACTIVE", "DEGRADED"):
        assert be.poka_yoke_display_status(_device(status=live, isBypassed=False)) == live


def test_a_closed_bypass_measures_to_its_restore():
    started = _now() - timedelta(hours=10)
    b = _bypass(bypassedAt=started, restoredAt=started + timedelta(hours=4))
    assert be.bypass_duration_hours(b) == 4.0


def test_an_open_bypass_measures_to_now_rather_than_returning_null():
    """A blank cell until somebody restores it is how a bypass becomes
    permanent without anybody deciding it should be. The running total is the
    number that makes someone act."""
    b = _bypass(bypassedAt=_now() - timedelta(hours=72), restoredAt=None)
    hours = be.bypass_duration_hours(b)
    assert hours is not None
    assert 71.5 <= hours <= 72.5


def test_bypass_duration_tolerates_a_naive_datetime_from_the_db():
    """Every timestamp on these tables is `timestamp WITHOUT time zone`. A
    duration helper that assumed tz-aware values would raise on real rows."""
    naive_start = (_now() - timedelta(hours=3)).replace(tzinfo=None)
    b = _bypass(bypassedAt=naive_start, restoredAt=None)
    hours = be.bypass_duration_hours(b)
    assert hours is not None and hours > 0


def test_bypass_duration_is_never_negative():
    """Clock skew between an app server and the DB must not produce a device
    that has been bypassed for minus four hours."""
    b = _bypass(bypassedAt=_now() + timedelta(hours=4), restoredAt=None)
    assert be.bypass_duration_hours(b) == 0.0


def test_a_bypass_with_no_start_reports_nothing_rather_than_zero():
    """0.0 hours reads as 'just now'; None reads as 'unknown'. They are
    different facts and the screen renders them differently."""
    assert be.bypass_duration_hours(_bypass(bypassedAt=None)) is None


def test_a_bypass_reason_below_the_floor_is_refused():
    """Ten characters is not a high bar, but it refuses 'n/a' and 'temp'.

    The reason is the only thing the register can hand an auditor about why
    protection was removed from a line.
    """
    with pytest.raises(ValidationError):
        BypassRequest(reason="n/a")
    with pytest.raises(ValidationError):
        BypassRequest(reason="temp")


def test_a_real_bypass_reason_is_accepted_with_an_optional_approver():
    b = BypassRequest(
        reason="Sensor awaiting replacement part; 100% inspection in place.",
        approvedById="u-supervisor",
    )
    assert b.approvedById == "u-supervisor"
    # The authoriser is optional — a shift supervisor logging their own override
    # at 2am must not be blocked from recording it because nobody senior is on
    # site. Who logged it is captured either way.
    assert BypassRequest(reason="Jig removed for scheduled tool change.").approvedById is None


def test_a_restore_note_is_optional():
    """Requiring one would push people towards ending bypasses they have not
    actually resolved, just to clear the tile."""
    assert BypassRestore().note is None
    assert BypassRestore(note="Part fitted.").note == "Part fitted."


def test_failed_verification_must_carry_a_note():
    """A red row nobody can act on is worse than no row — and the CAPA it
    raises would have nothing to describe."""
    with pytest.raises(ValidationError):
        VerificationCreate(result="FAIL", note="   ")
    assert VerificationCreate(result="FAIL", note="Sensor taped over").note


def test_passed_verification_needs_no_note():
    assert VerificationCreate(result="PASS").note is None


# ─────────────────────────────────────────────────────────────────────────────
# OPL acknowledgement
# ─────────────────────────────────────────────────────────────────────────────
def _ack(**kw) -> SimpleNamespace:
    base = dict(status="ASSIGNED", dueAt=None, readAt=None, acknowledgedAt=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_percent_is_none_when_nobody_has_been_asked():
    """None, not 0. "Nobody has been asked" and "nobody has read it" are
    different facts, and a 0% badge on an unpublished lesson reads as a failure
    that has not happened yet."""
    assert be.summarise_acknowledgements([])["percent"] is None


def test_percent_counts_acknowledged_not_read():
    rows = [
        _ack(status="ACKNOWLEDGED", readAt=_now(), acknowledgedAt=_now()),
        _ack(status="READ", readAt=_now()),
        _ack(status="ASSIGNED"),
        _ack(status="WAIVED"),
    ]
    s = be.summarise_acknowledgements(rows)
    assert s["assigned"] == 4
    assert s["read"] == 2  # acknowledged rows have a readAt too
    assert s["acknowledged"] == 1
    assert s["waived"] == 1
    assert s["percent"] == 25.0


def test_acknowledged_and_waived_are_never_overdue():
    past = _now() - timedelta(days=1)
    assert be.is_ack_overdue(_ack(status="ACKNOWLEDGED", dueAt=past)) is False
    assert be.is_ack_overdue(_ack(status="WAIVED", dueAt=past)) is False
    assert be.is_ack_overdue(_ack(status="ASSIGNED", dueAt=past)) is True


def test_ack_with_no_due_date_is_never_overdue():
    assert be.is_ack_overdue(_ack(status="ASSIGNED", dueAt=None)) is False


def test_published_opl_cannot_be_edited_only_revised():
    published = SimpleNamespace(status="PUBLISHED")
    actions = be.allowed_opl_actions(published, {"OPL.CREATE", "OPL.UPDATE", "OPL.APPROVE"})
    assert "EDIT" not in actions
    assert "REVISE" in actions


def test_draft_opl_is_editable():
    assert "EDIT" in be.allowed_opl_actions(SimpleNamespace(status="DRAFT"), {"OPL.UPDATE"})


def test_opl_review_overdue_only_while_published():
    past = _now() - timedelta(days=1)
    assert be.is_opl_review_overdue(SimpleNamespace(status="PUBLISHED", reviewDueAt=past)) is True
    assert be.is_opl_review_overdue(SimpleNamespace(status="RETIRED", reviewDueAt=past)) is False


# ─────────────────────────────────────────────────────────────────────────────
# Validators
# ─────────────────────────────────────────────────────────────────────────────
def test_opl_rejects_more_than_five_key_points():
    """The cap is the format's whole value — a lesson that teaches six things
    teaches none of them."""
    with pytest.raises(ValidationError):
        OplCreate(
            plantId="p1",
            title="Six points",
            category="BASIC_KNOWLEDGE",
            keyPoints=[f"point {i}" for i in range(6)],
        )


def test_kaizen_saving_type_requires_a_figure():
    """A saving TYPE with no saving AMOUNT renders on the register as if a
    value had been recorded."""
    with pytest.raises(ValidationError):
        KaizenCreate(
            plantId="p1",
            title="A good idea",
            category="COST",
            problemStatement="Something is wrong here",
            proposedImprovement="Do it differently instead",
            savingType="HARD",
        )


def test_kaizen_accepts_a_saving_type_with_a_figure():
    k = KaizenCreate(
        plantId="p1",
        title="A good idea",
        category="COST",
        problemStatement="Something is wrong here",
        proposedImprovement="Do it differently instead",
        estimatedAnnualSaving=50000,
        savingType="HARD",
    )
    assert k.savingType == "HARD"


def test_vocabulary_values_outside_the_tuple_are_rejected():
    with pytest.raises(ValidationError):
        PokaYokeCreate(
            plantId="p1",
            title="A device",
            defectModePrevented="Wrong part fitted",
            deviceType="MAGIC",  # not in POKA_YOKE_DEVICE_TYPES
            approach="PREVENTION",
            reactionMode="CONTROL",
        )


# ─────────────────────────────────────────────────────────────────────────────
# The workflow bridge
# ─────────────────────────────────────────────────────────────────────────────
class _FakeSession:
    """Just enough of AsyncSession for sync_be_record_status."""

    def __init__(self, record=None):
        self._record = record
        self.flushed = False

    async def get(self, _model, _pk):
        return self._record

    async def flush(self):
        self.flushed = True


@pytest.mark.asyncio
async def test_bridge_is_a_no_op_for_a_module_it_does_not_own():
    """The engine calls this unconditionally, so a foreign module must fall
    through untouched rather than raising."""
    session = _FakeSession(SimpleNamespace(status="SUBMITTED"))
    applied = await be.sync_be_record_status(
        session, module="PTW", record_id="x", instance_completed=True
    )
    assert applied is False
    assert session.flushed is False


@pytest.mark.asyncio
async def test_bridge_handles_a_missing_record_without_raising():
    session = _FakeSession(None)
    applied = await be.sync_be_record_status(
        session, module=be.WF_MODULE_KAIZEN, record_id="gone", instance_completed=True
    )
    assert applied is False


@pytest.mark.asyncio
async def test_rejection_lands_the_status_and_the_reason():
    """reject() does NOT route through _sync_record_status, so without this a
    rejected Kaizen sits at SUBMITTED while its instance reads REJECTED — the
    dead-code bug PTW carried until Phase 6."""
    record = SimpleNamespace(status="SUBMITTED", rejectionReason=None)
    session = _FakeSession(record)
    applied = await be.sync_be_record_status(
        session,
        module=be.WF_MODULE_KAIZEN,
        record_id="k1",
        instance_completed=False,
        rejected=True,
        reason="Not cost-justified",
    )
    assert applied is True
    assert record.status == "REJECTED"
    assert record.rejectionReason == "Not cost-justified"


@pytest.mark.asyncio
async def test_completion_approves_the_record():
    record = SimpleNamespace(status="SUBMITTED", rejectionReason=None)
    session = _FakeSession(record)
    await be.sync_be_record_status(
        session, module=be.WF_MODULE_KAIZEN, record_id="k1", instance_completed=True
    )
    assert record.status == "APPROVED"


@pytest.mark.asyncio
async def test_completion_never_resurrects_a_terminal_record():
    """A repair job that recreates a closure task must not reopen something
    already closed."""
    for terminal in ("CLOSED", "REJECTED", "RETIRED", "SUPERSEDED"):
        record = SimpleNamespace(status=terminal, rejectionReason=None)
        applied = await be.sync_be_record_status(
            _FakeSession(record),
            module=be.WF_MODULE_KAIZEN,
            record_id="k1",
            instance_completed=True,
        )
        assert applied is False
        assert record.status == terminal


@pytest.mark.asyncio
async def test_mid_chain_advance_never_drags_a_record_backwards():
    record = SimpleNamespace(status="SCREENED", rejectionReason=None)
    applied = await be.sync_be_record_status(
        _FakeSession(record),
        module=be.WF_MODULE_KAIZEN,
        record_id="k1",
        instance_completed=False,
    )
    assert applied is False
    assert record.status == "SCREENED"


@pytest.mark.asyncio
async def test_opl_completion_stamps_the_approval_time():
    record = SimpleNamespace(status="IN_REVIEW", rejectionReason=None, approvedAt=None)
    await be.sync_be_record_status(
        _FakeSession(record),
        module=be.WF_MODULE_OPL,
        record_id="o1",
        instance_completed=True,
    )
    assert record.status == "APPROVED"
    assert record.approvedAt is not None


# ─────────────────────────────────────────────────────────────────────────────
# Audience resolution
# ─────────────────────────────────────────────────────────────────────────────
class _AudienceSession:
    """Returns a fixed id list for any select, which is all resolve_opl_audience
    needs — the point under test is the set arithmetic, not the SQL."""

    def __init__(self, ids: list[str]):
        self._ids = ids

    async def execute(self, _stmt):
        ids = self._ids

        class _Result:
            def scalars(self):
                class _S:
                    def all(self_inner):
                        return ids

                return _S()

        return _Result()


@pytest.mark.asyncio
async def test_author_is_never_assigned_their_own_lesson():
    """An acknowledgement from the person who wrote it is not evidence of
    anything."""
    opl = SimpleNamespace(
        id="o1",
        plantId="p1",
        authorId="u-author",
        audience={"roleCodes": [], "areaIds": [], "userIds": ["u-author", "u-other"], "allPlant": False},
    )
    resolved = await be.resolve_opl_audience(_AudienceSession([]), opl)
    assert resolved == ["u-other"]


@pytest.mark.asyncio
async def test_an_empty_audience_resolves_to_nobody():
    """Deliberately NOT "empty means everyone" — an accidental plant-wide
    assignment is a notification nobody asked for."""
    opl = SimpleNamespace(
        id="o1",
        plantId="p1",
        authorId="u-author",
        audience={"roleCodes": [], "areaIds": [], "userIds": [], "allPlant": False},
    )
    assert await be.resolve_opl_audience(_AudienceSession(["u-x"]), opl) == []


@pytest.mark.asyncio
async def test_audience_is_a_union_and_is_deduplicated():
    """Two ticked boxes mean both groups, and a person in both is asked once."""
    opl = SimpleNamespace(
        id="o1",
        plantId="p1",
        authorId="u-author",
        audience={"roleCodes": ["SUPERVISOR"], "areaIds": [], "userIds": ["u-1"], "allPlant": False},
    )
    resolved = await be.resolve_opl_audience(_AudienceSession(["u-1", "u-2"]), opl)
    assert resolved == ["u-1", "u-2"]


# ─────────────────────────────────────────────────────────────────────────────
# Numbering
#
# The bug this guards against has shipped ~15 times in this codebase: a
# generator that counts LIVE rows and adds one. The moment any row is soft
# deleted the live count no longer matches the highest number issued, so the
# next insert re-issues an existing number and 500s on the unique index.
# ─────────────────────────────────────────────────────────────────────────────
class _NumberingSession:
    """Records the execution options the scan was issued with, and returns a
    fixed set of existing numbers."""

    def __init__(self, existing: list[str]):
        self._existing = existing
        self.saw_include_deleted = False

    async def execute(self, stmt):
        # The soft-delete listener reads execution options off the statement;
        # asserting on them is how we prove the scan is not being filtered.
        self.saw_include_deleted = bool(
            getattr(stmt, "get_execution_options", lambda: {})().get("include_deleted")
        )
        existing = self._existing

        class _Result:
            def scalars(self):
                class _S:
                    def all(self_inner):
                        return existing

                return _S()

        return _Result()


@pytest.mark.asyncio
async def test_numbering_uses_max_plus_one_not_a_row_count():
    """0007 exists but only three rows are visible — count+1 would re-issue
    0004 and collide."""
    from app.models.business_excellence import BeKaizen

    session = _NumberingSession(["KZN-2026-0001", "KZN-2026-0003", "KZN-2026-0007"])
    number = await be.next_record_number(
        session,
        model=BeKaizen,
        column=BeKaizen.kaizenNo,
        prefix="KZN",
        plant_id="p1",
        year=2026,
    )
    assert number == "KZN-2026-0008"


@pytest.mark.asyncio
async def test_numbering_scan_opts_out_of_the_soft_delete_filter():
    """These models are registered governed, so every ORM SELECT is rewritten
    to isDeleted=false unless the query opts out. Without the opt-out a deleted
    record's number is handed straight back out."""
    from app.models.business_excellence import BeKaizen

    session = _NumberingSession([])
    await be.next_record_number(
        session, model=BeKaizen, column=BeKaizen.kaizenNo, prefix="KZN", plant_id="p1", year=2026
    )
    assert session.saw_include_deleted is True


@pytest.mark.asyncio
async def test_numbering_starts_at_one_for_an_empty_sequence():
    from app.models.business_excellence import BeOpl

    number = await be.next_record_number(
        _NumberingSession([]),
        model=BeOpl,
        column=BeOpl.oplNo,
        prefix="OPL",
        plant_id="p1",
        year=2026,
    )
    assert number == "OPL-2026-0001"


@pytest.mark.asyncio
async def test_numbering_ignores_a_malformed_row_rather_than_crashing():
    """One hand-edited number must not stop the register issuing the next one."""
    from app.models.business_excellence import BePokaYoke

    number = await be.next_record_number(
        _NumberingSession(["PY-2026-0002", "PY-2026-LEGACY", None]),
        model=BePokaYoke,
        column=BePokaYoke.deviceNo,
        prefix="PY",
        plant_id="p1",
        year=2026,
    )
    assert number == "PY-2026-0003"
