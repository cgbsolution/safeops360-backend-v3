"""Copilot HIRA builder — offline (no DB, no network, no LLM spend).

Covers the argument rules, merging additions into an entry's saved lists, the
guards that stop a save before anything is written, and how one entry edit is
versioned when it touches several lists (it must file one version, like the
editor's Save, not one per list).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.copilot import form_drafts, hira_builder, runtime, tools
from app.services.copilot.hira_builder import InputError

HZ = {"hazardId": "hz-1", "consequence": "Furnace operator splashed by molten metal: severe burns to face and arms"}


def _entry_args(**over: Any) -> dict[str, Any]:
    base = {
        "activityDescription": "Charging scrap into the EAF with the charging crane",
        "routine": "ROUTINE", "frequency": "DAILY",
        "initialLikelihood": 4, "initialSeverity": 5,
        "initialLikelihoodRationale": "Charging happens every heat and wet scrap is common",
        "initialSeverityRationale": "A splash of molten metal can be fatal",
        "hazards": [HZ],
    }
    base.update(over)
    return base


# ─── Study ──────────────────────────────────────────────────────────────


def test_study_scope_follows_area_or_department():
    assert hira_builder.parse_study({"plantId": "p1", "title": "SMS EAF"})["scopeType"] == "PLANT"
    assert hira_builder.parse_study({"plantId": "p1", "title": "SMS EAF", "areaId": "a1"})["scopeType"] == "AREA"
    assert hira_builder.parse_study({"plantId": "p1", "title": "SMS EAF", "departmentId": "d1"})["scopeType"] == "DEPARTMENT"


def test_study_rules():
    with pytest.raises(InputError, match="title"):
        hira_builder.parse_study({"plantId": "p1", "title": "x"})
    with pytest.raises(InputError, match="customReviewMonths"):
        hira_builder.parse_study({"plantId": "p1", "title": "SMS EAF", "reviewFrequency": "CUSTOM"})
    with pytest.raises(InputError, match="scopeType"):
        hira_builder.parse_study({"plantId": "p1", "title": "SMS EAF", "scopeType": "GALAXY"})
    p = hira_builder.parse_study({"plantId": "p1", "title": "SMS EAF", "targetCompletionDate": "2026-12-31",
                                  "team": [{"userId": "u2"}, {"userId": "u2", "teamRole": "SAFETY_OFFICER"}]})
    assert p["targetCompletionDate"].year == 2026
    assert p["team"] == [{"userId": "u2", "teamRole": "SUBJECT_MATTER_EXPERT"}]


# ─── New entry ──────────────────────────────────────────────────────────


def test_new_entry_needs_hazards_consequences_and_rationales():
    assert hira_builder.parse_new_entry(_entry_args())["hazards"][0]["hazardId"] == "hz-1"
    with pytest.raises(InputError, match="at least one hazard"):
        hira_builder.parse_new_entry(_entry_args(hazards=[]))
    with pytest.raises(InputError, match="consequence"):
        hira_builder.parse_new_entry(_entry_args(hazards=[{"hazardId": "hz-1", "consequence": "injury"}]))
    with pytest.raises(InputError, match="listed twice"):
        hira_builder.parse_new_entry(_entry_args(hazards=[HZ, HZ]))
    with pytest.raises(InputError, match="initialSeverityRationale"):
        hira_builder.parse_new_entry(_entry_args(initialSeverityRationale=None))
    with pytest.raises(InputError, match="initialLikelihood"):
        hira_builder.parse_new_entry(_entry_args(initialLikelihood=None))


def test_new_entry_controls_are_checked():
    p = hira_builder.parse_new_entry(_entry_args(
        existingControls=[{"hierarchy": "engineering", "description": "Scrap drying bay before charging"}],
        recommendedControls=[{"hierarchy": "ELIMINATION", "description": "Scrap moisture check gate", "estimatedCostBand": "MEDIUM"}],
    ))
    assert p["existingControls"][0]["hierarchy"] == "ENGINEERING"
    assert p["recommendedControls"][0]["estimatedCostBand"] == "MEDIUM"
    with pytest.raises(InputError, match="hierarchy"):
        hira_builder.parse_new_entry(_entry_args(existingControls=[{"hierarchy": "PRAYER", "description": "Hope it holds"}]))


# ─── Entry update ───────────────────────────────────────────────────────


def test_update_needs_something_and_never_blanks_saved_values():
    with pytest.raises(InputError, match="Nothing to change"):
        hira_builder.parse_entry_update({"routine": None, "initialSeverityRationale": None})
    u = hira_builder.parse_entry_update({"routine": "non_routine", "frequency": None})
    assert u["fields"] == {"routine": "NON_ROUTINE"}
    with pytest.raises(InputError, match="together"):
        hira_builder.parse_entry_update({"initialLikelihood": 3})


def test_merge_hazards_add_update_remove():
    existing = [{"hazardId": "a", "consequence": "Old consequence text", "contextualDescription": "ctx",
                 "regulationRef": "Factories Act 1948", "regulationSection": "s.38"},
                {"hazardId": "b", "consequence": "Second hazard consequence", "contextualDescription": None,
                 "regulationRef": None, "regulationSection": None}]
    out = hira_builder.merge_hazards(
        existing,
        add=[{"hazardId": "c", "consequence": "New hazard consequence", "contextualDescription": None,
              "regulationRef": None, "regulationSection": None}],
        update=[{"hazardId": "a", "consequence": "Sharper consequence text", "contextualDescription": None,
                 "contextualDescriptionSet": False}],
        remove=["b"])
    assert [h["hazardId"] for h in out] == ["a", "c"]
    assert out[0]["consequence"] == "Sharper consequence text" and out[0]["contextualDescription"] == "ctx"
    assert out[0]["regulationRef"] == "Factories Act 1948"  # saved citation kept
    assert [h["sortOrder"] for h in out] == [0, 1]
    with pytest.raises(InputError, match="already on this entry"):
        hira_builder.merge_hazards(existing, add=[{"hazardId": "a", "consequence": "x" * 12}], update=[], remove=[])
    with pytest.raises(InputError, match="at least one hazard"):
        hira_builder.merge_hazards(existing, add=[], update=[], remove=["a", "b"])
    with pytest.raises(InputError, match="not on this entry"):
        hira_builder.merge_hazards(existing, add=[], update=[], remove=["zzz"])


def test_merge_existing_controls_keeps_details_and_skips_repeats():
    existing = [{"id": "k1", "controlId": None, "hierarchy": "ENGINEERING", "description": "Splash guard on furnace door",
                 "effectiveness": None, "verificationMethod": "Weekly check", "verificationFreq": "WEEKLY",
                 "responsibleRole": "Shift in-charge", "evidenceAttached": True, "documentReference": "SOP-12"}]
    out = hira_builder.merge_existing_controls(
        existing,
        add=[{"hierarchy": "ENGINEERING", "description": "splash guard on furnace door ", "effectiveness": None},
             {"hierarchy": "PPE", "description": "Aluminised suit and face shield", "effectiveness": "EFFECTIVE"}],
        set_eff=[{"key": "k1", "effectiveness": "EFFECTIVE"}], remove=[])
    assert len(out) == 2
    assert out[0]["effectiveness"] == "EFFECTIVE" and out[0]["verificationMethod"] == "Weekly check"
    assert out[0]["documentReference"] == "SOP-12"
    with pytest.raises(InputError, match="Unknown existing-control keys"):
        hira_builder.merge_existing_controls(existing, add=[], set_eff=[{"key": "nope", "effectiveness": "EFFECTIVE"}],
                                             remove=[])


def test_merge_recommended_only_removes_untouched_proposals():
    existing = [{"id": "r1", "hierarchy": "ENGINEERING", "description": "Interlock", "status": "PROPOSED", "capaId": None},
                {"id": "r2", "hierarchy": "ADMINISTRATIVE", "description": "Training", "status": "APPROVED", "capaId": None}]
    out = hira_builder.merge_recommended(existing, add=[{"hierarchy": "PPE", "description": "Face shield",
                                                         "rationale": None, "estimatedCostBand": None}], remove=["r1"])
    assert [c.get("id") for c in out] == ["r2", None] and out[1]["status"] == "PROPOSED"
    with pytest.raises(InputError, match="moved on"):
        hira_builder.merge_recommended(existing, add=[], remove=["r2"])


# ─── Wiring ─────────────────────────────────────────────────────────────

_NEW = ("get_hira_options", "get_hira_study", "find_hazard_masters", "create_hira_study", "create_hira_entry",
        "update_hira_entry", "submit_hira_entry_for_review")


def test_hira_builder_tools_are_registered():
    for name in _NEW:
        assert name in tools.TOOL_NAMES and name in tools.TOOL_LABELS
        schema = next(t for t in tools.TOOL_DEFINITIONS if t["name"] == name)["input_schema"]
        assert schema["additionalProperties"] is False
    json.dumps(tools.TOOL_DEFINITIONS)


def test_prompt_no_longer_sends_users_away_to_create_a_hira():
    prompt = runtime.load_system_prompt()
    assert "create_hira_study" in prompt and "create_hira_entry" in prompt and "update_hira_entry" in prompt
    assert "Never say you can't create a HIRA" in prompt


def test_page_context_carries_the_open_study_and_dirty_flag():
    msg = runtime.build_user_message(text="hi", page={"path": "/hira/s1", "module": "HIRA", "studyId": "s1"},
                                     draft=None, now_local=None, tz_offset=0, user_plant_id="p1", user_id="u1")
    assert '"hiraStudyId": "s1"' in msg["content"][0]["text"]
    kind, draft = form_drafts.normalise_client_form({"kind": "HIRA_ENTRY", "draft": {"entryId": "e1", "dirty": True}})
    assert kind == "HIRA_ENTRY" and draft["dirty"] is True
    _, draft = form_drafts.normalise_client_form({"kind": "HIRA_ENTRY", "draft": {"entryId": "e1", "dirty": "yes"}})
    assert draft["dirty"] is False


def _ctx(**over: Any) -> tools.TurnContext:
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(event: str, data: dict[str, Any]) -> None:
        events.append((event, data))

    ctx = tools.TurnContext(user_id="u1", user_plant_id="p1", conversation_id="c" * 10, page={}, emit=emit)
    for k, v in over.items():
        setattr(ctx, k, v)
    ctx.events = events  # type: ignore[attr-defined]
    return ctx


async def test_write_tools_refuse_before_touching_the_database():
    # db=None: each of these must fail before any query.
    with pytest.raises(tools.ToolError, match="Nothing was saved: title"):
        await tools._create_hira_study(None, {"plantId": "p1", "title": "ab"}, _ctx())  # type: ignore[arg-type]
    with pytest.raises(tools.ToolError, match="which study"):
        await tools._create_hira_entry(None, _entry_args(), _ctx())  # type: ignore[arg-type]
    with pytest.raises(tools.ToolError, match="which entry"):
        await tools._update_hira_entry(None, {"routine": "ROUTINE"}, _ctx())  # type: ignore[arg-type]
    dirty = _ctx(form_kind="HIRA_ENTRY", form_draft={"entryId": "e1", "studyId": "s1", "dirty": True})
    with pytest.raises(tools.ToolError, match="unsaved edits"):
        await tools._update_hira_entry(None, {"routine": "ROUTINE"}, dirty)  # type: ignore[arg-type]
    with pytest.raises(tools.ToolError, match="unsaved edits"):
        await tools._submit_hira_entry(None, {}, dirty)  # type: ignore[arg-type]
    busy = _ctx(hira_writes=tools._HIRA_WRITES_PER_TURN)
    with pytest.raises(tools.ToolError, match="a lot of saving"):
        await tools._create_hira_study(None, {"plantId": "p1", "title": "SMS EAF"}, busy)  # type: ignore[arg-type]


def test_open_entry_and_study_come_from_the_page():
    ctx = _ctx(page={"module": "HIRA", "recordId": "e9", "studyId": "s9"})
    assert tools._page_entry_id(ctx) == "e9" and tools._page_study_id(ctx) == "s9"
    ctx = _ctx(form_kind="HIRA_ENTRY", form_draft={"entryId": "e1", "studyId": "s1"}, page={"module": "HIRA"})
    assert tools._page_entry_id(ctx) == "e1" and tools._page_study_id(ctx) == "s1"
    assert tools._page_entry_id(_ctx(page={"module": "PTW", "recordId": "p1"})) is None


# ─── One edit, one version ──────────────────────────────────────────────


def _saved_entry(study_status: str, version: int = 1) -> SimpleNamespace:
    hz = SimpleNamespace(hazardId="hz-1", consequence="Operator burned by splash", contextualDescription=None,
                         regulationRef=None, regulationSection=None, sortOrder=0,
                         hazard=SimpleNamespace(name="Molten metal", category="thermal", requiresPermit=False))
    ctrl = SimpleNamespace(id="k1", controlId=None, hierarchy="ENGINEERING", description="Splash guard",
                           effectiveness=None, verificationMethod=None, verificationFreq=None, responsibleRole=None,
                           evidenceAttached=False, documentReference=None, sortOrder=0)
    return SimpleNamespace(
        id="e1", studyId="s1", sequenceNumber=3, isCurrentVersion=True, versionNumber=version, status="APPROVED",
        study=SimpleNamespace(status=study_status, number="HIRA-2026-HSR-001", riskMatrixId="m1"),
        activityDescription="EAF charging", routine="ROUTINE", frequency="DAILY",
        hazards=[hz], existingControls=[ctrl], recommendedControls=[],
        residualAutoCalculated=True, residualLikelihoodScore=2, residualSeverityScore=4, residualRiskScore=8,
        residualRiskLevel="HIGH", residualAcceptable=False, residualAlarpRegion="TOLERABLE", alarpStatus="REQUIRED",
        initialLikelihoodScore=4, initialSeverityScore=5, initialRiskScore=20, initialRiskLevel="CRITICAL",
        targetLikelihoodScore=None, targetSeverityScore=None, targetRiskScore=None, targetRiskLevel=None,
        initialLikelihoodRationale="ok ok ok ok", initialSeverityRationale="ok ok ok ok",
        residualLikelihoodRationale="ok ok ok ok", residualSeverityRationale="ok ok ok ok",
    )


class _FakeDb:
    def expire(self, *_a: Any, **_k: Any) -> None:
        pass

    async def flush(self) -> None:
        pass


@pytest.fixture
def fake_router(monkeypatch):
    import app.routers.hira as hira_router

    calls: list[tuple[str, dict[str, Any]]] = []

    def record(name: str):
        async def fn(**kw: Any) -> dict[str, Any]:
            calls.append((name, {k: kw.get(k) for k in ("change_reason", "skip_version")}))
            return {}
        return fn

    for name in ("update_entry", "replace_entry_hazards", "replace_existing_controls", "replace_recommended_controls"):
        monkeypatch.setattr(hira_router, name, record(name))

    async def noop(*_a: Any, **_k: Any) -> None:
        return None

    async def user(*_a: Any, **_k: Any) -> Any:
        return SimpleNamespace(id="u1")

    monkeypatch.setattr(tools, "_commit", noop)
    monkeypatch.setattr(tools, "_load_user", user)
    return calls


async def test_edit_on_approved_study_needs_a_reason(monkeypatch, fake_router):
    entry = _saved_entry("APPROVED")

    async def load(*_a: Any) -> Any:
        return entry
    monkeypatch.setattr(tools, "_load_entry", load)
    with pytest.raises(tools.ToolError, match="change reason"):
        await tools._update_hira_entry(_FakeDb(), {"entryId": "e1", "removeExistingControlKeys": ["k1"],  # type: ignore[arg-type]
                                                   "updateHazards": [{"hazardId": "hz-1", "consequence": "x" * 20}]}, _ctx())
    assert fake_router == []


async def test_one_edit_files_one_version(monkeypatch, fake_router):
    entry = _saved_entry("APPROVED")

    async def load(*_a: Any) -> Any:
        return entry
    monkeypatch.setattr(tools, "_load_entry", load)
    ctx = _ctx()
    out = await tools._update_hira_entry(_FakeDb(), {  # type: ignore[arg-type]
        "entryId": "e1", "changeReason": "Splash guard found damaged on site walk",
        "frequency": "WEEKLY",
        "updateHazards": [{"hazardId": "hz-1", "consequence": "Operator burned by splash through damaged guard"}],
        "setControlEffectiveness": [{"key": "k1", "effectiveness": "INEFFECTIVE"}],
        "addRecommendedControls": [{"hierarchy": "ENGINEERING", "description": "Replace splash guard"}],
    }, ctx)
    assert [c[0] for c in fake_router] == ["update_entry", "replace_entry_hazards", "replace_existing_controls",
                                           "replace_recommended_controls"]
    assert fake_router[0][1]["change_reason"] == "Splash guard found damaged on site walk"
    # The PATCH files the version; the list saves ride on it.
    assert [c[1]["skip_version"] for c in fake_router[1:]] == [True, True, True]
    assert out["saved"] == ["details", "hazards", "existing controls", "recommended controls"]
    assert ctx.events[-1][1]["type"] == "record" and ctx.events[-1][1]["verb"] == "updated"  # type: ignore[attr-defined]
    assert ctx.hira_writes == 1


async def test_draft_edit_without_patch_does_not_skip_versioning(monkeypatch, fake_router):
    entry = _saved_entry("DRAFT")

    async def load(*_a: Any) -> Any:
        return entry
    monkeypatch.setattr(tools, "_load_entry", load)
    await tools._update_hira_entry(_FakeDb(), {  # type: ignore[arg-type]
        "entryId": "e1", "addExistingControls": [{"hierarchy": "PPE", "description": "Aluminised suit"}]}, _ctx())
    assert fake_router == [("replace_existing_controls", {"change_reason": None, "skip_version": False})]
