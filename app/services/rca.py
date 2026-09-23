"""RCA helpers — port of `src/lib/rca/types.ts`. Just the bits the API
needs: method normalisation + summary generation.
"""

from __future__ import annotations

from typing import Any

# Bridge any legacy code value (5-Why / Fishbone / etc.) to canonical RcaMethod.
_NORMALISE = {
    "5-Why": "FIVE_WHY",
    "FIVE_WHY": "FIVE_WHY",
    "Fishbone": "FISHBONE",
    "FISHBONE": "FISHBONE",
    "FTA": "FTA",
    "Bowtie": "BOWTIE",
    "BOWTIE": "BOWTIE",
    "TapRoot": "TAPROOT",
    "TAPROOT": "TAPROOT",
    "Cause Map": "CAUSE_MAP",
    "CAUSE_MAP": "CAUSE_MAP",
    # The CAPA module shipped its own spelling of three of these, so a CAPA
    # saved as 5_WHY and an incident saved as FIVE_WHY were the same technique
    # under two codes -- the CAPA one matched no template and no read view.
    "5_WHY": "FIVE_WHY",
    "FAULT_TREE": "FTA",
    "TAP_ROOT": "TAPROOT",
    # NARRATIVE — structured narrative for reputational/external/strategic RCAs
    # where formal trees don't fit (added for the ERM cross-domain RCA module).
    "Narrative": "NARRATIVE",
    "NARRATIVE": "NARRATIVE",
}



# -- Defensive readers ---------------------------------------------------
# The TS original (`src/lib/rca/types.ts`) reads every field through optional
# chaining -- `w.question?.trim()`. This port dropped that, so a canvas payload
# with a null field, or a list entry that is not an object, raised
# AttributeError/TypeError *inside a request handler* and surfaced as
# "Internal server error. The team has been notified." on Save Cause Analysis.
# RCA data is free-form JSON authored by several editors (web canvas, mobile,
# AI drafts, older rows), so these readers must tolerate any shape.

_SIX_M = ("manpower", "machine", "method", "material", "measurement", "environment")


def _s(value: Any) -> str:
    """Any value -> a trimmed string. None / non-strings never raise."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _dicts(value: Any) -> list[dict]:
    """A list field -> only its dict entries."""
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, dict)]


def _strings(value: Any) -> list[str]:
    """A list field -> its non-empty values as display strings.

    Entries are sometimes plain strings and sometimes node objects (the canvas
    writes `{id, text, ...}`), so a dict is unwrapped by its label field rather
    than stringified into `{'text': ...}`.
    """
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for v in value:
        if isinstance(v, dict):
            label = next(
                (_s(v.get(k)) for k in ("text", "label", "description", "name") if _s(v.get(k))),
                "",
            )
        else:
            label = _s(v)
        if label:
            out.append(label)
    return out


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _count(value: Any) -> int:
    return len(value) if isinstance(value, (list, tuple)) else 0


def normalise_rca_method(input_str: str | None) -> str | None:
    if not input_str:
        return None
    return _NORMALISE.get(input_str.strip())


def is_empty_rca_data(method: str, data: Any) -> bool:
    if data is None or not isinstance(data, dict):
        return True
    if method == "FIVE_WHY":
        whys = _dicts(data.get("whys"))
        return (
            not _s(data.get("problemStatement"))
            and not _s(data.get("rootCause"))
            and not any(_s(w.get("question")) or _s(w.get("answer")) for w in whys)
        )
    if method == "FISHBONE":
        cats = _mapping(data.get("categories"))
        any_cause = any(_count(cats.get(k)) > 0 for k in _SIX_M)
        return (
            not _s(data.get("problemStatement"))
            and not any_cause
            and not _count(data.get("rootCauses"))
        )
    if method == "FTA":
        root = _mapping(data.get("rootNode"))
        return not _s(data.get("topEvent")) and not _count(root.get("children"))
    if method == "BOWTIE":
        return (
            not _s(data.get("topEvent"))
            and not _count(data.get("threats"))
            and not _count(data.get("consequences"))
        )
    if method == "TAPROOT":
        return (
            not _s(data.get("eventDescription"))
            and not _count(data.get("snapChart"))
            and not _count(data.get("causalFactors"))
        )
    if method == "CAUSE_MAP":
        return (
            not _s(data.get("rootEvent"))
            and not _count(data.get("impacts"))
            and not _count(data.get("causeNodes"))
        )
    if method == "NARRATIVE":
        return not _s(data.get("summary")) and not any(
            _s(f.get("description")) for f in _dicts(data.get("factors"))
        )
    return True


def generate_rca_summary(method: str | None, data: Any) -> str | None:
    """Plain-English summary used on dashboards / list views / statutory exports."""
    if not method or data is None or is_empty_rca_data(method, data):
        return None
    if not isinstance(data, dict):
        return None
    if method == "FIVE_WHY":
        whys = _dicts(data.get("whys"))
        last_answer = next((a for a in (_s(w.get("answer")) for w in reversed(whys)) if a), "")
        cause = _s(data.get("rootCause")) or last_answer
        problem = _s(data.get("problemStatement")) or "Incident"
        return f"{problem}. Root cause: {cause or '—'}."
    if method == "FISHBONE":
        roots = _strings(data.get("rootCauses"))[:2]
        cats = _mapping(data.get("categories"))
        all_count = sum(_count(cats.get(k)) for k in _SIX_M)
        problem = _s(data.get("problemStatement")) or "Incident"
        suffix = f" Root cause(s): {'; '.join(roots)}." if roots else ""
        return f"{problem}. {all_count} contributing factor(s) identified across 6M categories.{suffix}"
    if method == "FTA":
        return f"{_s(data.get('topEvent')) or 'Top event'}."
    if method == "BOWTIE":
        return (
            f"{_s(data.get('topEvent')) or 'Top event'}. "
            f"{_count(data.get('threats'))} threat(s), "
            f"{_count(data.get('consequences'))} consequence(s)."
        )
    if method == "TAPROOT":
        cfs = [c for c in (_s(cf.get("description")) for cf in _dicts(data.get("causalFactors"))) if c][:3]
        cf_text = f" Top: {'; '.join(cfs)}." if cfs else ""
        return (
            f"{_s(data.get('eventDescription')) or 'Event'}. "
            f"{_count(data.get('causalFactors'))} causal factor(s) identified.{cf_text}"
        )
    if method == "CAUSE_MAP":
        impacts = ", ".join(_strings(data.get("impacts"))) or "—"
        return (
            f"{_s(data.get('rootEvent')) or 'Event'}. Impacts: {impacts}. "
            f"{_count(data.get('causeNodes'))} cause node(s) mapped."
        )
    if method == "NARRATIVE":
        n = len([d for d in (_s(f.get("description")) for f in _dicts(data.get("factors"))) if d])
        summary = _s(data.get("summary")) or "Causal narrative"
        return f"{summary} {n} contributing factor(s) identified." if n else summary
    return None
