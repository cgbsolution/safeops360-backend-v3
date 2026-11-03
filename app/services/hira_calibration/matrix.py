"""Matrix adapter (HIRA calibration round 3, L).

Every study matrix is described by one `MatrixConfig`: its dimensions (3x3 up
to 6x6), level labels, an explicit cell -> band map (cells may differ from a
pure score threshold: L2 x S2 can be MODERATE in one matrix and LOW in
another), the band order, tolerability rules, the ALARP toggle, the override
roles and the scoring mode.

The engine (anchors, evidence cap, control credit) works on a normalised
5-point scale. `likelihood_map` / `severity_map` take a native level to that
scale; bands are always read from the native cell, so a matrix's own cell map
decides the level. For the standard 5x5 and the JSL plant-index matrix the maps
are the identity and the cells follow the agreed thresholds, so behaviour is
exactly what it was before the adapter existed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

LEVELS = ("LOW", "MODERATE", "HIGH", "CRITICAL")
SCORING_MODES = ("product", "plantIndex", "custom")
DEFAULT_OVERRIDE_ROLES = ("PLANT_HEAD", "CORPORATE_HSE")


def linear_map(levels: int) -> dict[int, int]:
    """Native level -> normalised 1..5, spread evenly (3 levels -> 1, 3, 5)."""
    if levels == 5:
        return {n: n for n in range(1, 6)}
    if levels < 2:
        raise ValueError("a matrix needs at least 2 levels per axis")
    return {n: int(round(1 + (n - 1) * 4 / (levels - 1))) for n in range(1, levels + 1)}


def band_by_thresholds(score: int, thresholds: dict[str, int]) -> str:
    if score <= thresholds["lowMax"]:
        return "LOW"
    if score <= thresholds["moderateMax"]:
        return "MODERATE"
    if score <= thresholds["highMax"]:
        return "HIGH"
    return "CRITICAL"


@dataclass(frozen=True)
class MatrixConfig:
    code: str
    likelihood_levels: int
    severity_levels: int
    cells: dict[tuple[int, int], str]  # native (L, S) -> band
    scoring_mode: str = "product"
    likelihood_map: dict[int, int] = field(default_factory=dict)  # native -> normalised
    severity_map: dict[int, int] = field(default_factory=dict)
    band_order: tuple[str, ...] = LEVELS
    likelihood_labels: dict[int, str] = field(default_factory=dict)
    severity_labels: dict[int, str] = field(default_factory=dict)
    cell_scores: dict[tuple[int, int], int] = field(default_factory=dict)  # custom scoring only
    alarp_enabled: bool = True
    override_roles: tuple[str, ...] = DEFAULT_OVERRIDE_ROLES
    tolerability: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not (3 <= self.likelihood_levels <= 6 and 3 <= self.severity_levels <= 6):
            raise ValueError("matrices from 3x3 up to 6x6 are supported")
        if self.scoring_mode not in SCORING_MODES:
            raise ValueError(f"scoring_mode must be one of {SCORING_MODES}")
        missing = [
            (l, s)
            for l in range(1, self.likelihood_levels + 1)
            for s in range(1, self.severity_levels + 1)
            if (l, s) not in self.cells
        ]
        if missing:
            raise ValueError(f"cell map is missing {missing[:3]}...")
        bad = {b for b in self.cells.values() if b not in self.band_order}
        if bad:
            raise ValueError(f"unknown bands in the cell map: {sorted(bad)}")

    # ── scale mapping ──
    @property
    def identity(self) -> bool:
        return (
            self.likelihood_levels == 5
            and self.severity_levels == 5
            and self.lmap == linear_map(5)
            and self.smap == linear_map(5)
        )

    @property
    def lmap(self) -> dict[int, int]:
        return self.likelihood_map or linear_map(self.likelihood_levels)

    @property
    def smap(self) -> dict[int, int]:
        return self.severity_map or linear_map(self.severity_levels)

    @staticmethod
    def _to_native(value: int, mapping: dict[int, int]) -> int:
        """Native level for a normalised value, never rounding a risk down: an
        exact match takes the highest native level with that value (a 6-level
        axis folds two levels into one), otherwise the next level up."""
        exact = [n for n in mapping if mapping[n] == value]
        if exact:
            return max(exact)
        for native in sorted(mapping):
            if mapping[native] > value:
                return native
        return max(mapping)

    def native_l(self, l_norm: int) -> int:
        return self._to_native(int(l_norm), self.lmap)

    def native_s(self, s_norm: int) -> int:
        return self._to_native(int(s_norm), self.smap)

    def norm_l(self, l_native: int) -> int:
        return self.lmap[int(l_native)]

    def norm_s(self, s_native: int) -> int:
        return self.smap[int(s_native)]

    # ── rating ──
    def rate(self, l_norm: int, s_norm: int) -> dict[str, Any]:
        """Rating for a normalised (L, S): the band comes from the native cell.
        On an identity 5x5 the result is exactly {likelihood, severity, score, level}."""
        if not (1 <= int(l_norm) <= 5 and 1 <= int(s_norm) <= 5):
            raise ValueError(f"likelihood and severity must be 1-5 (got L{l_norm} S{s_norm})")
        nl, ns = self.native_l(l_norm), self.native_s(s_norm)
        level = self.cells[(nl, ns)]
        if self.identity:
            return {
                "likelihood": int(l_norm),
                "severity": int(s_norm),
                "score": int(l_norm) * int(s_norm),
                "level": level,
            }
        score = self.cell_scores.get((nl, ns), nl * ns) if self.scoring_mode == "custom" else nl * ns
        return {
            "likelihood": int(l_norm),
            "severity": int(s_norm),
            "score": score,
            "level": level,
            "native": {
                "likelihood": nl,
                "severity": ns,
                "matrix": self.code,
                "dimensions": f"{self.likelihood_levels}x{self.severity_levels}",
            },
        }

    def level_index(self, level: str | None) -> int:
        return self.band_order.index(level) if level in self.band_order else -1


# ── builders ──────────────────────────────────────────────────────────────


def from_thresholds(
    code: str,
    thresholds: dict[str, int],
    *,
    scoring_mode: str = "product",
    tolerability: dict[str, Any] | None = None,
    alarp_enabled: bool = True,
) -> MatrixConfig:
    """A 5x5 whose cells follow the score thresholds (the agreed band set)."""
    cells = {(l, s): band_by_thresholds(l * s, thresholds) for l in range(1, 6) for s in range(1, 6)}
    return MatrixConfig(
        code=code,
        likelihood_levels=5,
        severity_levels=5,
        cells=cells,
        scoring_mode=scoring_mode,
        tolerability=dict(tolerability or {}),
        alarp_enabled=alarp_enabled,
    )


def builtin(code: str | None, cfg: dict[str, Any]) -> MatrixConfig:
    """STD_5X5 and JSL_QSD24_GT as configs; any other code falls back to a 5x5
    on the agreed thresholds (the behaviour before the adapter)."""
    th = cfg["thresholds"]
    if code == "JSL_QSD24_GT":
        return from_thresholds(
            "JSL_QSD24_GT",
            th,
            scoring_mode="plantIndex",
            tolerability={"moderateNotAcceptable": True},
            alarp_enabled=cfg["tolerability"]["alarpBandEnabled"],
        )
    return from_thresholds(code or "STD_5X5", th, alarp_enabled=cfg["tolerability"]["alarpBandEnabled"])


def from_db(
    matrix: Any,
    cells: list[Any],
    likelihoods: list[Any],
    severities: list[Any],
    *,
    scoring_mode: str = "product",
) -> MatrixConfig:
    """A study matrix as stored (RiskMatrix + RiskMatrixCell rows), with its own cell map."""
    lmax = int(
        getattr(matrix, "likelihoodLevels", None) or max((c.likelihoodScore for c in cells), default=5)
    )
    smax = int(getattr(matrix, "severityLevels", None) or max((c.severityScore for c in cells), default=5))
    return MatrixConfig(
        code=getattr(matrix, "code", None) or "MATRIX",
        likelihood_levels=lmax,
        severity_levels=smax,
        cells={(int(c.likelihoodScore), int(c.severityScore)): c.riskLevel for c in cells},
        cell_scores={(int(c.likelihoodScore), int(c.severityScore)): int(c.riskScore) for c in cells},
        scoring_mode=scoring_mode,
        likelihood_labels={int(x.score): x.label for x in likelihoods},
        severity_labels={int(x.score): x.label for x in severities},
    )


def from_dict(d: dict[str, Any]) -> MatrixConfig:
    """A matrix config written as JSON (plant config / tests): cells as
    {"L,S": band} or a list of {"l", "s", "band"}."""
    raw = d.get("cells") or {}
    if isinstance(raw, dict):
        cells = {tuple(int(x) for x in k.split(",")): v for k, v in raw.items()}
    else:
        cells = {(int(c["l"]), int(c["s"])): c["band"] for c in raw}
    if not cells and d.get("thresholds"):
        lv, sv = int(d.get("likelihoodLevels", 5)), int(d.get("severityLevels", 5))
        cells = {
            (l, s): band_by_thresholds(l * s, d["thresholds"])
            for l in range(1, lv + 1)
            for s in range(1, sv + 1)
        }
    for k, v in (d.get("cellOverrides") or {}).items():
        cells[tuple(int(x) for x in k.split(","))] = v
    return MatrixConfig(
        code=d.get("code", "CUSTOM"),
        likelihood_levels=int(d.get("likelihoodLevels", 5)),
        severity_levels=int(d.get("severityLevels", 5)),
        cells=cells,
        scoring_mode=d.get("scoringMode", "product"),
        likelihood_map={int(k): int(v) for k, v in (d.get("likelihoodMap") or {}).items()},
        severity_map={int(k): int(v) for k, v in (d.get("severityMap") or {}).items()},
        band_order=tuple(d.get("bandOrder") or LEVELS),
        alarp_enabled=bool(d.get("alarpEnabled", True)),
        override_roles=tuple(d.get("overrideRoles") or DEFAULT_OVERRIDE_ROLES),
        tolerability=dict(d.get("tolerability") or {}),
    )


__all__ = [
    "LEVELS",
    "MatrixConfig",
    "band_by_thresholds",
    "builtin",
    "from_db",
    "from_dict",
    "from_thresholds",
    "linear_map",
]
