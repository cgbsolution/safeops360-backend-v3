"""SIMOPS (simultaneous operations) detection — pure.

Two permits conflict when their validity windows overlap, a SimopsRule matches
their type pair, and either
  • the rule says same_area and both are in the same area, or
  • the rule has min_distance_m and the distance between their areas is within it.

Distance is the distance between zone polygon centroids, scaled by the plant
layout's real-world size (PlantLayout.widthM × heightM — the image's extents in
metres; polygons are stored in % of the image). Two permits in the same area
are 0 m apart. With no layout scale, only same-area rules can fire.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.services.ptw_ops.derived import is_open
from app.services.ptw_ops.types import LayoutView, PermitView, SimopsRuleView, ZoneView

# Permits that may have people on the job (or about to): approved through suspended.
SIMOPS_STATUSES = frozenset({"APPROVED", "ISSUED", "ACTIVE", "SUSPENDED"})
SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def centroid(polygon: list[dict[str, float]]) -> tuple[float, float] | None:
    """Area-weighted polygon centroid in the polygon's own units (%). Falls back
    to the vertex mean for degenerate polygons."""
    pts = [(float(p["x"]), float(p["y"])) for p in polygon or [] if "x" in p and "y" in p]
    if not pts:
        return None
    if len(pts) < 3:
        return (sum(x for x, _ in pts) / len(pts), sum(y for _, y in pts) / len(pts))
    a = cx = cy = 0.0
    for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1], strict=True):
        cross = x0 * y1 - x1 * y0
        a += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(a) < 1e-9:
        return (sum(x for x, _ in pts) / len(pts), sum(y for _, y in pts) / len(pts))
    a *= 0.5
    return (cx / (6 * a), cy / (6 * a))


def zone_distance_m(a: ZoneView | None, b: ZoneView | None, layout: LayoutView) -> float | None:
    if a is None or b is None:
        return None
    if a.id == b.id:
        return 0.0
    if not layout.widthM or not layout.heightM:
        return None
    ca, cb = centroid(a.polygon), centroid(b.polygon)
    if ca is None or cb is None:
        return None
    dx = (ca[0] - cb[0]) / 100.0 * layout.widthM
    dy = (ca[1] - cb[1]) / 100.0 * layout.heightM
    return math.hypot(dx, dy)


def match_rule(rules: list[SimopsRuleView], ta: str, tb: str, plant_id: str | None) -> list[SimopsRuleView]:
    """Every rule that covers the unordered pair (ta, tb); plant rules before global."""
    out = []
    for r in rules:
        if r.plantId is not None and r.plantId != plant_id:
            continue
        fwd = r.permitTypeA in (ta, "*") and r.permitTypeB in (tb, "*")
        rev = r.permitTypeA in (tb, "*") and r.permitTypeB in (ta, "*")
        if fwd or rev:
            out.append(r)
    out.sort(key=lambda r: (r.plantId is None, SEVERITY_RANK.get(r.severity, 9)))
    return out


def windows_overlap(a: PermitView, b: PermitView) -> bool:
    return a.validFrom < b.validTo and b.validFrom < a.validTo


@dataclass
class Conflict:
    permitIds: tuple[str, str]
    numbers: tuple[str, str]
    types: tuple[str, str]
    areaIds: list[str]
    distanceM: float | None
    severity: str
    ruleId: str
    sameArea: bool
    label: str = field(default="")

    @property
    def key(self) -> str:
        return ":".join(sorted(self.permitIds))


def _label(distance: float | None, same_area: bool) -> str:
    if same_area:
        return "SIMOPS · same area"
    if distance is None:
        return "SIMOPS"
    return f"SIMOPS · {round(distance)} m"


def detect(
    permits: list[PermitView],
    rules: list[SimopsRuleView],
    zones: list[ZoneView],
    layout: LayoutView,
    plant_id: str | None,
) -> list[Conflict]:
    live = [p for p in permits if p.status in SIMOPS_STATUSES and is_open(p) and p.areaId]
    zmap = {z.id: z for z in zones}
    out: list[Conflict] = []
    for i, a in enumerate(live):
        for b in live[i + 1:]:
            if not windows_overlap(a, b):
                continue
            same = a.areaId == b.areaId
            dist = 0.0 if same else zone_distance_m(zmap.get(a.areaId), zmap.get(b.areaId), layout)
            for r in match_rule(rules, a.type, b.type, plant_id):
                hit = (r.sameArea and same) or (
                    r.minDistanceM is not None and dist is not None and dist <= r.minDistanceM
                )
                if hit:
                    out.append(Conflict(
                        permitIds=(a.id, b.id), numbers=(a.number, b.number), types=(a.type, b.type),
                        areaIds=sorted({a.areaId, b.areaId}), distanceM=None if dist is None else round(dist, 1),
                        severity=r.severity, ruleId=r.id, sameArea=same, label=_label(dist, same),
                    ))
                    break
    out.sort(key=lambda c: (SEVERITY_RANK.get(c.severity, 9), c.distanceM if c.distanceM is not None else 1e9))
    return out
