"""Reconcile the SignalRule catalog from the code registry.

Strictly optional: the runner calls `sync_rule_catalog()` at the start of every
run, so the engine works on a fresh deployment with no seed step. This exists so
an operator can materialise and inspect the catalog BEFORE the first nightly
run — and so the admin panel has rows to show on day zero.

Non-destructive. Unlike `seed_workflows` / `seed_rbac`, this deletes nothing:
it inserts missing rules, refreshes code-owned metadata, and leaves the
`enabled` flag and every SignalRuleOverride untouched. An operator's decision to
disable a rule survives every deploy.

Run from safeops_360_bakend (after create_signal_engine_tables):

    python -m scripts.seed_signal_rules
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.core.db import AsyncSessionLocal
from app.models.signal_engine import SignalRule
from app.services.signal_engine.runner import sync_rule_catalog


async def main() -> int:
    async with AsyncSessionLocal() as db:
        before = len((await db.execute(select(SignalRule.id))).scalars().all())
        await sync_rule_catalog(db)
        rows = (await db.execute(select(SignalRule).order_by(SignalRule.code))).scalars().all()
        print(f"SignalRule catalog: {before} → {len(rows)}\n")
        for r in rows:
            state = "enabled" if r.enabled else "DISABLED"
            print(f"  {r.code}  {r.name}")
            print(f"      {r.category} · {r.defaultSeverity} · {state} · window {r.windowDays}d")
            print(f"      thresholds: {r.defaultThresholds}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
