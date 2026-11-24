"""PTW Control Center payload performance check (brief §11: p95 < 500 ms at 10k permits).

    python -m scripts.ptw_cc_perf --plant <plantId> [--runs 30]
    ALLOW_DEMO_SEED=1 python -m scripts.ptw_cc_perf --plant <plantId> --add-history 10000

--add-history bulk-inserts N closed historical permits into the plant (so the
plant holds ≥10k permits) before timing. Timing covers exactly what one
GET /api/ptw/control-center does after auth: loaders.load + payload.build.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.core.db import AsyncSessionLocal
from app.models._base import gen_id
from app.services.ptw_ops import loaders
from app.services.ptw_ops.payload import build

TYPES = ["HOT_WORK", "CONFINED_SPACE", "WORK_AT_HEIGHT", "EXCAVATION", "ELECTRICAL_LOTO", "LIFTING", "GENERAL_COLD"]


async def add_history(plant: str, n: int) -> None:
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        meta = (await db.execute(text(
            """SELECT (SELECT id FROM "User" WHERE "plantId" = :p LIMIT 1) AS u,
                      (SELECT array_agg(id) FROM "Area" WHERE "plantId" = :p) AS areas"""), {"p": plant})).one()
        if not meta.u:
            sys.exit("Plant has no users to originate permits")
        rnd = random.Random(1)
        rows = []
        for i in range(n):
            vf = now - timedelta(days=rnd.uniform(3, 365))
            vt = vf + timedelta(hours=rnd.choice((4, 6, 8)))
            rows.append({"id": gen_id(), "num": f"PTW-PERF-{plant[:6]}-{i:06d}", "t": rnd.choice(TYPES), "p": plant,
                         "a": rnd.choice(meta.areas) if meta.areas else None, "vf": vf, "vt": vt, "u": meta.u,
                         "c": vt + timedelta(minutes=30)})
        for chunk in range(0, n, 1000):
            await db.execute(text(
                """INSERT INTO "Permit" (id, number, type, "plantId", "areaId", location, "scopeOfWork", "validFrom",
                                        "validTo", "originatorId", status, "closedAt", "createdAt", "updatedAt",
                                        "closureType")
                   VALUES (:id, :num, CAST(:t AS "PermitType"), :p, :a, 'perf', 'perf history', :vf, :vt, :u,
                           'CLOSED', :c, :vf - interval '1 hour', :c, 'WORK_COMPLETED')"""), rows[chunk:chunk + 1000])
        await db.commit()
    print(f"inserted {n} historical permits")


async def time_payload(plant: str, runs: int) -> None:
    async with AsyncSessionLocal() as db:
        n = (await db.execute(text('SELECT count(*) FROM "Permit" WHERE "plantId" = :p'), {"p": plant})).scalar()
        snap, ex = await loaders.load(db, plant_id=plant)   # warm the schema cache + pool
        build(snap, ex)
        samples = []
        for _ in range(runs):
            t0 = time.perf_counter()
            snap, ex = await loaders.load(db, plant_id=plant)
            build(snap, ex)
            samples.append((time.perf_counter() - t0) * 1000)
    samples.sort()
    p95 = samples[max(0, int(round(0.95 * len(samples))) - 1)]
    print(f"plant permits: {n}  runs: {runs}  p50: {statistics.median(samples):.0f} ms  "
          f"p95: {p95:.0f} ms  max: {samples[-1]:.0f} ms  → {'PASS' if p95 < 500 else 'FAIL'} (< 500 ms)")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plant", required=True)
    ap.add_argument("--runs", type=int, default=30)
    ap.add_argument("--add-history", type=int, default=0)
    a = ap.parse_args()
    if a.add_history:
        if os.getenv("ALLOW_DEMO_SEED") != "1":
            sys.exit("Refusing to insert: set ALLOW_DEMO_SEED=1")
        await add_history(a.plant, a.add_history)
    await time_payload(a.plant, a.runs)


if __name__ == "__main__":
    asyncio.run(main())
