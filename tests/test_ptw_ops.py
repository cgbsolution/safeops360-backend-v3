"""PTW Control Center v2 — pure-logic tests (house style: no DB, no TestClient).

Covers every exception rule, the impairment conflict check (block / require
controls / allow), SIMOPS distance, the zero-baseline delta, derived statuses,
the engine's upsert/resolve plan, the brief's redaction, and the payload
against the seeded demo plant (app/services/ptw_ops/demo.py — the same data
scripts/seed_ptw_control_center.py writes).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services.ptw_cc import DEFAULT_SHIFTS
from app.services.ptw_ops import defaults, demo, simops, steps
from app.services.ptw_ops import impairment_check as ic
from app.services.ptw_ops.brief import brief_inputs, template_text
from app.services.ptw_ops.derived import (
    active_outside_validity,
    expired_open,
    expiring_2h,
    is_open,
    starting_2h,
)
from app.services.ptw_ops.engine import reconcile
from app.services.ptw_ops.payload import Extras, build, pack_lanes, pin_positions, zero_baseline_delta
from app.services.ptw_ops.rules import (
    RULES,
    approval_sla_breach,
    competency_expiring,
    evaluate_all,
    gas_test_overdue,
    handover_unacknowledged,
    hot_work_blocked_by_impairment,
    impairment_controls_missing,
    impairment_overdue,
    loto_not_verified,
    permit_expired_open,
    simops_conflict,
)
from app.services.ptw_ops.shifts import current_shift, local_day, range_window
from app.services.ptw_ops.types import (
    ExceptionDraft,
    ImpairmentView,
    LayoutView,
    PermitView,
    Snapshot,
    ZoneView,
)

# 21:44 IST on 23 Nov 2026 — Shift B, as in the client walkthrough design.
NOW = datetime(2026, 11, 23, 16, 14, tzinfo=timezone.utc)
H = timedelta(hours=1)
M = timedelta(minutes=1)


def permit(**kw) -> PermitView:
    base = dict(id="p1", number="PTW-1", type="HOT_WORK", status="ACTIVE", plantId="pl", areaId="z1",
                validFrom=NOW - 2 * H, validTo=NOW + 2 * H, createdAt=NOW - 3 * H, activatedAt=NOW - 2 * H)
    base.update(kw)
    return PermitView(**base)


def imp(**kw) -> ImpairmentView:
    base = dict(id="i1", code="SZ-03", plantId="pl", areaId="z1", systemType="sprinkler",
                systemName="Sprinkler zone SZ-03", reason="valve", status="active", startedAt=NOW - H,
                plannedRestoreAt=NOW + 4 * H)
    base.update(kw)
    return ImpairmentView(**base)


def snap(permits=(), impairments=(), zones=None, layout=None, **kw) -> Snapshot:
    return Snapshot(now=NOW, plantId="pl", permits=list(permits), impairments=list(impairments),
                    impairmentRules=defaults.impairment_rule_views(), simopsRules=defaults.simops_rule_views(),
                    zones=zones or [ZoneView("z1", "Zone 1", demo.rect(0, 0, 10, 10))],
                    layout=layout or LayoutView(), shifts=DEFAULT_SHIFTS, **kw)


# ── steps ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("status,step,expected", [
    ("SUBMITTED", "Issuer Review", steps.ISSUER_REVIEW),
    ("SUBMITTED", "Safety Officer Review", steps.SAFETY_REVIEW),
    ("SUBMITTED", "Plant Head Approval", steps.PLANT_HEAD),
    ("SUBMITTED", "Area Owner Sign-off", steps.APPROVAL_OTHER),
    ("APPROVED", None, steps.AWAITING_ACCEPT),
    ("ISSUED", "Receiver Accepts Permit", steps.AWAITING_ACCEPT),
    ("ACTIVE", "Safety Officer Closes", steps.ACTIVE),
    ("WORK_COMPLETED", None, steps.CLOSURE_PENDING),
    ("HANDBACK_INSPECTION", None, steps.CLOSURE_PENDING),
    ("SUSPENDED", None, steps.SUSPENDED),
    ("CLOSED", None, steps.CLOSED),
    ("EXPIRED", None, steps.EXPIRED),
])
def test_step_key(status, step, expected):
    assert steps.step_key(status, step) == expected


def test_sql_step_key_mirrors_python():
    """The DDL's ptw_step_key() must classify like steps.step_key()."""
    ddl = Path(__file__).resolve().parents[2] / "safeops_360" / "prisma" / "apply-ptw-control-center-ddl.ts"
    if not ddl.exists():
        pytest.skip("frontend repo not alongside")
    sql = ddl.read_text(encoding="utf-8")
    for key in (steps.ISSUER_REVIEW, steps.SAFETY_REVIEW, steps.PLANT_HEAD, steps.APPROVAL_OTHER,
                steps.AWAITING_ACCEPT, steps.CLOSURE_PENDING):
        assert f"'{key}'" in sql, key


# ── derived statuses ─────────────────────────────────────────────────────────
def test_expired_open_is_past_valid_to_and_not_closed():
    assert expired_open(permit(status="EXPIRED", validTo=NOW - M), NOW)
    assert expired_open(permit(status="ACTIVE", validTo=NOW - M), NOW)
    assert not expired_open(permit(status="CANCELLED", validTo=NOW - M, closureType="UNEXECUTED_CLOSURE"), NOW)
    # closureType alone is not a close-out: /complete stamps it while closure is pending
    assert expired_open(permit(status="WORK_COMPLETED", validTo=NOW - M, closureType="WORK_COMPLETED"), NOW)
    assert is_open(permit(status="HANDBACK_INSPECTION", closureType="WORK_COMPLETED"))
    assert not expired_open(permit(status="CLOSED", validTo=NOW - M, closedAt=NOW - M), NOW)
    assert not expired_open(permit(status="DRAFT", validTo=NOW - M), NOW)
    assert not expired_open(permit(validTo=NOW + M), NOW)


def test_other_derived_statuses():
    assert active_outside_validity(permit(validTo=NOW - M), NOW)
    assert active_outside_validity(permit(status="SUSPENDED", validFrom=NOW + M, validTo=NOW + H), NOW)
    assert not active_outside_validity(permit(), NOW)
    assert expiring_2h(permit(validTo=NOW + 90 * M), NOW)
    assert not expiring_2h(permit(validTo=NOW + 3 * H), NOW)
    assert not expiring_2h(permit(status="SUBMITTED", validTo=NOW + 90 * M), NOW)
    assert starting_2h(permit(status="ISSUED", validFrom=NOW + 30 * M), NOW)
    assert not starting_2h(permit(status="ACTIVE", validFrom=NOW + 30 * M), NOW)
    assert not starting_2h(permit(status="SUBMITTED", validFrom=NOW + 3 * H), NOW)
    assert not is_open(permit(status="CANCELLED"))


# ── impairment conflict check ────────────────────────────────────────────────
def _check(ptype, imps, satisfied=None, running=False, rules=None, area="z1"):
    return ic.check(permit_type=ptype, area_id=area, plant_id="pl", impairments=imps,
                    rules=rules or defaults.impairment_rule_views(), now=NOW, satisfied=satisfied, running=running)


def test_sprinkler_blocks_new_hot_work_with_clear_message():
    d = _check("HOT_WORK", [imp(plannedRestoreAt=datetime(2026, 11, 23, 20, 30, tzinfo=timezone.utc))])
    assert d.blocked
    assert d.blockingMessages == ["Sprinkler SZ-03 impaired until 02:00 — new hot work blocked"]


def test_sprinkler_allows_other_types_and_other_zones():
    assert _check("WORK_AT_HEIGHT", [imp()]).effect == "allow"
    assert _check("HOT_WORK", [imp(areaId="z2")]).effect == "allow"


def test_restored_or_unapproved_impairment_does_not_block():
    assert _check("HOT_WORK", [imp(status="restored", restoredAt=NOW - M)]).effect == "allow"
    assert _check("HOT_WORK", [imp(status="requested")]).effect == "allow"
    assert _check("HOT_WORK", [imp(status="approved", startedAt=NOW + H)]).effect == "allow"
    assert _check("HOT_WORK", [imp(status="approved", startedAt=NOW - M)]).blocked


def test_gas_detection_requires_controls_for_confined_space():
    g = imp(code="GD-07", systemType="gas_detection", systemName="Gas detector GD-07")
    d = _check("CONFINED_SPACE", [g])
    assert d.effect == "require_controls"
    assert d.missingControls == ["continuous_gas_monitor", "attendant"]
    d2 = _check("CONFINED_SPACE", [g], satisfied={"continuous_gas_monitor", "attendant"})
    assert d2.effect == "require_controls" and d2.missingControls == []


def test_fire_alarm_wildcard_and_exact_hot_work_override():
    fa = imp(code="FA-L2", systemType="fire_alarm", systemName="Fire alarm loop L2")
    assert _check("ELECTRICAL_LOTO", [fa]).missingControls == ["hourly_patrol", "no_unattended_work"]
    assert _check("HOT_WORK", [fa]).blocked


def test_running_permit_gets_controls_not_a_block():
    d = _check("HOT_WORK", [imp()], running=True, satisfied={"fire_watch"})
    assert not d.blocked
    assert d.missingControls == ["fire_officer_cosign"]


def test_plant_rule_beats_global():
    rules = defaults.impairment_rule_views() + [
        ic.ImpairmentRuleView(id="x", systemType="sprinkler", permitType="HOT_WORK", effect="allow", plantId="pl")]
    assert _check("HOT_WORK", [imp()], rules=rules).effect == "allow"
    rules_other = defaults.impairment_rule_views() + [
        ic.ImpairmentRuleView(id="x", systemType="sprinkler", permitType="HOT_WORK", effect="allow", plantId="other")]
    assert _check("HOT_WORK", [imp()], rules=rules_other).blocked


def test_rule_cards_and_effect_summary_are_data_driven():
    cards = ic.rule_cards(defaults.impairment_rule_views())
    assert any(c["when"].startswith("Sprinkler impaired + hot work") and c["then"].startswith("Block new")
               for c in cards)
    text, sev = ic.effect_summary(imp(), defaults.impairment_rule_views(), "pl")
    assert sev == "critical" and "hot work blocked" in text


# ── SIMOPS ───────────────────────────────────────────────────────────────────
def test_centroid_of_rectangle():
    assert simops.centroid(demo.rect(10, 20, 30, 40)) == pytest.approx((25.0, 40.0))


def _demo_zones():
    return [ZoneView(f"z-{z.key}", z.name, z.polygon) for z in demo.ZONES]


def test_simops_distance_uses_layout_scale():
    zs = {z.id: z for z in _demo_zones()}
    lay = LayoutView(widthM=demo.LAYOUT_W_M, heightM=demo.LAYOUT_H_M)
    assert simops.zone_distance_m(zs["z-dock"], zs["z-pump"], lay) == pytest.approx(8.26, abs=0.05)
    assert simops.zone_distance_m(zs["z-dock"], zs["z-tank"], lay) > 15
    assert simops.zone_distance_m(zs["z-dock"], zs["z-pump"], LayoutView()) is None


def test_simops_hot_work_near_confined_space():
    lay = LayoutView(widthM=demo.LAYOUT_W_M, heightM=demo.LAYOUT_H_M)
    hw = permit(id="a", number="A", type="HOT_WORK", areaId="z-dock")
    cs = permit(id="b", number="B", type="CONFINED_SPACE", areaId="z-pump")
    far = permit(id="c", number="C", type="CONFINED_SPACE", areaId="z-tank")
    out = simops.detect([hw, cs, far], defaults.simops_rule_views(), _demo_zones(), lay, "pl")
    assert [(c.permitIds, c.label, c.severity) for c in out] == [(("a", "b"), "SIMOPS · 8 m", "critical")]


def test_simops_same_area_and_window_overlap():
    a = permit(id="a", type="LIFTING")
    b = permit(id="b", type="WORK_AT_HEIGHT")
    assert len(simops.detect([a, b], defaults.simops_rule_views(), [], LayoutView(), "pl")) == 1
    later = permit(id="b", type="WORK_AT_HEIGHT", status="APPROVED", validFrom=NOW + 3 * H, validTo=NOW + 5 * H)
    assert simops.detect([a, later], defaults.simops_rule_views(), [], LayoutView(), "pl") == []
    no_rule = permit(id="b", type="GENERAL_COLD")
    assert simops.detect([a, no_rule], defaults.simops_rule_views(), [], LayoutView(), "pl") == []


# ── exception rules ──────────────────────────────────────────────────────────
def keys(drafts):
    return sorted(d.ruleKey for d in drafts)


def test_rule_registry_has_one_module_per_rule():
    assert len({r.RULE_KEY for r in RULES}) == len(RULES) == 10
    for r in RULES:
        assert r.SEVERITY in ("critical", "high", "medium", "low")


def test_permit_expired_open_rule():
    s = snap([permit(status="EXPIRED", validTo=NOW - 30 * M), permit(id="p2", validTo=NOW + H)])
    out = permit_expired_open.evaluate(s)
    assert len(out) == 1 and out[0].severity == "critical" and "expired 30 m ago" in out[0].title


def test_simops_rule():
    s = snap([permit(id="a", type="LIFTING"), permit(id="b", type="WORK_AT_HEIGHT")])
    out = simops_conflict.evaluate(s)
    assert len(out) == 1 and out[0].dedupeKey == "simops_conflict:a:b"


def test_impairment_overdue_rule():
    s = snap(impairments=[imp(plannedRestoreAt=NOW - 50 * M)], areaOwners={"z1": "owner"})
    out = impairment_overdue.evaluate(s)
    assert out[0].severity == "critical" and out[0].ownerUserId == "owner" and "50 m past" in out[0].title
    assert impairment_overdue.evaluate(snap(impairments=[imp()])) == []


def test_hot_work_blocked_by_impairment_rule_resolves_with_controls():
    running = permit(satisfiedControls={"fire_watch"})
    out = hot_work_blocked_by_impairment.evaluate(snap([running], [imp()]))
    assert len(out) == 1 and out[0].severity == "high" and "fire officer co-sign" in out[0].title
    ok = permit(satisfiedControls={"fire_watch", "fire_officer_cosign"})
    assert hot_work_blocked_by_impairment.evaluate(snap([ok], [imp()])) == []


def test_impairment_raised_on_zone_with_running_permits_creates_exceptions():
    """Integration-style: raising an impairment over running work → high exceptions."""
    work = [permit(id="h1"), permit(id="h2", number="PTW-2"),
            permit(id="e1", type="ELECTRICAL_LOTO")]
    before = evaluate_all(snap(work))
    after = evaluate_all(snap(work, [imp(), imp(id="i2", code="FA-L2", systemType="fire_alarm",
                                                 systemName="Fire alarm loop L2")]))
    new = [d for d in after if d.dedupeKey not in {b.dedupeKey for b in before}]
    assert {d.permitId for d in new if d.ruleKey == "hot_work_blocked_by_impairment"} == {"h1", "h2"}
    assert {d.permitId for d in new if d.ruleKey == "impairment_controls_missing"} == {"e1"}
    assert all(d.severity == "high" for d in new)


def test_impairment_controls_missing_rule():
    g = imp(systemType="gas_detection", code="GD-07")
    cs = permit(type="CONFINED_SPACE")
    assert len(impairment_controls_missing.evaluate(snap([cs], [g]))) == 1
    cs.satisfiedControls = {"continuous_gas_monitor", "attendant"}
    assert impairment_controls_missing.evaluate(snap([cs], [g])) == []


def test_gas_test_overdue_rule():
    due = permit(type="CONFINED_SPACE", gasRequired=True, lastGasAt=NOW - 2 * H, gasRefreshDueBy=NOW - 15 * M)
    ok = permit(id="p2", gasRequired=True, lastGasAt=NOW - 10 * M, gasRefreshDueBy=NOW + H)
    missing = permit(id="p3", gasRequired=True, activatedAt=NOW - H)
    fresh = permit(id="p4", gasRequired=True, activatedAt=NOW - 5 * M)
    out = gas_test_overdue.evaluate(snap([due, ok, missing, fresh]))
    assert sorted(d.permitId for d in out) == ["p1", "p3"]


def test_loto_not_verified_rule():
    a = permit(type="ELECTRICAL_LOTO", isolationsTotal=2, isolationsUnverified=1)
    b = permit(id="p2", lotoExecutionStatus="locks_applied")
    c = permit(id="p3", lotoExecutionStatus="verified", isolationsTotal=2)
    out = loto_not_verified.evaluate(snap([a, b, c]))
    assert sorted(d.permitId for d in out) == ["p1", "p2"]
    assert "1 isolation point not verified" == out[0].title


def test_approval_sla_breach_rule_uses_the_permits_own_sla():
    hot = permit(status="SUBMITTED", workflowStepName="Issuer Review", stepEnteredAt=NOW - 3 * H, stepSlaMin=120)
    cold = permit(id="p2", status="SUBMITTED", workflowStepName="Issuer Review", stepEnteredAt=NOW - 3 * H,
                  stepSlaMin=240)
    out = approval_sla_breach.evaluate(snap([hot, cold]))
    assert [d.permitId for d in out] == ["p1"]
    assert out[0].title == "Waiting 3h at issuer review (SLA 2h)"


def test_competency_expiring_rule():
    a = permit(crewCertMinExpiry=NOW + 5 * H, crewCertExpiringCount=2)
    b = permit(id="p2", crewCertMinExpiry=NOW + 3 * 24 * H)
    out = competency_expiring.evaluate(snap([a, b]))
    assert len(out) == 1 and out[0].title == "2 crew competency cards expiring within 24 h"


def test_handover_unacknowledged_rule():
    # Shift B began 14:00 IST (08:30 UTC); NOW is 21:44 IST.
    carried = permit(activatedAt=NOW - 10 * H, validFrom=NOW - 10 * H, lastActivityAt=NOW - 9 * H)
    touched = permit(id="p2", activatedAt=NOW - 10 * H, validFrom=NOW - 10 * H, lastActivityAt=NOW - H)
    new_in_shift = permit(id="p3", activatedAt=NOW - H)
    out = handover_unacknowledged.evaluate(snap([carried, touched, new_in_shift]))
    assert [d.permitId for d in out] == ["p1"] and "Shift B" in out[0].title


def test_evaluate_all_sorts_by_severity_then_age():
    s = snap([permit(status="EXPIRED", validTo=NOW - 30 * M),
              permit(id="p2", status="SUBMITTED", workflowStepName="Issuer Review", stepEnteredAt=NOW - 5 * H,
                     validTo=NOW + 8 * H)])
    out = evaluate_all(s)
    assert [d.severity for d in out] == sorted([d.severity for d in out],
                                               key=lambda x: ["critical", "high", "medium", "low"].index(x))


# ── engine plan ──────────────────────────────────────────────────────────────
def test_reconcile_opens_refreshes_and_resolves():
    d = lambda k: ExceptionDraft(ruleKey="r", severity="high", dedupeKey=k, title=k, plantId="pl")  # noqa: E731
    plan = reconcile({"keep", "gone"}, [d("keep"), d("new"), d("new")])
    assert [x.dedupeKey for x in plan.to_open] == ["new"]
    assert [x.dedupeKey for x in plan.to_refresh] == ["keep"]
    assert plan.to_resolve == ["gone"]


# ── payload helpers ──────────────────────────────────────────────────────────
def test_zero_baseline_delta():
    assert zero_baseline_delta(5, 0) is None
    assert zero_baseline_delta(0, 0) is None
    assert zero_baseline_delta(12, 9) == 3
    assert zero_baseline_delta(0, 4) == -4


def test_pins_stay_inside_their_zone():
    poly = demo.rect(10, 20, 30, 40)
    pts = pin_positions(poly, 7)
    assert len(pts) == 7 and all(10 <= x <= 40 and 20 <= y <= 60 for x, y in pts)
    assert len(set(pts)) == 7


def test_undrawn_zones_get_an_auto_layout_with_pins():
    z = [ZoneView("a", "A"), ZoneView("b", "B")]
    out = build(snap([permit(areaId="a")], zones=z), _extras())
    assert out["map"]["autoLayout"] and all(len(x["polygon"]) == 4 for x in out["map"]["zones"])
    assert len(out["map"]["pins"]) == 1


def test_pack_lanes_caps_at_three():
    bars = [{"_start": NOW, "_end": NOW + H} for _ in range(5)]
    placed, hidden = pack_lanes(bars)
    assert len(placed) == 3 and hidden == 2 and {b["lane"] for b in placed} == {0, 1, 2}


def test_shifts():
    code, start, end = current_shift(NOW)
    assert code == "B" and (end - start) == 8 * H
    day0, day1 = local_day(NOW)
    assert day0 == datetime(2026, 11, 22, 18, 30, tzinfo=timezone.utc) and day1 - day0 == 24 * H
    a, b = range_window(NOW, "7d", None, DEFAULT_SHIFTS)
    assert b - a == 7 * 24 * H


def _extras(**kw):
    day0, day1 = local_day(NOW)
    base = dict(windowStart=day0, windowEnd=day1, plantName="Demo")
    base.update(kw)
    return Extras(**base)


def test_all_zero_payload_has_no_red_and_no_fake_deltas():
    out = build(snap(), _extras())
    k = out["kpis"]
    assert k["deltas"]["activeNow"] is None
    assert all(v == 0 for key, v in k.items() if isinstance(v, int))
    assert all(c["pct"] is None for c in out["compliance"])   # n/a, not 0 % red
    assert all(not s["breached"] and s["avgDwellMin"] is None for s in out["pipeline"])
    assert out["exceptions"] == [] and out["timeline"] == [] and out["typeMix"] == []


# ── the seeded demo plant ────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def demo_payload():
    sc = demo.build(NOW)
    s = demo.snapshot(sc)
    day0, day1 = local_day(NOW)
    ex = Extras(windowStart=day0, windowEnd=day1, plantName=demo.PLANT_NAME,
                areaNames={z.id: z.name for z in s.zones}, activeBaseline=9)
    return build(s, ex)


def test_demo_payload_matches_seed_numbers(demo_payload):
    k = demo_payload["kpis"]
    assert k["activeNow"] == 12
    assert k["expiredOpen"] == 4 and k["expiredOpenToday"] == 2
    assert k["pendingApproval"] == 6 and k["pendingPastSla"] == 6
    assert k["workersUnderPermit"] == 87
    assert k["contractorsOnSite"] == 4
    assert k["activeImpairments"] == 3 and k["impairmentsOverdue"] == 1 and k["impairmentsBlocking"] == 2
    assert k["simopsConflicts"] == 1
    assert k["suspended"] == 1
    assert k["deltas"]["activeNow"] == 3
    steps_by = {s["step"]: s for s in demo_payload["pipeline"]}
    assert steps_by["ISSUER_REVIEW"]["count"] == 6 and steps_by["ISSUER_REVIEW"]["breached"]
    assert steps_by["CLOSURE_PENDING"]["count"] == 4
    closed = sum(1 for r in demo_payload["timeline"] for b in r["bars"] if b["kind"] == "closed")
    hidden = sum(r["hidden"] for r in demo_payload["timeline"])
    assert closed + hidden >= 11


def test_demo_impairment_statuses_and_simops_label(demo_payload):
    st = {i["code"]: i["status"] for i in demo_payload["impairments"]}
    assert st == {"SZ-03": "on_track", "GD-07": "due_soon", "FA-L2": "overdue"}
    (c,) = demo_payload["map"]["conflicts"]
    assert c["label"] == "SIMOPS · 8 m" and set(c["areaIds"]) == {"z-dock", "z-pump"}


def test_demo_exceptions_cover_the_story(demo_payload):
    rules = {e["ruleKey"] for e in demo_payload["exceptions"]}
    assert {"permit_expired_open", "simops_conflict", "impairment_overdue", "hot_work_blocked_by_impairment",
            "impairment_controls_missing", "gas_test_overdue", "loto_not_verified",
            "approval_sla_breach"} <= rules
    sev = [e["severity"] for e in demo_payload["exceptions"]]
    assert sev == sorted(sev, key=["critical", "high", "medium", "low"].index)


def test_demo_history_expiry_rate():
    sc = demo.build(NOW)
    recent = [p for p in sc.permits if p.createdAt and p.createdAt >= NOW - timedelta(days=84)]
    expired = [p for p in recent if p.status == "EXPIRED" or p.expiredAt is not None]
    assert len(recent) == 352 and len(expired) == 49


def test_brief_inputs_are_counts_only(demo_payload):
    inputs = brief_inputs(demo_payload)
    blob = json.dumps(inputs)
    assert "u-receiver" not in blob and "PTW-" not in blob and "u-issuer" not in blob
    text = template_text(inputs)
    assert text.count(".") <= 4 and "[[" in text and "12 permits active with 87 workers" in text


# ── shifts: the live master set (A inactive → 06–09 gap; G overlaps B) ──────
LIVE_SHIFTS = [
    {"id": "sB", "code": "B", "label": "B — Afternoon", "startHour": 14, "endHour": 22},
    {"id": "sC", "code": "C", "label": "C — Night", "startHour": 22, "endHour": 6},
    {"id": "sG", "code": "G", "label": "G — General", "startHour": 9, "endHour": 17},
]


def ist(h, m=0, day=24):
    """A UTC instant for h:m IST on 2026-11-<day>."""
    return datetime(2026, 11, day, h, m, tzinfo=timezone.utc) - timedelta(hours=5, minutes=30)


def test_shift_at_overlap_prefers_the_most_recent_start():
    from app.services.ptw_ops.shifts import shift_at

    assert shift_at(ist(10), LIVE_SHIFTS)["code"] == "G"
    assert shift_at(ist(15), LIVE_SHIFTS)["code"] == "B"   # G 09–17 and B 14–22 overlap
    assert shift_at(ist(23), LIVE_SHIFTS)["code"] == "C"
    assert shift_at(ist(3), LIVE_SHIFTS)["code"] == "C"    # crosses midnight


def test_shift_at_gap_assigns_nothing():
    from app.services.ptw_ops.shifts import shift_at

    assert shift_at(ist(7), LIVE_SHIFTS) is None           # A is inactive: 06–09 is uncovered


def test_current_shift_in_a_gap_is_the_one_just_ended_not_a_future_window():
    code, start, end = current_shift(ist(7), LIVE_SHIFTS)
    assert code == "C" and end <= ist(7) and start < end   # last night's C, never today's B (future)
    code, start, end = current_shift(ist(15), LIVE_SHIFTS)
    assert code == "B" and start <= ist(15) < end
