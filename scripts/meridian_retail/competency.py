"""Meridian Retail — PTW receiver competency certificates.

The permit create endpoint refuses a receiver with no certificate for the
permit type ("No "Hot Work Permit Holder" certificate or record on file").
No Retail user had any, so every permit except General Cold Work (and Lifting,
which has no mandatory program) bounced at the wizard's last step.

DC maintenance technicians are the people who actually hold DC maintenance
permits, so each gets Hot Work, Work at Height and Electrical/LOTO. DC managers
get Work at Height only (mezzanine / racking inspections). One certificate is
left expiring inside 30 days so the "expires soon" warning shows in the demo.

Idempotent: skips any (user, program) pair that already has a certificate.

    python -m scripts.meridian_retail.competency            # dry run
    python -m scripts.meridian_retail.competency --commit
"""

from __future__ import annotations

import sys
from datetime import timedelta

from scripts.meridian_retail.common import EMAIL_DOMAIN, conn, days_ago, new_id, now

ALL_THREE = ["PTW_HOT_WORK_HOLDER", "PTW_HEIGHT_HOLDER", "PTW_ELECTRICAL_HOLDER"]
GRANTS = {
    **{f"dc.maint.dc{d:02d}@{EMAIL_DOMAIN}": ALL_THREE for d in (1, 2, 3)},
    **{f"dc.manager.dc{d:02d}@{EMAIL_DOMAIN}": ["PTW_HEIGHT_HOLDER"] for d in (1, 2, 3)},
}
SHORT = {"PTW_HOT_WORK_HOLDER": "HWPH", "PTW_HEIGHT_HOLDER": "WAH", "PTW_ELECTRICAL_HOLDER": "ELEC"}
# (user, program) left close to expiry — a warning, not a block.
EXPIRING = (f"dc.maint.dc02@{EMAIL_DOMAIN}", "PTW_HEIGHT_HOLDER")


def seed(cur) -> None:
    cur.execute('select code, id from "TrainingProgram" where code = any(%s)', (ALL_THREE,))
    programs = dict(cur.fetchall())
    cur.execute('select email, id, "plantId" from "User" where email = any(%s)', (list(GRANTS),))
    users = {e: (uid, pid) for e, uid, pid in cur.fetchall()}
    cur.execute('select id, code from "Plant" where id = any(%s)', ([p for _, p in users.values()],))
    site = {pid: code.replace("MR-", "") for pid, code in cur.fetchall()}
    cur.execute('select id from "User" where email = %s', (f"store-ops.admin@{EMAIL_DOMAIN}",))
    issuer = cur.fetchone()[0]

    made = 0
    seq: dict[str, int] = {}
    for email, codes in GRANTS.items():
        uid, pid = users[email]
        for code in codes:
            cur.execute('select 1 from "TrainingCertificate" where "userId"=%s and "programId"=%s', (uid, programs[code]))
            if cur.fetchone():
                continue
            key = f"{site[pid]}-{SHORT[code]}"
            seq[key] = seq.get(key, 0) + 1
            if (email, code) == EXPIRING:
                valid_from = now() - timedelta(days=24 * 30 - 18)  # 24-month program, ~18 days left
            else:
                valid_from = days_ago(120)
            cur.execute(
                'insert into "TrainingCertificate"(id, "certificateNumber", "programId", "userId", "issuedAt", "issuedById", '
                '"finalAssessmentScore", "attendancePercent", "validFrom", "validTo", status, "isRenewable", "updatedAt") '
                "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'ACTIVE',true,now())",
                (new_id(), f"CERT-MR-{key}-{seq[key]:03d}-{valid_from:%Y}", programs[code], uid, valid_from, issuer,
                 82.0, 100.0, valid_from, valid_from + timedelta(days=24 * 30)),
            )
            made += 1
    print(f"competency: {made} certificates issued to {len(GRANTS)} DC staff")


def main(commit: bool) -> None:
    c = conn()
    cur = c.cursor()
    seed(cur)
    if commit:
        c.commit()
        print("committed")
    else:
        c.rollback()
        print("dry run - rolled back (pass --commit)")


if __name__ == "__main__":
    main("--commit" in sys.argv)
