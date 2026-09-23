"""Record validation against a PINNED definition version (§3.3).

Three rules the spec calls out, and the reason each is server-side:

  1. A record is always validated against the version it was created under.
     The caller never passes a schema — it is read from the pinned
     `FormRecord.formVersion`, so publishing v3 cannot retroactively invalidate
     a v2 record that is mid-approval.

  2. `visible_if` is evaluated HERE, not just in the browser. A hidden field is
     not required. If the client alone decided visibility, a required field
     could be satisfied by simply not rendering it.

  3. Calculated fields are computed here and the client's values for them are
     discarded. `evaluate()` returns (clean_data, computed) — two separate
     dicts — so a caller cannot accidentally merge a forged computed value back
     into the stored data.

Validation is also the reason drafts are allowed to be incomplete: `require_all`
is False while a record is a DRAFT and True at submit. A field-level error is
raised as a 422 with the field key, so the UI can anchor the message.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.services.form_engine.formula import evaluate as eval_formula
from app.services.form_engine.schema import (
    LIST_TYPES,
    OPTION_TYPES,
    TABLE_TYPES,
    calculation_order,
    field_map,
    iter_fields,
)

__all__ = ["ValidationError", "evaluate", "visible_fields"]


class ValidationError(ValueError):
    """Field-keyed validation failure. `errors` is {field_key: message}."""

    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))


# ── conditional visibility ──────────────────────────────────────────────────


def _rule_holds(rule: dict, values: dict[str, Any]) -> bool:
    actual = values.get(rule.get("field"))
    op = rule.get("operator")
    expected = rule.get("value")

    if op == "empty":
        return actual is None or actual == "" or actual == []
    if op == "not_empty":
        return not (actual is None or actual == "" or actual == [])
    if op == "=":
        return actual == expected
    if op == "!=":
        return actual != expected
    if op in ("in", "not_in"):
        items = expected if isinstance(expected, list) else [
            v.strip() for v in str(expected or "").split(",")
        ]
        return (actual in items) if op == "in" else (actual not in items)
    try:
        a, e = float(actual), float(expected)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if op == ">":
        return a > e
    if op == "<":
        return a < e
    if op == ">=":
        return a >= e
    if op == "<=":
        return a <= e
    return False


def _condition_holds(cond: Any, values: dict[str, Any]) -> bool:
    if not isinstance(cond, dict):
        return True
    if "rules" in cond:
        rules = cond.get("rules") or []
        results = [_condition_holds(r, values) for r in rules]
        if not results:
            return True
        return any(results) if cond.get("combinator") == "OR" else all(results)
    return _rule_holds(cond, values)


def visible_fields(schema: list[Any] | None, values: dict[str, Any]) -> set[str]:
    """Keys of every field currently visible.

    A field inside a hidden section is itself hidden — otherwise hiding a
    section would still leave its children required, which is never what the
    author meant.
    """
    visible: set[str] = set()

    def walk(items: Any, parent_shown: bool) -> None:
        if not isinstance(items, list):
            return
        for f in items:
            if not isinstance(f, dict) or not isinstance(f.get("key"), str):
                continue
            shown = parent_shown and _condition_holds(f.get("visible_if"), values)
            if shown:
                visible.add(f["key"])
            if f.get("type") == "section":
                walk(f.get("fields"), shown)

    walk(schema, True)
    return visible


# ── per-type coercion ───────────────────────────────────────────────────────


def _is_blank(v: Any) -> bool:
    return v is None or v == "" or v == []


def _coerce_scalar(field: dict, value: Any) -> Any:
    """Return the stored form of `value`, or raise ValueError with a message."""
    ftype = field.get("type")

    if ftype in ("number", "decimal"):
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ValueError("must be a number") from None

    if ftype == "integer":
        try:
            f = float(value)
        except (TypeError, ValueError):
            raise ValueError("must be a whole number") from None
        if f != int(f):
            raise ValueError("must be a whole number")
        return int(f)

    if ftype in ("boolean", "checkbox") and not isinstance(value, list):
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        raise ValueError("must be true or false")

    if ftype in ("date", "datetime"):
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        try:
            # Accept a trailing Z, which fromisoformat rejects before 3.11.
            datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"must be an ISO {ftype}") from None
        return str(value)

    if ftype in ("text", "textarea", "richtext", "user", "orgunit", "file", "signature"):
        if not isinstance(value, str):
            raise ValueError("must be text")
        return value

    return value


def _check_options(field: dict, value: Any) -> None:
    opts = field.get("options") or []
    allowed = {str(o.get("value") if isinstance(o, dict) else o) for o in opts}
    given = value if isinstance(value, list) else [value]
    for g in given:
        if str(g) not in allowed:
            raise ValueError(f"'{g}' is not one of the allowed options")


def _check_bounds(field: dict, value: Any) -> None:
    rules = field.get("validation") or {}
    if not isinstance(rules, dict):
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        lo, hi = rules.get("min"), rules.get("max")
        if lo is not None and value < lo:
            raise ValueError(f"must be at least {lo}")
        if hi is not None and value > hi:
            raise ValueError(f"must be at most {hi}")
    if isinstance(value, str):
        lo, hi = rules.get("minLength"), rules.get("maxLength")
        if lo is not None and len(value) < lo:
            raise ValueError(f"must be at least {lo} characters")
        if hi is not None and len(value) > hi:
            raise ValueError(f"must be at most {hi} characters")
    if isinstance(value, list):
        lo, hi = rules.get("minItems"), rules.get("maxItems")
        if lo is not None and len(value) < lo:
            raise ValueError(f"needs at least {lo} row(s)")
        if hi is not None and len(value) > hi:
            raise ValueError(f"allows at most {hi} row(s)")


def _coerce_table(field: dict, value: Any, errors: dict[str, str]) -> list[dict]:
    key = field["key"]
    if not isinstance(value, list):
        errors[key] = "must be a list of rows"
        return []
    columns = {c["key"]: c for c in (field.get("columns") or []) if isinstance(c, dict) and c.get("key")}
    rows: list[dict] = []
    for i, raw in enumerate(value):
        if not isinstance(raw, dict):
            errors[f"{key}[{i}]"] = "each row must be an object"
            continue
        row: dict[str, Any] = {}
        for ck, col in columns.items():
            cv = raw.get(ck)
            if _is_blank(cv):
                if col.get("required"):
                    errors[f"{key}[{i}].{ck}"] = "is required"
                row[ck] = None
                continue
            try:
                if col.get("type") in OPTION_TYPES:
                    _check_options(col, cv)
                    row[ck] = cv
                elif col.get("type") in LIST_TYPES:
                    row[ck] = list(cv) if isinstance(cv, list) else [cv]
                else:
                    row[ck] = _coerce_scalar(col, cv)
                _check_bounds(col, row[ck])
            except ValueError as e:
                errors[f"{key}[{i}].{ck}"] = str(e)
        # Unknown columns are dropped rather than rejected: a v2 definition that
        # removed a column must still accept a v2 client's stale payload without
        # the whole record failing to save.
        rows.append(row)
    return rows


# ── the entry point ─────────────────────────────────────────────────────────


def evaluate(
    schema: list[Any] | None,
    data: dict[str, Any] | None,
    *,
    require_all: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate `data` against `schema`.

    Returns `(clean_data, computed)`. `clean_data` holds only client-owned
    fields; `computed` holds only server-derived ones. They are never merged
    here — that is the whole point of the split.

    `require_all=False` (draft) skips required-field enforcement but still
    validates the type and bounds of everything that WAS supplied, so a draft
    can never carry a value that would fail on submit for a reason the author
    only discovers at submit time.
    """
    data = data or {}
    errors: dict[str, str] = {}
    fields = field_map(schema)

    # Visibility is resolved against the RAW input: a `visible_if` may key off a
    # field that later fails coercion, and evaluating against cleaned data would
    # make visibility depend on validation order.
    shown = visible_fields(schema, data)

    clean: dict[str, Any] = {}

    for key, field in fields.items():
        ftype = field.get("type")
        if ftype == "calculated":
            continue  # derived below; the client's value is ignored entirely
        if key not in shown:
            continue  # hidden → not required, and not stored

        value = data.get(key)

        if _is_blank(value):
            if require_all and field.get("required"):
                errors[key] = "is required"
            continue

        try:
            if ftype in TABLE_TYPES:
                clean[key] = _coerce_table(field, value, errors)
            elif ftype in LIST_TYPES:
                items = value if isinstance(value, list) else [value]
                _check_options(field, items)
                clean[key] = items
            elif ftype in OPTION_TYPES:
                _check_options(field, value)
                clean[key] = value
            else:
                clean[key] = _coerce_scalar(field, value)
            if key in clean:
                _check_bounds(field, clean[key])
        except ValueError as e:
            errors[key] = str(e)

    if errors:
        raise ValidationError(errors)

    # Calculated fields last, in dependency order, reading cleaned values plus
    # the calculated values already produced this pass.
    computed: dict[str, Any] = {}
    scope: dict[str, Any] = dict(clean)
    for field in calculation_order(schema):
        key = field["key"]
        if key not in shown:
            continue  # a hidden calculation is not computed, and not stored
        try:
            result = eval_formula(str(field.get("formula") or ""), scope)
        except Exception:  # noqa: BLE001
            # A formula that publish validation accepted can still hit unusual
            # runtime data. An uncomputable figure is recorded as unknown rather
            # than blocking the save — the same treatment as a zero denominator.
            result = None
        if result is not None:
            computed[key] = result
        scope[key] = result

    return clean, computed


def required_but_missing(schema: list[Any] | None, data: dict[str, Any] | None) -> list[str]:
    """Required visible fields still blank. Used to show a draft's readiness
    without raising — the register needs to say 'ready to submit' before the
    user presses submit."""
    data = data or {}
    shown = visible_fields(schema, data)
    return [
        f["key"]
        for f in iter_fields(schema)
        if isinstance(f.get("key"), str)
        and f.get("required")
        and f.get("type") not in ("calculated", "section")
        and f["key"] in shown
        and _is_blank(data.get(f["key"]))
    ]
