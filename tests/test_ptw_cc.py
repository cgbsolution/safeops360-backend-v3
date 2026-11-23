"""PTW control center, insights and paper-permit scan: offline unit tests (no DB, no network)."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import ptw_cc as cc
from app.services import ptw_scan as sc

NOW = datetime(2026, 11, 23, 6, 0, tzinfo=timezone.utc)  # 11:30 IST


def snap(pid: str, status: str = "ACTIVE", *, area: str | None = "a1", type_: str = "GENERAL_COLD",
         start: int = -60, end: int = 240, **kw) -> cc.PermitSnap:
    return cc.PermitSnap(
        id=pid, number=f"PTW-{pid}", type=type_, status=status, plantId="p1", areaId=area,
        validFrom=NOW + timedelta(minutes=start), validTo=NOW + timedelta(minutes=end), **kw,
    )


def codes(s: cc.PermitSnap) -> set[str]:
    return {e.code for e in cc.evaluate_permit(s, NOW)}


# ── SIMOPS ───────────────────────────────────────────────────────────────────
def test_windows_overlap_is_half_open():
    t = NOW
    assert cc.windows_overlap(t, t + timedelta(hours=2), t + timedelta(hours=1), t + timedelta(hours=3))
    assert not cc.windows_overlap(t, t + timedelta(hours=1), t + timedelta(hours=1), t + timedelta(hours=2))


def test_simops_same_area_overlap_only():
    a = snap("a", area="x")
    b = snap("b", area="x", start=60, end=300)
    c = snap("c", area="y")                      # other area
    d = snap("d", area="x", start=250, end=400)  # same area, overlaps b only
    e = snap("e", area=None)                     # cannot be placed
    pairs = cc.simops_pairs([a, b, c, d, e])
    assert pairs["a"] == {"b"} and pairs["b"] == {"a", "d"} and pairs["d"] == {"b"}
    assert "c" not in pairs and "e" not in pairs


def test_simops_stored_conflicts_only_while_other_is_live():
    a = snap("a", area="x", conflictingPermitIds=["z", "closed1"])
    z = snap("z", area="y")
    closed = snap("closed1", status="CLOSED", area="x")
    pairs = cc.simops_pairs([a, z, closed])
    assert pairs == {"a": {"z"}, "z": {"a"}}


def test_simops_severity():
    hot = snap("h", type_="HOT_WORK")
    cold = snap("c")
    approved = snap("ap", status="APPROVED", type_="HOT_WORK")
    assert cc.simops_severity(hot, cold) == "high"
    assert cc.simops_severity(cold, snap("c2")) == "medium"
    assert cc.simops_severity(approved, cold) == "medium"  # not both on site yet


def test_simops_exception_deduplicated():
    ex, _ = cc.evaluate([snap("a", area="x"), snap("b", area="x")], NOW)
    sim = [e for e in ex if e.code == "SIMOPS_CONFLICT"]
    assert len(sim) == 1 and sim[0].relatedPermitIds == ["b"]


# ── exception rules ──────────────────────────────────────────────────────────
def test_healthy_active_permit_has_no_exceptions():
    assert codes(snap("a")) == set()


def test_expired_not_closed_both_paths():
    assert codes(snap("a", status="EXPIRED", end=-30)) == {"EXPIRED_NOT_CLOSED"}
    assert codes(snap("b", status="EXPIRED", end=-30, closureType="UNEXECUTED_CLOSURE")) == set()
    e = cc.evaluate_permit(snap("c", status="ACTIVE", end=-90), NOW)[0]
    assert e.code == "EXPIRED_NOT_CLOSED" and e.severity == "critical" and e.minutes == 90


def test_expiring_soon_severity_by_status():
    act = cc.evaluate_permit(snap("a", end=45), NOW)
    iss = cc.evaluate_permit(snap("b", status="ISSUED", start=-10, end=45), NOW)
    assert [(e.code, e.severity, e.minutes) for e in act] == [("EXPIRING_SOON", "high", 45)]
    assert iss[0].severity == "medium"
    assert codes(snap("c", end=61)) == set()


def test_approved_awaiting_issue_and_lapsed():
    assert codes(snap("a", status="APPROVED", start=-5)) == {"APPROVED_AWAITING_ISSUE"}
    assert codes(snap("b", status="APPROVED", start=30)) == set()
    assert codes(snap("c", status="APPROVED", start=-300, end=-10)) == {"LAPSED_BEFORE_ISSUE"}


def test_issued_not_accepted_after_grace():
    assert codes(snap("a", status="ISSUED", start=-31)) == {"ISSUED_NOT_ACCEPTED"}
    assert codes(snap("b", status="ISSUED", start=-10)) == set()


def test_suspended():
    e = cc.evaluate_permit(snap("a", status="SUSPENDED", suspendedAt=NOW - timedelta(minutes=20),
                                suspendedReason="Rain"), NOW)
    assert [(x.code, x.minutes, x.detail) for x in e] == [("SUSPENDED", 20, "Rain")]


def test_gas_rules():
    base = dict(gasTestRequired=True)
    assert codes(snap("m", **base)) == {"GAS_TEST_MISSING"}
    assert codes(snap("o", lastGasAt=NOW - timedelta(hours=3), gasRefreshDueBy=NOW - timedelta(minutes=5), **base)) == {"GAS_TEST_OVERDUE"}
    assert codes(snap("d", lastGasAt=NOW - timedelta(hours=1), gasRefreshDueBy=NOW + timedelta(minutes=10), hasGasPlan=True)) == {"GAS_TEST_DUE"}
    assert codes(snap("k", lastGasAt=NOW, gasRefreshDueBy=NOW + timedelta(hours=2), **base)) == set()
    assert codes(snap("x", lastGasAt=NOW, gasRefreshDueBy=NOW + timedelta(hours=2), lastGasExceedance=True, **base)) == {"GAS_EXCEEDANCE"}
    # suspended / not-required permits are not chased for re-tests
    assert "GAS_TEST_MISSING" not in codes(snap("s", status="SUSPENDED", **base))
    assert codes(snap("n")) == set()


def test_evaluate_orders_worst_first():
    ex, _ = cc.evaluate([
        snap("low", status="APPROVED", start=-5, area="q"),
        snap("crit", status="EXPIRED", end=-30, area="r"),
        snap("high", end=30, area="s"),
    ], NOW)
    assert [e.severity for e in ex] == ["critical", "high", "low"]


def test_build_payload_kpis_and_area_grouping():
    snaps = [
        snap("a", area="x", crewCount=4),
        snap("b", area="x", crewCount=2, start=60, end=300, type_="HOT_WORK"),
        snap("c", status="SUSPENDED", area="y", crewCount=7),
        snap("d", status="EXPIRED", area=None, end=-10),
        snap("e", status="APPROVED", area="y", start=120, end=400),
    ]
    p = cc.build_payload(snaps, now=NOW, area_names={"x": ("Melt shop", "p1"), "y": ("CRM", "p1")},
                         user_names={}, plant_names={"p1": "Hisar"}, closed_last_24h=3)
    k = p["kpis"]
    assert (k["livePermits"], k["active"], k["suspended"], k["awaitingIssue"]) == (3, 2, 1, 1)
    assert k["workersOnSite"] == 6           # crew on ACTIVE permits only
    assert k["expiredNotClosed"] == 1 and k["simopsConflicts"] == 2 and k["closedLast24h"] == 3  # a/b in x, c/e in y
    names = [a["areaName"] for a in p["areas"]]
    assert names[0] == "No area recorded"    # the critical (expired) group sorts first
    melt = next(a for a in p["areas"] if a["areaName"] == "Melt shop")
    assert melt["workers"] == 6 and melt["worstSeverity"] == "high"
    card = next(c for c in melt["permits"] if c["id"] == "a")
    assert card["simopsWith"] == ["PTW-b"] and "SIMOPS_CONFLICT" in card["flags"]
    crm = next(a for a in p["areas"] if a["areaName"] == "CRM")
    assert next(c for c in crm["permits"] if c["id"] == "c")["crewCount"] == 7


# ── insights maths ───────────────────────────────────────────────────────────
def test_percentile_and_cycle_stats():
    assert cc.percentile([], 50) is None
    assert cc.percentile([4.0], 90) == 4.0
    assert cc.percentile([1, 2, 3, 4], 50) == 2.5
    assert cc.percentile([0, 10], 90) == pytest.approx(9.0)
    s = cc.cycle_stats([1.0, 3.0, -2.0])  # negative (clock skew) dropped
    assert s == {"n": 2, "medianHours": 2.0, "p90Hours": 2.8, "meanHours": 2.0}


def test_final_approval_falls_back_to_issue():
    t = NOW
    assert cc.final_approval_at(t, t + timedelta(hours=1), None, t + timedelta(hours=5)) == t + timedelta(hours=1)
    assert cc.final_approval_at(None, None, None, t) == t


def row(pid="r", **kw) -> cc.InsightRow:
    base = dict(id=pid, type="HOT_WORK", status="CLOSED", createdAt=NOW, validFrom=NOW + timedelta(hours=1),
                validTo=NOW + timedelta(hours=9))
    base.update(kw)
    return cc.InsightRow(**base)


def test_cycle_times_per_stage():
    r = row(approvedAt=NOW + timedelta(hours=2), issuedAt=NOW + timedelta(hours=2),
            activatedAt=NOW + timedelta(hours=3), workCompletedAt=NOW + timedelta(hours=8),
            closedAt=NOW + timedelta(hours=8, minutes=30))
    ct = {c["key"]: c for c in cc.cycle_times([r, row("open")])}
    assert ct["submitToApproved"]["medianHours"] == 2.0 and ct["submitToApproved"]["n"] == 1
    assert ct["approvedToIssued"]["medianHours"] == 0.0
    assert ct["issuedToActivated"]["medianHours"] == 1.0
    assert ct["workCompletedToClosed"]["medianHours"] == 0.5
    assert ct["endToEnd"]["medianHours"] == 8.5


def test_shift_for_wraps_midnight():
    assert cc.shift_for(6, cc.DEFAULT_SHIFTS) == "Shift A (06-14)"
    assert cc.shift_for(14, cc.DEFAULT_SHIFTS) == "Shift B (14-22)"
    assert cc.shift_for(23, cc.DEFAULT_SHIFTS) == "Shift C (22-06)"
    assert cc.shift_for(3, cc.DEFAULT_SHIFTS) == "Shift C (22-06)"


def test_gas_compliance():
    act = NOW
    r = row("g", gasRequired=True, activatedAt=act, workCompletedAt=act + timedelta(hours=6))
    h = lambda n: act + timedelta(hours=n)  # noqa: E731
    readings = {"g": [
        (h(0), h(2), False, True),     # pre-entry
        (h(1.5), h(3.5), False, False),  # on time (due h2)
        (h(4), h(6), True, False),     # late (due h3.5), exceedance
    ]}
    # the last reading falls due at h6 == work end → not counted as missed
    g = cc.gas_compliance([r, row("n")], readings, NOW + timedelta(days=1))
    assert g["permitsRequiringGasTest"] == 1 and g["permitsWithInitialTest"] == 1
    assert (g["refreshesEvaluated"], g["refreshesOnTime"], g["refreshCompliancePct"]) == (2, 1, 50.0)
    assert g["exceedances"] == 1
    r2 = row("g2", gasRequired=True, activatedAt=act, workCompletedAt=act + timedelta(hours=8))
    g2 = cc.gas_compliance([r2], {"g2": [(h(0.1), h(2), False, False)]}, NOW + timedelta(days=1))
    assert (g2["refreshesEvaluated"], g2["refreshesOnTime"]) == (1, 0)  # due at h2, work ran to h8


def test_on_time_closure():
    vt = NOW + timedelta(hours=9)
    rows = [
        row("a", validTo=vt, closedAt=vt + timedelta(minutes=30), closureType="WORK_COMPLETED"),
        row("b", validTo=vt, closedAt=vt + timedelta(hours=3), closureType="WORK_COMPLETED"),
        row("c", validTo=vt, closedAt=vt + timedelta(hours=3), closureType="UNEXECUTED_CLOSURE"),
        row("d", status="EXPIRED"),
    ]
    c = cc.on_time_closure(rows)
    assert (c["closed"], c["onTime"], c["late"], c["onTimePct"], c["expiredNotClosed"]) == (2, 1, 1, 50.0, 1)


def test_compute_insights_breakdowns():
    rows = [
        row("a", contractorCompanyId="c1", areaId="x", issuerId="u1", validFrom=datetime(2026, 11, 20, 3, 0, tzinfo=timezone.utc)),
        row("b", contractorName="Ram Builders", areaId="x", issuerId="u1", status="ACTIVE",
            validFrom=datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc)),
        row("c", type="LIFTING", issuerId="u2", status="CANCELLED"),
    ]
    out = cc.compute_insights(rows, now=NOW, start=NOW - timedelta(days=30), end=NOW,
                              extensions=[("a", "APPROVED"), ("a", "PENDING")], suspensions=[("b", "Rain"), ("b", "Rain")],
                              gas_readings={})
    assert out["totals"] == {"raised": 3, "closed": 1, "cancelled": 1, "rejected": 0, "expired": 0, "open": 1}
    assert out["byType"][0] == {"key": "HOT_WORK", "count": 2}
    assert {r["key"] for r in out["byContractor"]} == {"id:c1", "text:Ram Builders", "none"}
    assert out["byHour"][8]["count"] == 1      # 03:00 UTC = 08:30 IST
    assert out["byHour"][23]["count"] == 1     # 18:00 UTC = 23:30 IST
    shifts = {s["key"]: s["count"] for s in out["byShift"]}
    assert shifts["Shift A (06-14)"] == 2 and shifts["Shift C (22-06)"] == 1  # c defaults to 12:30 IST
    assert out["extensions"]["total"] == 2 and out["extensions"]["permitsExtended"] == 1
    assert out["suspensions"]["topReasons"] == [{"key": "Rain", "count": 2}]
    assert out["topIssuers"][0] == {"key": "u1", "count": 2}
    assert out["trend"]["granularity"] == "day"


def test_window_bounds():
    start, end, frm, to = cc.window_bounds(date(2026, 11, 1), date(2026, 11, 1))
    assert end - start == timedelta(days=1)
    assert start == datetime(2026, 10, 31, 18, 30, tzinfo=timezone.utc)  # midnight IST
    _, _, frm, to = cc.window_bounds(None, None, today=date(2026, 11, 23))
    assert (to - frm).days == 89
    with pytest.raises(ValueError):
        cc.window_bounds(date(2026, 11, 2), date(2026, 11, 1))


# ── scan: input handling ─────────────────────────────────────────────────────
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64
PDF = b"%PDF-1.4\n" + b"0" * 64


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def test_decode_files_rules():
    out = sc.decode_files([{"name": "p1.jpg", "dataB64": b64(JPEG)}])
    assert out[0].media_type == "image/jpeg"
    assert sc.decode_files([{"name": "p.pdf", "dataB64": b64(PDF)}])[0].media_type == "application/pdf"
    with pytest.raises(sc.ScanError, match="not both"):
        sc.decode_files([{"dataB64": b64(PDF)}, {"dataB64": b64(JPEG)}])
    with pytest.raises(sc.ScanError, match="at most 4"):
        sc.decode_files([{"dataB64": b64(JPEG)}] * 5)
    with pytest.raises(sc.ScanError, match="not a JPEG"):
        sc.decode_files([{"name": "x.gif", "dataB64": b64(b"GIF89a" + b"0" * 20)}])
    with pytest.raises(sc.ScanError, match="decoded"):
        sc.decode_files([{"name": "bad", "dataB64": "@@@@"}])
    with pytest.raises(sc.ScanError):
        sc.decode_files([])


def test_value_normalisers():
    assert sc.to_local_dt("23/11/2026 14:30") == "2026-11-23T14:30"
    assert sc.to_local_dt("2026-11-23T09:05") == "2026-11-23T09:05"
    assert sc.to_local_dt("23-11-2026 02:30 PM") == "2026-11-23T14:30"
    assert sc.to_local_dt("14:30") is None and sc.to_local_dt("31/02/2026 10:00") is None
    assert sc.to_permit_type("HOT_WORK") == "HOT_WORK"
    assert sc.to_permit_type(None, "Confined Space Entry Permit") == "CONFINED_SPACE"
    assert sc.to_permit_type("Electrical isolation") == "ELECTRICAL_LOTO"
    assert sc.to_permit_type("something else") is None
    assert sc.to_ppe_codes(["Safety Helmet", "Safety shoes", "Full body harness", "Welding gloves", "Helmet", "cape"]) == [
        "helmet", "shoes", "harness", "welding_gloves"]
    assert sc.guess_isolation_type("MCC breaker 4B") == "ELECTRICAL"


def test_match_name():
    users = [("u1", "Rajesh Kumar"), ("u2", "Rakesh Kumar"), ("u3", "Suresh Yadav"), ("u4", "Amit Singh")]
    assert sc.match_name("RAJESH KUMAR", users).id == "u1"
    assert sc.match_name("Mr. Suresh Yadav", users).id == "u3"
    assert sc.match_name("Singh Amit", users).id == "u4"
    amb = sc.match_name("R. Kumar", users)       # two Kumars with an R
    assert amb.id is None and amb.reason == "more than one close match" and len(amb.candidates) >= 2
    none = sc.match_name("Pooja Verma", users)
    assert none.id is None and none.reason == "no close match"
    areas = [("a1", "SMS Furnace 2"), ("a2", "Cold Rolling Mill"), ("a3", "SMS Furnace 3")]
    assert sc.match_name("cold rolling mill", areas).id == "a2"
    assert sc.match_name("Furnace 2", areas).id == "a1"


MODEL_REPLY = {
    "isPermit": True,
    "fields": {
        "permitType": {"value": "HOT_WORK", "text": "Hot Work", "confidence": 0.95},
        "paperPermitNumber": {"value": "HW/0457", "text": "HW/0457", "confidence": 0.9},
        "plant": {"value": "JSL Hisar", "text": "JSL Hisar", "confidence": 0.9},
        "area": {"value": "Cold Rolling Mill", "text": "CRM", "confidence": 0.8},
        "specificLocation": {"value": "Pickling line tank 3", "confidence": 0.75},
        "scopeOfWork": {"value": "Welding of support bracket on tank 3 platform", "confidence": 0.85},
        "validFrom": {"value": "23/11/2026 09:00", "text": "23/11/26 9:00", "confidence": 0.8},
        "validTo": {"value": "2026-11-23T17:00", "confidence": 0.8},
        "workOrderNumber": {"value": None, "confidence": 0},
        "contractor": {"value": "Ram Builders", "confidence": 0.7},
        "issuerName": {"value": "Rajesh Kumar", "confidence": 0.9},
        "receiverName": {"value": "Pooja Verma", "confidence": 0.6},
        "fireWatchName": {"value": "Suresh Yadav", "confidence": 0.5},
        "windSpeedKmh": {"value": "12", "confidence": 0.6},
        "rescuePlan": "Ladder rescue",  # bare value, not an object
    },
    "extraHazards": {"value": ["WORK_AT_HEIGHT", "HOT_WORK", "SPACE_TRAVEL"], "confidence": 0.7},
    "crew": [{"name": "Amit Singh", "role": "welder", "confidence": 0.8}, {"name": "Unknown Person", "confidence": 0.4}],
    "isolations": [{"description": "Steam line valve V-12", "isolationPointTag": None, "confidence": 0.7}],
    "ppe": {"value": ["Helmet", "Welding gloves", "FR coverall"], "confidence": 0.8},
    "gasReadings": [{"parameter": "lel", "value": "0", "unit": "%", "confidence": 0.9}],
    "unreadable": ["department", "bogus"],
    "notes": "Signature block partly torn",
}

MASTERS = sc.Masters(
    plant_id="p1", plant_name="JSL Hisar",
    areas=[("a1", "Cold Rolling Mill"), ("a2", "Steel Melt Shop")],
    departments=[("d1", "Maintenance")],
    contractors=[("c1", "Ram Builders Pvt Ltd"), ("c2", "Shyam Engineering")],
    users=[("u1", "Rajesh Kumar"), ("u3", "Suresh Yadav"), ("u4", "Amit Singh"), ("me", "Deepak Originator")],
    current_user_id="me",
)


def test_normalise_extraction_from_mocked_reply():
    ex = sc.normalise_extraction(MODEL_REPLY)
    f = ex.fields
    assert f["permitType"].value == "HOT_WORK"
    assert f["validFrom"].value == "2026-11-23T09:00" and f["validTo"].value == "2026-11-23T17:00"
    assert f["workOrderNumber"].value is None and f["workOrderNumber"].confidence == 0.0
    assert f["windSpeedKmh"].value == 12.0
    assert f["rescuePlan"].value == "Ladder rescue"
    assert ex.extra_hazards == ["WORK_AT_HEIGHT"]          # base type and unknown codes dropped
    assert ex.crew[0]["role"] == "WORKER"                  # unknown role → WORKER
    assert ex.isolations[0]["isolationType"] == "STEAM" and ex.isolations[0]["isolationPointTag"] == "TBC"
    assert ex.ppe == ["helmet", "welding_gloves", "fr_coverall"]
    assert ex.gas_readings[0]["parameter"] == "LEL"
    assert ex.unreadable == ["department"]
    assert not sc.normalise_extraction({"isPermit": False}).is_permit


def test_build_draft_resolves_and_flags():
    out = sc.build_draft(sc.normalise_extraction(MODEL_REPLY), MASTERS)
    d = out["draft"]
    assert d["type"] == "HOT_WORK" and d["plantId"] == "p1" and d["areaId"] == "a1"
    assert d["validFrom"] == "2026-11-23T09:00" and d["issuerId"] == "u1" and d["fireWatchPersonId"] == "u3"
    assert d["contractorCompanyId"] == "c1"               # "Ram Builders" ~ "Ram Builders Pvt Ltd"
    assert "receiverId" not in d                           # Pooja Verma is not a user
    assert d["workCrew"] == [{"userId": "u4", "role": "WORKER"}]
    assert d["extraHazards"] == ["WORK_AT_HEIGHT"] and d["requiredPpe"][0] == "helmet"
    assert d["isolations"][0]["isolationPointTag"] == "TBC"
    unresolved = {u["field"]: u for u in out["unresolved"]}
    assert set(unresolved) == {"receiverName", "crew"}
    fields = {f["key"]: f for f in out["fields"]}
    assert fields["department"]["status"] == "unreadable"
    assert fields["receiverName"]["status"] == "unresolved"
    assert fields["fireWatchName"]["status"] == "low"     # model confidence 0.5
    assert fields["issuerName"]["status"] == "ok" and fields["issuerName"]["display"] == "Rajesh Kumar"
    assert fields["workOrderNumber"]["status"] == "blank"
    assert out["paperPermitNumber"] == "HW/0457"
    assert any("TBC" in w for w in out["warnings"])
    # every key the draft carries is one the wizard's patch schema accepts
    from app.services.copilot.ptw_draft import PtwDraftPatch
    PtwDraftPatch.model_validate(d)


def test_build_draft_refuses_self_issuer_and_trims_long_text():
    reply = json.loads(json.dumps(MODEL_REPLY))
    reply["fields"]["issuerName"] = {"value": "Deepak Originator", "confidence": 0.9}
    reply["fields"]["specificLocation"] = {"value": "x" * 500, "confidence": 0.9}
    out = sc.build_draft(sc.normalise_extraction(reply), MASTERS)
    assert "issuerId" not in out["draft"] and any("own issuer" in w for w in out["warnings"])
    assert len(out["draft"]["specificLocation"]) == 200


# ── scan: model call with a mocked client ────────────────────────────────────
class FakeMessages:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def create(self, **kw):
        self.calls.append(kw)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(content=[SimpleNamespace(text=r)], usage=SimpleNamespace(input_tokens=10, output_tokens=5))


class FakeClient:
    def __init__(self, replies):
        self.messages = FakeMessages(replies)

    def with_options(self, **kw):
        return self


def test_extract_images_sends_image_blocks(monkeypatch):
    fake = FakeClient(["Here you go: " + json.dumps(MODEL_REPLY)])
    monkeypatch.setattr("app.services.ai.anthropic_client._get_client", lambda: fake)
    files = sc.decode_files([{"dataB64": b64(JPEG)}, {"dataB64": b64(JPEG)}])
    data, method = asyncio.run(sc.extract(files, today="2026-11-23", plant_name="JSL Hisar"))
    assert method == "vision" and data["fields"]["permitType"]["value"] == "HOT_WORK"
    content = fake.messages.calls[0]["messages"][0]["content"]
    assert [b["type"] for b in content] == ["image", "image", "text"]
    assert "temperature" not in fake.messages.calls[0]


def test_extract_pdf_falls_back_to_text(monkeypatch):
    fake = FakeClient([RuntimeError("document blocks unsupported"), json.dumps(MODEL_REPLY)])
    monkeypatch.setattr("app.services.ai.anthropic_client._get_client", lambda: fake)
    monkeypatch.setattr(sc, "pdf_text", lambda data, max_pages=10: "PERMIT TO WORK  Hot work  Issuer: Rajesh Kumar")
    monkeypatch.setattr(sc, "pdf_page_count", lambda data: 2)
    files = sc.decode_files([{"dataB64": b64(PDF)}])
    data, method = asyncio.run(sc.extract(files, today="2026-11-23", plant_name="JSL Hisar"))
    assert method == "pdf_text"
    first, second = fake.messages.calls
    assert first["messages"][0]["content"][0]["type"] == "document"
    assert "Rajesh Kumar" in second["messages"][0]["content"][0]["text"]


def test_extract_pdf_without_text_layer_fails_cleanly(monkeypatch):
    fake = FakeClient([RuntimeError("boom")])
    monkeypatch.setattr("app.services.ai.anthropic_client._get_client", lambda: fake)
    monkeypatch.setattr(sc, "pdf_text", lambda data, max_pages=10: "")
    monkeypatch.setattr(sc, "pdf_page_count", lambda data: 1)
    with pytest.raises(sc.ScanError, match="Photograph"):
        asyncio.run(sc.extract(sc.decode_files([{"dataB64": b64(PDF)}]), today="2026-11-23", plant_name=None))


def test_extract_without_ai_configured(monkeypatch):
    monkeypatch.setattr("app.services.ai.anthropic_client._get_client", lambda: None)
    with pytest.raises(sc.ScanError, match="AI_NOT_CONFIGURED"):
        asyncio.run(sc.extract(sc.decode_files([{"dataB64": b64(JPEG)}]), today="2026-11-23", plant_name=None))


def test_parse_model_json_tolerates_prose():
    assert sc.parse_model_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert sc.parse_model_json("no json here") is None
    assert sc.parse_model_json("[1, 2]") is None
