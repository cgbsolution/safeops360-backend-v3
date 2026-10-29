"""Open items from the 2026-10-25 live verification — offline, no network.

1. Observation categories the Postgres enum lacks (OTHER, EMERGENCY, …) never
   reach Observation.category: agent tool filters, triage, permit closure and
   the observations API all map onto stored labels.
2. SCHEDULER_JOBS limits which jobs the supervisor runs.
3. HIRA suggestions carry hazard names and one entry per hazard.
4. The benchmark tool defaults to the deployment's industry (STEEL).
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from app.models.observation import (
    DB_OBSERVATION_CATEGORIES,
    OBSERVATION_CATEGORY_ALIASES,
    ObservationCategory,
    storable_category,
)
from app.models.permit import PermitType
from app.services import scheduler
from app.services.agents import agent_service
from app.services.agents.context_builders import triage as triage_ctx
from app.services.agents.tools import find_recent_findings_in_area, find_related_observations
from app.services.agents.tools import get_industry_benchmark as bench
from app.services.agents.tools._enums import coerce_enum
from app.services.ai.agents import triage as auto_triage
from app.services.ptw_post_closure import _category_for_permit


# ─── 1. Observation categories ──────────────────────────────────────────


def test_db_labels_match_prisma_enum():
    prisma = (
        "PPE HOUSEKEEPING WORK_AT_HEIGHT HOT_WORK MOBILE_EQUIPMENT ELECTRICAL "
        "MATERIAL_HANDLING CONFINED_SPACE CHEMICAL_HANDLING EMERGENCY_PREP OTHERS "
        "REACTIONS_OF_PEOPLE POSITIONS_OF_PEOPLE TOOLS_EQUIPMENT PROCEDURES"
    ).split()
    assert DB_OBSERVATION_CATEGORIES == frozenset(prisma)


def test_every_python_label_is_storable_or_aliased():
    for member in ObservationCategory:
        assert storable_category(member).value in DB_OBSERVATION_CATEGORIES
    assert set(OBSERVATION_CATEGORY_ALIASES) == {m.value for m in ObservationCategory} - DB_OBSERVATION_CATEGORIES


@pytest.mark.parametrize("raw,stored", [
    ("OTHER", "OTHERS"), ("other", "OTHERS"), ("Emergency", "EMERGENCY_PREP"),
    ("LIFTING", "MATERIAL_HANDLING"), ("hot work", "HOT_WORK"), ("TOOLS_EQUIPMENT", "TOOLS_EQUIPMENT"),
])
def test_storable_category(raw, stored):
    assert storable_category(raw) is ObservationCategory(stored)


def test_storable_category_rejects_unknown():
    with pytest.raises(ValueError):
        storable_category("NOT_A_CATEGORY")


def _tool_category(raw: str) -> ObservationCategory:
    return coerce_enum(
        raw, ObservationCategory, "category",
        aliases=OBSERVATION_CATEGORY_ALIASES, allowed=DB_OBSERVATION_CATEGORIES,
    )


def test_tool_filter_maps_other_to_stored_label():
    # The 2026-10-25 failure: the RCA agent asked find_related_observations for "OTHER".
    assert _tool_category("OTHER") is ObservationCategory.OTHERS
    assert _tool_category("EMERGENCY") is ObservationCategory.EMERGENCY_PREP


def test_tool_filter_error_lists_only_stored_labels():
    with pytest.raises(ValueError) as e:
        _tool_category("SPACESHIP")
    listed = str(e.value).split("Valid values are: ")[1].split(".")[0].split(", ")
    assert set(listed) == DB_OBSERVATION_CATEGORIES


def test_coerce_enum_without_allowed_is_unchanged():
    assert coerce_enum("OTHER", ObservationCategory, "category") is ObservationCategory.OTHER


@pytest.mark.parametrize("tool", [find_related_observations, find_recent_findings_in_area])
def test_tool_descriptions_offer_only_stored_labels(tool):
    desc = tool.DEFINITION["input_schema"]["properties"]["category"]["description"]
    offered = {w.strip(" .,") for w in desc.split(":", 1)[1].replace("\n", " ").split(",")}
    assert offered <= DB_OBSERVATION_CATEGORIES
    src = inspect.getsource(tool)
    assert "allowed=DB_OBSERVATION_CATEGORIES" in src


def test_permit_closure_writes_only_stored_labels():
    for permit_type in PermitType:
        assert _category_for_permit(permit_type).value in DB_OBSERVATION_CATEGORIES


def test_auto_triage_keeps_only_stored_categories():
    assert auto_triage._valid_categories() == DB_OBSERVATION_CATEGORIES
    cleaned = auto_triage._clean({"suggestedCategory": "OTHER", "suggestedSeverity": "HIGH"}, None)
    assert cleaned["suggestedCategory"] is None
    cleaned = auto_triage._clean({"suggestedCategory": "tools equipment"}, None)
    assert cleaned["suggestedCategory"] == "TOOLS_EQUIPMENT"


async def test_triage_agent_is_offered_only_stored_categories():
    cats = await triage_ctx._available_categories(None, "observation")
    ids = {c["id"] for c in cats}
    assert ids == DB_OBSERVATION_CATEGORIES
    assert all(c["description"] for c in cats)


def test_observations_router_writes_through_storable_category():
    from app.routers import observations

    src = inspect.getsource(observations)
    assert "ObservationCategory(" not in src
    assert src.count("storable_category(") == 3


# ─── 2. Scheduler allow-list ────────────────────────────────────────────


def test_scheduler_runs_every_job_by_default():
    assert scheduler.scheduled_jobs("") == scheduler.JOBS
    assert scheduler.scheduled_jobs(" , ") == scheduler.JOBS


def test_scheduler_allow_list_and_unknown_ids():
    jobs = scheduler.scheduled_jobs("signal_engine_scan, observation_weekly_insights,nope")
    assert set(jobs) == {"signal_engine_scan", "observation_weekly_insights"}


# ─── 3. HIRA hazard labels ──────────────────────────────────────────────


class _FakeDb:
    """Answers the two lookups _label_hira_hazards makes: library rows by id,
    then entry-hazard rows by id."""

    def __init__(self, library, entry_hazards):
        self.calls = [library, entry_hazards]

    async def execute(self, _stmt):
        rows = self.calls.pop(0)
        return [SimpleNamespace(id=i, name=n, category=c) for i, (n, c) in rows.items()]


async def test_hira_hazards_get_names_and_duplicates_drop():
    suggestion = {
        "reasoning_summary": "x",
        "suggested_hazards": [
            {"hazard_master_id": "h1", "relevance_confidence": 0.95, "suggested_for_addition": True},
            {"hazard_master_id": "h2", "relevance_confidence": 0.78, "suggested_for_addition": True},
            {"hazard_master_id": "h2", "relevance_confidence": 0.0, "reasoning": "DUPLICATE — disregard"},
            {"hazard_master_id": "ghost", "relevance_confidence": 0.5},
        ],
        "control_suggestions": [{"hazard_id": "eh1", "ppe_controls": []}],
    }
    db = _FakeDb({"h1": ("Vapour ignition", "FIRE"), "h2": ("BLEVE", "PRESSURE")}, {"eh1": ("Hot surfaces", "THERMAL")})
    out = await agent_service._label_hira_hazards(db, "HIRA_ASSISTANT", suggestion)

    hazards = out["suggested_hazards"]
    assert [h["hazard"] for h in hazards] == ["Vapour ignition", "BLEVE", "Not found in the hazard library"]
    assert hazards[1]["relevance_confidence"] == 0.78  # the real entry kept, the 0% copy dropped
    assert list(hazards[0])[:2] == ["hazard", "hazard_category"]  # name renders first on the card
    assert out["control_suggestions"][0]["hazard"] == "Hot surfaces"
    assert out["reasoning_summary"] == "x"
    assert len(suggestion["suggested_hazards"]) == 4  # input not mutated


async def test_hira_labelling_leaves_other_agents_alone():
    s = {"suggested_hazards": [{"hazard_master_id": "h1"}]}
    assert await agent_service._label_hira_hazards(None, "RCA_ASSISTANT", s) is s
    assert await agent_service._label_hira_hazards(None, "HIRA_ASSISTANT", {"_unparsed": "x"}) == {"_unparsed": "x"}


# ─── 4. Benchmark industry ──────────────────────────────────────────────


async def test_benchmark_defaults_to_deployment_industry():
    out = await bench.handle({"incidentType": "LTI"})
    assert out["industryContext"] == "STEEL"
    assert out["patterns"] and all("iron and steel" in p["source"] for p in out["patterns"])


async def test_benchmark_explicit_industry_still_honoured():
    out = await bench.handle({"incidentType": "LTI", "industryContext": "cement"})
    assert out["industryContext"] == "CEMENT" and out["patterns"]


def test_benchmark_description_names_deployment_not_cement_default():
    desc = bench.DEFINITION["input_schema"]["properties"]["industryContext"]["description"]
    assert "'STEEL'" in desc and "primarily in cement" not in desc
