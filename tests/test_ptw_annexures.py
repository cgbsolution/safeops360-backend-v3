"""PTW multi-hazard annexures — offline unit tests.

Both things under test are pure functions over stand-in objects, so this needs
no DB — the house test style.

  1. `app.services.ptw_hazards`   — the taxonomy: which chain runs, how long
     the permit may be valid, which controls are mandatory, who must be
     certified.
  2. `ptw_annexures.evaluate_annexure` — the completeness rule that decides
     whether a checklist may be certified.

The property that matters most here is the FIRST one asserted: a single-hazard
permit must behave exactly as it did before annexures existed. Multi-hazard is
additive or it is a regression.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.models.permit import PermitType, PrecautionResponse
from app.services.ptw_annexures import evaluate_annexure
from app.services.ptw_hazards import (
    PermitHazardType as HZ,
    binding_cap_hazard,
    competency_types_for,
    effective_hazards,
    hazard_for_base_type,
    required_controls,
    validity_cap_hours,
    workflow_type_for,
)

# ── Taxonomy ───────────────────────────────────────────────────────────────

# (base type, expected chain, expected cap) with NO annexures attached —
# the pre-annexure behaviour, which must survive untouched.
_LEGACY = [
    (PermitType.HOT_WORK, PermitType.HOT_WORK, 24),
    (PermitType.CONFINED_SPACE, PermitType.CONFINED_SPACE, 24),
    (PermitType.WORK_AT_HEIGHT, PermitType.WORK_AT_HEIGHT, 72),
    (PermitType.EXCAVATION, PermitType.EXCAVATION, 72),
    (PermitType.ELECTRICAL_LOTO, PermitType.ELECTRICAL_LOTO, 72),
    (PermitType.LIFTING, PermitType.LIFTING, 72),
    (PermitType.GENERAL_COLD, PermitType.GENERAL_COLD, 72),
]


def test_single_hazard_permit_is_unchanged():
    """No annexures → same chain and same cap as before this build."""
    for base, expected_chain, expected_cap in _LEGACY:
        assert workflow_type_for(base, []) is expected_chain, base
        assert validity_cap_hours(base, []) == expected_cap, base
        # The base type always contributes exactly one hazard, never zero.
        assert effective_hazards(base, []) == [hazard_for_base_type(base)]


def test_base_hazard_is_always_present():
    """Even if the client sends unrelated hazards, the base type's own
    checklist is in force."""
    hazards = effective_hazards(PermitType.HOT_WORK, [HZ.WORK_AT_HEIGHT])
    assert HZ.HOT_WORK in hazards
    assert HZ.WORK_AT_HEIGHT in hazards


def test_union_chain_escalates_cold_work():
    """A cold-work permit carrying a hot-work annexure must route through the
    hot-work chain — Issuer → Safety Officer → Plant Head — not the 4-step
    cold chain that has no EHS gate at all."""
    assert workflow_type_for(PermitType.GENERAL_COLD, [HZ.HOT_WORK]) is PermitType.HOT_WORK
    assert workflow_type_for(PermitType.GENERAL_COLD, [HZ.CONFINED_SPACE]) is PermitType.CONFINED_SPACE


def test_union_chain_never_de_escalates():
    """Attaching a LOWER-risk annexure must not drop a high-risk permit onto a
    shorter chain."""
    assert workflow_type_for(PermitType.CONFINED_SPACE, [HZ.GENERAL]) is PermitType.CONFINED_SPACE
    assert workflow_type_for(PermitType.HOT_WORK, [HZ.CIVIL, HZ.GENERAL]) is PermitType.HOT_WORK


def test_new_hazards_borrow_an_existing_chain():
    """FRAGILE_ROOF and CIVIL have no seeded workflow of their own."""
    assert workflow_type_for(PermitType.GENERAL_COLD, [HZ.FRAGILE_ROOF]) is PermitType.WORK_AT_HEIGHT
    assert workflow_type_for(PermitType.GENERAL_COLD, [HZ.CIVIL]) is PermitType.GENERAL_COLD


def test_validity_cap_takes_the_tightest():
    """A 72h height job that also involves hot work is a 24h permit — there is
    no way to expire half a permit."""
    assert validity_cap_hours(PermitType.WORK_AT_HEIGHT, [HZ.HOT_WORK]) == 24
    assert binding_cap_hazard(PermitType.WORK_AT_HEIGHT, [HZ.HOT_WORK]) is HZ.HOT_WORK
    # …and the reverse direction is the same permit.
    assert validity_cap_hours(PermitType.HOT_WORK, [HZ.WORK_AT_HEIGHT]) == 24


def test_controls_are_a_union_not_a_max():
    """Height contributes a rescue plan, hot work a fire watch and gas test.
    Taking only the highest-risk hazard's controls would silently drop the
    rescue plan."""
    controls = required_controls(PermitType.WORK_AT_HEIGHT, [HZ.HOT_WORK])
    assert controls == {"RESCUE_PLAN", "FIRE_WATCH", "GAS_TEST"}

    # Confined space alone already carries three; adding height changes nothing.
    assert required_controls(PermitType.CONFINED_SPACE, []) == {
        "GAS_TEST",
        "STANDBY",
        "RESCUE_PLAN",
    }


def test_competency_covers_every_hazard():
    """The holder of a height + hot-work permit must be certified for BOTH —
    checking only the base type is how an uncertified person ends up holding
    the hot-work half of the job."""
    codes = competency_types_for(PermitType.WORK_AT_HEIGHT, [HZ.HOT_WORK])
    assert set(codes) == {"WORK_AT_HEIGHT", "HOT_WORK"}

    # Fragile roof is checked as height competency, not as a type of its own.
    codes = competency_types_for(PermitType.GENERAL_COLD, [HZ.FRAGILE_ROOF])
    assert set(codes) == {"WORK_AT_HEIGHT", "GENERAL_COLD"}


def test_unknown_hazard_string_is_ignored_not_fatal():
    """A legacy/bad row must stay readable rather than 500 the permit."""
    assert effective_hazards(PermitType.HOT_WORK, ["NOT_A_HAZARD"]) == [HZ.HOT_WORK]


# ── Completeness ───────────────────────────────────────────────────────────


def _item(item_id: str, *, mandatory: bool = True, allows_na: bool = True):
    return SimpleNamespace(
        id=item_id, text=f"item {item_id}", isMandatory=mandatory, allowsNA=allows_na
    )


def _answer(item_id: str, response: PrecautionResponse, remark: str | None = None):
    return SimpleNamespace(itemId=item_id, response=response, remark=remark)


def _annexure(responses, hazard=HZ.HOT_WORK, primary=True):
    return SimpleNamespace(hazardType=hazard, isPrimary=primary, responses=responses)


def test_all_yes_is_complete():
    catalog = [_item("a"), _item("b")]
    ann = _annexure([_answer("a", PrecautionResponse.YES), _answer("b", PrecautionResponse.YES)])
    v = evaluate_annexure(ann, catalog)
    assert v.complete
    assert v.answered == 2
    assert v.mandatoryItems == 2


def test_unanswered_mandatory_item_blocks():
    catalog = [_item("a"), _item("b")]
    v = evaluate_annexure(_annexure([_answer("a", PrecautionResponse.YES)]), catalog)
    assert not v.complete
    assert v.unanswered == ["item b"]


def test_unanswered_optional_item_does_not_block():
    catalog = [_item("a"), _item("b", mandatory=False)]
    v = evaluate_annexure(_annexure([_answer("a", PrecautionResponse.YES)]), catalog)
    assert v.complete
    assert v.unanswered == []


def test_no_on_a_mandatory_precaution_blocks():
    """NO means the control is not in place. That is the whole point of the
    checklist — it must not be a way to tick past it."""
    catalog = [_item("a")]
    v = evaluate_annexure(_annexure([_answer("a", PrecautionResponse.NO)]), catalog)
    assert not v.complete
    assert v.refused == ["item a"]


def test_na_requires_a_remark():
    catalog = [_item("a", allows_na=True)]
    v = evaluate_annexure(_annexure([_answer("a", PrecautionResponse.NA)]), catalog)
    assert not v.complete
    assert v.naWithoutRemark == ["item a"]

    v = evaluate_annexure(
        _annexure([_answer("a", PrecautionResponse.NA, "no electrical work in scope")]),
        catalog,
    )
    assert v.complete


def test_na_refused_where_the_item_forbids_it():
    """Some lines always apply — 'first aid box available' cannot be NA."""
    catalog = [_item("a", allows_na=False)]
    v = evaluate_annexure(
        _annexure([_answer("a", PrecautionResponse.NA, "a reason")]), catalog
    )
    assert not v.complete
    assert v.refused == ["item a"]


def test_empty_catalog_is_trivially_complete():
    """LIFTING and CIVIL have no seeded content. An annexure with nothing to
    answer must not deadlock the permit — it adds routing and the cap only."""
    v = evaluate_annexure(_annexure([], hazard=HZ.LIFTING), [])
    assert v.complete
    assert v.totalItems == 0


def test_retired_item_still_counts_as_answered():
    """An item retired after the permit was raised drops out of the catalog.
    The recorded answer must not vanish from the count with it."""
    catalog = [_item("a")]
    ann = _annexure(
        [_answer("a", PrecautionResponse.YES), _answer("retired", PrecautionResponse.YES)]
    )
    v = evaluate_annexure(ann, catalog)
    assert v.complete
    # The retired answer is carried on the annexure but is not re-asked.
    assert v.totalItems == 1
