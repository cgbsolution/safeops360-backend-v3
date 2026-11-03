# HIRA calibration rounds 2 and 3: report back

Branch `hira-calibration-v2`. Flag `hira.calibration.v2` stays OFF by default (shadow only). No database access was used for this work and no active study was changed. Full tables: [REPORT.md](REPORT.md). Machine-readable: [results.json](results.json).

## 1. Step 0 findings

Delivered before any behaviour change in [ROUND2_STEP0.md](ROUND2_STEP0.md) (commit `333a627`). In short:

1. **HH-03, HH-04, HH-09 and HH-12 came out at L2.** The round-1 exposure cap took routine and frequency into account, and that moved them below L3. No PTW / LOTO / baseline credit leaked into inherent. Fixed by F: flat L3 without evidence, no control credit.
2. **LOW-01 was raised to S4** by the ELECTRICAL family floor (min 4), which applied to any electrical text. In round 2 the floor applies only when the live-work scenario is named (`ELECTRICAL_FLOOR_NOT_TRIGGERED`).
3. **BENCH-6 (`JSL_QSD24_GT`) is rated WITH_CONTROLS on the plant index.** A new control cannot change the plant rating unless the team re-scores SP / LH / DE, so none of the plant formula is mapped to control credit. Improvements are tagged `IMPROVEMENT_NO_RATING_CREDIT` (H).
4. **The BENCH-1 draft and stored paths differed** in the hazard cap, severity adjustment and LLM candidates. In round 2 both paths use one candidate path (E). Parity holds on all 25 fixtures.
5. **There was no HIRA type field; only task-based entries existed.** Added `hiraType` and the gate (K). Matrices are a tenant master referenced by `study.riskMatrixId` and are now read through the adapter (L).

## 2. Commits and files

| Commit | Content |
|---|---|
| `333a627` | Step 0 report |
| `b8c3e2b` | Commit 1, K + L refactor (snapshot byte-identical): `matrix.py`, `config.py`, `risk_engine.py`, `loader.py`, `review_v2.py`, `assistant_v2.py`, matrix / snapshot tests |
| `dec4af0` | Commit 2, A-I: `risk_engine.py`, `recommend.py`, `review_v2.py`, `assistant_v2.py`, `loader.py`, `service.py`, router apply part, round-2 tests |
| `ae2427d` | Commit 3, M: `rubric_packs.py`, `packs/*.json`, `config.rubricPack`, `service.py` scope, pack tests, `tests/eval/hira_calibration/packs/*/activities.json` |
| commit 4 | J + N: eval harness, Appendix A load, `statutoryCurrent`, unverified scenario, verify-first route, review CSV, round-1 baseline, pack distribution, `GET /api/hira/calibration/scope`, evidence-breakdown script, rubric rule ids, apply-part split |
| frontend `263a3de` | Round-3 DDL script (not run) |
| frontend commit | Review card (neutral inherent chip, confirm routine controls, verify route, kinds, scope banners), Copilot panel "Validated for", `canShowResidualClaim` |

## 3. Before (round 1) and after

Before is the frozen round-1 output in `baseline_round1.json`; after is pass 1 / single pass now. A FLAG marks a row that moved for a reason that is not one of the rubric or round-2 rules (see 4).

| ID | End state (round 1 -> now) | Recs | Pass-1 blockers | Flag |
|---|---|---|---|---|
| BENCH-1 | TOLERABLE_ALARP -> TOLERABLE_ALARP | 3 -> 3 | HAZARD_NOT_ACCEPTABLE -> same | |
| BENCH-6 | OVERRIDE -> OVERRIDE | 1 -> 1 | ALARP_ENGINEERING -> none | |
| LOW-01 | TOLERABLE_ALARP -> ACCEPTABLE | 0 -> 0 | none | |
| LOW-02 | TOLERABLE_ALARP -> ACCEPTABLE | 4 -> 0 | NO_CONTROLS -> none | |
| LOW-03 | TOLERABLE_ALARP -> TOLERABLE_ALARP | 2 -> 2 | ALARP_ENGINEERING, HAZARD_NOT_ACCEPTABLE -> same | |
| LOW-04 | TOLERABLE_ALARP -> TOLERABLE_ALARP | 0 -> 0 | none | |
| LOW-05 | TOLERABLE_ALARP -> TOLERABLE_ALARP | 0 -> 0 | none | |
| LOW-06 | TOLERABLE_ALARP -> TOLERABLE_ALARP | 1 -> 0 | HAZARD_NOT_ACCEPTABLE -> none | FLAG |
| LOW-07 | TOLERABLE_ALARP -> TOLERABLE_ALARP | 0 -> 0 | none | |
| LOW-08 | TOLERABLE_ALARP -> TOLERABLE_ALARP | 2 -> 1 | NO_CONTROLS -> same | |
| HH-01 | TOLERABLE_ALARP -> TOLERABLE_ALARP | 3 -> 2 | HAZARD_NOT_ACCEPTABLE -> same | |
| HH-02 | NOT_REACHED (CONTROLS) -> OVERRIDE | 1 -> 1 | HAZARD_NOT_ACCEPTABLE -> same | |
| HH-03 | TOLERABLE_ALARP -> TOLERABLE_ALARP | 0 -> 0 | none | |
| HH-04 | NOT_REACHED (CONTROLS) -> TOLERABLE_ALARP | 3 -> 2 | ALARP_ENGINEERING, HAZARD_NOT_ACCEPTABLE -> HAZARD_NOT_ACCEPTABLE | FLAG |
| HH-05 | NOT_REACHED (CONTROLS) -> TOLERABLE_ALARP | 4 -> 4 | NO_CONTROLS -> same | |
| HH-06 | NOT_REACHED (CONTROLS) -> TOLERABLE_ALARP | 2 -> 3 | HAZARD_NOT_ACCEPTABLE -> same | FLAG |
| HH-07 | TOLERABLE_ALARP -> TOLERABLE_ALARP | 1 -> 0 | HAZARD_NOT_ACCEPTABLE -> none | |
| HH-08 | TOLERABLE_ALARP -> TOLERABLE_ALARP | 1 -> 0 | HAZARD_NOT_ACCEPTABLE -> none | FLAG |
| HH-09 | TOLERABLE_ALARP -> TOLERABLE_ALARP | 0 -> 0 | none | FLAG |
| HH-10 | TOLERABLE_ALARP -> ACCEPTABLE | 0 -> 0 | none | |
| HH-11 | NOT_REACHED (CONTROLS) -> TOLERABLE_ALARP | 2 -> 1 | HAZARD_NOT_ACCEPTABLE -> same | |
| HH-12 | TOLERABLE_ALARP -> TOLERABLE_ALARP | 2 -> 3 | NO_CONTROLS -> same | |
| HH-13 | NOT_REACHED (ALARP_CHECKLIST, CONTROLS) -> NOT_REACHED (ALARP_CHECKLIST) | 2 -> 2 | HAZARD_NOT_ACCEPTABLE -> same | |
| HH-14 | NOT_REACHED (CONTROLS) -> OVERRIDE | 2 -> 3 | HAZARD_NOT_ACCEPTABLE -> same | FLAG |
| HH-15 | NOT_REACHED (CONTROLS) -> TOLERABLE_ALARP | 2 -> 3 | HAZARD_NOT_ACCEPTABLE -> same | |

Pass-1 blockers are the expected "this hazard needs a control" findings. Pass 2 has zero blockers on every entry (no loops).

## 4. Appendix A deltas

All 20 rows are loaded as `expectedProposed` with status PROPOSED. The round-1 `expected` values are unchanged. Scoring per 5J:
- inherent ±1 band = TOLERANCE;
- residual band and end state must match exactly;
- recommendations must stay within caps.

| Result | Rows |
|---|---|
| EXACT (15) | LOW-01, LOW-02, LOW-05, LOW-07, HH-01, HH-02, HH-03, HH-04, HH-05, HH-07, HH-09, HH-11, HH-12, HH-14, HH-15 |
| TOLERANCE (2) | LOW-06 and HH-08: inherent CRITICAL 15 vs HIGH expected. A credible S5 hazard (truck at the gate; damaged grinder cable) at L3. |
| MISMATCH (2) | LOW-04: residual MODERATE 4 vs LOW 3. The falling-items hazard is FALLING_OBJECT S4; rule (c) covers ladder falls only, so the engine was not changed. |
| | HH-13: end state NOT_REACHED (ALARP_CHECKLIST) vs TOLERABLE_ALARP. The fixture records the air receiver hydraulic test as overdue (`statutoryCurrent: false`). This is a real gap. |
| VERIFY_FIXTURE (1) | LOW-03, see below |

**Rows that moved for a reason other than the rubric rules:**
- **The verify-first route (VERIFY_CONTROLS):** LOW-06, HH-04, HH-06, HH-08, HH-09 and HH-14. I added this route in commit 4 so the unverified-controls scenario raises no new blocker. When the controls already recorded would settle a hazard once verified, the review asks the assessor to verify them instead of proposing another control. It is not one of A-I and needs your sign-off.
- **HH-08: the 5-hazard draft cap now drops "Sparks igniting combustibles".** Rule (d) moved that hazard to S3, which ties it with grinding noise and dust at 9. The cap breaks ties by name, so sparks drops out. The user is told and can expand to include it.

Every other movement is attributed in REPORT.md ("Why each row moved") to a rubric rule (B), the inherent integrity rule (F), the evidence rules (I), or the tolerability / route rules (A, C, D, H).

**LOW-03:**
- The fixture has one engineering control: "Marked pedestrian walkways with zebra crossings at road junctions". The engine credits it provisionally at L-1 because it is unverified. The road-crossing hazard therefore goes from L3xS4 = HIGH 12 to L2xS4 = HIGH 8.
- Verifying it does not lower the rating further: one engineering control earns one step.
- The crane-bay exclusion is administrative and is credited on the crane hazard, not the road crossing.
- So the residual is HIGH 8 and the end state is TOLERABLE_ALARP after one more control. Appendix A's own note says HIGH 8 is correct unless barriers or speed control are recorded.

## 5. Per-class results against the targets

All targets pass:
- **Overall one pass:** 24/25 (96%), 3 of them OVERRIDE.
- **Critical residual:** 1/25 (HH-15, with evidence).
- **Recommendations:** average 1.24, maximum 4.
- **Loops and new blockers:** zero loops; no new blockers on pass 2, on pass 3, or in the unverified scenario.
- **Determinism:** two in-process runs, one reverse-order run, and subprocesses with `PYTHONHASHSEED` 0 and 4242 all match.

| Class | Target | Result |
|---|---|---|
| Low (LOW-*) | 100% one pass | 8/8 |
| Low | average recommendations <= 1 | 0.38 |
| Low | no S<=3 hazard in ALARP | 0 hazards (and 0 in any class or pass) |
| Low | none labelled TOLERABLE_ALARP | Not met: 6 entries, each through an S>=4 hazard. This conflicts with Appendix A, which expects TOLERABLE_ALARP for LOW-03, LOW-05, LOW-06 and LOW-07 (see open question 2). |
| High (HH-*, BENCH-*) | >= 70% one pass, the rest only on real gaps | 16/17. HH-13 is open on its overdue statutory test. |
| Inherent | CRITICAL only where the rubric allows S5; zero actions on inherent alone | 0 and 0 |

## 6. Live Entry #1 evidence breakdown

**Not run: this round has no database access.** Run the read-only script:

```
cd safeops_360_bakend
venv/Scripts/python.exe scripts/hira_evidence_breakdown.py            # HIRA-2026-NW-007, entry 1
venv/Scripts/python.exe scripts/hira_evidence_breakdown.py --json entry1_evidence.json
```

It opens a READ ONLY transaction and rolls it back. For every incident and near miss of the plant it lists the date, area, window, area scope and matched families, and whether it counts. Then, per hazard, it lists the events used and the resulting L with its rationale.

The round-1 read-only run counted events in that area per family:
- LIFTING 6 (4 in the last 12 months)
- FALLING_OBJECT 13
- MANUAL_HANDLING 9
- SHARP 4
- DUST 4
- HEIGHT 0

These were counted with the round-1 keyword families. The script shows whether each crane event is in window, in the area, and lifts only the lifting hazards. A crane event that says a load fell also counts, correctly, for FALLING_OBJECT. The evidence tests (family, area, window, event ids in the rationale) pass offline.

## 7. Draft packs: distribution

Shown = what the user sees. Every pack shows the banner "Not validated for this industry: ratings are suggestions", 0 residual claims and 0 blockers. Would-be = the numbers if the pack were validated. Expected values are TODO(HSE lead) for all 15 activities.

| Pack | Inherent | Would-be residual | Would-be end state | Avg / max recs |
|---|---|---|---|---|
| APPAREL_TEXTILE | CRITICAL 2, HIGH 3 | HIGH 1, MODERATE 2, not assessed 2 | TOLERABLE_ALARP 5 | 1.2 / 3 |
| CHEMICAL_PHARMA | CRITICAL 3, HIGH 2 | CRITICAL 2, HIGH 1, LOW 1, not assessed 1 | ACCEPTABLE 2, TOLERABLE_ALARP 3 | 2.2 / 4 |
| WAREHOUSE_LOGISTICS | CRITICAL 4, HIGH 1 | HIGH 2, MODERATE 1, not assessed 2 | TOLERABLE_ALARP 4, NOT_REACHED (VERIFY_CONTROLS) 1 | 1.4 / 4 |

WH-04 would end on "verify the controls in place". That needs a site check, not another control. The incidents and plant data in the pack fixtures are illustrative and need HSE lead review.

## 8. Files for the HSE lead

- `tests/eval/hira_calibration/expected_bands_proposed.csv`: Appendix A as given.
- `tests/eval/hira_calibration/expected_bands_review.csv`: all 25 activities, with engine values, expected values, checks, `status` (TODO) and `reason`. The `status` and `hse_lead_note` columns are kept when the eval is re-run.

## 9. DDL

- `safeops_360/prisma/apply-hira-calibration-ddl.ts`: round 1, still pending on your side.
- `safeops_360/prisma/apply-hira-calibration-round3-ddl.ts`: written, not run. It adds a nullable `HiraStudy.hiraType` and a CHECK constraint, with no backfill. Until it is applied, the type is derived from the entry structure.

Packs need no schema: a plant's `rubricPack` is a key in the existing calibration config JSON.

## 10. Open questions

1. **"26 open points":** Appendix A has 20 rows and all 20 are loaded. Which 6 are missing?
2. **The LOW class "none labelled TOLERABLE_ALARP" target, and the checklist line that LOW-03 should not end TOLERABLE_ALARP, conflict with Appendix A.** Appendix A expects TOLERABLE_ALARP for LOW-03, LOW-05, LOW-06 and LOW-07. These entries carry S>=4 hazards: pedestrian vs vehicle, LPG, gate truck movement, grinder wheel burst. Rule A sends those to ALARP, and the engine follows rule A.
3. **LOW-04:** should falling items from low racks get an S3 rule, like ladder work below the height threshold? Today FALLING_OBJECT stays S4.
4. **Verify-first route:** accept it, or remove it and accept a new blocker for unverified controls in the unverified scenario (HH-05)?
5. **HH-08 hazard cap:** should a fire hazard outrank noise or dust at equal score, instead of ties breaking by name?
6. **STEEL_HEAVY is marked VALIDATED in its pack file,** as the current behaviour, so that "Validated for" is not empty. Please confirm or set it to DRAFT.
7. **No plant has a pack until its config sets `rubricPack`** (default none, per M). When the flag is turned on, set JSL Hisar and the NW plant to `STEEL_HEAVY`, or they will show "Not validated for this industry".
8. **HH-10:** "50 L acid carboys" now counts as a heavy load (litres read as kg) and stays S3. Confirm this reading of rule (b).

## 11. Not done, and why

- **The live Entry #1 breakdown was not run:** no database access this round. The script is ready.
- **The DDL was not run,** as instructed.
- **The UI was not checked in a browser.** No dev server was run; `tsc` passes.
- **There is no automated test for the split apply parts** (EXISTING_CONTROLS adds plant proposals only; CONFIRM_ROUTINE_CONTROLS adds routine controls only). That path goes through the HIRA router and the database.
- **Lint:** the repo-wide `ruff` count was not reduced (about 16,000 existing findings). In the calibration files it fell from 75 before round 2 to 67. The files new in commit 4 are clean.
