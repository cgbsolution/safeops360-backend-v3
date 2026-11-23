"""The copilot fills any form that describes itself (near miss, incident, CAPA...):
values are checked against the form's own fields and dropdown options, so the
copilot can neither set a field the form didn't list nor invent a master record."""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.services.copilot import form_drafts, runtime, tools

AREAS = [{"value": "ar_1", "label": "Rolling Mill Bay"}, {"value": "ar_2", "label": "Melt Shop"}]


def _near_miss(**values: Any) -> dict[str, Any]:
    fields = [
        {"name": "date", "label": "Date / time", "type": "datetime", "required": True, "value": "2026-11-21T09:00"},
        {"name": "plantId", "label": "Plant", "type": "select", "required": True, "value": "pl_1",
         "options": [{"value": "pl_1", "label": "Hisar"}, {"value": "pl_2", "label": "Jajpur"}]},
        {"name": "areaId", "label": "Area", "type": "select", "value": "", "dependsOn": "plantId", "options": AREAS},
        {"name": "description", "label": "Description", "type": "textarea", "required": True,
         "minLength": 10, "maxLength": 1500, "value": ""},
        {"name": "severity", "label": "Potential severity", "type": "select", "required": True, "value": "MEDIUM",
         "options": [{"value": s, "label": s} for s in ("LOW", "MEDIUM", "HIGH", "CRITICAL")]},
        {"name": "consequenceTypes", "label": "Consequence types", "type": "multiselect", "value": [],
         "options": [{"value": "INJURY", "label": "Injury"}, {"value": "PROPERTY_DAMAGE", "label": "Property damage"}]},
        {"name": "riskLikelihood", "label": "Likelihood (1-5)", "type": "number", "min": 1, "max": 5, "value": None},
        {"name": "activityIsRoutine", "label": "Routine activity", "type": "boolean", "value": None},
        {"name": "photoCount", "label": "Photos attached", "type": "number", "value": 1, "readOnly": True},
    ]
    for f in fields:
        if f["name"] in values:
            f["value"] = values[f["name"]]
    kind, form = form_drafts.normalise_client_form({"kind": "NEAR_MISS", "draft": {"title": "New near miss form", "fields": fields}})
    assert kind == "NEAR_MISS" and form is not None
    return form


# ─── The form's description ─────────────────────────────────────────────


def test_described_form_is_whitelisted_and_clipped():
    kind, form = form_drafts.normalise_client_form({"kind": "near_miss", "draft": {"title": "x" * 500, "fields": [
        {"name": "ok", "label": "OK", "type": "text", "value": "y" * 9000},
        {"name": "bad name!", "label": "Bad", "type": "text"},
        {"name": "weird", "label": "Weird", "type": "html"},
        {"name": "pick", "label": "Pick", "type": "select", "value": "a",
         "options": [{"value": "a", "label": "A"}, {"value": "a", "label": "dup"}, "junk", {"label": "no value"}]},
    ]}})
    assert kind == "NEAR_MISS"
    assert [f["name"] for f in form["fields"]] == ["ok", "pick"]
    assert len(form["title"]) == 120 and len(form["fields"][0]["value"]) == form_drafts.MAX_TEXT
    assert form["fields"][1]["options"] == [{"value": "a", "label": "A"}]


@pytest.mark.parametrize("kind", ["", "1BAD", "near miss", "X" * 60, None])
def test_unknown_kinds_and_empty_forms_are_dropped(kind):
    assert form_drafts.normalise_client_form({"kind": kind, "draft": {"fields": [{"name": "a", "label": "A", "type": "text"}]}}) == (None, None)
    assert form_drafts.normalise_client_form({"kind": "INCIDENT", "draft": {"fields": []}}) == (None, None)


def test_observation_and_hira_keep_their_own_shape():
    kind, draft = form_drafts.normalise_client_form({"kind": "OBSERVATION", "draft": {"type": "UNSAFE_ACT", "fields": []}})
    assert kind == "OBSERVATION" and draft["type"] == "UNSAFE_ACT" and "fields" not in draft


# ─── Checking the copilot's values ──────────────────────────────────────


def test_values_are_checked_and_options_match_by_value_or_label():
    patch, labels, skipped, err = form_drafts.parse_described_patch(_near_miss(), {
        "areaId": "rolling mill bay",            # an option's label, any case
        "severity": "HIGH",
        "consequenceTypes": ["INJURY", "Property damage"],
        "description": "  Sling slipped during a motor lift; the load swung near two fitters.  ",
        "riskLikelihood": 3,
        "activityIsRoutine": "no",
        "date": "2026-11-21T10:30",
    })
    assert err is None and skipped == []
    assert patch == {"areaId": "ar_1", "severity": "HIGH", "consequenceTypes": ["INJURY", "PROPERTY_DAMAGE"],
                     "description": "Sling slipped during a motor lift; the load swung near two fitters.",
                     "riskLikelihood": 3, "activityIsRoutine": False, "date": "2026-11-21T10:30"}
    assert labels["areaId"] == "Area"


@pytest.mark.parametrize(("values", "says"), [
    ({"areaId": "ar_99"}, "not an option"),                 # an invented master record
    ({"severity": "SEVERE"}, "not an option"),
    ({"consequenceTypes": ["INJURY", "FLOOD"]}, "not an option"),
    ({"injuredPerson": "x"}, "not a field"),                # a field the form didn't list
    ({"photoCount": 3}, "read only"),
    ({"riskLikelihood": 7}, "between 1 and 5"),
    ({"riskLikelihood": "high"}, "must be a number"),
    ({"date": "21/11/2026 10:30"}, "YYYY-MM-DDTHH:MM"),
    ({"description": "short"}, "at least 10"),
    ({"description": "x" * 1501}, "at most 1500"),
    ({"activityIsRoutine": "maybe"}, "true or false"),
])
def test_a_wrong_value_changes_nothing(values, says):
    # Alongside a good value: one bad value still changes nothing.
    patch, _, _, err = form_drafts.parse_described_patch(_near_miss(), {"activityIsRoutine": True, **values})
    assert patch == {} and err is not None and err.startswith("Nothing was changed") and says in err


def test_null_or_empty_clears_a_field():
    patch, _, _, err = form_drafts.parse_described_patch(_near_miss(areaId="ar_2"), {"areaId": None, "riskLikelihood": "", "consequenceTypes": None})
    assert err is None and patch == {"areaId": "", "riskLikelihood": None, "consequenceTypes": []}


def test_dependent_field_waits_for_its_parent_to_reload():
    patch, _, skipped, err = form_drafts.parse_described_patch(_near_miss(), {"plantId": "Jajpur", "areaId": "ar_1"})
    assert err is None and patch == {"plantId": "pl_2"} and "Area" in skipped[0]
    # Same plant as now: the area list is current, so it applies.
    patch, _, skipped, _ = form_drafts.parse_described_patch(_near_miss(), {"plantId": "pl_1", "areaId": "ar_1"})
    assert patch == {"plantId": "pl_1", "areaId": "ar_1"} and skipped == []


def test_required_fields_still_empty():
    form = _near_miss()
    assert form_drafts.missing_required(form) == ["Description"]
    form = form_drafts.apply_described_patch(form, {"description": "A sling slipped during a lift."})
    assert form_drafts.missing_required(form) == []


# ─── What the model sees ────────────────────────────────────────────────


def test_open_form_is_in_the_page_context_compact_and_scrubbed():
    form = _near_miss(description="Fitter on 9876543210 saw the sling slip")
    msg = runtime.build_user_message(text="fill it", page={"path": "/near-miss/new", "module": "NEAR_MISS"}, draft=None,
                                     now_local="2026-11-21T10:40", tz_offset=-330, user_plant_id="pl_1",
                                     form_kind="NEAR_MISS", form_draft=form)
    ctx = json.loads(msg["content"][0]["text"].split("\n", 1)[1].rsplit("\n", 1)[0])
    open_form = ctx["openForm"]
    assert open_form["kind"] == "NEAR_MISS" and open_form["title"] == "New near miss form"
    area = next(f for f in open_form["fields"] if f["name"] == "areaId")
    assert area["options"] == [["ar_1", "Rolling Mill Bay"], ["ar_2", "Melt Shop"]] and area["dependsOn"] == "plantId"
    assert "9876543210" not in json.dumps(ctx)
    assert "observationDraft" not in ctx and "hiraEntryDraft" not in ctx


def test_long_option_lists_are_cut_for_the_model_but_still_checked():
    options = [{"value": f"eq_{i}", "label": f"Pump {i}"} for i in range(300)]
    _, form = form_drafts.normalise_client_form({"kind": "NEAR_MISS", "draft": {"fields": [
        {"name": "equipmentId", "label": "Equipment", "type": "select", "value": "", "options": options}]}})
    shown = form_drafts.described_form_for_model(form)["fields"][0]
    assert len(shown["options"]) == form_drafts.MODEL_OPTIONS and shown["moreOptions"] == 150
    patch, _, _, err = form_drafts.parse_described_patch(form, {"equipmentId": "Pump 250"})
    assert err is None and patch == {"equipmentId": "eq_250"}


# ─── The tool ───────────────────────────────────────────────────────────


def _ctx(kind: str | None, form: dict[str, Any] | None, events: list) -> tools.TurnContext:
    async def emit(event: str, data: dict[str, Any]) -> None:
        events.append((event, data))

    return tools.TurnContext(user_id="u1", user_plant_id="pl_1", conversation_id="c1", page={}, emit=emit,
                             form_kind=kind, form_draft=form)


async def test_tool_fills_the_open_form_and_reports_what_is_left():
    events: list = []
    ctx = _ctx("NEAR_MISS", _near_miss(), events)
    out = await tools._update_form_draft(None, {"values": {"severity": "HIGH", "areaId": "Melt Shop"}}, ctx)
    assert out["applied"] == ["Potential severity", "Area"] and out["stillRequired"] == ["Description"]
    assert events == [("form_patch", {"kind": "NEAR_MISS", "patch": {"severity": "HIGH", "areaId": "ar_2"},
                                      "labels": {"severity": "Potential severity", "areaId": "Area"}})]
    # The next call in the same turn sees the change.
    out = await tools._update_form_draft(None, {"values": {"description": "Sling slipped during a motor lift."}}, ctx)
    assert out["stillRequired"] == []


async def test_tool_errors_are_recoverable_and_point_to_the_right_place():
    events: list = []
    with pytest.raises(tools.ToolError, match="/near-miss/new"):
        await tools._update_form_draft(None, {"values": {"severity": "HIGH"}}, _ctx(None, None, events))
    with pytest.raises(tools.ToolError, match="update_observation_draft"):
        await tools._update_form_draft(None, {"values": {"type": "UNSAFE_ACT"}}, _ctx("OBSERVATION", {"type": None}, events))
    with pytest.raises(tools.ToolError, match="not an option"):
        await tools._update_form_draft(None, {"values": {"areaId": "ar_404"}}, _ctx("NEAR_MISS", _near_miss(), events))
    with pytest.raises(tools.ToolError, match="Send values"):
        await tools._update_form_draft(None, {"values": "severity=HIGH"}, _ctx("NEAR_MISS", _near_miss(), events))
    assert events == []


def test_tool_is_registered_and_strict():
    assert "update_form_draft" in tools.TOOL_NAMES and "update_form_draft" in tools.TOOL_LABELS
    schema = next(t for t in tools.TOOL_DEFINITIONS if t["name"] == "update_form_draft")["input_schema"]
    assert schema["required"] == ["values"] and schema["additionalProperties"] is False
