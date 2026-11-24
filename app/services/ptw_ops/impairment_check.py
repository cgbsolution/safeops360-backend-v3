"""Impairment conflict check — pure.

Given a permit type, the impairments in force in the permit's area and the
ImpairmentRule table, decide:

  allow             nothing in force restricts this permit type
  require_controls  the permit may proceed once every required control is
                    ticked/attached (PermitControl rows); it cannot pass Issuer
                    Review before that
  block_new         a NEW permit of this type may not be requested or approved

Rule resolution per (impairment, permit type): plant-specific rules beat
global ones (plantId NULL), and an exact permit-type rule beats "*". So the
seeded "fire_alarm × * = require_controls" plus "fire_alarm × HOT_WORK =
block_new" gives hot work a block and everything else the controls.

Running permits (already issued when the impairment came into force) are not
blocked retroactively — the block_new rule's requiredControls become the
controls they must now carry (e.g. fire watch + Fire Officer co-sign).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.services.ptw_ops.types import ImpairmentRuleView, ImpairmentView

LOCAL_TZ = timezone(timedelta(minutes=int(os.getenv("PTW_CC_TZ_OFFSET_MIN", "330"))))

EFFECT_RANK = {"allow": 0, "require_controls": 1, "block_new": 2}

SYSTEM_LABELS = {
    "sprinkler": "Sprinkler",
    "fire_detection": "Fire detection",
    "fire_alarm": "Fire alarm",
    "gas_detection": "Gas detection",
    "interlock": "Interlock",
    "emergency_lighting": "Emergency lighting",
    "other": "Safety system",
}
SYSTEM_TYPES = tuple(SYSTEM_LABELS)

CONTROL_LABELS = {
    "fire_watch": "Fire watch posted",
    "fire_officer_cosign": "Fire Officer co-sign",
    "continuous_gas_monitor": "Continuous portable gas monitor",
    "attendant": "Attendant at entry",
    "hourly_patrol": "Hourly fire patrol",
    "no_unattended_work": "No unattended work",
}

PERMIT_TYPE_LABELS = {
    "HOT_WORK": "hot work",
    "CONFINED_SPACE": "confined-space entry",
    "WORK_AT_HEIGHT": "work at height",
    "EXCAVATION": "excavation",
    "ELECTRICAL_LOTO": "electrical work",
    "LIFTING": "lifting",
    "GENERAL_COLD": "cold work",
}


def control_label(key: str) -> str:
    return CONTROL_LABELS.get(key, key.replace("_", " ").capitalize())


def type_label(t: str) -> str:
    return PERMIT_TYPE_LABELS.get(t, t.replace("_", " ").lower())


def local_hhmm(d: datetime | None) -> str:
    return d.astimezone(LOCAL_TZ).strftime("%H:%M") if d else "further notice"


@dataclass
class Hit:
    impairment: ImpairmentView
    rule: ImpairmentRuleView


@dataclass
class Decision:
    effect: str = "allow"
    blockingMessages: list[str] = field(default_factory=list)
    requiredControls: list[str] = field(default_factory=list)   # ordered, de-duplicated
    missingControls: list[str] = field(default_factory=list)
    hits: list[Hit] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.effect == "block_new"

    @property
    def impairment_codes(self) -> list[str]:
        return sorted({h.impairment.code for h in self.hits})


def resolve_rule(
    rules: list[ImpairmentRuleView], system_type: str, permit_type: str, plant_id: str | None
) -> ImpairmentRuleView | None:
    """Most specific rule for (system, permit type): plant > global, exact > '*'."""
    best: tuple[int, ImpairmentRuleView] | None = None
    for r in rules:
        if r.systemType != system_type:
            continue
        if r.permitType not in (permit_type, "*"):
            continue
        if r.plantId is not None and r.plantId != plant_id:
            continue
        score = (2 if r.plantId is not None else 0) + (1 if r.permitType == permit_type else 0)
        if best is None or score > best[0]:
            best = (score, r)
    return best[1] if best else None


def block_message(imp: ImpairmentView, rule: ImpairmentRuleView, permit_type: str) -> str:
    if rule.message:
        return (
            rule.message.replace("{code}", imp.code)
            .replace("{system}", imp.systemName)
            .replace("{until}", local_hhmm(imp.plannedRestoreAt))
        )
    return (
        f"{SYSTEM_LABELS.get(imp.systemType, 'System')} {imp.code} impaired until "
        f"{local_hhmm(imp.plannedRestoreAt)} — new {type_label(permit_type)} blocked"
    )


def check(
    *,
    permit_type: str,
    area_id: str | None,
    plant_id: str | None,
    impairments: list[ImpairmentView],
    rules: list[ImpairmentRuleView],
    now: datetime,
    satisfied: set[str] | None = None,
    running: bool = False,
) -> Decision:
    """Decision for one permit. `running=True` for a permit already issued/active
    (block_new turns into its requiredControls instead of a block)."""
    satisfied = satisfied or set()
    d = Decision()
    if not area_id:
        return d
    for imp in impairments:
        if imp.areaId != area_id or not imp.is_in_force(now):
            continue
        rule = resolve_rule(rules, imp.systemType, permit_type, plant_id)
        if rule is None or rule.effect == "allow":
            continue
        d.hits.append(Hit(imp, rule))
        if rule.effect == "block_new" and not running:
            d.blockingMessages.append(block_message(imp, rule, permit_type))
            d.effect = "block_new"
        else:
            for c in rule.requiredControls:
                if c not in d.requiredControls:
                    d.requiredControls.append(c)
            if EFFECT_RANK[d.effect] < EFFECT_RANK["require_controls"]:
                d.effect = "require_controls"
    d.missingControls = [c for c in d.requiredControls if c not in satisfied]
    return d


def effect_summary(
    imp: ImpairmentView, rules: list[ImpairmentRuleView], plant_id: str | None
) -> tuple[str, str]:
    """One line for the impairments table: what this impairment does to permits,
    plus a severity (critical | warning | info) for its colour."""
    mine = [r for r in rules if r.systemType == imp.systemType and (r.plantId in (None, plant_id))]
    blocked = sorted({r.permitType for r in mine if r.effect == "block_new"})
    controls: list[str] = []
    for r in mine:
        if r.effect == "require_controls":
            for c in r.requiredControls:
                if c not in controls:
                    controls.append(c)
    parts = []
    if blocked:
        names = ", ".join(type_label(t) if t != "*" else "all work" for t in blocked)
        parts.append(f"New {names} blocked")
    if controls:
        parts.append("Controls: " + ", ".join(control_label(c).lower() for c in controls))
    if not parts:
        return "No permit restrictions", "info"
    return " · ".join(parts), ("critical" if blocked else "warning")


def rule_cards(rules: list[ImpairmentRuleView]) -> list[dict[str, str]]:
    """The 'conflict rules checked on every permit request' cards, from data."""
    by_system: dict[str, list[ImpairmentRuleView]] = {}
    for r in rules:
        if r.effect != "allow":
            by_system.setdefault(r.systemType, []).append(r)
    cards = []
    for system, rs in by_system.items():
        for r in sorted(rs, key=lambda x: (x.permitType == "*", x.permitType)):
            who = "any permit" if r.permitType == "*" else type_label(r.permitType)
            when = f"{SYSTEM_LABELS.get(system, system)} impaired + {who} requested"
            if r.effect == "block_new":
                then = "Block new"
                if r.requiredControls:
                    then += " · running permits need " + " + ".join(control_label(c) for c in r.requiredControls)
            else:
                then = "Require " + " + ".join(control_label(c) for c in r.requiredControls) if r.requiredControls else "Require controls"
            cards.append({"when": when, "then": then, "effect": r.effect})
    return cards
