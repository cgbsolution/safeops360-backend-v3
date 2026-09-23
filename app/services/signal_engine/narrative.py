"""Signal Engine — the optional LLM narrative-rewrite layer.

**This module can never be required for a signal to exist.** Detection,
severity, evidence and the recommended action are all computed deterministically
by the rule; every rule already emits a complete, client-presentable sentence in
`narrativeTemplate`. What this layer does — when a tenant explicitly enables it
and supplies a key — is rewrite that sentence into `narrativeLLM`, a SEPARATE
column the UI prefers when present and ignores when absent.

That separation is the whole design. SafeOps360's default deployment is an
on-premise, airgapped client site with no route to any external API, so:

  • the flag defaults to OFF (`settings.signal_narrative_llm_enabled`),
  • with it off, nothing here is imported into the compute path at all,
  • with it on but no key configured, `maybe_rewrite` returns None and the run
    continues — an operator flipping a flag must not take the nightly scan down,
  • `narrativeTemplate` is never overwritten, so a rewrite can be turned off
    again, or audited against the deterministic original, at any time.

Stream 1 ships the gate and the contract. The actual call is deliberately not
implemented: shipping a half-tested model call into the one code path that runs
unattended every night, on a build whose remit was the gate, is how you get a
nightly job that fails silently — which is precisely the failure this same build
just spent its first hour fixing.
"""

from __future__ import annotations

import logging

from app.core.config import get_settings

log = logging.getLogger("safeops360.signal_engine.narrative")


def narrative_enabled() -> bool:
    """True only when the operator has opted in AND a key is configured.

    Both conditions, deliberately. A flag on with no key is a misconfiguration,
    and the safe reading of a misconfiguration in an unattended job is "stay
    deterministic".
    """
    s = get_settings()
    return bool(s.signal_narrative_llm_enabled and s.anthropic_api_key)


async def maybe_rewrite(
    *, rule_code: str, deterministic: str, facts: dict, severity: str
) -> str | None:
    """Return polished prose for an ALREADY-DETECTED signal, or None.

    None is the normal, supported, airgap-default answer — callers must treat it
    as success and fall back to `deterministic`, never as an error.
    """
    if not narrative_enabled():
        return None

    # Stream 5. The contract above is fixed so the call site never changes:
    # whatever lands here returns a string or None, and None is always safe.
    log.info(
        "signal narrative rewrite requested for %s but the LLM layer is not "
        "implemented yet; using the deterministic narrative",
        rule_code,
    )
    return None


__all__ = ["maybe_rewrite", "narrative_enabled"]
