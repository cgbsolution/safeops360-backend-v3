"""PHA / HAZOP: offline unit tests (no DB), house style of test_ims.py.

Covers the guideword library and row generation, risk ranking against a
matrix, the study workflow and its gates, recommendation / CAPA sync rules,
the XLSX worksheet shape, and AI suggestion parsing with a mocked response.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from app.services import pha as svc
from app.services import pha_ai
from app.services import pha_library as lib
from app.services.pha_export import REC_COLUMNS, WORKSHEET_COLUMNS, worksheet_xlsx

NOW = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)


# ── library ──────────────────────────────────────────────────────────────────
def test_library_codes_unique_and_combinations_valid():
    assert len(lib.GUIDEWORD_CODES) == 11 and len(set(lib.GUIDEWORD_CODES)) == 11
    assert len(lib.PARAMETER_CODES) == 13 and len(set(lib.PARAMETER_CODES)) == 13
    for param, gws in lib.COMBINATIONS.items():
        assert param in lib.PARAMETER_CODES
        for gw, text in gws.items():
            assert gw in lib.GUIDEWORD_CODES and text.strip()
    # every parameter has at least two meaningful guidewords
    assert all(len(lib.COMBINATIONS[p]) >= 2 for p in lib.PARAMETER_CODES)


def test_meaningless_pairs_are_excluded():
    assert lib.is_meaningful("REVERSE", "FLOW")
    assert not lib.is_meaningful("REVERSE", "TEMPERATURE")
    assert not lib.is_meaningful("EARLY", "LEVEL")
    assert lib.deviation_text("MORE", "PRESSURE") == "High pressure"


def test_generate_rows_orders_skips_and_dedupes():
    rows, skipped = lib.generate_rows(
        [("MORE", "TEMPERATURE"), ("NO", "FLOW"), ("REVERSE", "TEMPERATURE"), ("MORE", "PRESSURE"),
         ("NO", "FLOW"), {"guideword": "Less", "parameter": "Flow"}],
        existing=[("MORE", "PRESSURE")],
    )
    # library order: FLOW (NO, LESS) before TEMPERATURE
    assert [(r["guideword"], r["parameter"]) for r in rows] == [("NO", "FLOW"), ("LESS", "FLOW"), ("MORE", "TEMPERATURE")]
    assert rows[0]["deviation"] == "No flow"
    reasons = {(s["guideword"], s["parameter"]): s["reason"] for s in skipped}
    assert reasons[("REVERSE", "TEMPERATURE")] == "not a meaningful combination"
    assert reasons[("MORE", "PRESSURE")] == "already on this node"


def test_normalise_labels_and_aliases():
    assert lib.normalise_guideword("No / None") == "NO"
    assert lib.normalise_guideword("as well as") == "AS_WELL_AS"
    assert lib.normalise_guideword("High") == "MORE"
    assert lib.normalise_guideword("sideways") is None
    assert lib.normalise_parameter("Start-up / Shutdown") == "STARTUP_SHUTDOWN"
    assert lib.normalise_parameter("temp") == "TEMPERATURE"


def test_library_payload_lists_every_combination():
    payload = lib.library()
    assert len(payload["combinations"]) == sum(len(v) for v in lib.COMBINATIONS.values())


# ── risk ranking ─────────────────────────────────────────────────────────────
def _matrix_3x3() -> svc.Matrix:
    # deliberately NOT the product: cell (3, 3) is CRITICAL score 99 so the test
    # proves the cell map wins over arithmetic
    cells = {}
    for l in range(1, 4):
        for s in range(1, 4):
            lvl = "LOW" if l * s <= 2 else "MODERATE" if l * s <= 4 else "HIGH"
            cells[(l, s)] = {"score": l * s, "level": lvl, "color": "#00ff00" if lvl == "LOW" else "#ffaa00"}
    cells[(3, 3)] = {"score": 99, "level": "CRITICAL", "color": "#ff0000"}
    return svc.Matrix(id="m3", code="M3", name="3x3", likelihood_levels=3, severity_levels=3, cells=cells)


def test_rate_uses_matrix_cell():
    m = _matrix_3x3()
    assert svc.rate(m, severity=3, likelihood=3) == {"score": 99, "level": "CRITICAL", "color": "#ff0000"}
    assert svc.rate(m, severity=1, likelihood=2)["level"] == "LOW"


def test_rate_unrated_and_out_of_scale():
    m = _matrix_3x3()
    assert svc.rate(m, None, 2) is None
    with pytest.raises(ValueError):
        svc.rate(m, severity=4, likelihood=1)
    with pytest.raises(ValueError):
        svc.rate(m, severity=1, likelihood=0)


def test_rate_sparse_matrix_falls_back_to_banding():
    m = _matrix_3x3()
    del m.cells[(2, 2)]
    r = svc.rate(m, severity=2, likelihood=2)
    assert r["score"] == 4 and r["level"] in svc.LEVEL_COLORS


def test_default_matrix_bands():
    d = svc.DEFAULT_MATRIX
    assert svc.rate(d, 1, 1)["level"] == "LOW"
    assert svc.rate(d, 3, 3)["level"] == "MODERATE"
    assert svc.rate(d, 4, 4)["level"] == "HIGH"
    assert svc.rate(d, 5, 5) == {"score": 25, "level": "CRITICAL", "color": svc.LEVEL_COLORS["CRITICAL"]}


def test_apply_rating_sets_before_and_after():
    row = SimpleNamespace(severity=3, likelihood=3, residualSeverity=3, residualLikelihood=1,
                          riskScore=None, riskLevel=None, riskColor=None, residualScore=None, residualLevel=None, residualColor=None)
    svc.apply_rating(row, _matrix_3x3())
    assert (row.riskLevel, row.riskScore) == ("CRITICAL", 99)
    assert row.residualLevel == "MODERATE" and row.residualScore == 3
    row.residualLikelihood = None
    svc.apply_rating(row, _matrix_3x3())
    assert row.residualLevel is None and row.residualColor is None


# ── workflow ─────────────────────────────────────────────────────────────────
def test_study_moves():
    assert svc.can_move_study("DRAFT", "IN_PROGRESS")
    assert svc.can_move_study("IN_PROGRESS", "UNDER_REVIEW")
    assert svc.can_move_study("UNDER_REVIEW", "APPROVED")
    assert svc.can_move_study("UNDER_REVIEW", "IN_PROGRESS")  # returned for rework
    assert svc.can_move_study("APPROVED", "CLOSED")
    assert not svc.can_move_study("DRAFT", "APPROVED")
    assert not svc.can_move_study("IN_PROGRESS", "APPROVED")  # cannot skip review
    assert not svc.can_move_study("CLOSED", "IN_PROGRESS")
    assert svc.is_editable("IN_PROGRESS") and not svc.is_editable("UNDER_REVIEW")


def _gate(**kw):
    base = dict(status="IN_PROGRESS", facilitator_id="f", approver_id="a", team_count=3, session_count=1,
                node_row_counts={"Node 1": 4}, incomplete_rows=[])
    base.update(kw)
    return svc.submit_blockers(**base)


def test_submit_gate():
    assert _gate() == []
    assert any("in progress" in b for b in _gate(status="DRAFT"))
    assert any("approver" in b for b in _gate(approver_id=None))
    assert any("team" in b for b in _gate(team_count=1))
    assert any("session" in b for b in _gate(session_count=0))
    assert any("at least one node" in b for b in _gate(node_row_counts={}))
    assert any("Node 2" in b for b in _gate(node_row_counts={"Node 1": 3, "Node 2": 0}))
    msg = " ".join(_gate(incomplete_rows=[f"Node 1.{i}" for i in range(1, 12)]))
    assert "Node 1.1" in msg and "3 more" in msg


def test_approve_gate_blocks_self_approval():
    assert svc.approve_blockers(status="UNDER_REVIEW", actor_id="a", facilitator_id="f") == []
    assert svc.approve_blockers(status="UNDER_REVIEW", actor_id="f", facilitator_id="f")
    assert svc.approve_blockers(status="IN_PROGRESS", actor_id="a", facilitator_id="f")


def test_close_gate_needs_all_recs_closed_or_justified():
    ok = [(1, "CLOSED", None), (2, "REJECTED", "Duplicate of R-001; covered by SIL-2 trip")]
    assert svc.close_blockers(status="APPROVED", recs=ok) == []
    assert svc.close_blockers(status="APPROVED", recs=[]) == []
    b = svc.close_blockers(status="APPROVED", recs=ok + [(3, "OPEN", None), (4, "REJECTED", "  ")])
    assert any("R-003" in x for x in b) and any("R-004" in x for x in b)
    assert svc.close_blockers(status="UNDER_REVIEW", recs=ok)


def test_rec_moves_and_blockers():
    assert svc.can_move_rec("OPEN", "REJECTED") and svc.can_move_rec("CLOSED", "OPEN")
    assert not svc.can_move_rec("CLOSED", "REJECTED")
    assert svc.rec_move_blockers(current="OPEN", target="REJECTED", justification="too short", closure_note=None, capa_state=None)
    assert svc.rec_move_blockers(current="OPEN", target="REJECTED", justification="Existing PSV sized for fire case", closure_note=None, capa_state=None) == []
    assert svc.rec_move_blockers(current="OPEN", target="CLOSED", justification=None, closure_note=None, capa_state=None)
    # a live CAPA owns closure
    assert any("CAPA" in b for b in svc.rec_move_blockers(current="OPEN", target="CLOSED", justification=None, closure_note="done", capa_state="ACTIONS_PLANNED"))
    assert svc.rec_move_blockers(current="OPEN", target="CLOSED", justification=None, closure_note="done", capa_state="VERIFIED") == []
    assert svc.rec_move_blockers(current="OPEN", target="BOGUS", justification=None, closure_note=None, capa_state=None)


def test_rec_status_follows_capa():
    assert svc.rec_status_from_capa("OPEN", "CLOSED") == "CLOSED"
    assert svc.rec_status_from_capa("IN_PROGRESS", "VERIFIED") == "CLOSED"
    assert svc.rec_status_from_capa("OPEN", "ACTIONS_IN_PROGRESS") == "IN_PROGRESS"
    assert svc.rec_status_from_capa("IN_PROGRESS", "ACTIONS_IN_PROGRESS") is None
    assert svc.rec_status_from_capa("OPEN", "CANCELLED") is None
    assert svc.rec_status_from_capa("REJECTED", "CLOSED") is None
    assert svc.capa_severity("HIGH") == "HIGH" and svc.capa_severity(None) == "MODERATE"
    assert svc.capa_priority("MEDIUM") == "MODERATE"


def test_revalidation_dates():
    assert svc.default_revalidation(NOW) == NOW.replace(year=2031)
    leap = datetime(2028, 2, 29, tzinfo=timezone.utc)
    assert svc.default_revalidation(leap) == datetime(2033, 2, 28, tzinfo=timezone.utc)
    assert svc.revalidation_state(NOW - timedelta(days=1), "APPROVED", NOW) == "OVERDUE"
    assert svc.revalidation_state(NOW + timedelta(days=30), "CLOSED", NOW) == "DUE_SOON"
    assert svc.revalidation_state(NOW + timedelta(days=400), "APPROVED", NOW) == "CURRENT"
    assert svc.revalidation_state(NOW - timedelta(days=1), "IN_PROGRESS", NOW) is None
    # naive DB value treated as UTC
    assert svc.revalidation_state((NOW - timedelta(days=1)).replace(tzinfo=None), "APPROVED", NOW) == "OVERDUE"


def test_rec_overdue():
    assert svc.rec_overdue("OPEN", NOW - timedelta(hours=1), NOW)
    assert not svc.rec_overdue("CLOSED", NOW - timedelta(hours=1), NOW)
    assert not svc.rec_overdue("OPEN", None, NOW)


# ── XLSX export ──────────────────────────────────────────────────────────────
def _report():
    rec = {"id": "r1", "deviationId": "d1", "recNo": 1, "recLabel": "R-001", "text": "Add low-low gas pressure trip",
           "ownerName": "A. Kumar", "dueDate": "2027-01-15T00:00:00+00:00", "priority": "HIGH", "status": "OPEN",
           "nodeLabel": "Node 1 - Fuel gas skid", "deviationText": "Low pressure", "capaNumber": "CAPA-S-2026-HSR-001",
           "capaState": "ACTIONS_PLANNED", "justification": None, "closureNote": None}
    dev = {"id": "d1", "guideword": "LESS", "parameter": "PRESSURE", "deviation": "Low pressure",
           "causes": "PCV fails closed", "consequences": "Flame-out, unburnt gas accumulation, explosion on re-light",
           "safeguards": "Flame scanner, BMS purge", "severity": 5, "likelihood": 3, "riskScore": 15, "riskLevel": "HIGH",
           "riskColor": "#f97316", "residualSeverity": 5, "residualLikelihood": 1, "residualScore": 5,
           "residualLevel": "MODERATE", "residualColor": "#eab308", "remarks": None, "recommendations": [rec]}
    return {
        "study": {"studyNo": "PHA-2026-0001", "title": "Reheating furnace #2 fuel gas", "method": "HAZOP", "methodLabel": "HAZOP",
                  "plantName": "JSL Hisar", "unit": "HSM", "drawingRefs": ["P&ID-HSM-RF2-001"], "status": "IN_PROGRESS"},
        "team": [{"role": "FACILITATOR", "roleLabel": "Facilitator", "name": "R. Sharma", "discipline": "SAFETY"}],
        "sessions": [{"sessionNo": 1, "sessionDate": "2026-11-20T09:00:00+00:00", "durationMinutes": 180,
                      "attendees": [{"name": "R. Sharma"}], "attendeeNames": ["Vendor rep"], "nodeLabels": ["Node 1"], "notes": None}],
        "matrix": {"name": "Default"},
        "nodes": [
            {"nodeNo": 1, "title": "Fuel gas skid", "designIntent": "Supply NG at 0.3 barg", "drawingRef": "P&ID-001",
             "boundaries": "From battery limit to burner header", "operatingParameters": [{"name": "Pressure", "value": "0.3", "unit": "barg"}],
             "deviations": [dev]},
            {"nodeNo": 2, "title": "Combustion air", "deviations": []},
        ],
        "recommendations": [rec],
    }


def test_xlsx_worksheet_shape():
    wb = load_workbook(io.BytesIO(worksheet_xlsx(_report())))
    assert wb.sheetnames == ["Study", "Worksheet", "Recommendations"]
    ws = wb["Worksheet"]
    assert [ws.cell(row=4, column=i + 1).value for i in range(len(WORKSHEET_COLUMNS))] == [c for c, _ in WORKSHEET_COLUMNS]
    assert ws.freeze_panes == "A5"
    assert ws.cell(row=5, column=1).value.startswith("Node 1: Fuel gas skid")
    assert any(str(r) == f"A5:{get_column_letter(len(WORKSHEET_COLUMNS))}5" for r in ws.merged_cells.ranges)
    row = [ws.cell(row=6, column=i).value for i in range(1, len(WORKSHEET_COLUMNS) + 1)]
    assert row[0] == "1.1" and row[1] == "Less" and row[2] == "Pressure" and row[3] == "Low pressure"
    assert row[7] == 5 and row[8] == 3 and row[9] == "HIGH (15)"
    assert row[10] == "Add low-low gas pressure trip" and row[11] == "R-001"
    assert row[16] == "MODERATE (5)"
    assert ws.cell(row=6, column=10).fill.fgColor.rgb.endswith("F97316")
    assert ws.cell(row=7, column=1).value.startswith("Node 2")
    assert ws.cell(row=8, column=1).value == "(no worksheet rows)"
    reg = wb["Recommendations"]
    assert [reg.cell(row=1, column=i + 1).value for i in range(len(REC_COLUMNS))] == [c for c, _ in REC_COLUMNS]
    assert reg.cell(row=2, column=1).value == "R-001" and "CAPA-S-2026-HSR-001" in reg.cell(row=2, column=9).value
    assert wb["Study"]["B3"].value == "PHA-2026-0001"


# ── AI suggestion parsing ────────────────────────────────────────────────────
MOCK_AI = {
    "suggestions": [
        {"guideword": "No / None", "parameter": "Flow", "deviation": "No cooling water flow to furnace skid panels",
         "causes": ["Pump trip", "Strainer choked"], "consequences": "Panel overheat, water leak onto hot charge, steam explosion",
         "safeguards": "Low-flow alarm", "severity": 9, "likelihood": "3", "recommendation": "Add low-flow trip to stop firing"},
        {"guideword": "Sideways", "parameter": "Flow", "deviation": "nonsense"},          # bad guideword
        {"guideword": "More", "parameter": "Vibes", "deviation": "nonsense"},             # bad parameter
        {"guideword": "Less", "parameter": "Pressure", "deviation": "Low pressure"},      # already on node (pair)
        {"guideword": "More", "parameter": "Temperature", "deviation": "  "},             # no text
        {"guideword": "no", "parameter": "flow", "deviation": "duplicate pair"},          # duplicate within response
        {"guideword": "Other than", "parameter": "Composition", "deviation": "Oxygen enrichment near lancing station",
         "severity": 0, "likelihood": "often"},
        "garbage",
    ]
}


def test_parse_suggestions_clamps_and_filters():
    out = pha_ai.parse_suggestions(MOCK_AI, severity_levels=5, likelihood_levels=5,
                                   existing=[("LESS", "PRESSURE", "Low pressure")])
    assert [(s["guideword"], s["parameter"]) for s in out] == [("NO", "FLOW"), ("OTHER_THAN", "COMPOSITION")]
    first = out[0]
    assert first["severity"] == 5 and first["likelihood"] == 3  # 9 clamped, "3" parsed
    assert first["causes"] == "Pump trip; Strainer choked"
    assert first["recommendation"].startswith("Add low-flow trip")
    assert out[1]["severity"] == 1 and out[1]["likelihood"] is None  # 0 clamped up, junk dropped


def test_parse_suggestions_bad_shapes_and_cap():
    assert pha_ai.parse_suggestions(None, severity_levels=5, likelihood_levels=5) == []
    assert pha_ai.parse_suggestions({"suggestions": "x"}, severity_levels=5, likelihood_levels=5) == []
    many = [{"guideword": gw, "parameter": p, "deviation": f"{gw} {p}"} for p, gws in lib.COMBINATIONS.items() for gw in gws]
    assert len(pha_ai.parse_suggestions(many, severity_levels=5, likelihood_levels=5)) == pha_ai.MAX_SUGGESTIONS
    long = pha_ai.parse_suggestions([{"guideword": "More", "parameter": "Flow", "deviation": "x" * 5000, "causes": "y" * 5000}],
                                    severity_levels=3, likelihood_levels=3)
    assert len(long[0]["deviation"]) == 300 and len(long[0]["causes"]) == 1200


async def test_suggest_for_node_soft_fails_and_parses(monkeypatch):
    from app.services.ai import anthropic_client as ac

    study = {"title": "RF2", "method": "HAZOP"}
    node = {"id": "n1", "nodeNo": 1, "title": "Cooling water", "designIntent": "Cool skid pipes",
            "operatingParameters": [{"name": "Flow", "value": "120", "unit": "m3/h"}]}
    matrix = {"severityLevels": 5, "likelihoodLevels": 5, "severities": [], "likelihoods": []}

    monkeypatch.setattr(ac, "is_configured", lambda: False)
    assert await pha_ai.suggest_for_node(study=study, node=node, matrix=matrix, existing=[]) is None

    calls = {}

    async def fake_complete_json(*, system, user, max_tokens=1024, model=None):
        calls["user"] = user
        return MOCK_AI

    monkeypatch.setattr(ac, "is_configured", lambda: True)
    monkeypatch.setattr(ac, "complete_json", fake_complete_json)
    out = await pha_ai.suggest_for_node(study=study, node=node, matrix=matrix, existing=[])
    assert out and out[0]["guideword"] == "NO"
    assert "Flow: 120 m3/h" in calls["user"] and "Cooling water" in calls["user"]

    async def failing(**kw):
        return None

    monkeypatch.setattr(ac, "complete_json", failing)
    assert await pha_ai.suggest_for_node(study=study, node=node, matrix=matrix, existing=[]) is None


def test_vocabularies_have_no_duplicates():
    for vocab in (svc.METHODS, svc.STUDY_STATUSES, svc.REC_STATUSES, svc.PRIORITIES, svc.TEAM_ROLES, svc.DISCIPLINES):
        assert len(vocab) == len(set(vocab))
