"""HIRA calibration v2 (feature flag `hira.calibration.v2`).

Deterministic risk engine + calibration rules that keep Copilot output
proportionate and convergent:

  config.py       calibration settings per tenant / plant (versioned in DB)
  risk_engine.py  one matrix: score, band, control credit, likelihood cap,
                  severity rubric, tolerability (ALARP that terminates)
  recommend.py    capped, de-duplicated, ranked recommendations
  review_v2.py    the Copilot review of one entry (BLOCKER / ADVISORY / INFO)
  assistant_v2.py calibration of LLM proposals (HIRA Assistant, Copilot drafts)
  evidence.py     DB lookups: events, control failures, plant controls, CAPAs
  shadow.py       v1 / v2 side-by-side logging (hira_copilot_shadow_runs)

The LLM proposes hazards, ratings (with rationale) and controls. Code decides
every number: residuals, bands, tolerability and the recommendation list.
"""
