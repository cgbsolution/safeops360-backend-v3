"""The field-type contract, and the publish gate that enforces it (§3.1).

`validate_definition()` is the single place a definition is proved sound. It runs
identically for a definition authored as JSON config (Phase 2) and one built in
the drag-drop Builder (Phase 3) — which is what makes §10's "produces identical
behaviour" true by construction rather than by discipline. The Builder cannot
emit a form the engine can't execute, because publishing goes through here.

The checks that matter are the ones catching mistakes that would otherwise
surface much later as silently-null data:
  * a formula or a `visible_if` referencing a field key that doesn't exist
  * a calculated field whose formula reads itself (directly or through a cycle)
  * a select with no options; a duplicate field key; a required calculated field
Each of these is cheap here and expensive in a filed report.
"""

from __future__ import annotations

from typing import Any

from app.services.form_engine.formula import FormulaError, referenced_fields

__all__ = [
    "FIELD_TYPES",
    "CONTAINER_TYPES",
    "VALUE_TYPES",
    "SchemaError",
    "validate_definition",
    "iter_fields",
    "field_map",
    "calculation_order",
]


class SchemaError(ValueError):
    """A definition that cannot be published. Message is user-facing —
    it is shown in the Builder next to the offending field."""


# ── The supported field types (§3.1) ────────────────────────────────────────
# Grouped by how the engine treats them, not by how they render.

# Hold a scalar value.
SCALAR_TYPES = frozenset(
    {
        "text",
        "textarea",
        "richtext",
        "number",
        "integer",
        "decimal",
        "date",
        "datetime",
        "select",
        "radio",
        "boolean",
        "checkbox",
        "user",  # user-picker — also usable as a workflow router target
        "orgunit",  # plant / area picker
        "file",  # attachment slot; bytes live in the shared Attachment table
        "signature",
    }
)
# Hold a list of values.
LIST_TYPES = frozenset({"multiselect"})
# Hold a list of row dicts.
TABLE_TYPES = frozenset({"table"})
# Structural only — carry no value.
CONTAINER_TYPES = frozenset({"section"})
# Server-derived; never accepted from the client.
COMPUTED_TYPES = frozenset({"calculated"})

VALUE_TYPES = SCALAR_TYPES | LIST_TYPES | TABLE_TYPES
FIELD_TYPES = VALUE_TYPES | CONTAINER_TYPES | COMPUTED_TYPES

# Types whose `options` list is the set of permitted values.
OPTION_TYPES = frozenset({"select", "radio", "multiselect", "checkbox"})

# `visible_if` operators, shared with the existing workflow engine's condition
# grammar (workflow_engine._evaluate_rule) so an author learns one syntax.
CONDITION_OPERATORS = frozenset({"=", "!=", "in", "not_in", ">", "<", ">=", "<=", "empty", "not_empty"})


def iter_fields(schema: list[Any] | None) -> list[dict]:
    """Flatten the schema into every field, descending into sections and table
    columns. Section children are returned alongside the section itself; table
    columns are NOT — they live in the row namespace, not the record namespace."""
    out: list[dict] = []

    def walk(items: Any) -> None:
        if not isinstance(items, list):
            return
        for f in items:
            if not isinstance(f, dict):
                continue
            out.append(f)
            if f.get("type") == "section":
                walk(f.get("fields"))

    walk(schema)
    return out


def field_map(schema: list[Any] | None) -> dict[str, dict]:
    """key → field, for every field that holds or derives a value."""
    return {
        f["key"]: f
        for f in iter_fields(schema)
        if isinstance(f.get("key"), str) and f.get("type") not in CONTAINER_TYPES
    }


def calculation_order(schema: list[Any] | None) -> list[dict]:
    """Calculated fields in dependency order, so a formula may read another
    calculated field. Raises SchemaError on a cycle.

    Without this, evaluation order would be schema order, and
    `emission_intensity = co2e_emissions / production_output` would silently
    compute against a not-yet-computed `co2e_emissions` and store None — the
    exact class of bug that only shows up once a report is generated.
    """
    fields = field_map(schema)
    calcs = {k: f for k, f in fields.items() if f.get("type") == "calculated"}

    ordered: list[dict] = []
    state: dict[str, int] = {}  # 0 = visiting, 1 = done

    def visit(key: str, trail: tuple[str, ...]) -> None:
        if state.get(key) == 1:
            return
        if state.get(key) == 0:
            cycle = " → ".join([*trail[trail.index(key) :], key]) if key in trail else key
            raise SchemaError(f"Calculated fields form a cycle: {cycle}")
        state[key] = 0
        try:
            deps = referenced_fields(str(calcs[key].get("formula") or "0"))
        except FormulaError:
            deps = set()  # a malformed formula is reported by validate_definition
        for dep in sorted(deps):
            if dep in calcs:
                visit(dep, (*trail, key))
        state[key] = 1
        ordered.append(calcs[key])

    for key in calcs:
        visit(key, ())
    return ordered


def _validate_condition(cond: Any, known: set[str], where: str) -> list[str]:
    """A `visible_if` is `{field, operator, value}` or
    `{combinator: AND|OR, rules: [...]}` (nestable)."""
    errs: list[str] = []
    if cond is None:
        return errs
    if not isinstance(cond, dict):
        return [f"{where}: visible_if must be an object"]
    if "rules" in cond:
        if cond.get("combinator", "AND") not in {"AND", "OR"}:
            errs.append(f"{where}: combinator must be AND or OR")
        rules = cond.get("rules")
        if not isinstance(rules, list) or not rules:
            errs.append(f"{where}: visible_if.rules must be a non-empty list")
        else:
            for i, r in enumerate(rules):
                errs += _validate_condition(r, known, f"{where}.rules[{i}]")
        return errs
    fld = cond.get("field")
    if not isinstance(fld, str) or not fld:
        errs.append(f"{where}: visible_if.field is required")
    elif fld not in known:
        errs.append(f"{where}: visible_if references unknown field '{fld}'")
    op = cond.get("operator")
    if op not in CONDITION_OPERATORS:
        errs.append(f"{where}: unsupported operator '{op}'")
    return errs


def validate_definition(schema: Any, *, number_pattern: str | None = None) -> None:
    """Raise SchemaError (with EVERY problem, not just the first) if the schema
    cannot be published. Collecting all errors matters for the Builder: fixing
    one field at a time through six publish attempts is a bad authoring loop."""
    errs: list[str] = []

    if not isinstance(schema, list) or not schema:
        raise SchemaError("Form schema must be a non-empty list of fields.")

    fields = iter_fields(schema)
    seen: set[str] = set()

    for f in fields:
        key = f.get("key")
        where = f"field '{key}'" if key else "field <no key>"

        if not isinstance(key, str) or not key:
            errs.append("Every field needs a non-empty string `key`.")
            continue
        if not key.replace("_", "").isalnum() or key[0].isdigit():
            # Keys are referenced as bare identifiers inside formulas, so they
            # have to be valid identifiers or the formula parser can't see them.
            errs.append(f"{where}: key must be alphanumeric/underscore and not start with a digit.")
        if key in seen:
            errs.append(f"{where}: duplicate field key.")
        seen.add(key)

        ftype = f.get("type")
        if ftype not in FIELD_TYPES:
            errs.append(f"{where}: unknown type '{ftype}'.")
            continue
        if not f.get("label"):
            errs.append(f"{where}: label is required.")

        if ftype in OPTION_TYPES:
            opts = f.get("options")
            if not isinstance(opts, list) or not opts:
                errs.append(f"{where}: '{ftype}' requires a non-empty `options` list.")
            else:
                vals = [o.get("value") if isinstance(o, dict) else o for o in opts]
                if len(set(map(str, vals))) != len(vals):
                    errs.append(f"{where}: duplicate option values.")

        if ftype == "table":
            cols = f.get("columns")
            if not isinstance(cols, list) or not cols:
                errs.append(f"{where}: a table field requires a non-empty `columns` list.")
            else:
                col_keys: set[str] = set()
                for c in cols:
                    if not isinstance(c, dict) or not c.get("key"):
                        errs.append(f"{where}: every column needs a `key`.")
                        continue
                    if c["key"] in col_keys:
                        errs.append(f"{where}: duplicate column key '{c['key']}'.")
                    col_keys.add(c["key"])
                    # Nested tables and nested calculations are deliberately out
                    # of scope — §11 says let real module requirements drive
                    # capability, and no register in Parts C/D needs either.
                    if c.get("type") not in (SCALAR_TYPES | LIST_TYPES):
                        errs.append(
                            f"{where}: column '{c.get('key')}' has type "
                            f"'{c.get('type')}', which is not allowed inside a table."
                        )

        if ftype == "calculated":
            if f.get("required"):
                errs.append(f"{where}: a calculated field cannot be `required` — the server derives it.")
            expr = f.get("formula")
            if not isinstance(expr, str) or not expr.strip():
                errs.append(f"{where}: a calculated field requires a `formula`.")

    known = set(field_map(schema))

    # Second pass — cross-references, once every key is known.
    for f in fields:
        key = f.get("key")
        where = f"field '{key}'"
        errs += _validate_condition(f.get("visible_if"), known, where)

        if f.get("type") == "calculated" and isinstance(f.get("formula"), str):
            try:
                refs = referenced_fields(f["formula"])
            except FormulaError as e:
                errs.append(f"{where}: {e}")
                continue
            if key in refs:
                errs.append(f"{where}: a formula cannot reference itself.")
            for r in sorted(refs - known - {key}):
                errs.append(f"{where}: formula references unknown field '{r}'.")

    if errs:
        raise SchemaError(" | ".join(errs))

    # Cycle detection runs last: it needs a schema whose formulas already parse.
    calculation_order(schema)

    if number_pattern is not None:
        from app.services.form_engine.numbering import validate_pattern

        validate_pattern(number_pattern)
