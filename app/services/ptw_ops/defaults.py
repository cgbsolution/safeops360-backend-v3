"""Built-in rule sets. Used when the rule tables are missing (DDL not yet run)
or empty, and seeded into ImpairmentRule / SimopsRule by
prisma/apply-ptw-control-center-ddl.ts (same values — keep both in sync;
tests/test_ptw_ops.py pins them)."""

from __future__ import annotations

from app.services.ptw_ops.types import ImpairmentRuleView, SimopsRuleView

DEFAULT_IMPAIRMENT_RULES: list[dict] = [
    {"systemType": "sprinkler", "permitType": "HOT_WORK", "effect": "block_new",
     "requiredControls": ["fire_watch", "fire_officer_cosign"]},
    {"systemType": "fire_detection", "permitType": "HOT_WORK", "effect": "block_new",
     "requiredControls": ["fire_watch", "fire_officer_cosign"]},
    {"systemType": "gas_detection", "permitType": "CONFINED_SPACE", "effect": "require_controls",
     "requiredControls": ["continuous_gas_monitor", "attendant"]},
    {"systemType": "gas_detection", "permitType": "HOT_WORK", "effect": "require_controls",
     "requiredControls": ["continuous_gas_monitor", "attendant"]},
    {"systemType": "fire_alarm", "permitType": "*", "effect": "require_controls",
     "requiredControls": ["hourly_patrol", "no_unattended_work"]},
    {"systemType": "fire_alarm", "permitType": "HOT_WORK", "effect": "block_new",
     "requiredControls": ["fire_watch", "hourly_patrol"]},
]

DEFAULT_SIMOPS_RULES: list[dict] = [
    {"permitTypeA": "HOT_WORK", "permitTypeB": "CONFINED_SPACE", "minDistanceM": 15.0, "sameArea": True,
     "severity": "critical"},
    {"permitTypeA": "HOT_WORK", "permitTypeB": "EXCAVATION", "minDistanceM": 10.0, "sameArea": True,
     "severity": "critical"},
    {"permitTypeA": "HOT_WORK", "permitTypeB": "ELECTRICAL_LOTO", "minDistanceM": None, "sameArea": True,
     "severity": "high"},
    {"permitTypeA": "LIFTING", "permitTypeB": "WORK_AT_HEIGHT", "minDistanceM": None, "sameArea": True,
     "severity": "high"},
    {"permitTypeA": "LIFTING", "permitTypeB": "EXCAVATION", "minDistanceM": None, "sameArea": True,
     "severity": "high"},
    {"permitTypeA": "CONFINED_SPACE", "permitTypeB": "ELECTRICAL_LOTO", "minDistanceM": None, "sameArea": True,
     "severity": "high"},
]


def impairment_rule_views() -> list[ImpairmentRuleView]:
    return [ImpairmentRuleView(id=f"default-imp-{i}", **r) for i, r in enumerate(DEFAULT_IMPAIRMENT_RULES)]


def simops_rule_views() -> list[SimopsRuleView]:
    return [SimopsRuleView(id=f"default-simops-{i}", **r) for i, r in enumerate(DEFAULT_SIMOPS_RULES)]
