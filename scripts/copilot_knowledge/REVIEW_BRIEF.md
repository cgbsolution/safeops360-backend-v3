# Safety observation row review — brief

## Why this review exists
SafeOps360 is an EHS platform. Its in-app AI copilot will suggest values while a user records a safety
observation at a stainless steel plant (category, risk level, fatal-potential flag, responsible function,
corrective action). We are building a reference library from the plant's real register of 6,308
observations (Apr–Sep 2026). Every row must be reviewed by a careful EHS reviewer before anything goes
into the library: bad rows (wrong category, under-rated risk, vague text, weak closure, personal names)
must not teach the copilot bad habits, and good rows become worked examples.

You are reviewing ONE chunk (~631 rows). Every row in your chunk must get exactly one output line.

## Input fields (one JSON object per line)
code (UA-… = unsafe act, UC-… = unsafe condition), date, type, dept, area, section, location,
cat (category code), category, obs (observation text), action (action plan; only unsafe conditions have one),
severity ("General" or "Fatal Potential"), risk (Low/Medium/High), function (responsible function),
target_days (days from report to target date), status, special ("NDO" or "None"), impacted (for acts),
closing (closing remark — what was done), closing_rejected (a reviewer rejected an earlier closure, with reason),
exceptional, approve, dup_text_count (how many rows in the full register have the identical obs text).

The plant's own taxonomy:
- Unsafe ACT categories: A1 Tools, Equipment & Machine Guarding (unsafe use of tools/unguarded machines) ·
  A2 Environmental & Lone Work · A3 Procedures, PTW & Isolation · A4 Competency & Fitness for Duty ·
  A5 PPE & Clothing Compliance · A6 Indiscipline · A7 Material Handling · A8 Workplace Protocol &
  Unauthorized Access · A9 Road Safety · A10 Housekeeping
- Unsafe CONDITION categories: C1 Defective Equipment · C2 Defective Workplace · C3 Electrical Hazard ·
  C4 Ergonomics · C5 Fire Hazard · C6 Fugitive Emission · C7 Hazardous Substance · C8 Heat/Cold ·
  C9 Housekeeping · C10 Illumination · C11 Machine Guarding · C12 Noise · C13 Passage Obstruction ·
  C14 Radiation · C15 Signages · C16 Slip/Trip/Fall · C17 Tools & Tackles · C18 Transportation ·
  C19 Ventilation · C20 Work at Height
- Observed plant practice: "Fatal Potential" ⇒ risk High ⇒ target 0–1 day. Medium ⇒ ~13–14 days.
  Low ⇒ ~29–30 days.

Risk rubric for your judgement (be conservative — flag only clear cases, not borderline ones):
- High / Fatal Potential: could plausibly kill or permanently disable — unprotected edge/opening/fragile
  roof at height, live exposed conductors within reach, unguarded rotating/nip points within reach of
  people, molten metal/slag splash exposure, people under suspended loads or defective lifting gear in use,
  vehicle–pedestrian conflict, gas leak or confined-space atmosphere, pressurised system failure.
- Medium: could cause a lost-time injury (fracture, laceration, burn, electric shock without exposure
  at reach, slip on oil in a busy walkway, missing guard away from normal reach).
- Low: first-aid or nuisance level, housekeeping, signage, cosmetic damage.

## What to output — one JSON line per input row, same order
```
{"code":"UC-66342","verdict":"OK|ISSUE|EXCLUDE","issues":["..."],
 "suggested_cat":"C3","suggested_risk":"High","hazard":"exposed motor terminal wiring",
 "tags":["exposed_wiring","earthing"],"control_level":"ENGINEERING",
 "closure":"GOOD|WEAK|MISSING|NA","exemplar":true,
 "clean_obs":"...","clean_action":"...","note":"..."}
```
- verdict: OK (usable as is), ISSUE (usable but has a flagged problem), EXCLUDE (must not be learned from:
  garbled, not a safety issue, meaningless, or a test entry).
- issues (codes, may be empty): CATEGORY_MISMATCH · RISK_UNDERRATED · RISK_OVERRATED ·
  FATAL_POTENTIAL_MISSED · TYPE_MISMATCH (an act recorded as a condition or vice versa) ·
  VAGUE_OBSERVATION · MULTIPLE_HAZARDS · ACTION_WEAK (unsafe condition with missing/generic/wrong action) ·
  ACTION_NOT_ADDRESSING_HAZARD · CLOSURE_WEAK (closed with "done"/"ok"/unrelated remark) ·
  PII_IN_TEXT (a person's name, employee id or phone number inside obs/action/closing text) ·
  NON_ENGLISH_OR_GARBLED · NOT_A_SAFETY_ISSUE · TARGET_INCONSISTENT.
- suggested_cat / suggested_risk: only when you flag CATEGORY_MISMATCH / RISK_* (omit otherwise).
  For an unsafe act keep an A-code, for a condition a C-code, unless you flag TYPE_MISMATCH.
- hazard: 2–6 words naming the hazard itself (not the location), lower case.
- tags: 1–3 from this fixed list only:
  exposed_wiring, electrical_panel, earthing, cable_damage, electrical_shock, fall_from_height,
  open_edge_railing, floor_opening, ladder_scaffold, fragile_roof, slip_oil_water, trip_hazard,
  floor_damage, rotating_unguarded, nip_point, conveyor_roller, crane_hoist, lifting_tackle,
  suspended_load, falling_object, material_stacking, coil_handling, vehicle_pedestrian, forklift,
  road_condition, combustible_material, fire_equipment, hot_metal_slag, hot_surface_burn, gas_leak,
  gas_cylinder, chemical_acid, dust_fume, noise, heat_stress, illumination, ventilation,
  confined_space, ppe_noncompliance, unsafe_behaviour, permit_isolation, housekeeping_scrap,
  passage_blocked, signage, emergency_egress, defective_tool, pressure_system, hydraulic_oil_leak,
  structural_damage, ergonomics, lone_work, water_drainage, other
- control_level: the hierarchy level of the action plan (or of the closing remark if no action plan):
  ELIMINATION · SUBSTITUTION · ENGINEERING (guard, railing, cover, interlock, repair/replace hardware,
  insulation, earthing, lighting) · ADMINISTRATIVE (instruction, training, SOP, signage, inspection,
  housekeeping) · PPE · NONE.
- closure: GOOD (remark shows the hazard was fixed), WEAK, MISSING (closed with no remark), NA (pending).
- exemplar: true when the row is a clear, specific observation AND has a specific, correct fix (from the
  action plan or the closing remark). These become the copilot's worked examples, so be selective but not
  stingy — typically 25–45% of rows. Never mark an EXCLUDE row or a row with CATEGORY_MISMATCH,
  RISK_UNDERRATED or FATAL_POTENTIAL_MISSED as exemplar.
- clean_obs / clean_action: ONLY when exemplar is true. Rewrite into clear, professional English
  (fix spelling and grammar, keep the technical facts, ≤ 220 characters each). Remove every person's
  name, employee id and phone number. Keep equipment and area names (e.g. "AP4 line", "EOT crane").
  clean_action is the corrective action that should be taken (use the action plan, improved by what the
  closing remark shows was actually done).
- note: ≤ 120 characters, only when it explains a flag (e.g. "live busbar at reach → fatal potential").
  Omit otherwise.
- Omit empty/irrelevant keys to keep lines short (e.g. omit issues when empty, omit clean_* when not
  exemplar). Use ASCII quotes, valid JSON, one object per line.

## How to work
1. Read your chunk with the Read tool in slices of ~80 lines (offset/limit). Judge every row yourself —
   do not write a keyword script to label rows; the value of this review is your judgement per row.
2. After each slice, append that slice's output lines to your output file. Write the first slice with the
   Write tool, then append later slices with Bash: `cat >> FILE <<'EOF' … EOF` (quote the heredoc marker).
3. When done, verify with Bash that the output has exactly as many lines as the input and every line
   parses as JSON (e.g. a short python one-liner with
   `E:/main-safeops-docs/safeops_360_bakend/.venv/Scripts/python.exe -I -c "..."`). Fix any gaps.
4. Do not modify the input chunk. Do not write anywhere except your output file.
5. Final message (≤ 200 words): rows reviewed, counts per verdict, top 5 issue types with counts,
   exemplar count, and 3–5 notable patterns you saw (e.g. recurring hazards in this chunk, systematic
   mis-categorisation, risk under-rating patterns).
