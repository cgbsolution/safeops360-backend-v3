"""Per-flow analytics — trend, distribution, ageing, SLA and ownership for each
operational flow (Observation, Incident, Near Miss, CAPA, HIRA, EAI, MOC, Risk).

Complements rather than duplicates the two existing intelligence layers:

  • `app.services.insights` answers "what should I look at right now" as three
    narrative cards above a register.
  • `app.services.signal_engine` answers "what does the pattern ACROSS flows
    say" as cross-module Signals.
  • This layer answers "how is this flow performing over time, where is it
    concentrated, and is the backlog clearing" — the analytics a flow owner
    needs, which neither of the other two provides.

The analytics screens render the insight cards on top, so the narrative and the
charts always agree: both are computed from the same records by deterministic
rules, with no model call and no network egress.
"""

from app.services.analytics.engine import compute_flow
from app.services.analytics.specs import FLOW_KEYS, SPECS, FlowSpec, get_spec

__all__ = ["FLOW_KEYS", "SPECS", "FlowSpec", "compute_flow", "get_spec"]
