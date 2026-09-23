"""Seed the FIRE.* permission codes and the Page Industries FIRE role matrix.

Targeted and additive: inserts the 12 FIRE permissions and the FIRE role grants
only. Unlike `python -m app.seed.seed_rbac` it never wipes RolePermission, so no
other module's grants are touched. Idempotent — re-running is a no-op.

Once FIRE.READ exists, services/fire_permissions stops falling back to
INCIDENT.READ/UPDATE and these grants become the real fire authority (that is
the designed migration; see fire_permissions.py "THE MIGRATION GUARD").

The matrix is copied from the Page Industries build's prisma/seed-rbac.ts, plus
FIELD_TECHNICIAN (READ/EXECUTE, own plant) — the technician who fills routine
checklists; Page used SUPERVISOR for that role.

    python scripts/seed_fire_rbac.py            # dry run
    python scripts/seed_fire_rbac.py --commit
"""

from __future__ import annotations

import os
import sys
import uuid

import psycopg2
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))

PERMISSIONS = {
    "CREATE": "Register a fire asset / add a register row",
    "READ": "Read the fire register, checklists and inspection records",
    "UPDATE": "Edit a fire asset or register row",
    "DELETE": "Remove a fire asset or checklist (soft delete / retire)",
    "EXECUTE": "Fill a fire checklist — 'Prepared by: Person In-charge'",
    "VERIFY": "Review a filled checklist — 'Reviewed by: Intermediatory Head'",
    "APPROVE": "Approve and lock a checklist — 'Approved by: HOD'",
    "CLOSE": "Close a fire defect / finding",
    "EXPORT": "Export fire checklists and registers to PDF / Excel",
    "TEMPLATE_AUTHOR": "Create / edit / clone fire checklist templates",
    "TEMPLATE_APPROVE": "Publish or retire a fire checklist revision",
    "CALENDAR": "Mark plant non-working days on the daily checklist grids",
}
ALL = list(PERMISSIONS)

GRANTS: list[tuple[str, list[str], str]] = [
    ("SUPERVISOR", ["READ", "EXECUTE"], "OWN_DEPARTMENT"),
    ("SAFETY_OFFICER", ALL, "ALL_PLANTS"),
    ("DEPARTMENT_HEAD", ["READ", "VERIFY", "EXPORT"], "OWN_DEPARTMENT"),
    ("HSE_MANAGER", ALL, "ALL_PLANTS"),
    ("PLANT_HEAD", ["READ", "VERIFY", "APPROVE", "CLOSE", "EXPORT", "CALENDAR"], "OWN_PLANT"),
    ("CORPORATE_HSE", ALL, "ALL_PLANTS"),
    ("MAINTENANCE_HEAD", ["CREATE", "READ", "UPDATE", "DELETE", "EXECUTE", "EXPORT"], "OWN_PLANT"),
    ("EMERGENCY_RESPONSE_COORDINATOR", ["CREATE", "READ", "UPDATE", "EXECUTE", "VERIFY", "CLOSE", "EXPORT"], "OWN_PLANT"),
    ("ADMIN", ALL, "ALL_PLANTS"),
    ("SYSTEM_ADMIN", ALL, "ALL_PLANTS"),
    ("EXECUTIVE_VIEWER", ["READ"], "ALL_PLANTS"),
    ("PLANT_HSE_HEAD", ["READ", "VERIFY", "EXPORT"], "OWN_PLANT"),
    ("COMPLIANCE_OFFICER", ["READ", "EXPORT"], "ALL_PLANTS"),
    ("LEAD_AUDITOR", ["READ", "EXPORT"], "OWN_PLANT"),
    ("AUDITOR", ["READ", "EXPORT"], "OWN_PLANT"),
    ("FIELD_TECHNICIAN", ["READ", "EXECUTE"], "OWN_PLANT"),
]


def _conn():
    url = os.environ.get("DATABASE_URL_SYNC") or os.environ["DATABASE_URL"]
    for prefix in ("postgresql+psycopg2://", "postgresql+asyncpg://"):
        url = url.replace(prefix, "postgresql://")
    return psycopg2.connect(url)


def main(commit: bool) -> None:
    c = _conn()
    cur = c.cursor()
    perm_ids: dict[str, str] = {}
    added_p = added_g = 0
    for action, desc in PERMISSIONS.items():
        code = f"FIRE.{action}"
        cur.execute('select id from "Permission" where code=%s', (code,))
        row = cur.fetchone()
        if row:
            perm_ids[action] = row[0]
            continue
        pid = uuid.uuid4().hex
        cur.execute(
            'insert into "Permission"(id, code, module, action, description) values (%s,%s,%s,%s,%s)',
            (pid, code, "FIRE", action, desc),
        )
        perm_ids[action] = pid
        added_p += 1
    for role_code, actions, scope in GRANTS:
        cur.execute('select id from "Role" where code=%s', (role_code,))
        row = cur.fetchone()
        if not row:
            print(f"  ! role {role_code} not found — skipped")
            continue
        for a in actions:
            cur.execute(
                'select 1 from "RolePermission" where "roleId"=%s and "permissionId"=%s',
                (row[0], perm_ids[a]),
            )
            if cur.fetchone():
                continue
            cur.execute(
                'insert into "RolePermission"(id, "roleId", "permissionId", scope) values (%s,%s,%s,%s)',
                (uuid.uuid4().hex, row[0], perm_ids[a], scope),
            )
            added_g += 1
    print(f"FIRE permissions added: {added_p}; role grants added: {added_g}")
    if commit:
        c.commit()
        print("committed")
    else:
        c.rollback()
        print("dry run — rolled back (pass --commit to apply)")


if __name__ == "__main__":
    main("--commit" in sys.argv)
