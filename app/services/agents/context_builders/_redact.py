"""Strip personal identifiers from agent context before it leaves the platform.

The context dicts these builders return are sent verbatim to the model
provider. Names of injured people, witnesses and crew members are not
needed for the analysis — roles, injury categories and internal user ids
(which the tools use to look up training records) are. So names become
stable placeholders ("Person 1", "Witness 2") and free text is scrubbed of
those names plus e-mail addresses and phone numbers.

Deliberately minimal: this is a choke point for the obvious identifiers,
not a general NER pass. Names that never appear in a structured field
cannot be found here.
"""

from __future__ import annotations

import re
from typing import Any

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Phone numbers: optional +country code, then 10-12 digits that may be split
# by single spaces/hyphens ("+91 98765 43210", "98765-43210"). Dates and
# record numbers have too few digits to match.
_PHONE = re.compile(r"(?<!\w)(?:\+\d{1,3}[\s-]?)?(?:\d[\s-]?){9,11}\d(?!\w)")


class Redactor:
    """Collects the names seen while building one context and replaces them
    with placeholders, consistently across every field of that context."""

    def __init__(self) -> None:
        self._placeholders: dict[str, str] = {}
        self._counts: dict[str, int] = {}

    def name(self, value: str | None, kind: str = "Person") -> str | None:
        """Placeholder for a name; the same name always maps to the same one."""
        if not value or not value.strip():
            return None
        key = value.strip().lower()
        if key not in self._placeholders:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            self._placeholders[key] = f"{kind} {self._counts[kind]}"
        return self._placeholders[key]

    def text(self, value: Any) -> Any:
        """Scrub known names, e-mails and phone numbers from free text."""
        if not isinstance(value, str) or not value:
            return value
        out = _EMAIL.sub("[email]", value)
        out = _PHONE.sub("[phone]", out)
        # Longest first so "Ravi Kumar" wins over "Ravi".
        for key in sorted(self._placeholders, key=len, reverse=True):
            if len(key) < 3:
                continue
            out = re.sub(rf"(?i)\b{re.escape(key)}\b", self._placeholders[key], out)
        return out


__all__ = ["Redactor"]
