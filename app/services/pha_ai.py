"""AI assist for a HAZOP node: suggest worksheet rows. Soft-fail, never saves.

`suggest_for_node()` asks the model (through the platform's single seam,
services/ai/anthropic_client.complete_json) for deviations with causes,
consequences and existing safeguards for one node, given its design intent and
operating parameters. The team accepts or rejects each suggestion in the UI;
an accepted suggestion is posted back as an ordinary row (aiSuggested=true).

`parse_suggestions()` is the trust boundary and is pure (tests/test_hazop.py):
guidewords and parameters are mapped onto the library or dropped, text is
trimmed, ratings are clamped to the study matrix, and rows the node already
has are skipped. Nothing the model returns reaches the database unreviewed.

Model name, latency and token use are logged server-side only, never returned.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable

from app.services import pha_library as lib

log = logging.getLogger(__name__)

MAX_SUGGESTIONS = 12
_MAX_TEXT = 1200

_SYSTEM = (
    "You are an experienced HAZOP facilitator for an integrated stainless steel plant "
    "(melt shop with EAF and AOD converters, ladle furnace, continuous caster, reheating furnaces, "
    "hot and cold rolling mills, annealing and pickling lines, utilities). You know the process "
    "hazards of this industry: reheating and annealing furnace fuel gas (natural gas, LPG / propane, "
    "mixed gas) including purge, flame failure and burner management; oxygen lancing and oxygen-enriched "
    "fires; nitrogen and argon asphyxiation; high-pressure hydraulic systems and hydraulic oil fires; "
    "cooling water failure on furnace panels, moulds and lances (water-molten metal steam explosions); "
    "molten metal and slag handling with ladles and cranes; mixed-acid (HNO3 / HF / H2SO4) pickling lines, "
    "NOx fumes and acid regeneration; hydrogen in bright annealing; dust and fume extraction; "
    "electrical and power failure. "
    "Given one HAZOP node (design intent, boundaries, operating parameters) propose credible deviations "
    "using standard guidewords (No, More, Less, As well as, Part of, Reverse, Other than, Early, Late, "
    "Before, After) applied to parameters (Flow, Pressure, Temperature, Level, Composition, Reaction, "
    "Phase, Time, Sequence, Mixing, Utilities, Maintenance, Start-up/Shutdown). For each: specific causes "
    "(equipment failure, control failure, human error), the worst credible consequence assuming safeguards "
    "fail, and existing safeguards typically present (alarms, trips, interlocks, relief, procedures). "
    "Rate severity and likelihood on the scales given, BEFORE recommendations. Do not invent plant tag "
    "numbers. Be concise and concrete. Respond ONLY as JSON: "
    '{"suggestions": [{"guideword": "...", "parameter": "...", "deviation": "...", "causes": "...", '
    '"consequences": "...", "safeguards": "...", "severity": 1, "likelihood": 1, "recommendation": "..."}]}'
)


def _clip(v: Any, n: int = _MAX_TEXT) -> str | None:
    if v is None:
        return None
    if isinstance(v, list):
        v = "; ".join(str(x).strip() for x in v if str(x).strip())
    s = " ".join(str(v).split())
    return s[:n] if s else None


def _clamp_int(v: Any, hi: int) -> int | None:
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return None
    return max(1, min(hi, n))


def build_prompt(*, study: dict[str, Any], node: dict[str, Any], matrix: dict[str, Any], existing: Iterable[str]) -> str:
    params = node.get("operatingParameters") or []
    plines = "\n".join(
        f"  - {p.get('name', '')}: {p.get('value', '')} {p.get('unit', '') or ''}".rstrip()
        for p in params if isinstance(p, dict)
    ) or "  (none recorded)"
    sev = "; ".join(f"{s['score']}={s['label']}" for s in matrix.get("severities", [])) or f"1..{matrix.get('severityLevels', 5)}"
    lik = "; ".join(f"{s['score']}={s['label']}" for s in matrix.get("likelihoods", [])) or f"1..{matrix.get('likelihoodLevels', 5)}"
    have = "\n".join(f"  - {e}" for e in existing) or "  (none yet)"
    return (
        f"Study: {study.get('title')} ({study.get('method')}), unit {study.get('unit') or '-'}, area {study.get('area') or '-'}\n"
        f"Study scope: {study.get('scope') or '-'}\n"
        f"Node {node.get('nodeNo')}: {node.get('title')}\n"
        f"Description: {node.get('description') or '-'}\n"
        f"Design intent: {node.get('designIntent') or '-'}\n"
        f"Boundaries: {node.get('boundaries') or '-'}\n"
        f"Drawing: {node.get('drawingRef') or '-'}\n"
        f"Operating parameters:\n{plines}\n"
        f"Severity scale: {sev}\nLikelihood scale: {lik}\n"
        f"Deviations already on the worksheet (do not repeat):\n{have}\n"
        f"Suggest up to {MAX_SUGGESTIONS} of the most significant additional deviations."
    )


def parse_suggestions(
    raw: Any, *, severity_levels: int, likelihood_levels: int, existing: Iterable[tuple[str | None, str | None, str]] = (),
) -> list[dict[str, Any]]:
    """Validate and clamp model output into worksheet-row candidates.

    existing: (guideword, parameter, deviation text) already on the node.
    A suggestion is dropped when it has no deviation text, or its guideword or
    parameter is not in the library, or the node already has that pair / text.
    """
    items = raw.get("suggestions") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    have_pairs = {(g, p) for g, p, _ in existing if g and p}
    have_text = {" ".join((t or "").lower().split()) for _, _, t in existing}
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        gw = lib.normalise_guideword(it.get("guideword"))
        param = lib.normalise_parameter(it.get("parameter"))
        text = _clip(it.get("deviation"), 300)
        if not (gw and param and text):
            continue
        key_text = " ".join(text.lower().split())
        if (gw, param) in have_pairs or key_text in have_text or (gw, param) in seen:
            continue
        seen.add((gw, param))
        out.append({
            "guideword": gw, "parameter": param, "deviation": text,
            "causes": _clip(it.get("causes")), "consequences": _clip(it.get("consequences")),
            "safeguards": _clip(it.get("safeguards")),
            "severity": _clamp_int(it.get("severity"), severity_levels),
            "likelihood": _clamp_int(it.get("likelihood"), likelihood_levels),
            "recommendation": _clip(it.get("recommendation"), 600),
            "libraryMatch": lib.is_meaningful(gw, param),
        })
        if len(out) >= MAX_SUGGESTIONS:
            break
    return out


async def suggest_for_node(
    *, study: dict[str, Any], node: dict[str, Any], matrix: dict[str, Any],
    existing: list[tuple[str | None, str | None, str]],
) -> list[dict[str, Any]] | None:
    """Suggestions, or None when AI is unavailable or the call failed."""
    from app.services.ai.anthropic_client import complete_json, is_configured

    if not is_configured():
        return None
    prompt = build_prompt(study=study, node=node, matrix=matrix, existing=[t for _, _, t in existing])
    started = time.monotonic()
    raw = await complete_json(system=_SYSTEM, user=prompt, max_tokens=3000)
    log.info("pha.ai_suggest node=%s ok=%s ms=%d", node.get("id"), raw is not None, int((time.monotonic() - started) * 1000))
    if raw is None:
        return None
    return parse_suggestions(
        raw, severity_levels=int(matrix.get("severityLevels") or 5),
        likelihood_levels=int(matrix.get("likelihoodLevels") or 5), existing=existing,
    )
