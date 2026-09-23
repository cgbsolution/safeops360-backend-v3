"""EHS Scorecard — periodic leading/lag indicator reporting.

Three layers, deliberately separated:

  rollup.py    computes and FREEZES one row per plant-month across six modules
  payload.py   the ONE shape the dashboard, the PDF and the PPTX all render
  export_*.py  the two document renderers, both fed by payload.py

Nothing outside rollup.py reads a raw source record, and nothing outside
payload.py decides what an indicator means.
"""

from app.services.scorecard.payload import INDICATORS, build_payload
from app.services.scorecard.rollup import compute_month, run_rollup

__all__ = ["INDICATORS", "build_payload", "compute_month", "run_rollup"]
