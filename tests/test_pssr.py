"""PSSR rules: offline unit tests (no DB). Go / no-go gate, punch rules,
sign-off matrix, template seed consistency."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services import pssr as svc
from app.services.pssr import ItemState, SignState

NOW = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)
DUE = NOW + timedelta(days=7)
REQ = svc.normalise_disciplines(["PROCESS", "MECHANICAL"])


def yes(i: str) -> ItemState:
    return ItemState(id=i, question=f"Q{i}", response="YES")


def punch(i: str, cat: str = "A", st: str = "OPEN", owner: str | None = "u1", due: datetime | None = DUE) -> ItemState:
    return ItemState(id=i, question=f"Q{i}", response="NO", punchCategory=cat, punchStatus=st, punchOwnerId=owner, punchDueDate=due)


def approve_all(disciplines=REQ) -> list[SignState]:
    return [SignState(discipline=d, decision="APPROVE", userId=f"u-{d}", signedAt=NOW) for d in disciplines]


# ── disciplines ──────────────────────────────────────────────────────────────
def test_mandatory_disciplines_always_on_matrix_in_order():
    assert svc.normalise_disciplines([]) == ["SAFETY", "OPERATIONS", "PLANT_HEAD"]
    assert svc.normalise_disciplines(["electrical", "PROCESS"]) == ["PROCESS", "ELECTRICAL", "SAFETY", "OPERATIONS", "PLANT_HEAD"]


def test_unknown_discipline_rejected():
    with pytest.raises(ValueError):
        svc.normalise_disciplines(["CIVIL"])


# ── punch rules ──────────────────────────────────────────────────────────────
def test_no_opens_punch_with_template_default_category():
    f = svc.punch_fields_for_response("NO", "B")
    assert f == {"punchCategory": "B", "punchStatus": "OPEN"}
    assert svc.punch_fields_for_response("NO", "bogus")["punchCategory"] == "A"  # unknown default is the safe one


def test_yes_or_na_clears_punch():
    for r in ("YES", "NA", None):
        f = svc.punch_fields_for_response(r, "A")
        assert f["punchCategory"] is None and f["punchStatus"] is None and f["punchOwnerId"] is None


def test_no_without_owner_or_due_is_not_a_complete_punch_item():
    assert not svc.punch_complete(punch("1", owner=None))
    assert not svc.punch_complete(punch("1", due=None))
    assert svc.punch_complete(punch("1"))
    blockers = svc.checklist_blockers([yes("a"), punch("b", owner=None)])
    assert any("not complete punch items" in b for b in blockers)


def test_unanswered_items_block_submit():
    items = [yes("a"), ItemState(id="b", question="Qb")]
    assert any("unanswered" in b for b in svc.submit_blockers("IN_PROGRESS", items))
    assert svc.submit_blockers("IN_PROGRESS", [yes("a"), punch("b")]) == []


def test_submit_only_from_editable_status():
    assert svc.submit_blockers("PENDING_APPROVAL", [yes("a")])
    assert svc.submit_blockers("DRAFT", [yes("a")]) == []


def test_empty_checklist_blocks():
    assert svc.checklist_blockers([]) == ["The checklist has no items."]


def test_punch_category_moves_only_while_editable():
    assert svc.can_move_punch_category("IN_PROGRESS")
    assert not svc.can_move_punch_category("PENDING_APPROVAL")
    assert not svc.can_move_punch_category("APPROVED_GO")


def test_capa_only_for_open_b_punch_without_existing_capa():
    assert svc.capa_blockers(punch("1", cat="B"), None) == []
    assert svc.capa_blockers(punch("1", cat="A"), None)
    assert svc.capa_blockers(yes("1"), None)
    assert svc.capa_blockers(punch("1", cat="B", st="CLOSED"), None)
    assert svc.capa_blockers(punch("1", cat="B"), "capa-1")


def test_punch_overdue():
    assert svc.punch_overdue(punch("1", due=NOW - timedelta(days=1)), NOW)
    assert not svc.punch_overdue(punch("1", st="CLOSED", due=NOW - timedelta(days=1)), NOW)
    assert svc.punch_overdue(punch("1", due=(NOW - timedelta(days=1)).replace(tzinfo=None)), NOW)  # naive = UTC


def test_summary_counts():
    s = svc.checklist_summary([yes("a"), punch("b"), punch("c", cat="B"), punch("d", cat="B", st="CLOSED"),
                               ItemState(id="e", response="NA"), ItemState(id="f")])
    assert (s["total"], s["answered"], s["yes"], s["no"], s["na"], s["unanswered"]) == (6, 5, 1, 3, 1, 1)
    assert (s["punchAOpen"], s["punchBOpen"], s["punchBClosed"]) == (1, 1, 1)


# ── go / no-go gate ──────────────────────────────────────────────────────────
def test_go_when_everything_clear():
    items = [yes("a"), punch("b", cat="A", st="CLOSED"), punch("c", cat="B")]
    g = svc.gate(items, approve_all(), REQ)
    assert g["decision"] == "GO" and g["blockers"] == [] and g["openB"] == 1


def test_open_category_a_holds_go_even_with_all_signatures():
    g = svc.gate([yes("a"), punch("b", cat="A")], approve_all(), REQ)
    assert g["decision"] == "PENDING"
    assert any("category-A" in b for b in g["blockers"])


def test_open_category_b_does_not_hold_go():
    g = svc.gate([yes("a"), punch("b", cat="B")], approve_all(), REQ)
    assert g["decision"] == "GO"


def test_missing_signature_holds_go():
    g = svc.gate([yes("a")], approve_all([d for d in REQ if d != "PLANT_HEAD"]), REQ)
    assert g["decision"] == "PENDING" and g["missingSignoffs"] == ["PLANT_HEAD"]
    assert any("Plant Head" in b for b in g["blockers"])


def test_any_reject_is_no_go_with_reason():
    sigs = approve_all() + [SignState(discipline="ELECTRICAL", decision="REJECT", userId="ue", comment="Ex certificate missing", signedAt=NOW)]
    req = svc.normalise_disciplines(["PROCESS", "MECHANICAL", "ELECTRICAL"])
    g = svc.gate([yes("a")], sigs, req)
    assert g["decision"] == "NO_GO"
    assert g["noGoReason"] == "Electrical: Ex certificate missing"


def test_reject_wins_even_when_checklist_incomplete():
    sigs = [SignState(discipline="SAFETY", decision="REJECT", userId="us", comment="Hydrant not tested", signedAt=NOW)]
    g = svc.gate([ItemState(id="x")], sigs, REQ)
    assert g["decision"] == "NO_GO"


def test_later_signoff_for_same_discipline_wins():
    sigs = approve_all() + [SignState(discipline="SAFETY", decision="REJECT", userId="us", comment="x", signedAt=NOW - timedelta(hours=1))]
    # the APPROVE at NOW is later than the REJECT
    g = svc.gate([yes("a")], sigs, REQ)
    assert g["decision"] == "GO"


def test_incomplete_punch_holds_go():
    g = svc.gate([punch("b", cat="B", owner=None)], approve_all(), REQ)
    assert g["decision"] == "PENDING"


def test_status_after_evaluation_only_moves_pending_approval():
    go = {"decision": "GO"}
    nogo = {"decision": "NO_GO"}
    pending = {"decision": "PENDING"}
    assert svc.status_after_evaluation("PENDING_APPROVAL", go) == "APPROVED_GO"
    assert svc.status_after_evaluation("PENDING_APPROVAL", nogo) == "NO_GO"
    assert svc.status_after_evaluation("PENDING_APPROVAL", pending) == "PENDING_APPROVAL"
    assert svc.status_after_evaluation("IN_PROGRESS", go) == "IN_PROGRESS"


def test_close_needs_go_and_all_b_closed():
    assert svc.close_blockers("APPROVED_GO", [yes("a"), punch("b", cat="B", st="CLOSED")]) == []
    assert any("category-B" in b for b in svc.close_blockers("APPROVED_GO", [punch("b", cat="B")]))
    assert svc.close_blockers("PENDING_APPROVAL", [yes("a")])


def test_reopen_only_from_pending_or_no_go():
    assert svc.can_reopen("NO_GO") and svc.can_reopen("PENDING_APPROVAL")
    assert not svc.can_reopen("APPROVED_GO") and not svc.can_reopen("CLOSED")


# ── sign-off matrix ──────────────────────────────────────────────────────────
def test_signoff_matrix_lists_required_rows_in_order():
    rows = svc.signoff_matrix(REQ, [SignState(discipline="SAFETY", decision="APPROVE", userId="u", signedAt=NOW)])
    assert [r["discipline"] for r in rows] == REQ
    safety = next(r for r in rows if r["discipline"] == "SAFETY")
    assert safety["decision"] == "APPROVE" and safety["required"]
    assert next(r for r in rows if r["discipline"] == "PROCESS")["decision"] is None


def test_signoff_blockers():
    assert svc.signoff_blockers("PENDING_APPROVAL", "SAFETY", REQ, "APPROVE", None) == []
    assert svc.signoff_blockers("IN_PROGRESS", "SAFETY", REQ, "APPROVE", None)
    assert svc.signoff_blockers("PENDING_APPROVAL", "ELECTRICAL", REQ, "APPROVE", None)  # not on this matrix
    assert svc.signoff_blockers("PENDING_APPROVAL", "SAFETY", REQ, "REJECT", "  ")  # reject needs a reason
    assert svc.signoff_blockers("PENDING_APPROVAL", "SAFETY", REQ, "MAYBE", None)


# ── template ─────────────────────────────────────────────────────────────────
def test_default_template_covers_required_sections_and_vocab():
    rows = svc.default_template_rows()
    sections = {r["section"] for r in rows}
    for must in ("Design & construction per specification", "P&ID / as-built drawings updated", "Operating procedures",
                 "Training done", "PHA / HAZOP recommendations closed", "MOC complete", "Equipment integrity & tests",
                 "Relief / safety systems tested", "Interlocks & alarms", "Fire protection & emergency response ready",
                 "Electrical classification", "LOTO / PTW readiness", "Housekeeping & access", "Environmental"):
        assert must in sections
    assert all(r["discipline"] in svc.DISCIPLINES for r in rows)
    assert all(r["defaultPunchCategory"] in svc.PUNCH_CATEGORIES for r in rows)
    assert len({r["id"] for r in rows}) == len(rows)


DDL = Path(__file__).resolve().parents[2] / "safeops_360" / "prisma" / "apply-pssr-psi-ddl.ts"


@pytest.mark.skipif(not DDL.exists(), reason="frontend repo not alongside")
def test_ddl_seed_matches_python_default_template():
    text = DDL.read_text(encoding="utf-8")
    seeded_ids = set(re.findall(r"'(pssr-tpl-steel-default-\d{2}-\d{2})'", text))
    assert seeded_ids == {r["id"] for r in svc.default_template_rows()}
    for r in svc.default_template_rows():
        assert r["question"].replace("'", "''") in text, r["question"]


def test_vocabularies_have_no_duplicates():
    for v in (svc.TRIGGERS, svc.STATUSES, svc.RESPONSES, svc.DISCIPLINES, svc.DECISIONS):
        assert len(v) == len(set(v))
