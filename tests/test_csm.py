"""Contractor Safety Management: offline unit tests (no DB), house style of test_ims.py."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services import csm as svc

NOW = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)
ALL_DOCS = {"HSE_POLICY", "LABOUR_LICENCE", "PF_REGISTRATION", "ESIC_REGISTRATION", "WC_INSURANCE", "ISO_45001",
            "TRAINING_RECORD", "EQUIPMENT_FITNESS", "SAFETY_OFFICER_QUALIFICATION"}
OK_CHECKLIST = {k: True for k, _ in svc.SAFETY_CHECKLIST}


def _sections():
    return [
        {"code": "A", "title": "A", "weight": 60, "questions": [
            {"code": "Q1", "text": "yes?", "answerType": "YES_NO", "weight": 2},
            {"code": "Q2", "text": "scale", "answerType": "SCALE_1_5", "weight": 2},
            {"code": "QT", "text": "free text", "answerType": "TEXT", "weight": 0},
        ]},
        {"code": "B", "title": "B", "weight": 40, "questions": [
            {"code": "D1", "text": "licence", "answerType": "DOCUMENT_REQUIRED", "weight": 1, "critical": True, "docType": "LABOUR_LICENCE"},
            {"code": "N1", "text": "ltifr", "answerType": "NUMBER", "weight": 1,
             "bands": [{"max": 0.5, "score": 1}, {"max": 2, "score": 0.5}, {"max": None, "score": 0}]},
        ]},
    ]


# ── question scoring ─────────────────────────────────────────────────────────
def test_question_fraction_by_type():
    q = lambda t, **kw: {"code": "x", "answerType": t, "weight": 1, **kw}  # noqa: E731
    assert svc.question_fraction(q("YES_NO"), "yes", set()) == 1.0
    assert svc.question_fraction(q("YES_NO"), "NO", set()) == 0.0
    assert svc.question_fraction(q("YES_NO", preferredAnswer="NO"), "NO", set()) == 1.0
    assert svc.question_fraction(q("SCALE_1_5"), 1, set()) == 0.0
    assert svc.question_fraction(q("SCALE_1_5"), 3, set()) == 0.5
    assert svc.question_fraction(q("SCALE_1_5"), 5, set()) == 1.0
    assert svc.question_fraction(q("SCALE_1_5"), 7, set()) == 0.0
    bands = [{"max": 0.5, "score": 1}, {"max": 2, "score": 0.5}, {"max": None, "score": 0}]
    assert svc.question_fraction(q("NUMBER", bands=bands), 0.5, set()) == 1.0  # boundary is inclusive
    assert svc.question_fraction(q("NUMBER", bands=bands), 1.2, set()) == 0.5
    assert svc.question_fraction(q("NUMBER", bands=bands), 9, set()) == 0.0
    assert svc.question_fraction(q("NUMBER"), 42, set()) == 1.0  # no bands: answering is enough
    assert svc.question_fraction(q("DOCUMENT_REQUIRED", docType="WC_INSURANCE"), None, {"WC_INSURANCE"}) == 1.0
    assert svc.question_fraction(q("DOCUMENT_REQUIRED", docType="WC_INSURANCE"), "yes", set()) == 0.0
    assert svc.question_fraction(q("TEXT"), "anything", set()) is None
    assert svc.question_fraction({"code": "x", "answerType": "YES_NO", "weight": 0}, "YES", set()) is None
    assert svc.question_fraction(q("YES_NO"), None, set()) == 0.0  # unanswered scores zero


def test_weighted_score_sections_and_overall():
    answers = {"Q1": {"value": "YES"}, "Q2": {"value": 3}, "N1": {"value": 1.0}}
    r = svc.score_application(_sections(), answers, {}, {"LABOUR_LICENCE"})
    a, b = r["sections"]
    assert a["autoScore"] == 75.0  # (2*1 + 2*0.5) / 4
    assert b["autoScore"] == 75.0  # (1*1 + 1*0.5) / 2
    assert r["autoScore"] == 75.0 and r["finalScore"] == 75.0
    assert r["criticalFailures"] == []
    # without the licence: B = 25 %, overall = 0.6*75 + 0.4*25 = 55
    r2 = svc.score_application(_sections(), answers, {}, set())
    assert r2["autoScore"] == 55.0
    assert [c["code"] for c in r2["criticalFailures"]] == ["D1"]
    assert r2["missingDocuments"] == ["D1"]


def test_override_changes_final_not_auto_and_can_clear_critical():
    answers = {"Q1": {"value": "YES"}, "Q2": {"value": 3}, "N1": {"value": 1.0}}
    r = svc.score_application(_sections(), answers, {"D1": {"score": 1, "reason": "seen original"}}, set())
    assert r["autoScore"] == 55.0
    assert r["finalScore"] == 75.0
    assert r["criticalFailures"] == []
    row = next(q for q in r["sections"][1]["questions"] if q["code"] == "D1")
    assert row["overridden"] is True
    # an override on an unscored question is ignored
    r3 = svc.score_application(_sections(), answers, {"QT": {"score": 1}}, {"LABOUR_LICENCE"})
    assert r3["finalScore"] == 75.0


def test_unanswered_lists_required_questions_only():
    r = svc.score_application(_sections(), {"Q1": {"value": "NO"}}, {}, set())
    assert set(r["unanswered"]) == {"Q2", "N1"}  # TEXT with weight 0 is not required unless flagged
    secs = _sections()
    secs[0]["questions"][2]["required"] = True
    assert "QT" in svc.score_application(secs, {}, {}, set())["unanswered"]


def test_recommendation_thresholds_and_critical_cap():
    assert svc.recommend(82, [], 70, 55) == "QUALIFIED"
    assert svc.recommend(82, [{"code": "D1"}], 70, 55) == "CONDITIONAL"
    assert svc.recommend(60, [], 70, 55) == "CONDITIONAL"
    assert svc.recommend(54.9, [], 70, 55) == "REJECTED"
    assert svc.recommend(None, [], 70, 55) is None


def test_default_template_is_valid_and_weights_total_100():
    t = svc.DEFAULT_TEMPLATE
    assert svc.template_problems(t["sections"]) == []
    assert sum(s["weight"] for s in t["sections"]) == 100
    codes = [q["code"] for s in t["sections"] for q in s["questions"]]
    assert len(codes) == len(set(codes))
    # every statutory document is critical
    stat = next(s for s in t["sections"] if s["code"] == "STATUTORY")
    assert all(q["critical"] for q in stat["questions"] if q["answerType"] == "DOCUMENT_REQUIRED")


def test_default_template_perfect_and_zero_answers():
    secs = svc.DEFAULT_TEMPLATE["sections"]
    perfect = {}
    for s in secs:
        for q in s["questions"]:
            t = q["answerType"]
            perfect[q["code"]] = {"value": {"YES_NO": q.get("preferredAnswer", "YES"), "SCALE_1_5": 5, "NUMBER": 0,
                                            "TEXT": "x"}.get(t)}
    r = svc.score_application(secs, perfect, {}, ALL_DOCS)
    assert r["finalScore"] == 100.0 and not r["criticalFailures"] and not r["unanswered"]
    r0 = svc.score_application(secs, {}, {}, set())
    assert r0["finalScore"] == 0.0
    assert svc.recommend(r0["finalScore"], r0["criticalFailures"], 70, 55) == "REJECTED"


def test_template_problems_catch_bad_edits():
    assert svc.template_problems([])
    bad = [{"code": "A", "title": "A", "weight": 10, "questions": [
        {"code": "Q", "text": "t", "answerType": "MAYBE", "weight": 1},
        {"code": "Q", "text": "t", "answerType": "DOCUMENT_REQUIRED", "weight": 1},
        {"code": "N", "text": "t", "answerType": "NUMBER", "weight": 1, "bands": [{"max": 1, "score": 3}]},
    ]}]
    p = " ".join(svc.template_problems(bad))
    assert "answer type" in p and "Duplicate question" in p and "document type" in p and "between 0 and 1" in p


def test_normalise_answer():
    q = lambda t: {"code": "X", "answerType": t}  # noqa: E731
    assert svc.normalise_answer(q("YES_NO"), "yes") == ("YES", None)
    assert svc.normalise_answer(q("YES_NO"), "maybe")[1]
    assert svc.normalise_answer(q("SCALE_1_5"), "4") == (4, None)
    assert svc.normalise_answer(q("SCALE_1_5"), 4.5)[1]
    assert svc.normalise_answer(q("NUMBER"), "0.42") == (0.42, None)
    assert svc.normalise_answer(q("NUMBER"), -1)[1]
    assert svc.normalise_answer(q("TEXT"), "  ") == (None, None)


# ── prequalification workflow ────────────────────────────────────────────────
def test_prequal_moves():
    assert svc.can_move_pq("DRAFT", "SUBMITTED")
    assert svc.can_move_pq("SUBMITTED", "HSE_REVIEW")
    assert svc.can_move_pq("HSE_REVIEW", "APPROVAL")
    assert svc.can_move_pq("APPROVAL", "CONDITIONAL")
    assert svc.can_move_pq("APPROVAL", "HSE_REVIEW")  # approver sends back
    assert not svc.can_move_pq("SUBMITTED", "QUALIFIED")  # no skipping review and approval
    assert not svc.can_move_pq("HSE_REVIEW", "QUALIFIED")
    assert not svc.can_move_pq("QUALIFIED", "DRAFT")  # decided is final; renewal is a new application
    assert not svc.can_move_pq("REJECTED", "APPROVAL")


def test_submit_blockers():
    assert svc.submit_blockers(status="DRAFT", unanswered=[]) == []
    assert svc.submit_blockers(status="DRAFT", unanswered=["Q1"])
    assert svc.submit_blockers(status="SUBMITTED", unanswered=[])


def test_decision_rules():
    ok = dict(status="APPROVAL", conditions=None, notes=None, recommendation="QUALIFIED", recommended_by="hse", decided_by="ph")
    assert svc.decision_blockers(decision="QUALIFIED", **ok) == []
    assert svc.decision_blockers(decision="CONDITIONAL", **{**ok, "recommendation": "CONDITIONAL"})  # needs conditions
    assert svc.decision_blockers(decision="CONDITIONAL", **{**ok, "recommendation": "CONDITIONAL", "conditions": "Close PF gap in 30 days"}) == []
    assert svc.decision_blockers(decision="REJECTED", **ok)  # rejection and deviation both need notes
    assert svc.decision_blockers(decision="REJECTED", **{**ok, "notes": "Fatality in 2025"}) == []
    assert any("cannot also approve" in b for b in svc.decision_blockers(decision="QUALIFIED", **{**ok, "decided_by": "hse"}))
    assert svc.decision_blockers(decision="QUALIFIED", **{**ok, "status": "HSE_REVIEW"})
    assert svc.decision_blockers(decision="MAYBE", **ok)


def test_validity_months_clamps_month_end():
    assert svc.add_months(datetime(2026, 1, 31, tzinfo=timezone.utc), 1).day == 28
    assert svc.validity_for("QUALIFIED", NOW, 12, 6) == NOW.replace(year=2027)
    assert svc.validity_for("CONDITIONAL", NOW, 12, 6) == NOW.replace(year=2027, month=5)
    assert svc.validity_for("REJECTED", NOW, 12, 6) is None


def test_effective_qualification():
    assert svc.effective_qualification("approved", NOW + timedelta(days=5), NOW) == "QUALIFIED"
    assert svc.effective_qualification("conditionally_approved", None, NOW) == "CONDITIONAL"
    assert svc.effective_qualification("approved", NOW - timedelta(days=1), NOW) == "EXPIRED"
    assert svc.effective_qualification("approved", (NOW - timedelta(days=1)).replace(tzinfo=None), NOW) == "EXPIRED"
    assert svc.effective_qualification("rejected", None, NOW) == "REJECTED"
    assert svc.effective_qualification(None, None, NOW) == "NOT_QUALIFIED"


def test_expiry_state():
    assert svc.expiry_state(NOW - timedelta(days=1), NOW) == "EXPIRED"
    assert svc.expiry_state(NOW + timedelta(days=29), NOW) == "DUE"
    assert svc.expiry_state(NOW + timedelta(days=31), NOW) == "OK"
    assert svc.expiry_state(None, NOW) is None


# ── blacklist gate (also used by the EPC gate) ───────────────────────────────
def test_company_block_reason():
    assert svc.company_block_reason("BLACKLISTED")
    assert svc.company_block_reason("suspended")  # legacy lower-case values still block
    assert svc.company_block_reason("ACTIVE") is None
    assert svc.company_block_reason(None) is None


# ── work-order activation gate ───────────────────────────────────────────────
def _act(**kw):
    base = dict(company_status="ACTIVE", qualification="QUALIFIED", checklist=OK_CHECKLIST, start=NOW,
                end=NOW + timedelta(days=30), engineer_id="eic", manpower=30, supervisors=3, now=NOW)
    base.update(kw)
    return svc.activation_blockers(**base)


def test_activation_gate_passes_when_all_clear():
    assert _act() == []
    assert _act(qualification="CONDITIONAL") == []


def test_activation_gate_refusals():
    assert any("blacklisted" in b for b in _act(company_status="BLACKLISTED"))
    assert any("suspended" in b for b in _act(company_status="SUSPENDED"))
    assert any("not prequalified" in b for b in _act(qualification="EXPIRED"))
    assert any("not prequalified" in b for b in _act(qualification="NOT_QUALIFIED"))
    assert any("penalty clauses" in b for b in _act(checklist={**OK_CHECKLIST, "penaltyClausesAcknowledged": False}))
    assert any("engineer" in b for b in _act(engineer_id=None))
    assert any("End date" in b for b in _act(end=NOW - timedelta(days=1)))
    assert any("already passed" in b for b in _act(start=NOW - timedelta(days=10), end=NOW - timedelta(days=1)))
    assert any("1:30" in b for b in _act(manpower=60, supervisors=2))
    assert any("No supervisors" in b for b in _act(supervisors=0))


# ── worker clearance ─────────────────────────────────────────────────────────
def _clear(**kw):
    base = dict(wo_status="ACTIVE", wo_end=NOW + timedelta(days=20), company_status="ACTIVE", qualification="QUALIFIED",
                medical_valid_until=NOW + timedelta(days=100), worker_status="active",
                roster_check={"result": "pass", "detail": "ok"}, induction_valid_until=NOW + timedelta(days=200),
                same_company=True, now=NOW)
    base.update(kw)
    return svc.worker_clearance(**base)


def test_worker_clearance_pass_and_pass_validity():
    r = _clear()
    assert r["overall"] == "cleared"
    assert r["passValidUntil"] == (NOW + timedelta(days=20)).isoformat()  # earliest of WO end / medical / induction
    r2 = _clear(medical_valid_until=NOW + timedelta(days=5))
    assert r2["passValidUntil"] == (NOW + timedelta(days=5)).isoformat()


def test_worker_clearance_blocks():
    assert _clear(company_status="BLACKLISTED")["blocking"] == ["contractor_status"]
    assert "medical_fitness" in _clear(medical_valid_until=None)["blocking"]
    assert "plant_induction" in _clear(induction_valid_until=None)["blocking"]
    assert "safety_roster" in _clear(roster_check={"result": "fail", "detail": "derostered"})["blocking"]
    assert "work_order" in _clear(wo_status="SUSPENDED")["blocking"]
    assert "company_match" in _clear(same_company=False)["blocking"]
    r = _clear(induction_valid_until=NOW + timedelta(days=10))
    assert r["overall"] == "cleared_with_warnings" and r["warnings"] == ["plant_induction"]
    assert _clear(company_status="SUSPENDED")["passValidUntil"] is None


# ── evaluations ──────────────────────────────────────────────────────────────
def test_working_days_mon_to_sat():
    # 2026-11-01 is a Sunday; November 2026 has 30 days, 5 Sundays -> 25 working days
    assert svc.working_days(datetime(2026, 11, 1, tzinfo=timezone.utc), datetime(2026, 11, 30, tzinfo=timezone.utc)) == 25
    assert svc.working_days(NOW, NOW - timedelta(days=1)) == 0


def test_kpi_autofill_maths():
    counts = {"lti": 1, "fatalities": 0, "incidents": 2, "nearMisses": 3, "unsafeObservations": 4, "showCause": 1,
              "permitViolations": 0, "tbt": 20}
    k = {r["code"]: r for r in svc.evaluation_kpis(counts, expected_tbt=25)}
    assert k["LTI_FATALITY"]["score"] == 40.0
    assert k["INCIDENTS"]["score"] == 60.0
    assert k["NEAR_MISSES"]["score"] == 70.0
    assert k["UNSAFE_OBSERVATIONS"]["score"] == 80.0
    assert k["SHOW_CAUSE"]["score"] == 75.0
    assert k["PERMIT_VIOLATIONS"]["score"] == 100.0
    assert k["TBT_PARTICIPATION"]["score"] == 80.0 and k["TBT_PARTICIPATION"]["target"] == 25
    assert sum(r["weight"] for r in k.values()) == 60
    floor = {r["code"]: r for r in svc.evaluation_kpis({"incidents": 9, "showCause": 7}, expected_tbt=0)}
    assert floor["INCIDENTS"]["score"] == 0.0 and floor["SHOW_CAUSE"]["score"] == 0.0
    assert floor["TBT_PARTICIPATION"]["score"] is None  # unmeasured, not zero


def test_evaluation_weighting_and_bands():
    kpis = svc.evaluation_kpis({}, expected_tbt=10)  # clean period but no TBTs -> TBT 0
    manual = {c["code"]: {"score": 4} for c in svc.MANUAL_CRITERIA}
    s = svc.evaluation_score(kpis, manual)
    # auto: (55*100 + 5*0) / 60 = 91.7 ; manual: 80 ; final (5500 + 3200) / 100 = 87
    assert s["autoScore"] == 91.7 and s["manualScore"] == 80.0 and s["finalScore"] == 87.0
    assert s["ratingBand"] == "EXCELLENT"
    # unmeasured TBT is dropped from the denominator
    s2 = svc.evaluation_score(svc.evaluation_kpis({}, expected_tbt=0), manual)
    assert s2["autoScore"] == 100.0 and s2["finalScore"] == round((5500 + 3200) / 95, 1)
    assert [svc.rating_band(x) for x in (85, 84.9, 70, 55, 40, 39.9)] == ["EXCELLENT", "GOOD", "GOOD", "AVERAGE", "POOR", "UNACCEPTABLE"]


def test_fatality_caps_the_rating():
    kpis = svc.evaluation_kpis({"fatalities": 1}, expected_tbt=10)
    assert kpis[0]["score"] == 0.0
    manual = {c["code"]: {"score": 5} for c in svc.MANUAL_CRITERIA}
    s = svc.evaluation_score(kpis, manual, fatalities=1)
    assert s["finalScore"] == svc.FATALITY_SCORE_CAP and s["fatalityCapApplied"]
    assert s["ratingBand"] == "UNACCEPTABLE"


def test_evaluation_submit_and_signoff_rules():
    assert svc.eval_submit_blockers(status="DRAFT", manual={})
    full = {c["code"]: {"score": 3} for c in svc.MANUAL_CRITERIA}
    assert svc.eval_submit_blockers(status="DRAFT", manual=full) == []
    assert svc.eval_submit_blockers(status="DRAFT", manual={**full, "HOUSEKEEPING": {"score": 6}})
    assert svc.eval_signoff_blockers(status="SUBMITTED", evaluator_id="e", signer_id="ph") == []
    assert svc.eval_signoff_blockers(status="SUBMITTED", evaluator_id="e", signer_id="e")
    assert svc.eval_signoff_blockers(status="DRAFT", evaluator_id="e", signer_id="ph")


def test_previous_period_month_and_fy_quarter():
    s, e = svc.previous_period("MONTHLY", NOW)
    assert (s.month, s.day, e.month, e.day) == (10, 1, 10, 31)
    s, e = svc.previous_period("QUARTERLY", NOW)  # Nov is in Q3 (Oct-Dec) -> previous is Jul-Sep
    assert (s.year, s.month, e.month, e.day) == (2026, 7, 9, 30)
    s, e = svc.previous_period("QUARTERLY", datetime(2027, 2, 5, tzinfo=timezone.utc))  # Q4 -> Q3 Oct-Dec
    assert (s.year, s.month, e.year, e.month) == (2026, 10, 2026, 12)
    s, e = svc.previous_period("QUARTERLY", datetime(2026, 5, 5, tzinfo=timezone.utc))  # Q1 -> Q4 Jan-Mar
    assert (s.year, s.month, e.month) == (2026, 1, 3)


# ── sanctions ────────────────────────────────────────────────────────────────
def test_sanction_request_rules():
    r = "Fatal fall from CRM-2 roof, 20-Nov"
    assert svc.sanction_request_blockers(action="BLACKLIST", current_status="ACTIVE", reason=r, pending=False) == []
    assert svc.sanction_request_blockers(action="SUSPEND", current_status="BLACKLISTED", reason=r, pending=False)
    assert svc.sanction_request_blockers(action="BLACKLIST", current_status="BLACKLISTED", reason=r, pending=False)
    assert svc.sanction_request_blockers(action="BLACKLIST", current_status="SUSPENDED", reason=r, pending=False) == []
    assert svc.sanction_request_blockers(action="REINSTATE", current_status="ACTIVE", reason=r, pending=False)
    assert svc.sanction_request_blockers(action="REINSTATE", current_status="SUSPENDED", reason=r, pending=False) == []
    assert svc.sanction_request_blockers(action="SUSPEND", current_status="ACTIVE", reason="short", pending=False)
    assert svc.sanction_request_blockers(action="SUSPEND", current_status="ACTIVE", reason=r, pending=True)
    assert svc.sanction_request_blockers(action="FINE", current_status="ACTIVE", reason=r, pending=False)


def test_sanction_approval_needs_a_second_person():
    assert svc.sanction_decision_blockers(status="PENDING_APPROVAL", requested_by="a", decided_by="b") == []
    assert svc.sanction_decision_blockers(status="PENDING_APPROVAL", requested_by="a", decided_by="a")
    assert svc.sanction_decision_blockers(status="APPROVED", requested_by="a", decided_by="b")
    assert [svc.sanction_target_status(a) for a in svc.SANCTION_ACTIONS] == ["SUSPENDED", "BLACKLISTED", "ACTIVE"]


@pytest.mark.parametrize("vocab", [svc.PQ_STATUSES, svc.DOC_TYPES, svc.WORK_TYPES, svc.HIGH_RISK_ACTIVITIES,
                                   svc.SANCTION_REASONS, svc.EVAL_TYPES])
def test_vocabularies_have_no_duplicates(vocab):
    assert len(vocab) == len(set(vocab))
