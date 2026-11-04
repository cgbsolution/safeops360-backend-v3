# HIRA calibration v2: multi-HIRA test campaign

The offline half of the post-deploy campaign brief: the corpus, every check that
can run without a database, network or LLM, and the reports. The staging half
(live shadow sample, UI screenshots, DB row counts, flag OFF/SHADOW/ON per real
plant) needs a staging environment that does not exist yet.

## Run

```
cd safeops_360_bakend
.venv/Scripts/python.exe tests/campaign/hira_calibration/build_corpus.py   # writes corpus.json (deterministic)
.venv/Scripts/python.exe tests/campaign/hira_calibration/run_campaign.py   # ~8-10 min; writes out/
.venv/Scripts/python.exe tests/campaign/hira_calibration/llm_probe.py --budget-usd 2   # optional, live LLM (~$0.45, ~12 min)
.venv/Scripts/python.exe tests/campaign/hira_calibration/llm_probe.py --reanalyse      # re-derive I8/I13 from the saved answers, no API calls
```

Live shadow run (section 5), READ-ONLY against the database in `.env`:

```
.venv/Scripts/python.exe tests/campaign/hira_calibration/live_shadow.py --label after      # ~6 min, 157 entries
.venv/Scripts/python.exe tests/campaign/hira_calibration/live_shadow.py --label after --rerender   # report again, no DB access
```

`live_db.py` opens its own connection with `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY` (checked with
`SHOW transaction_read_only` before and after) and a client hook that refuses any non-read SQL; the app's own engine is
replaced by a stub that raises. Results go to `out/live/<label>/` (git-ignored: real plant data), with before/after row
counts. Step 5.2 (writing to `hira_copilot_shadow_runs`) is skipped because it is a write.

Run the probe before `run_campaign.py`: the campaign report merges `out/llm_probe_runs.json` in as the I8 / I13 rows marked
"live LLM" (live failures go to `defects.csv`). `run_campaign.py --no-subprocess` skips the two new-process (restart)
determinism runs (~4 min).

## Safety

- The runner overrides `DATABASE_URL`, `DATABASE_URL_SYNC` and `DIRECT_URL` with an unroutable host before importing
  the app, and counts (and refuses) any connection attempt. The count is in `out/run_manifest.json` and
  `out/db_row_count_diff.txt`.
- `--base-url` is refused unless it is listed in `staging_allowlist.json` (empty until staging exists).
- Engine code, rubric packs and expected bands are only read. The eval harness is imported; its `main()` (which rewrites
  `tests/eval/hira_calibration/REPORT.md`) is never called.
- `llm_probe.py` checks the worst-case cost of each call before sending it and stops before the cap.

## Files

| File | What it is |
|---|---|
| `build_corpus.py` -> `corpus.json` | 154 entries: S1 (25 regression fixtures), S2 (15 new heavy-industry), S3 (15 draft-pack fixtures, also run with no pack), S4 (4 activities x 4 matrix shapes), S5 (6 non-task types), S6 (15 edge cases), S7 (52 entries in 6 studies), ADV (10 prompt injections). Expected values are `TODO(HSE lead)` except S1's. |
| `run_campaign.py` | Runs the corpus through the v2 engine and the service layer (mocked DB), checks I1-I16 where possible offline, the cross-HIRA tests and the static UI / isolation checks. |
| `curated_findings.json` | Engine findings verified by hand while triaging (F-series), merged into `out/defects.csv`. |
| `llm_probe.py` | Live, budget-capped check of the Assistant model at temperature 0 (I8 repeats, I13 injection: each injected answer is compared with the clean answers on the same clean record). Writes `out/llm_probe_report.md` and `out/llm_probe_runs.json` (every answer, request id, tokens, cost). |
| `staging_allowlist.json` | Base URLs the campaign may run against. Empty. |
| `out/campaign_report.md` | Gate results, suite x invariant table, defects, cross-HIRA, performance, what was not run. |
| `out/defects.csv`, `out/invariant_results.csv` | Every failed check (D-series) and hand-verified finding (F-series); every check row. |
| `out/v1_vs_v2_deltas.csv` | Per entry: v1 model vs v2 inherent, residual, tolerability, recommendations, and why a band worsened. |
| `out/cross_hira_consistency.csv`, `out/study_rollup.json`, `out/monotonicity.json` | Section 7 tests. |
| `out/control_relevance_audit.csv`, `out/baseline_ppe_probe.json` | Supporting evidence for the control-credit findings. |
| `out/blind_scoring_sheet.csv` / `out/blind_scoring_key.csv` | 15 corpus entries (5 low / 5 medium / 5 high) for the HSE lead; the key holds the engine's answers. Synthetic corpus, not live data. |
| `out/live_shadow_report.md`, `out/db_row_count_diff.txt` | Placeholders that say why these were not run. |

## Still to do on staging

1. Deploy the `hira-calibration-v2` backend to an isolated environment with its own database (a copy of plant data),
   apply `prisma/apply-hira-calibration-ddl.ts` and `apply-hira-calibration-round3-ddl.ts` there, and add its URL to
   `staging_allowlist.json`.
2. Record row counts (HIRA study / entry / version / CAPA, `hira_copilot_shadow_runs`), run the live shadow sample
   (section 5), the UI pass (section 8) and the flag OFF/SHADOW/ON pass per plant, then the counts again.
3. Repeat the live LLM probe there with the production prompt (AgentPrompt table) and tools. It was run locally on
   2026-11-04 with the seed prompt file and no tools: 21 calls, $0.44.
