"""Reference numbering for form records (§5).

Pattern grammar: literal text plus these tokens —
    {YYYY}  four-digit year        {YY}   two-digit year
    {MM}    two-digit month        {SITE} the site code
    {####}  zero-padded sequence   (any run of 2-8 '#')

e.g. 'KAIZEN-{YYYY}-{####}' → KAIZEN-2026-0142
     'SUS-ENERGY-{SITE}-{YYYY}{MM}-{###}' → SUS-ENERGY-NW-202608-007

MAX + 1, NEVER COUNT + 1
The sequence is derived from the highest number already issued under the same
prefix — not from how many rows exist. FormRecord is soft-deleted, so a deleted
row keeps its number while dropping out of every ordinary query; counting live
rows would re-propose a number that still exists and the insert would die on
`uq_FormRecord_key_reference`. That precise bug (count+1 against a
soft-deleting table) has already cost this platform two outages — CAPA numbering
and Schedule Audit — so it is not repeated here.

`include_deleted=True` opts the lookup out of the global soft-delete filter for
the same reason `services/loto.py` does: uniqueness is a property of the table,
not of what the caller is allowed to see.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.form_engine import FormRecord

__all__ = ["validate_pattern", "next_reference", "PatternError"]

_TOKEN = re.compile(r"\{(YYYY|YY|MM|SITE|#{2,8})\}")
_SEQ_TOKEN = re.compile(r"^#{2,8}$")


class PatternError(ValueError):
    """An unusable number pattern. Raised at publish time only."""


def validate_pattern(pattern: str) -> None:
    if not isinstance(pattern, str) or not pattern.strip():
        raise PatternError("Number pattern must be a non-empty string.")

    seq_tokens = [t for t in _TOKEN.findall(pattern) if _SEQ_TOKEN.match(t)]
    if len(seq_tokens) != 1:
        raise PatternError(
            "Number pattern needs exactly one sequence token, e.g. {####}. "
            f"Found {len(seq_tokens)}."
        )
    # Anything in braces that isn't a token we know is almost certainly a typo
    # ({YYY}, {SITECODE}) that would otherwise be emitted literally into every
    # reference number in the register before anyone noticed.
    for raw in re.findall(r"\{([^}]*)\}", pattern):
        if not _TOKEN.match("{" + raw + "}"):
            raise PatternError(f"Unknown token '{{{raw}}}' in number pattern.")
    if not pattern.startswith(tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ")):
        raise PatternError("Number pattern should start with a literal prefix, e.g. 'KAIZEN-'.")


def _render_prefix_suffix(pattern: str, *, site_code: str | None) -> tuple[str, str, int]:
    """Split the pattern at its sequence token, substituting the date/site
    tokens on both sides. Returns (prefix, suffix, pad_width).

    The prefix is what scopes the sequence: a pattern containing {YYYY} restarts
    each year because the prefix changes, and one containing {SITE} numbers each
    site independently. That falls out of the substitution rather than needing a
    separate 'reset frequency' setting to keep in sync.
    """
    now = datetime.now(timezone.utc)
    subs = {
        "YYYY": f"{now.year:04d}",
        "YY": f"{now.year % 100:02d}",
        "MM": f"{now.month:02d}",
        "SITE": (site_code or "").upper(),
    }

    parts = _TOKEN.split(pattern)
    # re.split with one capture group alternates literal, token, literal, ...
    rendered: list[str] = []
    pad = 0
    seq_index = -1
    for i, part in enumerate(parts):
        if i % 2 == 1:  # a token
            if _SEQ_TOKEN.match(part):
                pad = len(part)
                seq_index = len(rendered)
                rendered.append("")
            else:
                rendered.append(subs.get(part, ""))
        else:
            rendered.append(part)

    if seq_index < 0:  # validate_pattern guarantees this cannot happen
        raise PatternError("Number pattern has no sequence token.")
    return "".join(rendered[:seq_index]), "".join(rendered[seq_index + 1 :]), pad


async def next_reference(
    db: AsyncSession,
    *,
    definition_key: str,
    pattern: str,
    site_code: str | None = None,
) -> str:
    """The next reference number for `definition_key`, gap-free per prefix."""
    prefix, suffix, pad = _render_prefix_suffix(pattern, site_code=site_code)

    # Strip the rendered prefix and (if any) suffix, leaving the digits.
    tail = func.substr(FormRecord.referenceNo, len(prefix) + 1)
    if suffix:
        tail = func.regexp_replace(tail, re.escape(suffix) + "$", "")

    like = f"{prefix}%{suffix}" if suffix else f"{prefix}%"
    # `~` anchors on digits so a hand-edited or legacy reference that happens to
    # share the prefix but isn't numeric can't blow up the cast.
    digits_only = "^[0-9]+$" if not suffix else "^[0-9]+" + re.escape(suffix) + "$"

    last = (
        await db.execute(
            select(func.max(cast(tail, Integer)))
            .where(FormRecord.definitionKey == definition_key)
            .where(FormRecord.referenceNo.like(like))
            .where(func.substr(FormRecord.referenceNo, len(prefix) + 1).op("~")(digits_only))
            .execution_options(include_deleted=True)
        )
    ).scalar() or 0

    return f"{prefix}{(last + 1):0{pad}d}{suffix}"
