"""Signal Engine — cross-module correlation & analytics ("SafeOps Signal").

The layer that reads ACROSS modules. The existing 8-screen AI Insights engine
(app/services/insights) computes within one module and renders on that module's
own screen; this engine correlates Observation → Near Miss → Incident → PTW →
HIRA → CAMS Audit → CAPA → Training as one dataset and emits Signals, a
first-class entity owned by no module. The two are complementary: the insight
engine is an INPUT here, not a thing this replaces.

Fully deterministic. Every signal is reproducible from its rule code, its
snapshotted thresholds and its evidence — no model, no embedding, no network
call anywhere in the compute path, so an airgapped deployment loses nothing.

Stream 1 (this pass) ships: the store, the runner, the two data-quality rules
(XCORR-019/020), the nightly job, the API and the admin Data Quality panel.
"""

from app.services.signal_engine.base import (
    EvidenceRef,
    RuleContext,
    SignalCandidate,
    SignalRuleImpl,
)
from app.services.signal_engine.registry import RULES, RULES_BY_CODE, get_rule
from app.services.signal_engine.runner import (
    run_for_modules,
    run_signal_engine,
    sync_rule_catalog,
)

__all__ = [
    "RULES",
    "RULES_BY_CODE",
    "EvidenceRef",
    "RuleContext",
    "SignalCandidate",
    "SignalRuleImpl",
    "get_rule",
    "run_for_modules",
    "run_signal_engine",
    "sync_rule_catalog",
]
