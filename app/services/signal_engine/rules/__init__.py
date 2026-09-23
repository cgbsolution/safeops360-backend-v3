"""Signal Engine rule implementations.

Twenty rules across three classes, grouped by the kind of computation they do:

  correlation.py   XCORR-001…008  — read two or more modules and relate them
  statistical.py   XSTAT-001…006  — outliers and anomalies, robust statistics
  data_quality.py  XDQ-021…024    — the data itself is not what it appears
  xcorr_019.py     XCORR-019      — silent zero-row field detector
  xcorr_020.py     XCORR-020      — cross-module reference integrity

XCORR-019 and XCORR-020 are data-quality rules carrying correlation-style codes.
They keep them: a rule code is the identity of every signal and every operator
override already persisted under it, so renaming one orphans both. Their
`rule_class` says what they are; the code says who they have always been.

Adding a rule is: write the class, add it to the tuple in its module. The
`SignalRule` catalog table is reconciled from the registry on every run, so
there is no seed step and no second place to keep in sync.
"""

from app.services.signal_engine.rules.correlation import CORRELATION_RULES
from app.services.signal_engine.rules.data_quality import DATA_QUALITY_RULES
from app.services.signal_engine.rules.statistical import STATISTICAL_RULES
from app.services.signal_engine.rules.xcorr_019 import Xcorr019SilentZeroRow
from app.services.signal_engine.rules.xcorr_020 import Xcorr020ReferenceIntegrity

__all__ = [
    "CORRELATION_RULES",
    "DATA_QUALITY_RULES",
    "STATISTICAL_RULES",
    "Xcorr019SilentZeroRow",
    "Xcorr020ReferenceIntegrity",
]
