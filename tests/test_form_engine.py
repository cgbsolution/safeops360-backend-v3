"""Form & Workflow Engine — Part A.1 acceptance.

The criterion this file exists to prove: *a form definition authored purely as
JSON/DB config renders, accepts a record, validates it (including conditional
required + calculated fields), and stores it — with zero form-specific code.*

So the tests deliberately use a form the engine has never heard of. Nothing
below imports a Sustainability or Kaizen module, because no such module exists
yet — that is the point. If any test here needed engine changes to pass for a
new form, the primitive would have failed.

Everything here is pure logic (no DB, no event loop) except where noted, so it
runs in CI without a database.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.form_engine.formula import FormulaError, referenced_fields
from app.services.form_engine.formula import evaluate as calc
from app.services.form_engine.numbering import PatternError, validate_pattern
from app.services.form_engine.schema import SchemaError, calculation_order, validate_definition
from app.services.form_engine.validation import (
    ValidationError,
    evaluate,
    required_but_missing,
    visible_fields,
)

# ── A form the engine has never seen. Config only. ──────────────────────────
# Modelled on the shape §7 asks of an environmental capture form: a repeatable
# source table, a conditional field, and two intensity calculations — one of
# which depends on the other.
ENERGY_FORM = [
    {"key": "reporting_period", "type": "text", "label": "Reporting period", "required": True},
    {
        "key": "sources",
        "type": "table",
        "label": "Energy sources",
        "columns": [
            {
                "key": "source_type",
                "type": "select",
                "label": "Source",
                "required": True,
                "options": [
                    {"value": "GRID", "label": "Grid electricity"},
                    {"value": "DIESEL", "label": "Diesel"},
                    {"value": "SOLAR", "label": "Solar"},
                ],
            },
            {"key": "quantity_gj", "type": "decimal", "label": "Quantity (GJ)", "required": True},
        ],
    },
    {"key": "production_output", "type": "decimal", "label": "Production output", "required": True},
    {
        "key": "has_renewable",
        "type": "boolean",
        "label": "Any renewable source this period?",
    },
    {
        "key": "renewable_certificate_no",
        "type": "text",
        "label": "Renewable certificate no.",
        "required": True,
        "visible_if": {"field": "has_renewable", "operator": "=", "value": True},
    },
    {
        "key": "total_energy_gj",
        "type": "calculated",
        "label": "Total energy (GJ)",
        "formula": "SUM(sources.quantity_gj)",
    },
    {
        "key": "energy_intensity",
        "type": "calculated",
        "label": "Energy intensity (GJ/unit)",
        # Depends on another calculated field — proves dependency ordering.
        "formula": "ROUND(total_energy_gj / production_output, 3)",
    },
]

VALID_DATA = {
    "reporting_period": "2026-Q2",
    "sources": [
        {"source_type": "GRID", "quantity_gj": 1200},
        {"source_type": "DIESEL", "quantity_gj": 300.5},
    ],
    "production_output": 500,
    "has_renewable": False,
}


# ═══════════════════════════════════════════════════════════════════════════
#  The publish gate
# ═══════════════════════════════════════════════════════════════════════════


def test_a_config_authored_form_publishes_with_no_engine_changes():
    """The headline acceptance: a form the engine has never seen passes the
    gate on nothing but its own JSON."""
    validate_definition(ENERGY_FORM, number_pattern="SUS-ENERGY-{YYYY}-{####}")


def test_the_gate_reports_every_error_at_once_not_just_the_first():
    """The Builder shows these next to the offending fields; fixing one at a
    time through six publish attempts is a bad authoring loop."""
    with pytest.raises(SchemaError) as e:
        validate_definition(
            [
                {"key": "a", "type": "text"},  # no label
                {"key": "a", "type": "number", "label": "Dup"},  # duplicate key
                {"key": "c", "type": "select", "label": "No options", "options": []},
            ]
        )
    msg = str(e.value)
    assert "label is required" in msg
    assert "duplicate field key" in msg
    assert "non-empty `options`" in msg


def test_a_formula_pointing_at_a_field_that_does_not_exist_is_refused():
    """The single most common authoring mistake. Caught at publish, because
    otherwise it surfaces as a silently-null column in a filed report."""
    schema = [
        {"key": "a", "type": "number", "label": "A"},
        {"key": "bad", "type": "calculated", "label": "Bad", "formula": "a / typo_field"},
    ]
    with pytest.raises(SchemaError, match="unknown field 'typo_field'"):
        validate_definition(schema)


def test_a_cycle_between_calculated_fields_is_refused():
    schema = [
        {"key": "x", "type": "calculated", "label": "X", "formula": "y + 1"},
        {"key": "y", "type": "calculated", "label": "Y", "formula": "x + 1"},
    ]
    with pytest.raises(SchemaError, match="cycle"):
        validate_definition(schema)


def test_a_calculated_field_cannot_be_required():
    """`required` on a server-derived field would be unsatisfiable by the user."""
    with pytest.raises(SchemaError, match="cannot be `required`"):
        validate_definition(
            [{"key": "c", "type": "calculated", "label": "C", "formula": "1", "required": True}]
        )


def test_visible_if_pointing_at_an_unknown_field_is_refused():
    with pytest.raises(SchemaError, match="unknown field 'nope'"):
        validate_definition(
            [
                {
                    "key": "a",
                    "type": "text",
                    "label": "A",
                    "visible_if": {"field": "nope", "operator": "=", "value": 1},
                }
            ]
        )


def test_a_table_column_may_not_itself_be_a_table_or_a_calculation():
    """Deliberately out of scope (§11: let real module requirements drive
    capability). Refused loudly rather than silently ignored."""
    with pytest.raises(SchemaError, match="not allowed inside a table"):
        validate_definition(
            [
                {
                    "key": "t",
                    "type": "table",
                    "label": "T",
                    "columns": [{"key": "n", "type": "table", "label": "Nested"}],
                }
            ]
        )


def test_calculations_are_ordered_by_dependency_not_by_schema_order():
    """`energy_intensity` reads `total_energy_gj`, and is declared after it —
    but the order must come from the dependency graph, not luck."""
    order = [f["key"] for f in calculation_order(ENERGY_FORM)]
    assert order.index("total_energy_gj") < order.index("energy_intensity")

    # Same schema, calculations declared in the WRONG order. Still correct.
    reversed_schema = [
        {"key": "a", "type": "number", "label": "A"},
        {"key": "second", "type": "calculated", "label": "2nd", "formula": "first * 2"},
        {"key": "first", "type": "calculated", "label": "1st", "formula": "a + 1"},
    ]
    order = [f["key"] for f in calculation_order(reversed_schema)]
    assert order == ["first", "second"]


# ═══════════════════════════════════════════════════════════════════════════
#  The formula evaluator
# ═══════════════════════════════════════════════════════════════════════════


def test_formulas_cannot_reach_python():
    """Formulas are authored by non-engineers in the Builder. If any of these
    evaluated, a form field would be remote code execution against the API."""
    for hostile in (
        "__import__('os').system('id')",
        "().__class__.__bases__",
        "open('/etc/passwd').read()",
        "[x for x in ().__class__.__mro__]",
        "lambda: 1",
    ):
        with pytest.raises(FormulaError):
            calc(hostile, {})


def test_an_unbounded_exponent_is_refused():
    """`9**9**9` would pin a CPU on a config field anyone can type into."""
    with pytest.raises(FormulaError, match="Exponent out of range"):
        calc("9 ** 99", {})


def test_division_by_zero_is_unknown_not_an_error():
    """An intensity ratio whose denominator has not been entered yet is
    unknown, not invalid — the form must still save. Mirrors how BRSR already
    treats a missing denominator."""
    assert calc("total / output", {"total": 100, "output": 0}) is None
    assert calc("total / output", {"total": 100, "output": None}) is None


def test_unknowns_propagate_rather_than_defaulting_to_zero():
    """A missing input must not silently become 0 and produce a plausible,
    wrong number in a disclosure."""
    assert calc("a + b", {"a": 5}) is None
    assert calc("a * 2", {}) is None


def test_sum_over_a_table_column_treats_blanks_as_zero():
    """A partially filled grid should total what HAS been entered — unlike a
    scalar, where a blank makes the result unknown."""
    rows = [{"q": 10}, {"q": None}, {"q": 5}]
    assert calc("SUM(rows.q)", {"rows": rows}) == 15.0


def test_referenced_fields_sees_through_table_column_access():
    assert referenced_fields("SUM(sources.quantity_gj) / production_output") == {
        "sources",
        "production_output",
    }


# ═══════════════════════════════════════════════════════════════════════════
#  Record validation — the §3.3 rules
# ═══════════════════════════════════════════════════════════════════════════


def test_a_valid_record_validates_and_computes():
    clean, computed = evaluate(ENERGY_FORM, VALID_DATA, require_all=True)
    assert clean["reporting_period"] == "2026-Q2"
    assert len(clean["sources"]) == 2
    assert computed["total_energy_gj"] == pytest.approx(1500.5)
    assert computed["energy_intensity"] == pytest.approx(3.001)


def test_computed_values_are_returned_separately_and_never_merged():
    """The split is the mechanism that stops a client forging a computed figure.
    If these ever came back in one dict, that protection would be a convention
    rather than a property."""
    clean, computed = evaluate(ENERGY_FORM, VALID_DATA, require_all=True)
    assert "total_energy_gj" not in clean
    assert "energy_intensity" not in clean
    assert set(computed) == {"total_energy_gj", "energy_intensity"}


def test_a_client_supplied_calculated_value_is_discarded_not_trusted():
    """POSTing a forged intensity must not land in storage."""
    forged = {**VALID_DATA, "energy_intensity": 0.001, "total_energy_gj": 1}
    clean, computed = evaluate(ENERGY_FORM, forged, require_all=True)
    assert "energy_intensity" not in clean
    assert computed["energy_intensity"] == pytest.approx(3.001)  # recomputed, not 0.001
    assert computed["total_energy_gj"] == pytest.approx(1500.5)


def test_a_hidden_field_is_not_required():
    """§3.3: conditional logic is evaluated server-side too. `has_renewable` is
    False, so the certificate number is hidden and must not be demanded."""
    assert "renewable_certificate_no" not in visible_fields(ENERGY_FORM, VALID_DATA)
    clean, _ = evaluate(ENERGY_FORM, VALID_DATA, require_all=True)
    assert "renewable_certificate_no" not in clean


def test_a_field_revealed_by_a_condition_becomes_required():
    shown = {**VALID_DATA, "has_renewable": True}
    assert "renewable_certificate_no" in visible_fields(ENERGY_FORM, shown)
    with pytest.raises(ValidationError) as e:
        evaluate(ENERGY_FORM, shown, require_all=True)
    assert e.value.errors == {"renewable_certificate_no": "is required"}


def test_a_hidden_field_is_not_stored_even_when_the_client_sends_it():
    """Otherwise toggling a condition off would leave the old branch's answers
    silently in the record, and a reviewer would read fields the form does not
    currently ask."""
    with_stale = {**VALID_DATA, "renewable_certificate_no": "REC-123"}
    clean, _ = evaluate(ENERGY_FORM, with_stale, require_all=True)
    assert "renewable_certificate_no" not in clean


def test_a_hidden_calculation_is_not_computed():
    schema = [
        {"key": "toggle", "type": "boolean", "label": "On?"},
        {"key": "n", "type": "number", "label": "N"},
        {
            "key": "derived",
            "type": "calculated",
            "label": "Derived",
            "formula": "n * 2",
            "visible_if": {"field": "toggle", "operator": "=", "value": True},
        },
    ]
    _, computed = evaluate(schema, {"toggle": False, "n": 4}, require_all=True)
    assert "derived" not in computed
    _, computed = evaluate(schema, {"toggle": True, "n": 4}, require_all=True)
    assert computed["derived"] == 8.0


def test_a_draft_may_be_incomplete_but_may_not_be_wrong():
    """require_all=False skips required-field enforcement, but still validates
    the type of everything supplied — so a draft can never carry a value that
    fails on submit for a reason the author only learns at submit time."""
    partial = {"reporting_period": "2026-Q2"}
    clean, _ = evaluate(ENERGY_FORM, partial, require_all=False)
    assert clean == {"reporting_period": "2026-Q2"}

    with pytest.raises(ValidationError) as e:
        evaluate(ENERGY_FORM, {"production_output": "not a number"}, require_all=False)
    assert "must be a number" in e.value.errors["production_output"]


def test_submit_demands_every_visible_required_field():
    with pytest.raises(ValidationError) as e:
        evaluate(ENERGY_FORM, {"reporting_period": "2026-Q2"}, require_all=True)
    assert set(e.value.errors) == {"production_output"}


def test_errors_are_keyed_by_field_including_inside_a_table():
    """The UI anchors each message to its input; a table cell needs its row."""
    bad = {
        **VALID_DATA,
        "sources": [{"source_type": "NOT_AN_OPTION", "quantity_gj": "abc"}],
    }
    with pytest.raises(ValidationError) as e:
        evaluate(ENERGY_FORM, bad, require_all=True)
    assert "sources[0].source_type" in e.value.errors
    assert "sources[0].quantity_gj" in e.value.errors


def test_an_option_outside_the_configured_set_is_refused():
    schema = [
        {
            "key": "cat",
            "type": "select",
            "label": "Category",
            "options": [{"value": "A"}, {"value": "B"}],
        }
    ]
    with pytest.raises(ValidationError, match="not one of the allowed options"):
        evaluate(schema, {"cat": "Z"}, require_all=True)


def test_an_unknown_table_column_is_dropped_rather_than_rejected():
    """A v2 definition that removed a column must still accept a v2 client's
    stale payload — the whole record failing to save would be worse."""
    bad = {**VALID_DATA, "sources": [{"source_type": "GRID", "quantity_gj": 1, "removed_col": "x"}]}
    clean, _ = evaluate(ENERGY_FORM, bad, require_all=True)
    assert clean["sources"][0] == {"source_type": "GRID", "quantity_gj": 1.0}


def test_numeric_bounds_are_enforced():
    schema = [
        {
            "key": "pct",
            "type": "decimal",
            "label": "Percent",
            "validation": {"min": 0, "max": 100},
        }
    ]
    evaluate(schema, {"pct": 55}, require_all=True)
    with pytest.raises(ValidationError, match="at most 100"):
        evaluate(schema, {"pct": 101}, require_all=True)


def test_required_but_missing_reports_submit_readiness_without_raising():
    """The register says 'ready to submit' before the user presses submit."""
    assert required_but_missing(ENERGY_FORM, VALID_DATA) == []
    assert set(required_but_missing(ENERGY_FORM, {})) == {"reporting_period", "production_output"}
    # The conditionally-hidden field must not be listed while it is hidden…
    assert "renewable_certificate_no" not in required_but_missing(ENERGY_FORM, VALID_DATA)
    # …and must be listed once revealed.
    revealed = {k: v for k, v in VALID_DATA.items() if k != "production_output"}
    revealed["has_renewable"] = True
    assert "renewable_certificate_no" in required_but_missing(ENERGY_FORM, revealed)


def test_a_record_is_validated_against_the_version_it_pinned():
    """v2 adds a required field. A v1 record re-validated against v1 must still
    pass — publishing v2 cannot retroactively invalidate work in progress."""
    v1 = [{"key": "a", "type": "text", "label": "A", "required": True}]
    v2 = [*v1, {"key": "b", "type": "text", "label": "B", "required": True}]
    data = {"a": "filled"}

    evaluate(v1, data, require_all=True)  # the pinned version — passes
    with pytest.raises(ValidationError):
        evaluate(v2, data, require_all=True)  # the newer one would not


# ═══════════════════════════════════════════════════════════════════════════
#  Builder ⇄ engine parity (Part B §4)
#
#  The other half of the proof. `prisma/parity-form-builder.ts` asserts that
#  replaying the Form Designer's authoring gestures produces exactly the bytes
#  in prisma/fixtures/parity-energy-form.json. These tests assert those SAME
#  bytes are executable by the engine.
#
#  Together the two halves close the loop §0 demands: what the Builder emits is
#  both identical to hand-authored config AND accepted by the runtime. Either
#  half alone proves much less — a Builder can agree perfectly with a fixture
#  that the engine would reject.
# ═══════════════════════════════════════════════════════════════════════════

PARITY_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "safeops_360"
    / "prisma"
    / "fixtures"
    / "parity-energy-form.json"
)


def _load_parity_fixture() -> dict:
    if not PARITY_FIXTURE.exists():
        pytest.skip(f"Parity fixture not present at {PARITY_FIXTURE}")
    return json.loads(PARITY_FIXTURE.read_text(encoding="utf-8"))


def test_the_parity_fixture_passes_the_publish_gate():
    """The exact config the Form Designer emits must be publishable.

    If this fails, the Builder is authoring forms that cannot go live — which
    is worse than having no Builder, because the failure only surfaces at
    publish time in front of the author.
    """
    fixture = _load_parity_fixture()
    validate_definition(fixture["schemaJson"], number_pattern=fixture["numberPattern"])


def test_the_parity_fixture_computes_its_intensity_correctly():
    """And it must actually work — not merely parse.

    Same numbers as the inline ENERGY_FORM tests above, deliberately: if the
    fixture and the inline schema ever diverge in meaning, this disagrees.
    """
    fixture = _load_parity_fixture()
    schema = fixture["schemaJson"]

    clean, computed = evaluate(schema, VALID_DATA, require_all=True)

    assert computed["total_energy_gj"] == pytest.approx(1500.5)
    assert computed["energy_intensity"] == pytest.approx(3.001)
    # The conditional field is hidden, so it is neither required nor stored.
    assert "renewable_certificate_no" not in clean


def test_the_parity_fixture_enforces_its_conditional_field():
    """The Builder's rule builder emits a bare clause, not a wrapped group.
    Both shapes are valid to the engine — this pins that the shape actually
    shipped still drives the behaviour the author configured."""
    fixture = _load_parity_fixture()
    schema = fixture["schemaJson"]

    revealed = {**VALID_DATA, "has_renewable": True}
    assert "renewable_certificate_no" in visible_fields(schema, revealed)
    with pytest.raises(ValidationError) as e:
        evaluate(schema, revealed, require_all=True)
    assert "renewable_certificate_no" in e.value.errors


def test_the_parity_fixture_field_keys_match_the_builder_order():
    """Guards against the fixture being edited without re-running the TS side."""
    fixture = _load_parity_fixture()
    assert [f["key"] for f in fixture["schemaJson"]] == [
        "reporting_period",
        "sources",
        "production_output",
        "has_renewable",
        "renewable_certificate_no",
        "total_energy_gj",
        "energy_intensity",
    ]


# ═══════════════════════════════════════════════════════════════════════════
#  Reference numbering
# ═══════════════════════════════════════════════════════════════════════════


def test_a_pattern_needs_exactly_one_sequence_token():
    validate_pattern("KAIZEN-{YYYY}-{####}")
    with pytest.raises(PatternError, match="exactly one sequence token"):
        validate_pattern("KAIZEN-{YYYY}")
    with pytest.raises(PatternError, match="exactly one sequence token"):
        validate_pattern("KAIZEN-{##}-{####}")


def test_an_unknown_token_is_a_typo_not_a_literal():
    """{YYY} would otherwise be emitted verbatim into every reference number in
    the register before anyone noticed."""
    with pytest.raises(PatternError, match="Unknown token"):
        validate_pattern("KAIZEN-{YYY}-{####}")


def test_the_rendered_prefix_is_what_scopes_the_sequence():
    """A pattern containing {YYYY} restarts each year and one containing {SITE}
    numbers each site independently — both fall out of substitution rather than
    needing a separate 'reset frequency' setting to keep in sync."""
    from app.services.form_engine.numbering import _render_prefix_suffix

    prefix, suffix, pad = _render_prefix_suffix("KAIZEN-{YYYY}-{####}", site_code=None)
    assert prefix.startswith("KAIZEN-") and prefix.endswith("-")
    assert suffix == "" and pad == 4

    prefix, _, _ = _render_prefix_suffix("SUS-{SITE}-{YYYY}{MM}-{###}", site_code="nw")
    assert prefix.startswith("SUS-NW-")
