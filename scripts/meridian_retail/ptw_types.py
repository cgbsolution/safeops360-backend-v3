"""Meridian Retail — PTW permit-type curation (build prompt Step 6).

Every Retail site gets the confirmed five-type set; Confined Space Entry and
Excavation are removed (and so are their hazard annexures — see
app/services/ptw_type_config.py). The wizard opens on Electrical / LOTO, the
representative DC maintenance permit, not Hot Work. Hot Work keeps its gas-test
control, so the Gas Test Plan step still appears for it.

Requires the table from prisma/apply-ptw-type-config-ddl.ts. Idempotent (upsert).

    python -m scripts.meridian_retail.ptw_types            # dry run
    python -m scripts.meridian_retail.ptw_types --commit
"""

from __future__ import annotations

import sys

from scripts.meridian_retail.common import PREFIX, conn

ENABLED = ["HOT_WORK", "WORK_AT_HEIGHT", "ELECTRICAL_LOTO", "LIFTING", "GENERAL_COLD"]
DEFAULT = "ELECTRICAL_LOTO"


def seed(cur) -> None:
    cur.execute('select id from "Plant" where code like %s', (f"{PREFIX}%",))
    ids = [r[0] for r in cur.fetchall()]
    for pid in ids:
        cur.execute(
            'insert into "PlantPermitTypeConfig"("plantId", "enabledTypes", "defaultType") values (%s, %s, %s) '
            'on conflict ("plantId") do update set "enabledTypes"=excluded."enabledTypes", '
            '"defaultType"=excluded."defaultType", "updatedAt"=now()',
            (pid, ENABLED, DEFAULT),
        )
    print(f"ptw_types: {len(ids)} Retail sites -> {ENABLED}, default {DEFAULT}")


def main(commit: bool) -> None:
    c = conn()
    cur = c.cursor()
    seed(cur)
    if commit:
        c.commit()
        print("committed")
    else:
        c.rollback()
        print("dry run — rolled back (pass --commit)")


if __name__ == "__main__":
    main("--commit" in sys.argv)
