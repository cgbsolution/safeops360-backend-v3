# HIRA calibration v2: test campaign report (offline part + live LLM probe)

Engine 2.0.0; corpus 154 entries (2f0782085bdb); generated 2026-11-04T04:29:32+00:00.
The engine run uses no database and no network (DB connection attempts: 0). The live LLM probe (21 Assistant calls to `claude-haiku-4-5-20251001` at temperature 0, $0.44 of the $2.00 cap, 2026-11-04) is merged in as the I8 / I13 rows marked 'live LLM'. Staging items (live sample, UI screenshots, DB diff, flag toggling) were not run: there is no staging environment yet.

## Gate results

| Gate | Result | Value |
|---|---|---|
| Invariants I1-I16 on 100% of the S1-S7 corpus (offline checks + live LLM probe) | PASS | no failures |
| Live sample: no Critical residual without evidence; avg recs <= 3.0, none > 5 | NOT RUN | needs staging |
| Live sample: <= 10% worsen; no 2-band worsening unexplained | NOT RUN | needs staging (corpus: 68 worsen, 23 by 2+ bands vs the v1 model) |
| Convergence and determinism 100% | PASS | I8 fails 0, I9 fails 0 (includes 10 items re-run live through the Assistant) |
| UI 12/12 | NOT RUN | browser run needs staging; static source checks: see I12 / UI rows |
| DB diff: no unexpected changes | NOT RUN | no DB touched (0 connection attempts) |
| Cross-HIRA: unexplained spread > 1 level in <= 5% of groups | PASS | 0/9 groups flagged |
| Corpus: Critical residual <= 5% and only with evidence | FAIL | 9/133 (7%); without evidence: 7 |
| Corpus: avg recommendations <= 3.0, none > 5 | PASS | avg 0.98, max 5 |
| One-pass resolution: LOW-* 100% | PASS | 8/8 (100%) |
| One-pass resolution: high-hazard (S1 HH/BENCH + S2) >= 70% | PASS | 30/32 (94%) |
| One-pass resolution overall (validated corpus) >= 80% | PASS | 130/133 (98%) |
| S1 reproduces REPORT.md / results.json | PASS | identical |

## Suite x invariant (pass / fail / not applicable)

| Suite | I1 | I2 | I3 | I4 | I5 | I6 | I7 | I8 | I9 | I10 | I11 | I12 | I13 | I14 | I15 | I16 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| S1 | 4/0/21 | 25/0/0 | 25/0/0 | 25/0/0 | 25/0/0 | 25/0/0 | 25/0/0 | 25/0/0 | 25/0/0 | 24/0/1 | 0/0/25 | - | - | 1/0/24 | - | 1/0/0 |
| S2 | 4/0/11 | 15/0/0 | 15/0/0 | 15/0/0 | 15/0/0 | 15/0/0 | 15/0/0 | 17/0/0 | 15/0/0 | 12/0/3 | 0/0/15 | - | - | 1/0/14 | - | 7/0/0 |
| S3 | 5/0/10 | 15/0/0 | 15/0/0 | 15/0/0 | 15/0/0 | 15/0/0 | 15/0/0 | 15/0/0 | 15/0/0 | 15/0/0 | 30/0/0 | - | - | 0/0/15 | - | - |
| S4 | 0/0/16 | 16/0/0 | 16/0/0 | 16/0/0 | 16/0/0 | 16/0/0 | 16/0/0 | 16/0/0 | 16/0/0 | 16/0/0 | 0/0/16 | - | - | 0/0/16 | - | - |
| S5 | - | - | - | - | - | - | - | 6/0/0 | - | - | 16/0/0 | - | - | - | - | 16/0/0 |
| S6 | 10/0/13 | 15/0/0 | 23/0/0 | 15/0/0 | 15/0/0 | 15/0/0 | 15/0/0 | 15/0/0 | 15/0/0 | 15/0/0 | 0/0/15 | - | - | 0/0/15 | - | - |
| S7 | 0/0/52 | 52/0/0 | 52/0/0 | 52/0/0 | 52/0/0 | 52/0/0 | 52/0/0 | 52/0/0 | 52/0/0 | 25/0/27 | 0/0/52 | - | - | 0/0/52 | - | 27/0/0 |
| ADV | 0/0/10 | 10/0/0 | 10/0/0 | 10/0/0 | 10/0/0 | 10/0/0 | 10/0/0 | 18/0/0 | 10/0/0 | 10/0/0 | 0/0/10 | - | 27/0/0 | 0/0/10 | - | 20/0/0 |

Global checks:

- **I12** PASS: 3 inherent chip(s), all neutral (bg-slate-100 text-slate-700 border-slate-300); residual chips use status colours
- **UI-3-findings** PASS: default findings slice(0, 3): True; 'Show more' control: True
- **I15** PASS: static review: every loader query is scoped to the entry's plant, study or area (siblings by study/area id, which are plant-scoped)
- **MONOTONIC** PASS: adding a verified engineering control never raised a residual; removing evidence never raised L; adding an event never lowered L
- **S1-regression** PASS: S1 reproduces results.json / REPORT.md exactly

## Engine findings verified by hand (F-series)

Found while triaging the automated results; each was reproduced directly against the engine. They sit outside the literal I1-I16 wording but change what users see.

### F01 [fixed (was major)] Control relevance: incidental keywords exclude the controls built for the hazard

- Entries: S2-02, S4-CAL_3X3-D, S4-CAL_4X4-D, S4-CAL_6X6-D, S4-CAL_5X5_OVR-D, S6-14, HH-15
- Observed: 'Remote-operated tap hole drill and mud gun from the cabin' is read as a HEAT control ('cabin') and ruled not relevant to molten-metal splash (S2-02 residual CRITICAL 15). 'Remote skimming machine with heat shield' (HEAT via 'heat') gets no credit for slag splash (S4 D on every matrix). 'Wheel stopper / bund at the hopper edge' is read as CHEMICAL ('bund') and ruled not relevant to 'Reversing tipper' and 'Tipper topples at the hopper edge' (HH-15, one of the original 25 fixtures, residual CRITICAL 15). 'Driver stays in the cabin during loading' (HEAT) is not relevant to a billet falling from the magnet (S6-14, CRITICAL 15). 'Casting SOP ... runner pre-heating check' (HEAT) is not relevant to molten splash.
- Expected: A control is credited against the hazard it was written for; an incidental word ('cabin', 'heat shield', 'pre-heating', 'bund') does not move it to another family and exclude it.
- Likely cause: risk_engine.CONTROL_KEYWORDS: HEAT includes 'cabin' and the stem 'heat'; CHEMICAL includes 'bund'. applies_to() excludes a control whose families do not include the hazard's family or a RELATED one (MOLTEN is related only to THERMAL and FIRE_EXPLOSION, not HEAT).
- Repro: eng.applies_to(eng.Control(None,'ENGINEERING','Remote skimming machine with heat shield'), eng.Hazard('h','Molten slag splash','FIRE_MOLTEN_METAL','fire_explosion','Molten slag splash at the converter mouth')) -> (False, 'addresses heat, not molten'). Full list: out/control_relevance_audit.csv
- Fix: risk_engine.CONTROL_KEYWORDS: HEAT now lists heat-stress words only ('cabin', the bare stem 'heat', 'rest', 'cool' removed); heat shields are THERMAL and no longer count against a metal splash; MOLTEN gains tap hole / mud gun / skimming / pre-heat / runner; 'bund' alone is no longer CHEMICAL. Live data showed more of the same ('drain' = slip, 'interlock' = machinery only, ventilation / SCBA / positive isolation not linked to toxic vapour): fixed too.

### F02 [fixed (was major)] An engineering control with no family keyword is credited against every hazard in the entry

- Entries: S2-02, LOW-06, S2-06, HH-10
- Observed: 'Fixed CO detectors on the cast house floor with alarm' earns L-1 on molten-metal splash, heat stress and noise (S2-02). 'Boom barrier with a speed breaker' earns credit on heat stress and slips (LOW-06). 'Remote racking device for the 33 kV VCB' earns credit on the transformer oil fire and on tripping over trench covers (S2-06). 'Closed transfer by acid-resistant pump' earns credit on slips and manual handling (HH-10). 15 such pairs on the corpus.
- Expected: A control with no recognisable link is not credited to unrelated hazards; the user is asked which hazard it controls (or it counts only where it plausibly applies).
- Likely cause: applies_to() returns (True, 'not linked to a hazard: counted for the whole activity') whenever control_families() is empty and the control has no hazard links.
- Repro: eng.applies_to(eng.Control(None,'ENGINEERING','Fixed CO detectors on the cast house floor with alarm'), eng.Hazard('h','Molten metal splash at the tap hole','FIRE_MOLTEN_METAL','fire_explosion','Hot metal splash')) -> (True, 'not linked to a hazard: counted for the whole activity')
- Fix: risk_engine.applies_to: an unlinked engineering / substitution / elimination control that names no hazard counts for the whole activity only in a one-family entry or when its text says so ('for this activity'); otherwise it earns no credit and the review raises LINK_CONTROL. Keyword gaps behind the examples were filled (CO detection, boom barrier / speed breaker, VCB / kV / racking, acid / closed transfer).

### F03 [partly fixed (was minor)] Hazard family from free text: non-English wording and incidental nouns misclassify hazards

- Entries: S6-05
- Observed: Hindi 'भार गिरना' (load falls from the crane, fatal) gets family GENERAL: no severity rubric, the banksman control is ruled not relevant, residual CRITICAL 15 and the hazard is routed to the elevated override. Hinglish 'Haath dabna' (finger pinched between coil and trailer) is classed VEHICLE because of the word 'trailer': severity raised S3 -> S4 and 'Reversing camera / proximity sensor' and 'segregated pedestrian walkway' are recommended for a finger pinch.
- Expected: A hazard whose family cannot be read is flagged for a hazard-master link instead of defaulting to GENERAL; an incidental noun does not reclassify a pinch hazard.
- Likely cause: hazard_family() falls back to English keywords when the hazard has no master code or category. Minor because Assistant and Copilot drafts use hazard-master items (code and category set), so this mainly affects free-text or imported hazards without a master link.
- Repro: corpus item S6-05 (run_campaign.run_one)
- Fix: review_v2 raises HAZARD_TYPE_UNKNOWN for a hazard with no master code whose type cannot be read (the Hindi example). The Hinglish 'trailer' wording is still read as VEHICLE.

### F04 [fixed (was minor)] A task-based entry with no hazards gets no finding and can be submitted

- Entries: S6-01
- Observed: review_v2.evaluate() on an entry with an empty hazard list returns findings [] and canSubmit True (status OPEN).
- Expected: An advisory such as 'No hazards recorded' (the structural review for non-task studies already raises STRUCT_NO_HAZARDS).
- Likely cause: evaluate() has no check for an empty hazard list on the calibrated path.
- Repro: corpus item S6-01
- Fix: review_v2 raises the NO_HAZARDS advisory (the same class as STRUCT_NO_HAZARDS on non-task studies), so the empty entry is no longer silent.

### F05 [fixed (was minor)] Controls with no hierarchy silently earn nothing; the advice shown does not fix it

- Entries: S6-08
- Observed: 'Pull cord switch along the conveyor' and 'LOTO before scraper change' with an empty hierarchy are not in the credits list at all; the residual equals the inherent (HIGH 12) and the only related advice is 'Rate the effectiveness of the listed controls'.
- Expected: A finding that asks for the control's hierarchy (rating its effectiveness does not change anything here).
- Likely cause: residual_from_controls() buckets usable controls by hierarchy; a control with no hierarchy falls into no bucket and gets no credit row.
- Repro: corpus item S6-08
- Fix: review_v2 raises CONTROL_HIERARCHY_MISSING.

### F06 [fixed (was minor)] Duplicate hazards are kept and assessed twice

- Entries: S6-04
- Observed: Two identical 'Strap recoil' hazards (same code and text) are both kept and both routed to CONFIRM_CONTROLS; no finding mentions the duplicate.
- Expected: Duplicates merged or flagged.
- Likely cause: No duplicate-hazard check in calibrate_draft() or evaluate().
- Repro: corpus item S6-04
- Fix: review_v2 raises DUPLICATE_HAZARD (flagged, not merged).


## Critical residuals on the corpus, by cause

- HH-15 Reversing tipper CRITICAL 15 (L3xS5), evidence: yes. controls recorded but ruled not relevant to this hazard: 'Wheel stopper / bund at the hopper edge' (names no hazard and is not linked: link ); 'Grizzly grating (200 mm) over the hopper' (addresses height, not vehicle)
- S2-03 Fall from the crane girder walkway CRITICAL 15 (L3xS5), evidence: no. controls recorded but ruled not relevant to this hazard: 'Work permit with electrical isolation of the ' (addresses electrical/lifting/machinery, ); 'Second crane stopped and its controller tagge' (addresses lifting, not height)
- S4-CAL_5X5_OVR-D Molten slag splash CRITICAL 15 (L3xS5), evidence: yes. no control applies to this hazard
- S6-05 भार गिरना CRITICAL 15 (L3xS5), evidence: no. controls recorded but ruled not relevant to this hazard: 'Banksman har lift ke liye / हर लिफ्ट के लिए ब' (addresses lifting, not general)
- S6-09 Fall through a fragile roof sheet CRITICAL 15 (L3xS5), evidence: no. controls recorded but ruled not relevant to this hazard: 'Anti-slip safety shoes' (addresses slip_trip, not height)
- S7-ST2-02 Molten metal splash CRITICAL 15 (L3xS5), evidence: no. controls recorded but ruled not relevant to this hazard: 'EOT crane overload cut-out and hoist upper li' (addresses lifting, not molten); 'Banksman for every crane lift' (addresses lifting, not molten)
- S7-ST2-06 Molten metal splash CRITICAL 15 (L3xS5), evidence: no. controls recorded but ruled not relevant to this hazard: 'EOT crane overload cut-out and hoist upper li' (addresses lifting, not molten); 'Banksman for every crane lift' (addresses lifting, not molten)
- S7-ST4-02 Caught in conveyor nip point CRITICAL 15 (L3xS5), evidence: no. controls recorded but ruled not relevant to this hazard: 'EOT crane overload cut-out and hoist upper li' (addresses lifting, not machinery); 'Banksman for every crane lift' (addresses lifting, not machinery)
- S7-ST4-06 Caught in conveyor nip point CRITICAL 15 (L3xS5), evidence: no. controls recorded but ruled not relevant to this hazard: 'EOT crane overload cut-out and hoist upper li' (addresses lifting, not machinery); 'Banksman for every crane lift' (addresses lifting, not machinery)

## Control relevance audit (control_relevance_audit.csv)

153 engineering-control/hazard pairs ruled not relevant, 4 keyword-less engineering controls credited to a hazard. Candidates for the HSE lead; the clear keyword collisions are in the F-series above.


## Defects

| ID | HIRA | Invariant | Severity | Observed |
|---|---|---|---|---|
| F01 | S2-02 S4-CAL_3X3-D S4-CAL_4X4-D S4-CAL_6X6-D S4-CAL_5X5_OVR-D S6-14 HH-15 | control-relevance (residual correctness) | fixed (was major) | 'Remote-operated tap hole drill and mud gun from the cabin' is read as a HEAT control ('cabin') and ruled not relevant to molten-metal splash (S2-02 residual CRITICAL 15). 'Remote skimming machine with heat shield' (HEAT |
| F02 | S2-02 LOW-06 S2-06 HH-10 | control-relevance (false credit) | fixed (was major) | 'Fixed CO detectors on the cast house floor with alarm' earns L-1 on molten-metal splash, heat stress and noise (S2-02). 'Boom barrier with a speed breaker' earns credit on heat stress and slips (LOW-06). 'Remote racking |
| F03 | S6-05 | hazard-family (edge case) | partly fixed (was minor) | Hindi 'भार गिरना' (load falls from the crane, fatal) gets family GENERAL: no severity rubric, the banksman control is ruled not relevant, residual CRITICAL 15 and the hazard is routed to the elevated override. Hinglish ' |
| F04 | S6-01 | completeness (edge case) | fixed (was minor) | review_v2.evaluate() on an entry with an empty hazard list returns findings [] and canSubmit True (status OPEN). |
| F05 | S6-08 | completeness (edge case) | fixed (was minor) | 'Pull cord switch along the conveyor' and 'LOTO before scraper change' with an empty hierarchy are not in the credits list at all; the residual equals the inherent (HIGH 12) and the only related advice is 'Rate the effec |
| F06 | S6-04 | completeness (edge case) | fixed (was minor) | Two identical 'Strap recoil' hazards (same code and text) are both kept and both routed to CONFIRM_CONTROLS; no finding mentions the duplicate. |

## Determinism and order independence (I8)

- in-process #2: identical
- reverse order: identical
- random order seed 1: identical
- random order seed 2: identical
- random order seed 3: identical
- parallel, 10 workers: identical
- new process (restart), PYTHONHASHSEED=0: identical
- new process (restart), PYTHONHASHSEED=4242: identical

## Live LLM probe (Assistant, flag ON path)

`claude-haiku-4-5-20251001`, temperature 0, system prompt sha256 b379e8585cd40a71..., engine 2.0.0; 21 calls, $0.4378 spent. Each answer went through the same path the flag-ON Assistant uses (from_assistant_suggestion -> calibrate_draft, STEEL_HEAVY). Full tables: llm_probe_report.md.

- I8, repeats: 10 of 10 items gave identical engine output; LLM text identical in 10 of 10.
- I13, injections: 7 of 7 injected items changed no rating. Each injected run is compared with the clean runs on the same clean record, so an extra control slot is not counted as an effect.
- The LLM quoted the injected band words in its own text for ADV-02, ADV-04, ADV-05 (it flagged them as not a technical calibration); no rating moved.
- The LLM wrote residual numbers in its residual_*_guidance text in every run (the prompt asks for that field); with the flag ON the service replaces both fields with the engine's own text, so they do not reach the user.

## Cross-HIRA consistency

9 groups of 3+ similar hazards; flagged (unexplained spread > 1): 0.

| Group | Size | L spread | S spread | Reason | Flagged |
|---|---|---|---|---|---|
| LIFTING: Dropped coil from the EOT crane. 10 tonne coil falls from th | 52 | 2-3 | 4-5 | - |  |
| MACHINERY: Strap recoil. Steel strap whips back when cut and strikes th | 3 | 3-3 | 3-4 | - |  |
| SLIP_TRIP: Trip on dunnage. Trip on timber dunnage in the bay | 16 | 3-3 | 2-3 | - |  |
| MACHINERY: Caught in the conveyor tail pulley. Shovel and arm drawn int | 5 | 3-3 | 5-5 | - |  |
| MACHINERY: Hand caught between chock and roll. Hand caught between choc | 5 | 3-3 | 4-4 | - |  |
| MOLTEN: Molten metal splash. Molten metal splash during ladle moveme | 4 | 3-3 | 5-5 | - |  |
| SHARP: Cut from sharp edges. Cut to the hands from sharp edges of f | 4 | 3-3 | 2-2 | - |  |
| VEHICLE: Struck by the coil car. Coil car strikes an operator at the  | 4 | 3-3 | 4-4 | - |  |
| VEHICLE: Trailer movement in the bay. Trailer reverses onto a lashing | 4 | 3-3 | 4-4 | - |  |

Monotonicity: no violations (verified engineering control never raised a residual; removing evidence never raised L; adding an event never lowered L).

## S4 matrix shapes (relative order vs STD_5X5)

- PASS relative order preserved | CAL_3X3: A=MODERATE/MODERATE B=MODERATE/MODERATE C=HIGH/HIGH D=CRITICAL/HIGH | STD_5X5: A=MODERATE/MODERATE B=HIGH/MODERATE C=CRITICAL/HIGH D=CRITICAL/CRITICAL
- PASS relative order preserved | CAL_4X4: A=MODERATE/MODERATE B=HIGH/MODERATE C=HIGH/HIGH D=HIGH/HIGH | STD_5X5: A=MODERATE/MODERATE B=HIGH/MODERATE C=CRITICAL/HIGH D=CRITICAL/CRITICAL
- PASS relative order preserved | CAL_5X5_OVR: A=MODERATE/LOW B=HIGH/MODERATE C=CRITICAL/HIGH D=CRITICAL/CRITICAL | STD_5X5: A=MODERATE/MODERATE B=HIGH/MODERATE C=CRITICAL/HIGH D=CRITICAL/CRITICAL
- PASS relative order preserved | CAL_6X6: A=MODERATE/LOW B=HIGH/MODERATE C=HIGH/HIGH D=HIGH/HIGH | STD_5X5: A=MODERATE/MODERATE B=HIGH/MODERATE C=CRITICAL/HIGH D=CRITICAL/CRITICAL

## S7 study rollup and recommendation load

The v2 engine has no study-level risk summary, so 'study summary equals the worst-hazard rollup' cannot be checked; the harness rollup is listed for the HSE lead.

| Study | Entries | Worst entry rating | Sum of recs | Unique recs | Unique < sum | Recs by owner role | Roles > 10 |
|---|---|---|---|---|---|---|---|
| ST1 | 9 | HIGH 12 (L3xS4) | 6 | 2 | yes | Maintenance Head: 2 | - |
| ST2 | 8 | CRITICAL 15 (L3xS5) | 6 | 2 | yes | Maintenance Head: 2 | - |
| ST3 | 10 | HIGH 12 (L3xS4) | 8 | 2 | yes | Maintenance Head: 2 | - |
| ST4 | 8 | CRITICAL 15 (L3xS5) | 6 | 2 | yes | Maintenance Head: 2 | - |
| ST5 | 9 | MODERATE 5 (L1xS5) | 0 | 0 | no |  | - |
| ST6 | 8 | HIGH 12 (L3xS4) | 6 | 2 | yes | Maintenance Head: 2 | - |

## v1 vs v2 on the corpus (validated, task-based entries)

- v1_inherent: CRITICAL 86, HIGH 38, MODERATE 7, v1 model not computable: KeyError 1, v1 model not computable: ValueError 1
- v2_inherent: - 1, CRITICAL 78, HIGH 47, MODERATE 7
- v1_residual: - 2, CRITICAL 7, HIGH 37, LOW 38, MODERATE 49
- v2_residual: CRITICAL 9, HIGH 58, INHERENT_ONLY 10, LOW 3, MODERATE 52, NOT_ASSESSED 1
- recommendations per entry: v1 2.17, v2 0.98
- residual band worse under v2: 68 (BENCH-6, LOW-01, LOW-03, LOW-04, LOW-05, LOW-07, HH-01, HH-02, HH-04, HH-06, HH-07, HH-13); 2+ bands: 23

## Performance (engine only, no LLM)

- engineDraftMs: n=108, p50 45.0 ms, p95 103.9 ms, max 166.3 ms
- engineReviewMs: n=93, p50 5.2 ms, p95 13.6 ms, max 35.7 ms
- engineStudyRunMs: n=37, p50 469.6 ms, p95 778.0 ms, max 955.4 ms

### LLM (live probe, one Assistant call per entry)

- Latency: p50 34.0 s, p95 54.9 s, max 55.0 s. **Flagged (over 30 s): 19 of 21 calls.** Output length drives it (avg 3579 output tokens per call).
- Tokens: input 29730, output 75174, cache write 9908, cache read 198160.
- Cost: $0.0208 per entry, about **$2.08 per 100 entries** and $20.85 per 1,000 (warm prompt cache). The engine itself adds no cost.
- A 10-entry study through the Assistant is about 10 calls: roughly 6 minutes if run one after another, at about $0.21.

## UI static checks (frontend source)

- line 330: RatingChip r={h.inherent} neutral=True ok
- line 336: RatingChip r={h.residual} neutral=False ok
- line 387: RatingChip r={after} neutral=False ok
- line 667: RatingChip r={entry.inherent} neutral=True ok
- line 680: RatingChip r={entry.residual} neutral=False ok
- line 821: RatingChip r={h.residual} neutral=False ok
- line 825: RatingChip r={h.inherent} neutral=True ok

## I15 tenant isolation (static review of loader queries)

- Incidents (evidence): yes
- Near misses (evidence): yes
- Equipment / statutory register: yes
- Open CAPAs (entry review): yes
- Open CAPAs (Copilot draft): yes
- Regulation map: yes
- Sibling entries (same study or same area): yes
- Draft siblings (same study): yes

## Not run, and why

- Live shadow run (section 5), blind scoring on live entries, DB row counts, flag OFF/SHADOW/ON per real plant, UI screenshots (section 8), end-to-end latency through the deployed API: no staging environment exists (backend `hira-calibration-v2` is not deployed; the only DB is production).
- I15 dynamic check (plant A never reads plant B): static review only; the engine itself is pure and takes no plant id.
- Live LLM probe: small by design (10 items, 21 calls, Haiku 4.5, seed prompt file with no tools). Production loads the prompt from the AgentPrompt table and runs with tools, so a staging run should repeat it.
- Section 7 'study summary equals worst-hazard rollup': v2 has no study-level summary to compare against.

