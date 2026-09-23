"""Where a form's data physically lives — the storage-binding seam.

Phase 1 implements exactly one binding, NATIVE (rows in `FormRecord.dataJson`).
The seam exists now because two confirmed pieces of downstream work need the
engine to sit in FRONT of a table it did not create:

  * Sustainability (Part C) — the four environmental forms bind to the existing
    `BrsrEnvironmentalMetric` / `BrsrEnvMetricLine` / `BrsrEmissionFactor`
    tables, which already capture energy/water/waste/emissions with a cited,
    validity-windowed factor lookup and per-site intensity denominators. A
    second environmental store would immediately contradict the requirement that
    BRSR's P1/P3/P5/P9 auto-population read one source of truth.
  * RCA (Part D5) — binds to ERM's existing `RootCauseAnalysis` entity, so the
    platform keeps one RCA register with two entry points.

Declaring the seam is not the same as building those bindings, and this file
does not pretend otherwise: `resolve()` raises for any kind but NATIVE. What it
buys is that adding one is a `BindingSpec` entry plus a reader/writer pair,
rather than a refactor of the record router against tables that are already
filed against.

A binding is declared on the definition:
    {"kind": "NATIVE"}
    {"kind": "ENTITY", "entity": "brsr_env_metric"}     # Phase 2
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["BindingSpec", "NATIVE", "resolve", "normalise", "BindingError"]

NATIVE = "NATIVE"
ENTITY = "ENTITY"


class BindingError(ValueError):
    """An unusable or not-yet-implemented storage binding."""


@dataclass(frozen=True)
class BindingSpec:
    kind: str
    entity: str | None = None
    options: dict[str, Any] | None = None

    @property
    def is_native(self) -> bool:
        return self.kind == NATIVE


def normalise(raw: Any) -> dict[str, Any]:
    """The stored form of a binding. A definition with no binding declared is
    NATIVE — every form authored before bindings existed keeps working."""
    if raw is None:
        return {"kind": NATIVE}
    if not isinstance(raw, dict) or not raw.get("kind"):
        raise BindingError("storageBinding must be an object with a `kind`.")
    kind = str(raw["kind"]).upper()
    if kind == NATIVE:
        return {"kind": NATIVE}
    if kind == ENTITY:
        entity = raw.get("entity")
        if not isinstance(entity, str) or not entity:
            raise BindingError("An ENTITY binding requires an `entity` name.")
        return {"kind": ENTITY, "entity": entity, "options": raw.get("options") or {}}
    raise BindingError(f"Unknown storage binding kind '{raw['kind']}'.")


def resolve(raw: Any) -> BindingSpec:
    """Resolve a stored binding to something executable, or refuse.

    Refusing loudly here is deliberate. A definition bound to an entity the
    engine cannot write yet must fail at publish and at every write — silently
    falling back to NATIVE would create precisely the duplicate data store the
    binding exists to prevent, and it would do so invisibly.
    """
    spec = normalise(raw)
    if spec["kind"] == NATIVE:
        return BindingSpec(kind=NATIVE)
    raise BindingError(
        f"Storage binding '{spec['kind']}"
        f"{':' + spec['entity'] if spec.get('entity') else ''}' is declared but not "
        "implemented in this phase. Only NATIVE storage is available."
    )
