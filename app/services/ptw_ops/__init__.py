"""PTW Control Center v2 (docs/ptw-control-center/PLAN.md).

Package layout — pure logic first, DB at the edges:

  steps.py             lifecycle pipeline step keys (mirrors the SQL ptw_step_key())
  derived.py           derived permit statuses (expired_open, expiring_2h, ...)
  impairment_check.py  impairment × permit-type conflict decision
  simops.py            SIMOPS pair detection (same area / centroid distance)
  rules/               exception rules, one pure function per file
  engine.py            snapshot → rules → upsert/resolve PtwException rows
  payload.py           the single control-center payload (pure builder)
  loaders.py           async loaders that build the snapshot (few, batched queries)
  brief.py             shift brief via Claude (redacted counts only)
  notify.py            email + WhatsApp delivery, recorded on PtwException.notified
  jobs.py              scheduler job bodies (replaces the brief's Inngest functions)

Every loader scopes by plant (the caller's QueryScope) and every new table
carries tenantId, which QueryScope.apply() filters unconditionally.
"""
