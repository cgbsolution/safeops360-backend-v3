"""Last-line PII scrub for everything the platform sends to the model provider.

Every LLM call goes through `anthropic_client` (`complete_json`,
`complete_with_tools`), so that is where this runs — on the user payload, and
on every agent tool result before it is fed back to the model. Call sites that
already redact (the incident context builder's `Redactor`) are unaffected:
their placeholders pass through unchanged.

What it removes:
  * person names held in name-bearing JSON keys (`injuredPersonName`,
    `witnessName`, `ownerName`, ... and a plain `name` on an object that is
    clearly a person — one that also carries `userId`, `email`, `designation`,
    ...). Each distinct name becomes a stable placeholder ("Person 3"), and
    the same names are then scrubbed out of every free-text value in the
    payload;
  * contact and identity fields (email, phone, address, date of birth,
    Aadhaar, PAN, blood group, signatures, GPS) — the value is dropped;
  * e-mail addresses, phone numbers, Aadhaar and PAN numbers anywhere in
    free text (also for non-JSON payloads).

What it keeps: roles, designations, departments, plant/area/equipment names
and codes, company (contractor) names, categories, severities and dates.

Deliberately deterministic and dependency-free: no NER model, so a name typed
into free text that never appears in a structured field is not caught. It is
a safety net under the per-builder redaction, not a substitute for it.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Keys whose value is a person's name, wherever they appear.
_PERSON_NAME_KEYS = frozenset({
    "approvername", "assigneename", "assignedtoname", "closedbyname", "createdbyname",
    "employeename", "externalname", "fullname", "injuredname", "injuredpersonname",
    "investigatorname", "issuername", "leadername", "originatorname", "ownername",
    "personname", "primaryownername", "raisedbyname", "receivername", "reportername",
    "reportedbyname", "responsiblepersonname", "submittedbyname", "teamleadername",
    "username", "verifiername", "witnessname", "workername", "observername",
    "actionownername", "analystname", "auditeename", "auditorname", "inspectorname",
    "trainername", "supervisorname", "uploadedbyname", "updatedbyname", "requestedbyname",
})
# A plain "name" is a person's name when the same object also has one of these.
_PERSON_HINT_KEYS = frozenset({
    "userid", "email", "designation", "employeeid", "employeecode", "phone", "mobile",
    "witnessuserid", "personid", "workerid",
})
# Contact / identity fields: the value is dropped outright.
_DROP_KEYS = frozenset({
    "email", "emailaddress", "phone", "phonenumber", "mobile", "mobilenumber",
    "contactnumber", "whatsappnumber", "address", "homeaddress", "dob", "dateofbirth",
    "aadhaar", "aadhaarnumber", "aadhaarlast4", "pan", "pannumber", "bloodgroup",
    "emergencycontact", "emergencycontactname", "emergencycontactphone", "signature",
    "signatureurl", "signaturedata", "gpslatitude", "gpslongitude", "latitude", "longitude",
})

_PLACEHOLDER = re.compile(r"^(Person|Witness|Crew|User) \d+$")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<!\w)(?:\+\d{1,3}[\s-]?)?(?:\d[\s-]?){9,11}\d(?!\w)")
_AADHAAR = re.compile(r"(?<!\d)\d{4}[\s-]\d{4}[\s-]\d{4}(?!\d)")
_PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")


def scrub_text(value: str, names: dict[str, str] | None = None) -> str:
    """Remove contact/identity patterns and any known names from free text."""
    out = _EMAIL.sub("[email]", value)
    out = _AADHAAR.sub("[id]", out)
    out = _PAN.sub("[id]", out)
    out = _PHONE.sub("[phone]", out)
    if names:
        for key in sorted(names, key=len, reverse=True):
            if len(key) < 3:
                continue
            out = re.sub(rf"(?i)\b{re.escape(key)}\b", names[key], out)
    return out


class _Collector:
    def __init__(self) -> None:
        self.names: dict[str, str] = {}

    def placeholder(self, name: str) -> str:
        key = name.strip().lower()
        if key not in self.names:
            self.names[key] = f"Person {len(self.names) + 1}"
        return self.names[key]


def _is_person(obj: dict[str, Any]) -> bool:
    return any(k.lower() in _PERSON_HINT_KEYS for k in obj)


def _collect(node: Any, col: _Collector) -> Any:
    """First pass: replace structured names / drop contact fields."""
    if isinstance(node, list):
        return [_collect(v, col) for v in node]
    if not isinstance(node, dict):
        return node
    person = _is_person(node)
    out: dict[str, Any] = {}
    for k, v in node.items():
        lk = k.lower() if isinstance(k, str) else k
        if lk in _DROP_KEYS and v not in (None, ""):
            out[k] = "[redacted]"
        elif isinstance(v, str) and v.strip() and not _PLACEHOLDER.match(v.strip()) and (
            lk in _PERSON_NAME_KEYS or (lk == "name" and person)
        ):
            out[k] = col.placeholder(v)
        else:
            out[k] = _collect(v, col)
    return out


def _scrub(node: Any, names: dict[str, str]) -> Any:
    """Second pass: scrub every string value."""
    if isinstance(node, list):
        return [_scrub(v, names) for v in node]
    if isinstance(node, dict):
        return {k: _scrub(v, names) for k, v in node.items()}
    if isinstance(node, str):
        return scrub_text(node, names)
    return node


def redact_data(data: Any) -> Any:
    """Redact a JSON-shaped Python value (dict / list / scalars)."""
    col = _Collector()
    first = _collect(data, col)
    return _scrub(first, col.names)


def redact_payload(text: str) -> str:
    """Redact a payload string. JSON payloads are redacted structurally and
    re-serialised; anything else gets the free-text pattern scrub."""
    if not isinstance(text, str) or not text:
        return text
    stripped = text.lstrip()
    if stripped[:1] in ("{", "["):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if data is not None:
            indent = 2 if "\n" in text[:200] else None
            return json.dumps(redact_data(data), default=str, indent=indent, ensure_ascii=False)
    return scrub_text(text)


__all__ = ["redact_data", "redact_payload", "scrub_text"]
