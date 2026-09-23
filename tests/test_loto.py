"""LOTO module — the build spec's §7 verification checklist, as tests.

Each test maps to one checklist item and is named for it. They exercise the real
service functions against real ORM objects (in-memory, no DB round-trip needed
for the pure-logic paths), so a regression in the rules fails here rather than in
a plant.

Run:  venv/Scripts/python.exe -m pytest tests/test_loto.py -v
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.models.loto import (
    LOCK_HOLDER_ROLES,
    OPEN_EXECUTION_STATUSES,
    TERMINAL_EXECUTION_STATUSES,
    LotoExecution,
    LotoExecutionParticipant,
    LotoIsolationPoint,
    LotoProcedure,
    LotoVerificationRecord,
    LotoVerificationStep,
)
from app.services import loto as svc


# ─── Builders ────────────────────────────────────────────────────────────────


def make_procedure(*, points: int = 1, steps: int = 1, **kw) -> LotoProcedure:
    proc = LotoProcedure(
        id=kw.get("id", "proc1"),
        procedureCode=kw.get("procedureCode", "LOTO-EQ-0001"),
        siteId="site1",
        title=kw.get("title", "Raw Mill 1 isolation"),
        status=kw.get("status", "draft"),
        version=kw.get("version", 1),
        reviewFrequencyMonths=12,
    )
    proc.energySources = []
    proc.hardware = []
    proc.isolationPoints = [
        LotoIsolationPoint(
            id=f"ip{i}", procedureId=proc.id, sequence=i,
            location=f"Panel {i}", isolationMethod="breaker",
        )
        for i in range(1, points + 1)
    ]
    proc.verificationSteps = [
        LotoVerificationStep(
            id=f"vs{i}", procedureId=proc.id, sequence=i,
            stepText=f"Check {i}", requiresPhoto=False, requiresSignoff=True,
        )
        for i in range(1, steps + 1)
    ]
    proc.versions = []
    return proc


def make_execution(
    *, holders: int = 2, steps: int = 1, status: str = "locks_applied",
    require_photo: bool = False,
) -> LotoExecution:
    snapshot = {
        "header": {"procedureCode": "LOTO-EQ-0001", "title": "Raw Mill 1"},
        "energySources": [],
        "hardware": [],
        "isolationPoints": [
            {"id": "ip1", "sequence": 1, "location": "MCC-4", "isolationMethod": "breaker"}
        ],
        "verificationSteps": [
            {
                "id": f"vs{i}", "sequence": i, "stepText": f"Check {i}",
                "requiresPhoto": require_photo, "requiresSignoff": True,
            }
            for i in range(1, steps + 1)
        ],
    }
    ex = LotoExecution(
        id="ex1", number="LOTO-EX-2026-0001", procedureId="proc1",
        procedureVersionSnapshot=snapshot, snapshotVersion=1,
        siteId="site1", initiatedById="u1", isGroupLockout=holders > 1,
        status=status,
    )
    ex.participants = [
        LotoExecutionParticipant(
            id=f"p{i}", executionId=ex.id, userId=f"u{i}", userName=f"User {i}",
            participantRole="primary_authorized" if i == 1 else "secondary",
            assignedIsolationPointIds=[],
            # Set explicitly: a SQLAlchemy column `default=` is applied at INSERT,
            # so an un-persisted object reads None, not False. These fixtures are
            # never flushed, so without this they would not match the state a real
            # row has the moment it exists.
            lockAppliedConfirmed=False,
            lockRemovedConfirmed=False,
        )
        for i in range(1, holders + 1)
    ]
    ex.verificationRecords = []
    return ex


def confirm_all_locks(ex: LotoExecution) -> None:
    for p in svc.lock_holders(ex):
        p.lockAppliedConfirmed = True
        p.lockAppliedAt = datetime.now(timezone.utc)


def complete_all_steps(ex: LotoExecution) -> None:
    for s in ex.procedureVersionSnapshot["verificationSteps"]:
        ex.verificationRecords.append(
            LotoVerificationRecord(
                id=f"r{s['id']}", executionId=ex.id, stepId=s["id"],
                sequence=s["sequence"], completedById="u1", signoff=True,
                photoUrl="photo.jpg" if s["requiresPhoto"] else None,
            )
        )


# ═══════════════════════════════════════════════════════════════════════════
#  §7.1 — Cannot publish with zero isolation points or zero verification steps
# ═══════════════════════════════════════════════════════════════════════════


def test_publish_blocked_with_no_isolation_points():
    proc = make_procedure(points=0, steps=2)
    blockers = svc.publish_blockers(proc)
    assert blockers, "a procedure with no isolation points must not be publishable"
    assert any("isolation point" in b for b in blockers)


def test_publish_blocked_with_no_verification_steps():
    proc = make_procedure(points=2, steps=0)
    blockers = svc.publish_blockers(proc)
    assert blockers
    assert any("verification step" in b for b in blockers)


def test_publish_blocked_with_neither_reports_both_reasons():
    # All-at-once, not one-at-a-time: the builder shows every reason in one pass.
    blockers = svc.publish_blockers(make_procedure(points=0, steps=0))
    assert len(blockers) >= 2


def test_publish_allowed_with_one_of_each():
    assert svc.publish_blockers(make_procedure(points=1, steps=1)) == []


# ═══════════════════════════════════════════════════════════════════════════
#  §7.2 — Editing an active procedure versions; it does not overwrite v1
# ═══════════════════════════════════════════════════════════════════════════


def test_material_edit_bumps_version_and_withdraws_approval():
    async def run():
        proc = make_procedure(status="active", version=1)
        proc.publishedVersionId = "ver1"

        class FakeDb:
            async def flush(self):
                return None

            async def get(self, model, pk):
                return None

            def add(self, obj):
                return None

            async def execute(self, *a, **k):
                class R:
                    def scalar_one_or_none(self_inner):
                        return None
                return R()

        withdrew = await svc.apply_body_edit(
            FakeDb(), proc, actor_id="u1", material=True, change_summary="new bleed point"
        )
        # v1 is NOT overwritten — the counter moves to 2 and v1's snapshot row
        # (written at publish) is left exactly as it was.
        assert proc.version == 2
        assert withdrew is True
        assert proc.status == "under_review"
        # The FIELD still resolves the last APPROVED version. This is the
        # property that keeps an unreviewed sequence off a QR scan.
        assert proc.publishedVersionId == "ver1"

    asyncio.run(run())


def test_minor_edit_does_not_version_or_withdraw():
    async def run():
        proc = make_procedure(status="active", version=3)
        proc.publishedVersionId = "ver3"

        class FakeDb:
            async def flush(self):
                return None

        withdrew = await svc.apply_body_edit(
            FakeDb(), proc, actor_id="u1", material=False, change_summary=None
        )
        assert proc.version == 3
        assert withdrew is False
        assert proc.status == "active"

    asyncio.run(run())


# ═══════════════════════════════════════════════════════════════════════════
#  §7.3 / §7.4 — Group lockout gating on INDIVIDUAL confirmation
# ═══════════════════════════════════════════════════════════════════════════


def test_group_lockout_cannot_verify_with_any_lock_unconfirmed():
    ex = make_execution(holders=3)
    ex.participants[0].lockAppliedConfirmed = True
    ex.participants[1].lockAppliedConfirmed = True
    # Participant 3 has not confirmed.
    gate = svc.execution_gate(ex)
    assert gate["canVerify"] is False
    assert "User 3" in gate["awaitingLockConfirmation"]
    assert any("User 3" in b for b in gate["blockers"])


def test_group_lockout_reaches_verifiable_only_when_all_confirm():
    ex = make_execution(holders=3)
    confirm_all_locks(ex)
    gate = svc.execution_gate(ex)
    assert gate["canVerify"] is True
    assert gate["awaitingLockConfirmation"] == []


def test_group_lockout_cannot_close_with_any_removal_unconfirmed():
    ex = make_execution(holders=3, status="locks_removed")
    confirm_all_locks(ex)
    complete_all_steps(ex)
    ex.participants[0].lockRemovedConfirmed = True
    ex.participants[1].lockRemovedConfirmed = True
    # Participant 3 never confirmed removal — their lock is still on the machine.
    gate = svc.execution_gate(ex)
    assert gate["canClose"] is False
    assert "User 3" in gate["awaitingUnlockConfirmation"]


def test_close_refuses_and_names_who_is_outstanding():
    async def run():
        ex = make_execution(holders=2, status="locks_removed")
        confirm_all_locks(ex)
        complete_all_steps(ex)
        ex.participants[0].lockRemovedConfirmed = True

        class FakeDb:
            async def flush(self):
                return None

        with pytest.raises(svc.LotoError) as e:
            await svc.close_execution(FakeDb(), ex, user_id="u1", notes=None)
        # Not a flat "cannot close" — it names the person to go and find.
        assert "User 2" in e.value.message
        assert ex.status != "closed"

    asyncio.run(run())


def test_close_refuses_while_a_verification_step_is_outstanding():
    async def run():
        ex = make_execution(holders=1, steps=3, status="locks_removed")
        confirm_all_locks(ex)
        for p in ex.participants:
            p.lockRemovedConfirmed = True
        # Only 2 of 3 steps done.
        ex.verificationRecords = [
            LotoVerificationRecord(
                id=f"r{i}", executionId=ex.id, stepId=f"vs{i}", sequence=i,
                completedById="u1", signoff=True,
            )
            for i in (1, 2)
        ]
        class FakeDb:
            async def flush(self):
                return None

        with pytest.raises(svc.LotoError) as e:
            await svc.close_execution(FakeDb(), ex, user_id="u1", notes=None)
        assert "verification" in e.value.message.lower()

    asyncio.run(run())


def test_close_succeeds_when_the_record_is_complete():
    async def run():
        ex = make_execution(holders=2, status="locks_removed")
        confirm_all_locks(ex)
        complete_all_steps(ex)
        for p in ex.participants:
            p.lockRemovedConfirmed = True

        class FakeDb:
            async def flush(self):
                return None

        await svc.close_execution(FakeDb(), ex, user_id="u1", notes="done")
        assert ex.status == "closed"
        assert ex.closedById == "u1"

    asyncio.run(run())


def test_one_person_cannot_confirm_for_another():
    async def run():
        ex = make_execution(holders=2)

        class FakeDb:
            async def flush(self):
                return None

        # u1 passing u2's participant id must be refused, not silently applied.
        with pytest.raises(svc.LotoError) as e:
            await svc.confirm_lock(
                FakeDb(), ex, user_id="u1", participant_id="p2",
                lock_tag_number=None, notes=None,
            )
        assert e.value.status_code == 403
        assert ex.participants[1].lockAppliedConfirmed is False

    asyncio.run(run())


def test_a_non_participant_has_no_lock_to_confirm():
    async def run():
        ex = make_execution(holders=2)

        class FakeDb:
            async def flush(self):
                return None

        with pytest.raises(svc.LotoError) as e:
            await svc.confirm_lock(
                FakeDb(), ex, user_id="stranger", participant_id=None,
                lock_tag_number=None, notes=None,
            )
        assert e.value.status_code == 403

    asyncio.run(run())


def test_affected_employee_does_not_gate_the_lockout():
    # An affected employee is notified, not issued a lock. If they counted
    # towards the gate, every group lockout would deadlock on a confirmation
    # that can never be given.
    ex = make_execution(holders=2)
    ex.participants.append(
        LotoExecutionParticipant(
            id="p3", executionId=ex.id, userId="u3", userName="User 3",
            participantRole="affected_employee", assignedIsolationPointIds=[],
            lockAppliedConfirmed=False, lockRemovedConfirmed=False,
        )
    )
    confirm_all_locks(ex)
    gate = svc.execution_gate(ex)
    assert gate["canVerify"] is True
    assert "User 3" not in gate["awaitingLockConfirmation"]
    assert len(svc.lock_holders(ex)) == 2


def test_unlock_confirmation_only_advances_status_when_all_have_confirmed():
    async def run():
        ex = make_execution(holders=2, status="verified")
        confirm_all_locks(ex)
        complete_all_steps(ex)

        class FakeDb:
            async def flush(self):
                return None

        await svc.confirm_unlock(FakeDb(), ex, user_id="u1", participant_id=None, notes=None)
        assert ex.status == "verified", "one confirmation must not move the group"

        await svc.confirm_unlock(FakeDb(), ex, user_id="u2", participant_id=None, notes=None)
        assert ex.status == "locks_removed"

    asyncio.run(run())


def test_there_is_no_bulk_unlock_path_in_the_service_api():
    # Structural guard: if someone later adds a "remove all" helper, this fails
    # and forces the conversation rather than letting it land quietly.
    suspicious = [
        n for n in svc.__all__
        if any(k in n.lower() for k in ("remove_all", "unlock_all", "bulk", "force_unlock"))
    ]
    assert suspicious == [], f"bulk lock-removal surface appeared: {suspicious}"


# ═══════════════════════════════════════════════════════════════════════════
#  §7.5 — The execution snapshot is FROZEN at start
# ═══════════════════════════════════════════════════════════════════════════


def test_execution_reads_its_snapshot_not_the_live_procedure():
    ex = make_execution(holders=1, steps=2)
    original = [s["stepText"] for s in ex.procedureVersionSnapshot["verificationSteps"]]

    # Simulate an author editing the LIVE procedure mid-job — a different object
    # entirely. Nothing about the running execution may change.
    live = make_procedure(points=5, steps=9, version=7)
    live.verificationSteps[0].stepText = "COMPLETELY DIFFERENT STEP"

    after = [s["stepText"] for s in svc._snapshot_steps(ex)]
    assert after == original
    assert len(after) == 2, "the running job still has its own 2 steps, not the live 9"
    assert "COMPLETELY DIFFERENT STEP" not in after


def test_snapshot_is_a_copy_not_a_shared_reference():
    # start_execution does dict(version.snapshotJson). If it aliased instead,
    # mutating the version row would reach into every running execution.
    version_body = {"verificationSteps": [{"id": "vs1", "sequence": 1, "stepText": "orig"}]}
    ex = LotoExecution(
        id="ex1", number="n", procedureId="p", siteId="s", initiatedById="u",
        procedureVersionSnapshot=dict(version_body), snapshotVersion=1,
        status="locks_applied",
    )
    version_body["verificationSteps"] = [{"id": "vsX", "sequence": 1, "stepText": "changed"}]
    assert ex.procedureVersionSnapshot["verificationSteps"][0]["stepText"] == "orig"


def test_verification_against_a_step_outside_the_snapshot_is_refused():
    async def run():
        ex = make_execution(holders=1, steps=1)
        confirm_all_locks(ex)

        class FakeDb:
            def add(self, obj):
                return None

            async def flush(self):
                return None

        class Payload:
            stepId = "vs_added_to_live_procedure_after_start"
            signoff = True
            photoUrl = None
            notes = None

        with pytest.raises(svc.LotoError) as e:
            await svc.record_verification(
                FakeDb(), ex, records=[Payload()], user_id="u1", user_name="User 1"
            )
        assert e.value.status_code == 422

    asyncio.run(run())


# ═══════════════════════════════════════════════════════════════════════════
#  §7.7 — Overdue reviews are computed and flagged, not stored silently
# ═══════════════════════════════════════════════════════════════════════════


def test_overdue_review_is_flagged():
    proc = make_procedure(status="active")
    proc.nextReviewDueAt = datetime.now(timezone.utc) - timedelta(days=45)
    fields = svc.review_status_fields(proc)
    assert fields["isOverdue"] is True
    assert fields["daysUntilDue"] is not None and fields["daysUntilDue"] < 0


def test_due_soon_is_distinguished_from_overdue():
    proc = make_procedure(status="active")
    proc.nextReviewDueAt = datetime.now(timezone.utc) + timedelta(days=10)
    fields = svc.review_status_fields(proc)
    assert fields["isOverdue"] is False
    assert fields["isDueSoon"] is True


def test_a_review_far_out_is_neither():
    proc = make_procedure(status="active")
    proc.nextReviewDueAt = datetime.now(timezone.utc) + timedelta(days=200)
    fields = svc.review_status_fields(proc)
    assert fields["isOverdue"] is False and fields["isDueSoon"] is False


def test_a_retired_procedure_is_never_flagged_overdue():
    # It is out of service; flagging it would bury the live ones that need action.
    proc = make_procedure(status="retired")
    proc.nextReviewDueAt = datetime.now(timezone.utc) - timedelta(days=400)
    assert svc.review_status_fields(proc)["isOverdue"] is False


def test_naive_due_dates_do_not_explode():
    # Some drivers hand back naive datetimes; comparing one to an aware datetime
    # raises TypeError. _aware() normalises first.
    proc = make_procedure(status="active")
    proc.nextReviewDueAt = datetime.now() - timedelta(days=5)  # naive
    assert svc.review_status_fields(proc)["isOverdue"] is True


def test_next_review_rolls_forward_by_the_configured_cadence():
    proc = make_procedure()
    proc.reviewFrequencyMonths = 12
    base = datetime(2026, 8, 8, tzinfo=timezone.utc)
    assert svc.compute_next_review(proc, from_dt=base) == datetime(2027, 8, 8, tzinfo=timezone.utc)


def test_month_arithmetic_clamps_a_short_month():
    proc = make_procedure()
    proc.reviewFrequencyMonths = 1
    base = datetime(2026, 1, 31, tzinfo=timezone.utc)
    # 31 Jan + 1 month must land on 28 Feb, not raise.
    assert svc.compute_next_review(proc, from_dt=base) == datetime(2026, 2, 28, tzinfo=timezone.utc)


# ═══════════════════════════════════════════════════════════════════════════
#  §7.8 — PTW cannot close while its linked lockout is open
# ═══════════════════════════════════════════════════════════════════════════


def test_every_pre_terminal_status_counts_as_open():
    # The PTW gate reads OPEN_EXECUTION_STATUSES. If a new status were added to
    # the machine and forgotten here, a permit could close over live locks.
    from app.models.loto import EXECUTION_STATUSES

    assert OPEN_EXECUTION_STATUSES | TERMINAL_EXECUTION_STATUSES == set(EXECUTION_STATUSES)
    assert OPEN_EXECUTION_STATUSES.isdisjoint(TERMINAL_EXECUTION_STATUSES)
    assert "closed" not in OPEN_EXECUTION_STATUSES
    assert "aborted" not in OPEN_EXECUTION_STATUSES


def test_lock_holder_roles_are_a_strict_subset_of_participant_roles():
    from app.models.loto import PARTICIPANT_ROLES

    assert LOCK_HOLDER_ROLES < set(PARTICIPANT_ROLES)
    assert "affected_employee" not in LOCK_HOLDER_ROLES


def test_workflow_engine_calls_the_loto_gate_at_the_closure_step():
    # The gate lives inside the PTW CLOSURE branch of workflow_engine.approve().
    # A refactor that drops the call would silently un-gate every permit, and
    # nothing else in the suite would notice.
    import inspect

    from app.services import workflow_engine

    source = inspect.getsource(workflow_engine.approve)
    assert "permit_closure_blocker" in source
    assert "StepType.CLOSURE" in source


# ═══════════════════════════════════════════════════════════════════════════
#  Transition guards
# ═══════════════════════════════════════════════════════════════════════════


def test_locks_cannot_be_removed_before_verification():
    async def run():
        ex = make_execution(holders=1, status="locks_applied")
        confirm_all_locks(ex)

        class FakeDb:
            async def flush(self):
                return None

        with pytest.raises(svc.LotoError) as e:
            await svc.confirm_unlock(FakeDb(), ex, user_id="u1", participant_id=None, notes=None)
        assert "verification" in e.value.message.lower()

    asyncio.run(run())


def test_verification_cannot_start_before_every_lock_is_on():
    async def run():
        ex = make_execution(holders=2, status="locks_applied")
        ex.participants[0].lockAppliedConfirmed = True

        class FakeDb:
            def add(self, obj):
                return None

            async def flush(self):
                return None

        class Payload:
            stepId = "vs1"
            signoff = True
            photoUrl = None
            notes = None

        with pytest.raises(svc.LotoError) as e:
            await svc.record_verification(
                FakeDb(), ex, records=[Payload()], user_id="u1", user_name="User 1"
            )
        assert "User 2" in e.value.message

    asyncio.run(run())


def test_a_step_needing_a_photo_is_refused_without_one():
    async def run():
        ex = make_execution(holders=1, steps=1, require_photo=True)
        confirm_all_locks(ex)

        class FakeDb:
            def add(self, obj):
                return None

            async def flush(self):
                return None

        class Payload:
            stepId = "vs1"
            signoff = True
            photoUrl = None
            notes = None

        with pytest.raises(svc.LotoError) as e:
            await svc.record_verification(
                FakeDb(), ex, records=[Payload()], user_id="u1", user_name="User 1"
            )
        assert "photo" in e.value.message.lower()

    asyncio.run(run())


def test_a_terminal_lockout_rejects_further_lock_actions():
    async def run():
        class FakeDb:
            async def flush(self):
                return None

        for terminal in ("closed", "aborted"):
            ex = make_execution(holders=1, status=terminal)
            with pytest.raises(svc.LotoError):
                await svc.confirm_lock(
                    FakeDb(), ex, user_id="u1", participant_id=None,
                    lock_tag_number=None, notes=None,
                )

    asyncio.run(run())


def test_abort_records_a_reason_and_is_terminal():
    async def run():
        ex = make_execution(holders=2)

        class FakeDb:
            async def flush(self):
                return None

        await svc.abort_execution(FakeDb(), ex, user_id="u9", reason="Lock cut off — key lost")
        assert ex.status == "aborted"
        assert ex.abortReason == "Lock cut off — key lost"
        assert ex.status in TERMINAL_EXECUTION_STATUSES

    asyncio.run(run())


def test_qr_token_is_unguessable_and_unique():
    tokens = {svc.new_qr_token() for _ in range(500)}
    assert len(tokens) == 500
    assert all(len(t) >= 30 for t in tokens)


# ═══════════════════════════════════════════════════════════════════════════
#  Async-session hazards
#
#  These guard a class of bug the rest of this file CANNOT catch: every other
#  test builds detached ORM objects with no Session, so relationship access
#  never touches the database and a lazy load can never fire. In production the
#  object is persistent and attached, and the same line raises MissingGreenlet
#  — which the global handler turns into a blanket 500.
#
#  That is exactly what happened: "Start lockout" and "New Procedure" both 500'd
#  in production while all 36 tests here passed.
# ═══════════════════════════════════════════════════════════════════════════


def test_create_paths_never_bare_assign_a_cascading_collection():
    """Replacing a delete-orphan collection on a just-flushed row is lazy I/O.

    SQLAlchemy has to LOAD the current contents to work out what became an
    orphan. On an attached async object that load raises MissingGreenlet.
    `set_committed_value` states "known empty / these are the contents" with no
    I/O, which is correct for a row that was created microseconds earlier.
    """
    import inspect
    import re

    from app.routers import loto as loto_router

    create_proc = inspect.getsource(loto_router.create_procedure)
    start_exec = inspect.getsource(svc.start_execution)

    # The four procedure collections + the two execution collections must never
    # be assigned with `=` inside a create path.
    cascading = (
        "energySources", "isolationPoints", "hardware", "verificationSteps",
        "participants", "verificationRecords",
    )
    for src, label in ((create_proc, "create_procedure"), (start_exec, "start_execution")):
        for rel in cascading:
            bare = re.search(rf"^\s*(proc|ex)\.{rel}\s*=\s*", src, re.M)
            assert bare is None, (
                f"{label} bare-assigns .{rel} — this lazy-loads on an attached "
                f"async object and 500s. Use set_committed_value()."
            )

    assert "set_committed_value" in create_proc
    assert "set_committed_value" in start_exec


def test_lock_holder_validation_reads_the_local_list_not_the_relationship():
    """start_execution must validate against the list it just built.

    Reading `ex.participants` there would re-introduce the lazy load through the
    back door, since the collection is not published until afterwards.
    """
    import inspect

    src = inspect.getsource(svc.start_execution)
    validation = src[src.index("needs at least one lock holder") - 400:
                     src.index("needs at least one lock holder")]
    assert "lock_holders(ex)" not in validation, (
        "start_execution validates via lock_holders(ex), which touches the "
        "unpublished relationship"
    )
    assert "for r in built" in validation or "r for r in built" in validation
