"""Deterministic expression evaluator for `calculated` form fields.

Config authored here is written by non-engineers through the Builder (Part B),
so `eval()` is not an option at any effort level — a formula field would be
remote code execution against the API process. This walks Python's own parsed
AST and refuses every node type that isn't arithmetic, which makes the set of
reachable behaviour a whitelist rather than a blocklist.

No LLM, no I/O, no clock, no randomness: the same inputs always produce the same
output, which is what an assurable disclosure figure and an airgapped
deployment both require.

Supported:
    numbers, field references (bare identifiers), + - * / %, unary minus,
    parentheses, comparisons, and the functions below.

DIVISION BY ZERO RETURNS None, NOT AN ERROR
An intensity ratio whose denominator hasn't been entered yet is *unknown*, not
invalid — the form must still save. This mirrors how BRSR already treats a
missing denominator ("a site with no denominator still reports absolute figures
— intensity is simply omitted rather than divided by a guess"). A None
propagates through the rest of the expression and the field is simply absent
from `computedJson`.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from typing import Any

__all__ = ["evaluate", "referenced_fields", "FormulaError"]


class FormulaError(ValueError):
    """Raised for a malformed or disallowed formula. Publish-time only —
    a definition carrying one of these can never be published, so record
    evaluation never has to handle it."""


# ── Functions available inside a formula ────────────────────────────────────
# Each takes already-evaluated arguments. None means "unknown"; a function that
# cannot produce a meaningful answer with an unknown input returns None too.


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _fn_sum(*args: Any) -> float:
    """SUM(a, b, ...) — also flattens a list argument, so SUM(rows.qty) over a
    table field's column works. Unknowns count as zero: a partially filled grid
    should total what has been entered, not collapse to unknown."""
    total = 0.0
    for a in args:
        if isinstance(a, (list, tuple)):
            total += _fn_sum(*a)
        else:
            n = _num(a)
            if n is not None:
                total += n
    return total


def _fn_if(cond: Any, when_true: Any, when_false: Any) -> Any:
    return when_true if bool(cond) else when_false


def _fn_round(value: Any, digits: Any = 0) -> float | None:
    n, d = _num(value), _num(digits)
    if n is None:
        return None
    return round(n, int(d or 0))


def _fn_min(*args: Any) -> float | None:
    vals = [n for n in (_num(a) for a in _flatten(args)) if n is not None]
    return min(vals) if vals else None


def _fn_max(*args: Any) -> float | None:
    vals = [n for n in (_num(a) for a in _flatten(args)) if n is not None]
    return max(vals) if vals else None


def _fn_abs(value: Any) -> float | None:
    n = _num(value)
    return None if n is None else abs(n)


def _fn_count(*args: Any) -> int:
    """COUNT(rows) — number of entered (non-null) values."""
    return sum(1 for a in _flatten(args) if a is not None and a != "")


def _flatten(args: Iterable[Any]) -> list[Any]:
    out: list[Any] = []
    for a in args:
        if isinstance(a, (list, tuple)):
            out.extend(_flatten(a))
        else:
            out.append(a)
    return out


FUNCTIONS = {
    "SUM": _fn_sum,
    "IF": _fn_if,
    "ROUND": _fn_round,
    "MIN": _fn_min,
    "MAX": _fn_max,
    "ABS": _fn_abs,
    "COUNT": _fn_count,
}

_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow)
_ALLOWED_CMPOPS = (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)


def _parse(expr: str) -> ast.expr:
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError as e:
        raise FormulaError(f"Cannot parse formula: {e.msg}") from e
    return tree.body


def referenced_fields(expr: str) -> set[str]:
    """Every field key a formula reads. Used at publish time to reject a formula
    pointing at a field that doesn't exist — the single most common authoring
    mistake, and one that would otherwise surface as a silently-null column
    months later in a filed report."""
    names: set[str] = set()
    for node in ast.walk(_parse(expr)):
        if isinstance(node, ast.Name):
            if node.id not in FUNCTIONS:
                names.add(node.id)
        elif isinstance(node, ast.Attribute):
            # `rows.quantity` — a table field's column. The base name is the field.
            # Same restriction as evaluation, applied here so the publish gate
            # refuses the formula rather than leaving it to fail per record.
            if not isinstance(node.value, ast.Name):
                raise FormulaError(
                    "Attribute access is only valid on a field name, e.g. `sources.quantity`."
                )
            if node.attr.startswith("__"):
                raise FormulaError(f"Invalid column name '{node.attr}'.")
            names.add(node.value.id)
    return names


def evaluate(expr: str, values: dict[str, Any]) -> Any:
    """Evaluate `expr` against `values`. Returns None when the result is unknown.

    Raises FormulaError only for a structurally invalid formula, which publish
    validation has already ruled out for any definition that reached a record.
    """

    def _eval(node: ast.expr) -> Any:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float, str, bool)) or node.value is None:
                return node.value
            raise FormulaError(f"Unsupported constant: {node.value!r}")

        if isinstance(node, ast.Name):
            return values.get(node.id)

        if isinstance(node, ast.Attribute):
            # `rows.quantity` → the `quantity` column of every row in `rows`.
            #
            # The base MUST be a bare field name. Attribute access never reaches
            # a real Python attribute here (it is interpreted as a dict/list
            # lookup), but allowing an arbitrary base expression meant
            # `().__class__.__bases__` evaluated quietly to [] instead of being
            # refused — a dunder chain that reads as an escape attempt has no
            # business returning a value at all, and swallowing it would hide
            # both attacks and ordinary authoring mistakes.
            if not isinstance(node.value, ast.Name):
                raise FormulaError(
                    "Attribute access is only valid on a field name, e.g. `sources.quantity`."
                )
            if node.attr.startswith("__"):
                raise FormulaError(f"Invalid column name '{node.attr}'.")
            base = values.get(node.value.id)
            if isinstance(base, list):
                return [r.get(node.attr) if isinstance(r, dict) else None for r in base]
            if isinstance(base, dict):
                return base.get(node.attr)
            return None

        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.USub):
                n = _num(_eval(node.operand))
                return None if n is None else -n
            if isinstance(node.op, ast.UAdd):
                return _num(_eval(node.operand))
            if isinstance(node.op, ast.Not):
                return not bool(_eval(node.operand))
            raise FormulaError("Unsupported unary operator")

        if isinstance(node, ast.BinOp):
            if not isinstance(node.op, _ALLOWED_BINOPS):
                raise FormulaError(f"Unsupported operator: {type(node.op).__name__}")
            left, right = _num(_eval(node.left)), _num(_eval(node.right))
            if left is None or right is None:
                return None
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, (ast.Div, ast.Mod)):
                if right == 0:
                    return None  # unknown, not an error — see the module docstring
                return left / right if isinstance(node.op, ast.Div) else left % right
            if isinstance(node.op, ast.Pow):
                # Bounded: an unbounded exponent is a CPU denial-of-service on a
                # config field a non-engineer can type into.
                if abs(right) > 8:
                    raise FormulaError("Exponent out of range (max 8)")
                return left**right

        if isinstance(node, ast.BoolOp):
            vals = [bool(_eval(v)) for v in node.values]
            return all(vals) if isinstance(node.op, ast.And) else any(vals)

        if isinstance(node, ast.Compare):
            if len(node.ops) != 1 or not isinstance(node.ops[0], _ALLOWED_CMPOPS):
                raise FormulaError("Unsupported comparison")
            left, right = _eval(node.left), _eval(node.comparators[0])
            ln, rn = _num(left), _num(right)
            if ln is not None and rn is not None:
                left, right = ln, rn
            op = node.ops[0]
            try:
                if isinstance(op, ast.Eq):
                    return left == right
                if isinstance(op, ast.NotEq):
                    return left != right
                if left is None or right is None:
                    return False
                if isinstance(op, ast.Lt):
                    return left < right
                if isinstance(op, ast.LtE):
                    return left <= right
                if isinstance(op, ast.Gt):
                    return left > right
                if isinstance(op, ast.GtE):
                    return left >= right
            except TypeError:
                return False

        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
                name = getattr(node.func, "id", "<expr>")
                raise FormulaError(f"Unknown function: {name}")
            if node.keywords:
                raise FormulaError("Keyword arguments are not supported")
            args = [_eval(a) for a in node.args]
            try:
                return FUNCTIONS[node.func.id](*args)
            except FormulaError:
                raise
            except (TypeError, ValueError) as e:
                raise FormulaError(f"{node.func.id}(): {e}") from e

        if isinstance(node, (ast.List, ast.Tuple)):
            return [_eval(e) for e in node.elts]

        raise FormulaError(f"Unsupported expression: {type(node).__name__}")

    return _eval(_parse(expr))
