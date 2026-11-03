# HIRA calibration v2, round 2: Step 0 findings (before any behaviour change)

Answered from the code at commit `702c090` (engine 2.0.0) and the eval output in
`tests/eval/hira_calibration/results.json`. No database was read.

## 1. HH-03, HH-04, HH-09, HH-12: why inherent L2 when the LLM proposed L4?

Not control credit. It is the **exposure cap** added in round 1 (commit `90e727a`):

- `app/services/hira_calibration/risk_engine.py:505-511` `default_likelihood()` returns
  `defaultHighExposure` (3) only for `ROUTINE` + `DAILY`/`CONTINUOUS`, otherwise `defaultRoutine` (2).
- `risk_engine.py:558` `cap = min(lk["capWithoutEvidence"], default_likelihood(routine, frequency, cfg))`
  and `:559-566` cap any unevidenced proposal above it.
- All four are non-daily or non-routine: HH-03 `NON_ROUTINE/MONTHLY`, HH-04 `NON_ROUTINE/QUARTERLY`,
  HH-09 `NON_ROUTINE/MONTHLY`, HH-12 `NON_ROUTINE/OCCASIONAL`; none has evidence. So L4 -> L2.

No PTW, LOTO or baseline credit reaches the inherent rating: inherent is `rate(L, S)` from the
calibrated proposal (`review_v2.py` hazard loop, `inherent = eng.rate(L, S, cfg)`), computed before
`residual_from_controls()` is called. The LOTO / permit controls on HH-03 and HH-09 only act in the
residual. Fix (round 2 F): drop the exposure term, cap unevidenced proposals at a flat L3.

## 2. LOW-01: why did the damaged extension board go from S3 to S4?

`config.py:70` `"ELECTRICAL": {"min": 4}`, applied by `assistant_v2.py:78`
`calibrate_severity(..., adjust=True)` (drafts are moved into the credible range). The floor fires for
every ELECTRICAL hazard, whatever the text, so a cracked 230 V office extension board gets the HV/live
panel floor. (The stored-entry review uses `adjust=False` at `review_v2.py:236` and only reports it.)

Every family-level floor and ceiling today (`config.py:61-85`):

| Family | Floor (min) | Ceiling (max) | Lifted by |
|---|---|---|---|
| LIFTING | 4 (5 with "1 t ", "tonne", "fatal", "over people", "crushed by"...) | - | - |
| HEIGHT | 4 (5 with "fatal", "into the bin", "engulf", "2 m"...) | - | - |
| MOLTEN | 4 | - | - |
| ELECTRICAL | 4 | - | - |
| CONFINED_SPACE | 4 (5 with "oxygen", "toxic", "engulf"...) | - | - |
| VEHICLE | 4 | - | - |
| PRESSURE | 4 | - | - |
| FIRE_EXPLOSION | 4 | - | - |
| MACHINERY | 3 | 4 | "fatal", "drawn into", "crushed to death" |
| CHEMICAL | 3 | 4 | toxic gas words |
| FALLING_OBJECT | - | 4 | - |
| HEAT | - | 4 | "fatal" |
| THERMAL | - | 3 | "steam", "molten", "fatal" |
| SHARP, MANUAL_HANDLING, SLIP_TRIP, NOISE, PSYCHOSOCIAL | - | 3 | - |
| DUST | - | 3 | asbestos / silica / carcinogen words |

Every floor is unconditional on the scenario: that is the defect round 2 B fixes.

## 3. BENCH-6: can any control change the rating on `JSL_QSD24_GT`?

No. BENCH-6 is a client sheet entry, so the loader rates it `WITH_CONTROLS`
(`loader.py`, `rating_basis="WITH_CONTROLS" if src`): the QSD/24/F GT already includes the existing
controls. `review_v2.py:252-262` then sets `residual = inherent` and marks every control `IN_RATING`
(no credit), for new controls as well. But `recommend.py:614` models a new control's effect on that
rating (`kept_existing = []` then the new control alone), so the recommendation promises "L -1" that the
review never applies: after implementing, BENCH-6 stays L2 x S3 and ends on OVERRIDE.

How the plant formula could take credit: GT = SP x LH x CN x DE. A control can only change GT when the
team re-scores a factor on the sheet: LH (likelihood) after a preventive control, DE (detectability)
after a detection / inspection control, SP (spread / persons) after an exclusion zone; CN only with
elimination / substitution. The engine has no defined mapping from a control to those factors, and
round 2 H says not to invent one: such recommendations are tagged `IMPROVEMENT_NO_RATING_CREDIT`.

## 4. BENCH-1 draft vs BENCH-1/stored: why different end states?

Same activity, different inputs to the same `review_v2.evaluate()`:

| | Draft (`calibrate_draft`) | Stored (`stored_variant`, = tests `entry1`) |
|---|---|---|
| Hazards | capped at 5 (sharp edge, manual handling dropped) | all 7 |
| Ratings | per hazard, `adjust=True` | one entry rating spread, clamped down only |
| Candidate controls | catalogue + the 8 LLM-proposed controls | catalogue only |

The difference that decides the outcome: "Falling object" (L2 x S4 = HIGH after the existing
controls). The draft path recommends the LLM's "Overload limit switch on EOT crane" (engineering,
LIFTING, related to FALLING_OBJECT) -> L1 x S4, ALARP. The stored path has no such candidate: the
catalogue's toe-board item requires height words that the text lacks, and a second administrative
control earns nothing (admin credit is one step combined), so the hazard is left HIGH -> `NOT_REACHED
(CONTROLS)`. The stored entry's own proposed recommendations are not used as candidates
(`loader.py` only treats recommendations with a date as planned). Fix (round 2 E): both paths take the
entry's proposed (not yet planned) controls as candidates.

## 5. Data model: HIRA types and matrices

- **Type:** there is no HIRA type field. `HiraStudy.scopeType` (`app/models/hira.py:221`) is the scope
  (PLANT / DEPARTMENT / AREA / ACTIVITY / EQUIPMENT / PROCESS), not the method. Every study is
  task-based (activity -> hazards -> existing / recommended controls -> initial / residual). HAZOP,
  FMEA, MOC, environmental (EAI) and ERM live in other modules with their own tables.
- **Matrices:** `RiskMatrix` rows are tenant-global masters (`isGlobal`, `isDefault`); each study picks
  one by `HiraStudy.riskMatrixId` (`hira.py:229`). There is no plant -> matrix link. Each matrix stores
  an **explicit cell map** (`RiskMatrixCell` per L x S with `riskLevel`), `acceptableResidual` per
  routine type and `alarpBands` per level. The v2 engine ignores the cells and bands by score
  thresholds from config (`config.py` `thresholds`; `risk_engine.band()`), and reads the plant rule
  from `alarpBands` / `acceptableResidual` (`config.matrix_rule()`). Calibration settings are per
  tenant / plant in `HiraCalibrationConfig`.
