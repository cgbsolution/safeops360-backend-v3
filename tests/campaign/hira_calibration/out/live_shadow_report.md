# Live shadow report: NOT RUN

The live-data shadow run (section 5 of the campaign brief) needs a staging backend with the v2 code deployed, the
calibration DDL applied, and a copy of plant data. None of these exist yet:

- the backend deploys only `main` (to Azure); `hira-calibration-v2` is not deployed anywhere;
- the Vercel preview of the branch is frontend only;
- the only database configured is the production Supabase project (real JSL Hisar data).

Nothing was read from or written to it. When staging exists, run section 5 against it; the offline corpus results are in
campaign_report.md. The blind scoring sheet in this folder is drawn from the synthetic campaign corpus, not live data.
