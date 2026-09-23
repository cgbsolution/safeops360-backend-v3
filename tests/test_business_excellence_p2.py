"""Tests for Business Excellence Phase 2 — Suggestion, QCC, SIP, benefit.

Offline, in the same house style as test_business_excellence.py: the state
machines, the RAG derivations and the Pydantic validators need no DB, and the
handful of functions that do take a session are exercised against a fake that
records what would be written.

The cases here are chosen for the mistakes this platform has actually made
before, plus the three this build could newly make:
  * a status missing from a transition table, silently stranding a record
  * an overdue / red flag that fires on a finished record
  * a percentage reported as 0 when the truth is "undefined"
  * the Phase 1 → Phase 2 workflow-token literals drifting apart
  * a separation-of-duties gate that a circle member can walk through
  * anonymity leaking through a payload that built its own dict
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.models.business_excellence_p2 import (
    BENEFIT_STATUSES,
    QCC_DMAIC_STAGES,
    QCC_PDCA_STAGES,
    QCC_PROJECT_STATUSES,
    QCC_RCA_STAGE,
    QCC_STAGES_BY_METHODOLOGY,
    SIP_STATUSES,
    SUGGESTION_STATUSES,
    VALIDATION_WINDOW_DAYS,
    VALIDATION_WINDOW_MONTHS,
)
from app.schemas.business_excellence_p2 import (
    BenefitCreate,
    QccProjectCreate,
    SipCreate,
    SuggestionCreate,
    SuggestionDecide,
    SuggestionScreen,
)
from app.services import business_excellence as be
from app.services import business_excellence_p2 as be2


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ═════════════════════════════════════════════════════════════════════════════
# The Phase 1 ↔ Phase 2 seam
# ═════════════════════════════════════════════════════════════════════════════
def test_workflow_tokens_match_across_the_two_service_modules():
    """business_excellence.py writes the Phase 2 tokens as LITERALS to avoid a
    circular import. If they ever drift, the engine's
    `module in BE_WORKFLOW_MODULES` gate stops matching and every Phase 2
    approval silently stops landing on its record — with no error anywhere."""
    assert be.WF_MODULE_SUGGESTION == be2.WF_MODULE_SUGGESTION
    assert be.WF_MODULE_QCC == be2.WF_MODULE_QCC
    assert be.WF_MODULE_SIP == be2.WF_MODULE_SIP


def test_phase2_modules_are_inside_the_engines_gate():
    assert be2.P2_WORKFLOW_MODULES <= be.BE_WORKFLOW_MODULES


def test_phase1_modules_still_route_to_phase1():
    """Widening BE_WORKFLOW_MODULES must not have made Kaizen/OPL/Poka Yoke fall
    through to the Phase 2 delegate."""
    for token in (be.WF_MODULE_KAIZEN, be.WF_MODULE_OPL, be.WF_MODULE_POKA_YOKE):
        assert token not in be2.P2_WORKFLOW_MODULES


# ═════════════════════════════════════════════════════════════════════════════
# Suggestion Scheme
# ═════════════════════════════════════════════════════════════════════════════
def _suggestion(**kw) -> SimpleNamespace:
    base = dict(
        id="s1",
        status="DRAFT",
        isAnonymous=False,
        createdById="u-author",
        targetDate=None,
        deferredUntil=None,
        screeningOutcome=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_every_suggestion_status_has_a_transition_entry():
    missing = [s for s in SUGGESTION_STATUSES if s not in be2.SUGGESTION_TRANSITIONS]
    assert missing == [], f"statuses with no transition entry: {missing}"


def test_suggestion_transitions_only_target_real_statuses():
    for source, targets in be2.SUGGESTION_TRANSITIONS.items():
        for target in targets:
            assert target in SUGGESTION_STATUSES, f"{source} → unknown status {target}"


def test_terminal_suggestions_offer_nothing():
    for terminal in be2.SUGGESTION_TERMINAL:
        s = _suggestion(status=terminal)
        assert be2.allowed_suggestion_actions(s, {"SUGGESTION.DECIDE", "SUGGESTION.SCREEN", "SUGGESTION.UPDATE"}) == []


def test_screen_and_decide_are_separate_permissions():
    """§3 describes two acts by two groups. Somebody holding only SCREEN must not
    be able to accept a suggestion — that is what would let whoever clears the
    inbox also approve the incentive attached to it."""
    s = _suggestion(status="SCREENING", screeningOutcome="RELEVANT")
    screener_only = be2.allowed_suggestion_actions(s, {"SUGGESTION.SCREEN"})
    assert "ACCEPTED" not in screener_only
    assert "REJECTED" not in screener_only
    assert "DEFERRED" not in screener_only
    # DUPLICATE is a triage outcome and IS available to a screener.
    assert "DUPLICATE" in screener_only


def test_deferred_suggestion_returns_through_screening_not_straight_to_accepted():
    s = _suggestion(status="DEFERRED")
    actions = be2.allowed_suggestion_actions(
        s, {"SUGGESTION.UPDATE", "SUGGESTION.DECIDE"}
    )
    assert "SUBMITTED" in actions
    assert "ACCEPTED" not in actions


def test_overdue_ignores_finished_and_parked_records():
    """An overdue flag on a finished record is noise that trains people to
    ignore the column."""
    past = _now() - timedelta(days=30)
    for st in ("CLOSED", "REJECTED", "DUPLICATE", "IMPLEMENTED", "DEFERRED"):
        assert not be2.is_suggestion_overdue(_suggestion(status=st, targetDate=past))
    assert be2.is_suggestion_overdue(
        _suggestion(status="IN_IMPLEMENTATION", targetDate=past)
    )


def test_overdue_is_false_with_no_target_date():
    assert not be2.is_suggestion_overdue(
        _suggestion(status="IN_IMPLEMENTATION", targetDate=None)
    )


def test_deferral_becomes_due_when_its_date_passes():
    """Without this a DEFER is indistinguishable from a quiet rejection."""
    assert be2.is_deferral_due(
        _suggestion(status="DEFERRED", deferredUntil=_now() - timedelta(days=1))
    )
    assert not be2.is_deferral_due(
        _suggestion(status="DEFERRED", deferredUntil=_now() + timedelta(days=30))
    )
    # Not deferred at all → never "due".
    assert not be2.is_deferral_due(
        _suggestion(status="ACCEPTED", deferredUntil=_now() - timedelta(days=1))
    )


def test_naive_datetimes_do_not_raise():
    """Rows written before a column was timezone-aware come back naive from
    asyncpg, and a naive ↔ aware comparison raises TypeError mid-request."""
    naive = (_now() - timedelta(days=5)).replace(tzinfo=None)
    assert be2.is_suggestion_overdue(
        _suggestion(status="IN_IMPLEMENTATION", targetDate=naive)
    )


# ── Anonymity ────────────────────────────────────────────────────────────────
def test_anonymous_submitter_is_hidden_from_everyone_but_themselves():
    s = _suggestion(isAnonymous=True, createdById="u-author")
    assert be2.submitter_visible_to(s, "u-author", set())
    assert not be2.submitter_visible_to(s, "u-someone-else", set())


def test_no_permission_unmasks_an_anonymous_submitter():
    """Deliberately including administrators. A scheme where a manager can
    unmask a critic is not a scheme people use twice."""
    s = _suggestion(isAnonymous=True, createdById="u-author")
    god = {
        "SUGGESTION.READ",
        "SUGGESTION.SCREEN",
        "SUGGESTION.DECIDE",
        "SUGGESTION.DELETE",
        "SYSTEM.ADMIN",
    }
    assert not be2.submitter_visible_to(s, "u-admin", god)


def test_non_anonymous_submitter_is_visible_to_any_reader():
    s = _suggestion(isAnonymous=False, createdById="u-author")
    assert be2.submitter_visible_to(s, "u-anyone", set())


def test_suggestion_update_schema_cannot_change_anonymity():
    """Un-anonymising after the fact would retroactively expose someone who
    chose not to be named."""
    from app.schemas.business_excellence_p2 import SuggestionUpdate

    assert "isAnonymous" not in SuggestionUpdate.model_fields


# ── Suggestion validators ────────────────────────────────────────────────────
def test_duplicate_triage_must_name_the_original():
    with pytest.raises(ValidationError):
        SuggestionScreen(outcome="DUPLICATE")
    SuggestionScreen(outcome="DUPLICATE", duplicateOfSuggestionId="s-other")


def test_defer_requires_a_return_date():
    with pytest.raises(ValidationError):
        SuggestionDecide(decision="DEFER", rationale="Good idea, no budget yet.")
    SuggestionDecide(
        decision="DEFER",
        rationale="Good idea, no budget yet.",
        deferredUntil=_now() + timedelta(days=90),
    )


def test_a_non_defer_decision_rejects_a_deferral_date():
    with pytest.raises(ValidationError):
        SuggestionDecide(
            decision="ACCEPT",
            rationale="Clear saving, low effort.",
            deferredUntil=_now() + timedelta(days=90),
        )


def test_rationale_is_required_on_acceptance_too():
    """§3: rationale "captured against every outcome". A scheme that explains
    its rejections but not its acceptances teaches people nothing."""
    with pytest.raises(ValidationError):
        SuggestionDecide(decision="ACCEPT", rationale="")


def test_suggestion_create_requires_a_real_description():
    with pytest.raises(ValidationError):
        SuggestionCreate(
            plantId="p1", title="Fix it", category="QUALITY", description="short"
        )


# ═════════════════════════════════════════════════════════════════════════════
# QCC
# ═════════════════════════════════════════════════════════════════════════════
def _project(**kw) -> SimpleNamespace:
    base = dict(
        id="p1",
        status="IN_PROGRESS",
        methodology="DMAIC",
        teamId="t1",
        createdById="u-leader",
        rcaId=None,
        targetDate=None,
        charteredAt=None,
        plantId="pl1",
        title="Cut sleeve rework",
        category="QUALITY",
        problemStatement="Rework at sleeve attach is 4.2%.",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _stage(stage, seq, status="NOT_STARTED", summary=None, target=None) -> SimpleNamespace:
    return SimpleNamespace(
        stage=stage,
        sequence=seq,
        status=status,
        summary=summary,
        targetDate=target,
        signedOffById=None,
        signedOffAt=None,
    )


def _dmaic(statuses: dict[str, str] | None = None) -> list[SimpleNamespace]:
    statuses = statuses or {}
    return [
        _stage(s, i, statuses.get(s, "NOT_STARTED"), summary="done")
        for i, s in enumerate(QCC_DMAIC_STAGES)
    ]


def test_every_qcc_status_has_a_transition_entry():
    missing = [s for s in QCC_PROJECT_STATUSES if s not in be2.QCC_PROJECT_TRANSITIONS]
    assert missing == [], f"statuses with no transition entry: {missing}"


def test_qcc_transitions_only_target_real_statuses():
    for source, targets in be2.QCC_PROJECT_TRANSITIONS.items():
        for target in targets:
            assert target in QCC_PROJECT_STATUSES, f"{source} → unknown status {target}"


def test_both_methodologies_have_stages_and_an_rca_gate():
    for m, stages in QCC_STAGES_BY_METHODOLOGY.items():
        assert stages, f"{m} has no stages"
        assert m in QCC_RCA_STAGE, f"{m} has no RCA gate"
        assert QCC_RCA_STAGE[m] in stages, f"{m}'s RCA gate is not one of its stages"


def test_stages_for_degrades_to_dmaic_rather_than_raising():
    """A project with a typo'd methodology should still render a board, not 500
    the detail screen."""
    assert be2.stages_for("NONSENSE") == QCC_DMAIC_STAGES


def test_build_project_stages_materialises_the_whole_path_up_front():
    """The board has to show a circle the whole path with un-started gates
    greyed out. One that only renders the stages already reached tells them
    nothing about what is coming."""
    p = _project(methodology="PDCA")
    stages = be2.build_project_stages(p)
    assert [s.stage for s in stages] == list(QCC_PDCA_STAGES)
    assert stages[0].status == "IN_PROGRESS"
    assert all(s.status == "NOT_STARTED" for s in stages[1:])
    assert stages[0].startedAt is not None


def test_analyze_cannot_be_signed_off_without_the_shared_rca():
    """§6: the analysis stage "draws on the platform's shared RCA engine". The
    gate is what makes that true rather than aspirational."""
    p = _project(rcaId=None)
    stages = _dmaic({"DEFINE": "SIGNED_OFF", "MEASURE": "SIGNED_OFF", "ANALYZE": "IN_PROGRESS"})
    analyze = next(s for s in stages if s.stage == "ANALYZE")
    blockers = be2.stage_signoff_blockers(p, analyze, stages)
    assert any("root-cause analysis" in b for b in blockers)

    p.rcaId = "rca-1"
    assert be2.stage_signoff_blockers(p, analyze, stages) == []


def test_pdca_puts_the_rca_gate_on_plan_not_analyze():
    p = _project(methodology="PDCA", rcaId=None)
    assert be2.rca_stage_for(p) == "PLAN"
    stages = [_stage(s, i, "IN_PROGRESS" if s == "PLAN" else "NOT_STARTED", summary="x")
              for i, s in enumerate(QCC_PDCA_STAGES)]
    plan = stages[0]
    assert any("root-cause" in b for b in be2.stage_signoff_blockers(p, plan, stages))


def test_gates_are_sequential():
    """Skipping one would let a circle sign off Control on a project whose
    measurement was never done."""
    p = _project(rcaId="rca-1")
    stages = _dmaic()
    control = next(s for s in stages if s.stage == "CONTROL")
    blockers = be2.stage_signoff_blockers(p, control, stages)
    assert any("before this stage" in b for b in blockers)


def test_a_gate_needs_a_summary_before_sign_off():
    p = _project(rcaId="rca-1")
    stages = _dmaic()
    define = stages[0]
    define.summary = "   "
    assert any("what this stage concluded" in b for b in be2.stage_signoff_blockers(p, define, stages))


def test_an_already_signed_off_gate_reports_that_and_stops():
    p = _project(rcaId="rca-1")
    stages = _dmaic({"DEFINE": "SIGNED_OFF"})
    define = stages[0]
    blockers = be2.stage_signoff_blockers(p, define, stages)
    assert len(blockers) == 1
    assert "already been signed off" in blockers[0]


def test_project_cannot_complete_with_open_gates():
    p = _project()
    stages = _dmaic({"DEFINE": "SIGNED_OFF"})
    blockers = be2.close_gate_blockers(p, stages, [], "COMPLETED")
    assert any("still open" in b for b in blockers)


def test_project_completes_when_every_gate_is_signed_off_or_skipped():
    p = _project()
    stages = _dmaic({s: "SIGNED_OFF" for s in QCC_DMAIC_STAGES})
    stages[-1].status = "SKIPPED"
    assert be2.close_gate_blockers(p, stages, [], "COMPLETED") == []


def test_project_cannot_close_without_a_validated_benefit():
    """§6: a benefit counts only once someone outside the circle has confirmed
    it held."""
    p = _project(status="BENEFIT_VALIDATION")
    stages = _dmaic({s: "SIGNED_OFF" for s in QCC_DMAIC_STAGES})
    assert any("Record the project's benefit" in b for b in be2.close_gate_blockers(p, stages, [], "CLOSED"))

    pending = SimpleNamespace(status="PENDING_VALIDATION")
    assert any("not been validated" in b for b in be2.close_gate_blockers(p, stages, [pending], "CLOSED"))

    validated = SimpleNamespace(status="VALIDATED")
    assert be2.close_gate_blockers(p, stages, [validated], "CLOSED") == []


# ── QCC RAG ──────────────────────────────────────────────────────────────────
def test_finished_project_is_green_however_it_got_there():
    """The board shows what needs attention, and a closed project needs none.
    History belongs in the dates, not a permanent red badge."""
    past = _now() - timedelta(days=200)
    for st in ("CLOSED", "ABANDONED", "REJECTED", "BENEFIT_VALIDATION"):
        assert be2.project_rag(_project(status=st, targetDate=past), _dmaic()) == "GREEN"


def test_project_past_its_target_date_is_red():
    p = _project(targetDate=_now() - timedelta(days=1))
    assert be2.project_rag(p, _dmaic()) == "RED"


def test_a_slightly_late_gate_is_amber_and_a_badly_late_one_is_red():
    p = _project(targetDate=_now() + timedelta(days=120))
    slightly = _dmaic()
    slightly[1].targetDate = _now() - timedelta(days=3)
    assert be2.project_rag(p, slightly) == "AMBER"

    badly = _dmaic()
    badly[1].targetDate = _now() - timedelta(days=60)
    assert be2.project_rag(p, badly) == "RED"


def test_a_signed_off_late_gate_does_not_colour_the_board():
    p = _project(targetDate=_now() + timedelta(days=120))
    stages = _dmaic({"DEFINE": "SIGNED_OFF"})
    stages[0].targetDate = _now() - timedelta(days=60)
    assert be2.project_rag(p, stages) == "GREEN"


def test_project_close_to_its_target_with_open_gates_is_amber():
    p = _project(targetDate=_now() + timedelta(days=5))
    assert be2.project_rag(p, _dmaic()) == "AMBER"


def test_project_progress_reports_none_not_zero_when_there_are_no_stages():
    """"Not chartered yet" and "chartered and nothing done" are different
    facts, and a 0% badge on the first reads as a failure that has not
    happened."""
    assert be2.project_progress([])["percent"] is None
    assert be2.project_progress(_dmaic())["percent"] == 0.0


def test_project_progress_names_the_current_gate():
    stages = _dmaic({"DEFINE": "SIGNED_OFF", "MEASURE": "IN_PROGRESS"})
    p = be2.project_progress(stages)
    assert p["currentStage"] == "MEASURE"
    assert p["signedOffStages"] == 1
    assert p["totalStages"] == 5


# ── QCC schema validators ────────────────────────────────────────────────────
def test_a_target_without_a_baseline_is_rejected():
    """A target with no baseline cannot be measured against anything, and the
    benefit that comes out at the end would be unfalsifiable."""
    with pytest.raises(ValidationError):
        QccProjectCreate(
            teamId="t1",
            plantId="p1",
            title="Cut rework",
            category="QUALITY",
            problemStatement="Rework at sleeve attach is 4.2%.",
            targetValue=1.0,
        )


def test_project_update_cannot_change_methodology():
    """Changing it after the charter would orphan the stage rows already
    materialised from it."""
    from app.schemas.business_excellence_p2 import QccProjectUpdate

    assert "methodology" not in QccProjectUpdate.model_fields


# ═════════════════════════════════════════════════════════════════════════════
# SIP
# ═════════════════════════════════════════════════════════════════════════════
def _sip(**kw) -> SimpleNamespace:
    base = dict(
        id="sip1",
        status="IN_PROGRESS",
        targetDate=None,
        baselineValue=None,
        targetValue=None,
        latestActualValue=None,
        lessonsLearned=None,
        ownerId="u-owner",
        sponsorId="u-sponsor",
        createdById="u-owner",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _milestone(**kw) -> SimpleNamespace:
    base = dict(
        name="M1",
        status="NOT_STARTED",
        plannedDate=None,
        revisedDate=None,
        actualDate=None,
        sequence=0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_every_sip_status_has_a_transition_entry():
    missing = [s for s in SIP_STATUSES if s not in be2.SIP_TRANSITIONS]
    assert missing == [], f"statuses with no transition entry: {missing}"


def test_sip_transitions_only_target_real_statuses():
    for source, targets in be2.SIP_TRANSITIONS.items():
        for target in targets:
            assert target in SIP_STATUSES, f"{source} → unknown status {target}"


def test_a_revised_date_supersedes_the_plan_for_lateness():
    m = _milestone(
        plannedDate=_now() - timedelta(days=10),
        revisedDate=_now() + timedelta(days=10),
    )
    assert not be2.is_milestone_late(m)
    # …and the original commitment is still on the row, unmodified.
    assert m.plannedDate < _now()


def test_a_completed_milestone_is_never_late():
    m = _milestone(status="COMPLETED", plannedDate=_now() - timedelta(days=30))
    assert not be2.is_milestone_late(m)


def test_sip_on_hold_is_amber_not_green():
    """A project nobody is working on is exactly the thing a portfolio review
    exists to surface."""
    assert be2.sip_rag(_sip(status="ON_HOLD"), []) == "AMBER"


def test_finished_sip_is_green():
    past = _now() - timedelta(days=100)
    for st in ("CLOSED", "REJECTED", "CANCELLED", "COMPLETED", "BENEFIT_VALIDATION"):
        assert be2.sip_rag(_sip(status=st, targetDate=past), []) == "GREEN"


def test_sip_past_target_is_red():
    assert be2.sip_rag(_sip(targetDate=_now() - timedelta(days=1)), []) == "RED"


def test_a_badly_late_milestone_is_red_and_a_slightly_late_one_amber():
    s = _sip(targetDate=_now() + timedelta(days=120))
    assert be2.sip_rag(s, [_milestone(plannedDate=_now() - timedelta(days=3))]) == "AMBER"
    assert be2.sip_rag(s, [_milestone(plannedDate=_now() - timedelta(days=60))]) == "RED"


def test_an_explicitly_delayed_milestone_is_at_least_amber():
    s = _sip(targetDate=_now() + timedelta(days=120))
    assert be2.sip_rag(s, [_milestone(status="DELAYED")]) == "AMBER"


def test_metric_progress_is_direction_agnostic():
    """A SIP that drives scrap DOWN and one that drives OTIF UP both read as a
    percentage of the distance covered."""
    down = _sip(baselineValue=4.0, targetValue=1.0, latestActualValue=2.5)
    assert be2.sip_metric_progress(down)["percent"] == 50.0
    assert be2.sip_metric_progress(down)["direction"] == "DECREASE"

    up = _sip(baselineValue=80.0, targetValue=95.0, latestActualValue=87.5)
    assert be2.sip_metric_progress(up)["percent"] == 50.0
    assert be2.sip_metric_progress(up)["direction"] == "INCREASE"


def test_metric_progress_is_none_not_zero_when_undefined():
    """A percentage of a zero span is not 0%, it is undefined, and rendering it
    as 0 reads as failure."""
    assert be2.sip_metric_progress(_sip())["percent"] is None
    flat = _sip(baselineValue=5.0, targetValue=5.0, latestActualValue=5.0)
    assert be2.sip_metric_progress(flat)["percent"] is None


def test_moving_the_wrong_way_reports_off_track():
    worse = _sip(baselineValue=4.0, targetValue=1.0, latestActualValue=4.8)
    assert be2.sip_metric_progress(worse)["onTrack"] is False


def test_sip_cannot_close_without_lessons_learned():
    """§7 closure: "with a lessons-learned note captured back into the shared
    repository". The note is the only part of a finished project that helps the
    next one."""
    s = _sip(status="BENEFIT_VALIDATION", lessonsLearned=None)
    validated = SimpleNamespace(status="VALIDATED")
    blockers = be2.sip_close_blockers(s, [], [validated], "CLOSED")
    assert any("lessons-learned" in b for b in blockers)

    s.lessonsLearned = "Fixture wear drove the drift; add it to the PM schedule."
    assert be2.sip_close_blockers(s, [], [validated], "CLOSED") == []


def test_sip_cannot_complete_with_open_milestones():
    s = _sip()
    blockers = be2.sip_close_blockers(s, [_milestone(name="Pilot")], [], "COMPLETED")
    assert any("still open" in b for b in blockers)


def test_cancelled_milestones_do_not_block_completion():
    s = _sip()
    ms = [_milestone(name="Pilot", status="COMPLETED"), _milestone(name="Rollout", status="CANCELLED")]
    assert be2.sip_close_blockers(s, ms, [], "COMPLETED") == []


def test_sponsor_and_owner_must_differ():
    """§7 names both roles separately and they do different jobs. It also
    removes the independent voice the benefit sign-off depends on."""
    with pytest.raises(ValidationError):
        SipCreate(
            plantId="p1",
            title="OTIF recovery",
            category="DELIVERY",
            scope="Improve on-time-in-full across the finishing lines.",
            sponsorId="u1",
            ownerId="u1",
        )


def test_milestone_update_cannot_move_the_planned_date():
    """A tracker that lets the plan follow the actuals always reports green."""
    from app.schemas.business_excellence_p2 import SipMilestoneUpdate

    assert "plannedDate" not in SipMilestoneUpdate.model_fields
    assert "revisedDate" in SipMilestoneUpdate.model_fields


# ═════════════════════════════════════════════════════════════════════════════
# Benefit realisation
# ═════════════════════════════════════════════════════════════════════════════
def _benefit(**kw) -> SimpleNamespace:
    base = dict(
        id="b1",
        status="PENDING_VALIDATION",
        sourceType="KAIZEN",
        sourceId="k1",
        createdById="u-recorder",
        realizedValue=120000.0,
        validatingAuthorityId=None,
        validationDueAt=None,
        valueKind="FINANCIAL",
        projectedValue=100000.0,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class _FakeSession:
    """Records nothing, returns what it was primed with. Enough for the
    separation-of-duties gate, which only ever calls db.get()."""

    def __init__(self, objects: dict | None = None):
        self.objects = objects or {}

    async def get(self, model, pk):
        return self.objects.get((model.__name__, pk))

    async def execute(self, stmt):  # pragma: no cover - not reached in these tests
        raise AssertionError("execute() should not be called by these paths")


def test_validation_window_days_covers_every_offered_window():
    missing = [m for m in VALIDATION_WINDOW_MONTHS if m not in VALIDATION_WINDOW_DAYS]
    assert missing == [], f"windows with no day count: {missing}"


def test_validation_due_is_none_without_a_window():
    assert be2.validation_due_from(None) is None
    assert be2.validation_due_from(0) is None


def test_validation_due_lands_in_the_future():
    due = be2.validation_due_from(3)
    assert due is not None and due > _now()


def test_validation_becomes_due_when_the_window_elapses():
    assert be2.is_validation_due(_benefit(validationDueAt=_now() - timedelta(days=1)))
    assert not be2.is_validation_due(_benefit(validationDueAt=_now() + timedelta(days=30)))


def test_a_settled_benefit_is_never_due():
    past = _now() - timedelta(days=30)
    for st in ("VALIDATED", "REJECTED", "LAPSED"):
        assert not be2.is_validation_due(_benefit(status=st, validationDueAt=past))


@pytest.mark.asyncio
async def test_the_recorder_cannot_validate_their_own_benefit():
    db = _FakeSession()
    b = _benefit(createdById="u-recorder")
    blockers = await be2.validation_blockers(db, b, actor_id="u-recorder")
    assert any("other than the person who recorded it" in x for x in blockers)


@pytest.mark.asyncio
async def test_an_independent_person_can_validate():
    from app.models.business_excellence import BeKaizen

    db = _FakeSession({("BeKaizen", "k1"): SimpleNamespace(createdById="u-author")})
    b = _benefit(createdById="u-recorder", sourceType="KAIZEN", sourceId="k1")
    assert await be2.validation_blockers(db, b, actor_id="u-finance") == []


@pytest.mark.asyncio
async def test_the_kaizen_author_cannot_validate_its_savings():
    """Mirrors the rule Phase 1 already enforces on the Kaizen VERIFIED
    transition, so the two paths cannot disagree."""
    db = _FakeSession({("BeKaizen", "k1"): SimpleNamespace(createdById="u-author")})
    b = _benefit(createdById="u-recorder", sourceType="KAIZEN", sourceId="k1")
    blockers = await be2.validation_blockers(db, b, actor_id="u-author")
    assert any("raised the idea" in x for x in blockers)


@pytest.mark.asyncio
async def test_a_benefit_with_no_realised_figure_cannot_be_validated():
    db = _FakeSession()
    b = _benefit(realizedValue=None)
    blockers = await be2.validation_blockers(db, b, actor_id="u-finance")
    assert any("Record the realised value" in x for x in blockers)


@pytest.mark.asyncio
async def test_an_already_validated_benefit_stops_immediately():
    db = _FakeSession()
    blockers = await be2.validation_blockers(db, _benefit(status="VALIDATED"), actor_id="u-finance")
    assert blockers == ["This benefit has already been validated."]


@pytest.mark.asyncio
async def test_a_nominated_authority_excludes_everybody_else():
    db = _FakeSession()
    b = _benefit(validatingAuthorityId="u-cfo")
    assert any("nominated validating authority" in x for x in await be2.validation_blockers(db, b, actor_id="u-finance"))
    assert await be2.validation_blockers(db, b, actor_id="u-cfo") == []


@pytest.mark.asyncio
async def test_a_sip_owner_and_sponsor_cannot_validate_their_own_benefit():
    """§7: "validated by a designated authority". A sponsor signing off the
    benefit of the project they championed is the conflict this prevents."""
    from app.models.business_excellence_p2 import BeSip

    sip = SimpleNamespace(ownerId="u-owner", sponsorId="u-sponsor", createdById="u-owner")
    db = _FakeSession({("BeSip", "sip1"): sip})
    b = _benefit(sourceType="SIP", sourceId="sip1", createdById="u-recorder")

    for who in ("u-owner", "u-sponsor"):
        blockers = await be2.validation_blockers(db, b, actor_id=who)
        assert any("owner or sponsor" in x for x in blockers), who
    assert await be2.validation_blockers(db, b, actor_id="u-finance") == []


@pytest.mark.asyncio
async def test_a_circle_cannot_validate_its_own_benefit_including_past_members(monkeypatch):
    """§6: sign-off must be "separate from the circle's own reporting". A member
    who left last month is still too close to it."""
    project = SimpleNamespace(teamId="t1", createdById="u-leader")
    db = _FakeSession({("BeQccProject", "p1"): project})

    async def fake_members(_db, team_id, *, include_past=True):
        assert include_past is True, "past members must be included or the gate leaks"
        return {"u-leader", "u-member", "u-left-last-month"}

    monkeypatch.setattr(be2, "team_member_ids", fake_members)

    b = _benefit(sourceType="QCC", sourceId="p1", createdById="u-recorder")
    for who in ("u-leader", "u-member", "u-left-last-month"):
        blockers = await be2.validation_blockers(db, b, actor_id=who)
        assert any("cannot validate its own benefit" in x for x in blockers), who
    assert await be2.validation_blockers(db, b, actor_id="u-outsider") == []


# ── Benefit rollup ───────────────────────────────────────────────────────────
def test_only_validated_lines_count_toward_realised():
    """A realised total that includes unvalidated claims is the projected total
    wearing a different label."""
    rows = [
        SimpleNamespace(valueKind="FINANCIAL", status="VALIDATED", projectedValue=100.0, realizedValue=90.0, validationDueAt=None),
        SimpleNamespace(valueKind="FINANCIAL", status="PENDING_VALIDATION", projectedValue=200.0, realizedValue=500.0, validationDueAt=None),
    ]
    s = be2.summarise_benefits(rows)
    assert s["realisedFinancial"] == 90.0
    assert s["projectedFinancial"] == 300.0
    assert s["validatedLines"] == 1
    assert s["pendingValidation"] == 1


def test_non_financial_lines_are_counted_not_summed():
    """Adding rupees to parts-per-million produces a number that looks like a
    benefit and means nothing."""
    rows = [
        SimpleNamespace(valueKind="FINANCIAL", status="VALIDATED", projectedValue=100.0, realizedValue=100.0, validationDueAt=None),
        SimpleNamespace(valueKind="NON_FINANCIAL", status="VALIDATED", projectedValue=4000.0, realizedValue=1200.0, validationDueAt=None),
    ]
    s = be2.summarise_benefits(rows)
    assert s["realisedFinancial"] == 100.0
    assert s["nonFinancialLines"] == 1


def test_empty_rollup_reports_none_not_zero():
    s = be2.summarise_benefits([])
    assert s["realisedFinancial"] is None
    assert s["projectedFinancial"] is None
    assert s["lines"] == 0


# ── Benefit schema validators ────────────────────────────────────────────────
def test_a_financial_benefit_cannot_carry_a_unit():
    with pytest.raises(ValidationError):
        BenefitCreate(
            sourceType="QCC", sourceId="p1", benefitType="COST_SAVING",
            valueKind="FINANCIAL", unit="ppm",
        )


def test_a_non_financial_benefit_needs_a_unit_and_no_currency():
    with pytest.raises(ValidationError):
        BenefitCreate(
            sourceType="QCC", sourceId="p1", benefitType="QUALITY_IMPROVEMENT",
            valueKind="NON_FINANCIAL",
        )
    with pytest.raises(ValidationError):
        BenefitCreate(
            sourceType="QCC", sourceId="p1", benefitType="QUALITY_IMPROVEMENT",
            valueKind="NON_FINANCIAL", unit="ppm", currency="INR",
        )
    BenefitCreate(
        sourceType="QCC", sourceId="p1", benefitType="QUALITY_IMPROVEMENT",
        valueKind="NON_FINANCIAL", unit="ppm",
    )


def test_benefit_update_cannot_re_point_the_line_at_another_record():
    from app.schemas.business_excellence_p2 import BenefitUpdate

    for f in ("sourceType", "sourceId", "valueKind"):
        assert f not in BenefitUpdate.model_fields


def test_benefit_statuses_are_all_reachable_from_the_service():
    """Every status in the vocabulary should be one the code can actually set,
    or it is a value the UI will render a filter for and never populate."""
    settable = {"PROJECTED", "PENDING_VALIDATION", "VALIDATED", "REJECTED", "LAPSED"}
    assert set(BENEFIT_STATUSES) == settable


# ═════════════════════════════════════════════════════════════════════════════
# The workflow bridge
# ═════════════════════════════════════════════════════════════════════════════
class _BridgeSession:
    def __init__(self, record):
        self.record = record
        self.flushed = 0

    async def get(self, model, pk):
        return self.record

    async def flush(self):
        self.flushed += 1


@pytest.mark.asyncio
async def test_bridge_ignores_modules_it_does_not_own():
    db = _BridgeSession(SimpleNamespace(status="DRAFT"))
    assert await be2.sync_p2_record_status(
        db, module="PTW", record_id="x", instance_completed=True
    ) is False


@pytest.mark.asyncio
async def test_bridge_lands_an_approval_on_each_register():
    for module, expected in (
        (be2.WF_MODULE_SUGGESTION, "SCREENING"),
        (be2.WF_MODULE_QCC, "CHARTERED"),
        (be2.WF_MODULE_SIP, "APPROVED"),
    ):
        rec = SimpleNamespace(status="SUBMITTED", charteredAt=None)
        db = _BridgeSession(rec)
        assert await be2.sync_p2_record_status(
            db, module=module, record_id="r1", instance_completed=True
        )
        assert rec.status == expected


@pytest.mark.asyncio
async def test_bridge_lands_a_rejection_with_its_reason():
    rec = SimpleNamespace(status="SUBMITTED", rejectionReason=None)
    db = _BridgeSession(rec)
    assert await be2.sync_p2_record_status(
        db,
        module=be2.WF_MODULE_SIP,
        record_id="r1",
        instance_completed=False,
        rejected=True,
        reason="Out of budget this year.",
    )
    assert rec.status == "REJECTED"
    assert rec.rejectionReason == "Out of budget this year."


@pytest.mark.asyncio
async def test_bridge_never_resurrects_a_terminal_record():
    """A repair job that recreates a closure task must not reopen something
    already finished."""
    for terminal in ("CLOSED", "REJECTED", "CANCELLED"):
        rec = SimpleNamespace(status=terminal, charteredAt=None)
        db = _BridgeSession(rec)
        assert await be2.sync_p2_record_status(
            db, module=be2.WF_MODULE_SIP, record_id="r1", instance_completed=True
        ) is False
        assert rec.status == terminal


@pytest.mark.asyncio
async def test_a_mid_chain_advance_never_drags_a_record_backwards():
    rec = SimpleNamespace(status="SCREENING")
    db = _BridgeSession(rec)
    assert await be2.sync_p2_record_status(
        db, module=be2.WF_MODULE_SUGGESTION, record_id="r1", instance_completed=False
    ) is False
    assert rec.status == "SCREENING"


@pytest.mark.asyncio
async def test_a_mid_chain_advance_moves_a_draft_suggestion_to_submitted():
    rec = SimpleNamespace(status="DRAFT")
    db = _BridgeSession(rec)
    assert await be2.sync_p2_record_status(
        db, module=be2.WF_MODULE_SUGGESTION, record_id="r1", instance_completed=False
    )
    assert rec.status == "SUBMITTED"


@pytest.mark.asyncio
async def test_bridge_returns_false_for_a_missing_record():
    class _Empty:
        async def get(self, model, pk):
            return None

    assert await be2.sync_p2_record_status(
        _Empty(), module=be2.WF_MODULE_QCC, record_id="gone", instance_completed=True
    ) is False


# =============================================================================
# Regressions found by LIVE verification, 2026-08-25
#
# Both of these passed every offline test and still shipped broken, because the
# offline suite exercises pure functions and never touches a session or a
# delete. They are pinned here so they cannot come back.
# =============================================================================
def test_loaders_use_populate_existing_found_by_live_verification():
    """Chartering a project returned "0 gates" for gates it had just written.

    The session is built with expire_on_commit=False, so after a handler adds
    child rows and commits, the parent is still in the identity map holding its
    already-loaded (empty) collection. The re-read handed back that cached
    instance. The DB was always right; the payload lied.

    Asserted at the source level because reproducing it needs a real async
    session against a real database — which is exactly why it survived the unit
    tests. Three loaders must carry the option: team, project and SIP.
    """
    import pathlib as _p

    src = (
        _p.Path(__file__).resolve().parents[1]
        / "app"
        / "routers"
        / "business_excellence_p2.py"
    ).read_text(encoding="utf-8")
    assert src.count("populate_existing=True") >= 3, (
        "a collection loader lost populate_existing — a POST that creates child "
        "rows will report zero of them"
    )


@pytest.mark.asyncio
async def test_withdrawing_a_source_withdraws_its_benefit_lines():
    """Soft-deleting a SIP left its benefit lines live.

    The register stopped showing the project and the cross-workflow dashboard
    kept counting its money. Found live: the deploy verifier said "clean"
    because the source ROW still existed (isDeleted=true), so an existence
    check could not see it.
    """
    from app.routers.business_excellence_p2 import _withdraw_benefits

    lines = [
        SimpleNamespace(id="b1", isDeleted=False, deletedAt=None, deletedBy=None, deletionReason=None),
        SimpleNamespace(id="b2", isDeleted=False, deletedAt=None, deletedBy=None, deletionReason=None),
    ]

    async def fake_benefits_for(_db, *, source_type, source_id):
        assert source_type == "SIP" and source_id == "sip-1"
        return lines

    import app.services.business_excellence_p2 as _be2
    real = _be2.benefits_for
    _be2.benefits_for = fake_benefits_for
    try:
        n = await _withdraw_benefits(
            None,
            source_type="SIP",
            source_id="sip-1",
            actor_id="u-1",
            reason="source record withdrawn by its owner",
        )
    finally:
        _be2.benefits_for = real

    assert n == 2
    assert all(b.isDeleted for b in lines), "a benefit line survived its source"
    assert all(b.deletedBy == "u-1" for b in lines)
