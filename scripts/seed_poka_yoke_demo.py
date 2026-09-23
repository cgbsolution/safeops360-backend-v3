"""Poka Yoke register demo seed — 14 devices for the Meridian garment plant.

The register had TWO live devices, which is not enough to see whether anything
on the screen works: one status tab, no overdue row, no bypass, and a dashboard
whose tiles all read 1 or 2. This seeds a register that exercises every state
the module can be in, so the filters, the tiles and the new bypass log have
something behind them.

WHY ALL ON ONE PLANT
Meridian North Works is the only plant whose Areas are a real garment layout —
Cutting Hall, Sewing Line 1 and 2, Finishing & Packing, Fabric Warehouse. South
Works has generic process areas (Boiler House, ETP, Substation), so seeding
sleeve-orientation jigs there would produce a register that reads as nonsense to
anybody who knows the plant. A cross-plant spread is worth having, but it is
worth having with content that fits the plant, not with these devices copied.

WHY THE API AND NOT SQL
Numbering (max+1, soft-delete-safe), the three-step approval chain, the install
and verification state machine, the bypass log and the CAPA a failed check
raises all live in the service layer. A direct-SQL seed writes rows that look
right and proves none of it — and would silently skip the machinery the demo
exists to show.

  API_BASE=http://127.0.0.1:8000 python scripts/seed_poka_yoke_demo.py
  API_BASE=... python scripts/seed_poka_yoke_demo.py --wipe   # soft-delete + re-seed

IDEMPOTENT: devices are recognised by their exact titles. A second run reports
what already exists and writes nothing rather than doubling the register.

⚠ This WRITES to whatever database the backend at API_BASE is pointed at, which
for this project is prod. It only creates demo devices; it modifies nothing that
already exists. --wipe soft-deletes only the devices in THIS script's title list.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import create_engine, text

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.config import get_settings  # noqa: E402
from app.core.security import create_access_token  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000")
PLANT = "cmq42hc7b000913589h1l7q23"  # Meridian North Works

#: Area ids at North Works. Hard-coded rather than looked up by name because a
#: name lookup that silently misses puts every device in "not area-specific",
#: and the register's Where column then says nothing on all 14 rows.
AREA = {
    "CUTTING": "cmraiylhl002bv88cab1leflr",   # Cutting Hall
    "SEW1": "cmraiylq3002dv88cnuw91lv4",      # Sewing Line 1
    "SEW2": "cmraiylyr002fv88caw2as5dh",      # Sewing Line 2
    "FINISH": "cmraiym78002hv88cknt1tcxj",    # Finishing & Packing
    "FABRIC": "cmraiymo6002lv88cd0ptch84",    # Fabric Warehouse
}

now = datetime.now(timezone.utc)


def iso(d: datetime) -> str:
    return d.isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# The register
#
# Deliberately NOT all green. A poka yoke register where every device passes
# every check is the one thing a real plant never has, and it is the version
# that teaches a demo audience nothing: the module's whole argument is that it
# tells you which devices are NOT protecting the line right now.
#
# `plan` drives how far each device is taken:
#   PROPOSED  — raised, not submitted
#   APPROVED  — through the approval chain, not yet fitted
#   INSTALLED — fitted, never verified (the honest "we don't know" state)
#   ACTIVE    — fitted and verified PASS
#   DEGRADED  — fitted and verified FAIL (auto-raises a CAPA)
#
# `verified_days_ago` backdates the check THROUGH THE API (VerificationCreate
# accepts verifiedAt), so the next-due date it derives lands naturally in the
# past for the devices that should read overdue. No SQL fudging of due dates.
# ─────────────────────────────────────────────────────────────────────────────
DEVICES: list[dict] = [
    # ── Working, recently checked ────────────────────────────────────────────
    {
        "title": "Sleeve orientation jig, Line 1",
        "defectModePrevented": "Sleeve attached inside-out at the shoulder seam.",
        "description": "Keyed jig; the panel physically will not seat the wrong way round.",
        "deviceType": "CONTACT", "approach": "PREVENTION", "reactionMode": "CONTROL",
        "areaId": AREA["SEW1"], "lineOrMachine": "Sewing Line 1", "processStep": "Sleeve attach",
        "beforeCondition": "Operator judged orientation by eye against a sample.",
        "afterCondition": "Only one orientation seats in the jig.",
        "cost": 14000.0, "verificationFrequency": "MONTHLY",
        "plan": "ACTIVE", "verified_days_ago": 6, "owner": "MAINTENANCE_HEAD",
    },
    {
        "title": "Collar fusing press temperature interlock",
        "defectModePrevented": "Collar fused below temperature — delamination after first wash.",
        "description": "Press will not cycle until the platen reaches the set point.",
        "deviceType": "FIXED_VALUE", "approach": "PREVENTION", "reactionMode": "CONTROL",
        "areaId": AREA["FINISH"], "lineOrMachine": "Fusing press FP-02", "processStep": "Collar fusing",
        "beforeCondition": "Operator started the cycle on a warm-up timer.",
        "afterCondition": "Interlocked to the actual platen thermocouple.",
        "cost": 48000.0, "verificationFrequency": "MONTHLY",
        "plan": "ACTIVE", "verified_days_ago": 11, "owner": "SUPERVISOR",
    },
    {
        "title": "Needle-detector conveyor gate",
        "defectModePrevented": "Broken needle fragment leaving the plant inside a garment.",
        "description": "Detector stops the belt and locks the gate on any ferrous hit. "
                       "This is a customer and regulatory requirement, not an efficiency device.",
        "deviceType": "FIXED_VALUE", "approach": "DETECTION", "reactionMode": "CONTROL",
        "areaId": AREA["FINISH"], "lineOrMachine": "Needle detector ND-01", "processStep": "Final inspection",
        "beforeCondition": "Hand-held wand check on a sample of cartons.",
        "afterCondition": "Every garment passes the detector; a hit stops the line.",
        "cost": 265000.0, "verificationFrequency": "SHIFT",
        "plan": "ACTIVE", "verified_days_ago": 0, "owner": "SAFETY_OFFICER",
    },
    {
        "title": "Bobbin-empty photo sensor, Line 2",
        "defectModePrevented": "Seam sewn with no bobbin thread — the garment falls apart at the seam.",
        "deviceType": "FIXED_VALUE", "approach": "DETECTION", "reactionMode": "WARNING",
        "areaId": AREA["SEW2"], "lineOrMachine": "Sewing Line 2", "processStep": "Side seam",
        "cost": 9500.0, "verificationFrequency": "WEEKLY",
        "plan": "ACTIVE", "verified_days_ago": 3, "owner": "SUPERVISOR",
    },
    # ── Working, but somebody has switched it off ────────────────────────────
    {
        "title": "Shade-lot barcode gate at the lay table",
        "defectModePrevented": "Two shade lots cut into one garment — visible panel mismatch.",
        "description": "Spreader will not start until every roll on the table scans to one lot.",
        "deviceType": "MOTION_STEP", "approach": "PREVENTION", "reactionMode": "CONTROL",
        "areaId": AREA["CUTTING"], "lineOrMachine": "Spreader SP-01", "processStep": "Lay spreading",
        "beforeCondition": "Cutter read the shade ticket by eye off each roll.",
        "afterCondition": "Mixed lots physically cannot be spread together.",
        "cost": 132000.0, "verificationFrequency": "WEEKLY",
        "plan": "ACTIVE", "verified_days_ago": 9, "owner": "DEPARTMENT_HEAD",
        "bypass": {
            "reason": "Scanner head failed; replacement on order, ETA Thursday. "
                      "Supervisor is checking shade tickets manually against the lay sheet.",
            "approved_by": "PLANT_HEAD",
            "days_ago": 4,
        },
    },
    {
        "title": "Cutting-table light curtain",
        "defectModePrevented": "Hand entering the blade path while the cutter is powered.",
        "description": "Curtain break cuts power to the straight knife.",
        "deviceType": "CONTACT", "approach": "PREVENTION", "reactionMode": "CONTROL",
        "areaId": AREA["CUTTING"], "lineOrMachine": "Straight knife CK-03", "processStep": "Panel cutting",
        "cost": 78000.0, "verificationFrequency": "MONTHLY",
        "plan": "ACTIVE", "verified_days_ago": 21, "owner": "MAINTENANCE_HEAD",
        "bypass": {
            "reason": "Curtain muted for blade-change access during the shift changeover. "
                      "Machine is locked out; to be un-muted before the line restarts.",
            "approved_by": "MAINTENANCE_HEAD",
            "days_ago": 12,
        },
    },
    # ── Working now, but with a bypass in their history ──────────────────────
    {
        "title": "Cuff-pair matching tray",
        "defectModePrevented": "Two left cuffs attached to one shirt.",
        "deviceType": "CONTACT", "approach": "PREVENTION", "reactionMode": "WARNING",
        "areaId": AREA["SEW1"], "lineOrMachine": "Sewing Line 1", "processStep": "Cuff attach",
        "beforeCondition": "Cuffs picked from a common bin.",
        "afterCondition": "Tray issues a matched pair; an odd cuff has nowhere to sit.",
        "cost": 6200.0, "verificationFrequency": "MONTHLY",
        "plan": "ACTIVE", "verified_days_ago": 2, "owner": "SUPERVISOR",
        "bypass": {
            "reason": "Tray removed during the trial of a new cuff style; operators "
                      "working to a printed pair-check sheet meanwhile.",
            "approved_by": "DEPARTMENT_HEAD",
            "days_ago": 26,
            "restore_days_ago": 19,
            "restore_note": "Trial finished, original tray refitted and re-checked.",
        },
    },
    {
        "title": "Steam press two-hand control",
        "defectModePrevented": "Operator's hand under the head as the press closes.",
        "deviceType": "CONTACT", "approach": "PREVENTION", "reactionMode": "CONTROL",
        "areaId": AREA["FINISH"], "lineOrMachine": "Steam press SP-04", "processStep": "Final press",
        "cost": 22000.0, "verificationFrequency": "MONTHLY",
        "plan": "ACTIVE", "verified_days_ago": 8, "owner": "MAINTENANCE_HEAD",
        "bypass": {
            "reason": "One palm button intermittent; press run single-hand under a "
                      "standing supervisor watch while the button was replaced.",
            "approved_by": "PLANT_HEAD",
            "days_ago": 33,
            "restore_days_ago": 31,
            "restore_note": "Palm button replaced and both-hand function proven.",
        },
    },
    # ── Overdue: nobody has checked these in longer than their cadence ───────
    {
        "title": "Fabric roll weight checkweigher",
        "defectModePrevented": "Short-weight roll accepted into stock and cut short.",
        "deviceType": "FIXED_VALUE", "approach": "DETECTION", "reactionMode": "WARNING",
        "areaId": AREA["FABRIC"], "lineOrMachine": "Goods-in bay 2", "processStep": "Fabric receipt",
        "cost": 41000.0, "verificationFrequency": "MONTHLY",
        "plan": "ACTIVE", "verified_days_ago": 47, "owner": "DEPARTMENT_HEAD",
    },
    {
        "title": "Ply-count laser at the spreader",
        "defectModePrevented": "Lay spread to the wrong ply count — whole cut short or over.",
        "deviceType": "FIXED_VALUE", "approach": "DETECTION", "reactionMode": "WARNING",
        "areaId": AREA["CUTTING"], "lineOrMachine": "Spreader SP-01", "processStep": "Lay spreading",
        "cost": 55000.0, "verificationFrequency": "WEEKLY",
        "plan": "ACTIVE", "verified_days_ago": 24, "owner": None,
    },
    # ── Failed its last check ───────────────────────────────────────────────
    {
        "title": "Button-count photo eye, Line 2",
        "defectModePrevented": "Garment leaves the line one button short.",
        "deviceType": "FIXED_VALUE", "approach": "DETECTION", "reactionMode": "WARNING",
        "areaId": AREA["SEW2"], "lineOrMachine": "Button station 2", "processStep": "Button attach",
        "cost": 11800.0, "verificationFrequency": "WEEKLY",
        "plan": "DEGRADED", "verified_days_ago": 5, "owner": "SUPERVISOR",
        "fail_note": "Photo eye taped over. Counted 11 buttons on a 12-button placket "
                     "and did not alarm.",
    },
    # ── Fitted, never proven ────────────────────────────────────────────────
    {
        "title": "Care-label printer lot-code lock",
        "defectModePrevented": "Care label printed with the previous order's lot code.",
        "description": "Printer pulls the lot code from the work order and refuses a manual override.",
        "deviceType": "MOTION_STEP", "approach": "PREVENTION", "reactionMode": "CONTROL",
        "areaId": AREA["FINISH"], "lineOrMachine": "Label printer LP-01", "processStep": "Labelling",
        "cost": 18500.0, "verificationFrequency": "MONTHLY",
        "plan": "INSTALLED", "owner": None,
    },
    # ── Approved, not yet fitted ────────────────────────────────────────────
    {
        "title": "Metal-detector reject-bin lock",
        "defectModePrevented": "A rejected garment lifted back onto the line from the reject bin.",
        "description": "Bin lid locks; only QA's key opens it, and every opening is logged.",
        "deviceType": "MOTION_STEP", "approach": "DETECTION", "reactionMode": "CONTROL",
        "areaId": AREA["FINISH"], "lineOrMachine": "Needle detector ND-01", "processStep": "Final inspection",
        "cost": 31000.0, "verificationFrequency": "MONTHLY",
        "plan": "APPROVED", "owner": "SAFETY_OFFICER",
    },
    # ── Raised, still in the queue ──────────────────────────────────────────
    {
        "title": "Thread-tension out-of-range alarm, Line 1",
        "defectModePrevented": "Puckered seam from drifting top-thread tension.",
        "deviceType": "FIXED_VALUE", "approach": "DETECTION", "reactionMode": "WARNING",
        "areaId": AREA["SEW1"], "lineOrMachine": "Sewing Line 1", "processStep": "Side seam",
        "cost": 27000.0, "verificationFrequency": "WEEKLY",
        "plan": "PROPOSED", "owner": None,
    },
]

TITLES = [d["title"] for d in DEVICES]

CAST_ROLES = [
    "MAINTENANCE_HEAD", "PLANT_HEAD", "SUPERVISOR", "DEPARTMENT_HEAD",
    "SAFETY_OFFICER", "HSE_MANAGER",
]


# ─────────────────────────────────────────────────────────────────────────────
def _engine():
    url = get_settings().database_url_sync or get_settings().async_database_url
    return create_engine(url.replace("+asyncpg", "+psycopg2"), pool_pre_ping=True)


ENG = _engine()


def q(sql: str, **kw):
    """A SELECT (or a RETURNING) — always rows."""
    with ENG.begin() as c:
        return c.execute(text(sql), kw).mappings().all()


def execute(sql: str, **kw) -> int:
    """An UPDATE/INSERT with no RETURNING clause — rowcount, not rows.

    Split from q() because `.mappings().all()` on a non-returning statement
    raises ResourceClosedError, which reads like a connection fault rather than
    "you asked a write for rows".
    """
    with ENG.begin() as c:
        return c.execute(text(sql), kw).rowcount


class Api:
    def __init__(self, cast: dict):
        self.c = httpx.Client(base_url=BASE, timeout=90.0)
        self.cast = cast
        self._extra: dict[str, str] = {}
        self.calls = 0

    def h(self, who: str) -> dict:
        return {
            "Authorization": f"Bearer {self.cast[who]['token']}",
            "Content-Type": "application/json",
        }

    def post(self, path: str, who: str, body=None, ok=(200, 201)):
        r = self.c.post(path, headers=self.h(who), json=body if body is not None else {})
        self.calls += 1
        if r.status_code not in ok:
            raise RuntimeError(f"POST {path} as {who} -> {r.status_code} {r.text[:220]}")
        return r.json() if r.content else {}

    def token_for(self, user_id: str) -> str:
        for v in self.cast.values():
            if v["id"] == user_id:
                return v["token"]
        if user_id not in self._extra:
            row = q('select id, email, role, "plantId" from "User" where id = :i', i=user_id)
            info = dict(row[0]) if row else {"email": "unknown@local", "role": "WORKER"}
            self._extra[user_id] = create_access_token(
                subject=user_id,
                extra_claims={
                    "role": info["role"],
                    "plantId": info.get("plantId") or PLANT,
                    "email": info["email"],
                },
            )
        return self._extra[user_id]

    def advance(self, record_id: str, max_steps: int = 6) -> int:
        """Approve every pending task on a device until its chain completes.

        Approves AS THE ACTUAL ASSIGNEE, whoever the engine picked — not as a
        cast member we hoped it would pick. A seed that approves as the wrong
        person silently approves nothing and leaves records stuck at SUBMITTED
        while reporting success.
        """
        done = 0
        for _ in range(max_steps):
            rows = q(
                '''select id, "stepName", "assignedToId" from "WorkflowTask"
                     where module = 'BE_POKA_YOKE' and "recordId" = :r
                       and status in ('PENDING','OVERDUE','ESCALATED')
                     order by "assignedAt" limit 1''',
                r=record_id,
            )
            if not rows:
                break
            task = dict(rows[0])
            tok = self.token_for(task["assignedToId"])
            r = self.c.post(
                "/api/workflow/approve",
                headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                json={"taskId": task["id"], "comments": "Approved (Poka Yoke demo seed)."},
            )
            self.calls += 1
            if r.status_code not in (200, 201):
                print(f"      ! approve {task['stepName']}: {r.status_code} {r.text[:140]}")
                break
            done += 1
        return done


def build_cast() -> dict:
    cast: dict = {}
    for role in CAST_ROLES:
        rows = q(
            'select id, name, email, role from "User" '
            'where "plantId" = :p and role = :r order by email limit 1',
            p=PLANT, r=role,
        )
        if not rows:
            continue
        u = dict(rows[0])
        cast[role] = {
            "id": u["id"], "name": u["name"], "email": u["email"], "role": u["role"],
            "token": create_access_token(
                subject=u["id"],
                extra_claims={"role": u["role"], "plantId": PLANT, "email": u["email"]},
            ),
        }
    return cast


def existing() -> dict[str, str]:
    rows = q(
        'select id, title, "deviceNo" from "BePokaYoke" '
        'where title = any(:t) and "plantId" = :p and "isDeleted" = false',
        t=TITLES, p=PLANT,
    )
    return {r["title"]: r["id"] for r in rows}


def wipe() -> int:
    """Soft-delete ONLY the devices this script owns, by exact title.

    Soft, not hard: these rows own issued device numbers, and a hard delete
    would free those numbers for re-use — which is how an export produced last
    month stops matching the register.
    """
    rows = q(
        '''update "BePokaYoke" set "isDeleted" = true, "deletedAt" = now(),
              "deletionReason" = 'Re-seeded by scripts/seed_poka_yoke_demo.py --wipe'
            where title = any(:t) and "plantId" = :p and "isDeleted" = false
            returning id''',
        t=TITLES, p=PLANT,
    )
    return len(rows)


def seed() -> int:
    cast = build_cast()
    missing = [r for r in CAST_ROLES if r not in cast]
    if missing:
        print(f"  ! roles absent at this plant, falling back: {', '.join(missing)}")
    if "MAINTENANCE_HEAD" not in cast:
        print("  ✗ No MAINTENANCE_HEAD at this plant — nobody to raise devices as.")
        return 1
    print(f"Cast: " + ", ".join(f"{r}={cast[r]['name']}" for r in sorted(cast)))

    api = Api(cast)
    have = existing()
    made = skipped = 0

    for spec in DEVICES:
        title = spec["title"]
        if title in have:
            print(f"  · {title[:52]:<54} already present")
            skipped += 1
            continue

        raiser = "MAINTENANCE_HEAD"
        owner_role = spec.get("owner")
        body = {
            "plantId": PLANT,
            "areaId": spec["areaId"],
            "title": title,
            "defectModePrevented": spec["defectModePrevented"],
            "deviceType": spec["deviceType"],
            "approach": spec["approach"],
            "reactionMode": spec["reactionMode"],
            "lineOrMachine": spec.get("lineOrMachine"),
            "processStep": spec.get("processStep"),
            "verificationFrequency": spec["verificationFrequency"],
            "cost": spec.get("cost"),
        }
        for opt in ("description", "beforeCondition", "afterCondition"):
            if spec.get(opt):
                body[opt] = spec[opt]
        if owner_role and owner_role in cast:
            body["ownerId"] = cast[owner_role]["id"]

        dev = api.post("/api/be/poka-yoke", raiser, body, ok=(200, 201))
        did = dev["id"]
        plan = spec["plan"]
        trail = [plan]

        if plan != "PROPOSED":
            api.post(f"/api/be/poka-yoke/{did}/submit", raiser)
            steps = api.advance(did)
            trail.append(f"{steps} approval(s)")

            fresh = api.c.get(f"/api/be/poka-yoke/{did}", headers=api.h(raiser)).json()
            if fresh["status"] != "APPROVED" and plan in ("INSTALLED", "ACTIVE", "DEGRADED"):
                print(f"  ! {title[:44]:<46} stuck at {fresh['status']}, cannot install")
                made += 1
                continue

        if plan in ("INSTALLED", "ACTIVE", "DEGRADED"):
            api.post(f"/api/be/poka-yoke/{did}/install", raiser, {})

        if plan in ("ACTIVE", "DEGRADED"):
            checker = "SAFETY_OFFICER" if "SAFETY_OFFICER" in cast else raiser
            when = now - timedelta(days=spec.get("verified_days_ago", 1))
            if plan == "ACTIVE":
                api.post(
                    f"/api/be/poka-yoke/{did}/verify", checker,
                    {"result": "PASS", "verifiedAt": iso(when),
                     "note": "Function proven at the machine."},
                )
            else:
                # A FAIL auto-raises a CAPA through the universal engine — which
                # is the point of seeding one: the demo needs a device whose
                # failure produced actual downstream work.
                api.post(
                    f"/api/be/poka-yoke/{did}/verify", checker,
                    {"result": "FAIL", "verifiedAt": iso(when),
                     "note": spec.get("fail_note", "Device did not respond to the test condition.")},
                )
            trail.append(f"checked {spec.get('verified_days_ago', 1)}d ago")

        # ── Bypass history ──────────────────────────────────────────────────
        bp = spec.get("bypass")
        if bp:
            approver = bp.get("approved_by")
            api.post(
                f"/api/be/poka-yoke/{did}/bypass", raiser,
                {
                    "reason": bp["reason"],
                    "approvedById": cast[approver]["id"] if approver in cast else None,
                },
            )
            if "restore_days_ago" in bp:
                api.post(
                    f"/api/be/poka-yoke/{did}/restore", raiser,
                    {"note": bp.get("restore_note")},
                )
                # A restore correctly leaves the device DEGRADED owing a check.
                # These devices are meant to read as working again, so record
                # the check that a real restore would be followed by.
                checker = "SAFETY_OFFICER" if "SAFETY_OFFICER" in cast else raiser
                api.post(
                    f"/api/be/poka-yoke/{did}/verify", checker,
                    {
                        "result": "PASS",
                        "verifiedAt": iso(now - timedelta(days=spec.get("verified_days_ago", 1))),
                        "note": "Post-restore check — function proven before the line restarted.",
                    },
                )
                trail.append("bypassed + restored")
            else:
                trail.append("BYPASSED (open)")

            # Backdating happens in reconcile() below, NOT here — see the note
            # there on why it must run for devices this pass skipped too.

        print(f"  ✓ {title[:52]:<54} {' · '.join(trail)}")
        made += 1

    reconcile()

    # ── Spread the raise dates ──────────────────────────────────────────────
    # Cosmetic only, and the one thing with no API surface: every device would
    # otherwise carry today's createdAt, so the register (which sorts createdAt
    # DESC) would show 14 devices raised in the same second. Touches nothing
    # behavioural — no status, no due date, no numbering.
    execute(
        '''update "BePokaYoke" b set "createdAt" = now() - make_interval(days => x.d::int)
             from (select id, (row_number() over (order by "createdAt")) * 17 as d
                     from "BePokaYoke"
                    where title = any(:t) and "plantId" = :p and "isDeleted" = false) x
            where b.id = x.id''',
        t=TITLES, p=PLANT,
    )

    print(f"\n{made} created, {skipped} already present ({api.calls} API calls)")
    return 0


def reconcile() -> int:
    """Re-apply the backdating to EVERY device in the list, created now or not.

    The dates are the only part of this seed that no API can set — a bypass is
    always opened "now", so without this every episode reads as zero hours old
    and the ACTIVE BYPASSES tile looks like a rounding error rather than two
    devices somebody switched off last week.

    Deliberately a separate pass over the whole list rather than a step inside
    the create branch, because that is exactly what went wrong the first time
    this ran: the script aborted part-way, the already-created device took the
    "already present" path on the retry, and its backdating was skipped
    forever. A repair that only runs on the records you happen to be creating
    is not a repair. This is idempotent — it sets absolute dates, so running it
    ten times leaves the same values.
    """
    fixed = 0
    for spec in DEVICES:
        bp = spec.get("bypass")
        if not bp:
            continue
        rows = q(
            'select id from "BePokaYoke" where title = :t and "plantId" = :p '
            'and "isDeleted" = false',
            t=spec["title"], p=PLANT,
        )
        if not rows:
            continue
        did = rows[0]["id"]
        fixed += execute(
            '''update "BePokaYokeBypass"
                  set "bypassedAt" = now() - make_interval(days => (:d)::int)
                where "deviceId" = :i
                  and "bypassedAt" <> now() - make_interval(days => (:d)::int)''',
            d=bp["days_ago"], i=did,
        )
        if "restore_days_ago" in bp:
            execute(
                '''update "BePokaYokeBypass"
                      set "restoredAt" = now() - make_interval(days => (:d)::int)
                    where "deviceId" = :i and "restoredAt" is not null''',
                d=bp["restore_days_ago"], i=did,
            )
    if fixed:
        print(f"  reconciled {fixed} bypass date(s)")
    return fixed


def report() -> None:
    rows = q(
        '''select b."status", count(*) from "BePokaYoke" b
            where b."plantId" = :p and b."isDeleted" = false group by 1 order by 1''',
        p=PLANT,
    )
    print("\nRegister now reads:")
    for r in rows:
        print(f"  {r['status']:<12} {r['count']}")
    openb = q(
        '''select count(*) as n from "BePokaYokeBypass" x
             join "BePokaYoke" d on d.id = x."deviceId"
            where x."restoredAt" is null and d."isDeleted" = false and d."plantId" = :p''',
        p=PLANT,
    )[0]["n"]
    allb = q(
        '''select count(*) as n from "BePokaYokeBypass" x
             join "BePokaYoke" d on d.id = x."deviceId"
            where d."isDeleted" = false and d."plantId" = :p''',
        p=PLANT,
    )[0]["n"]
    overdue = q(
        '''select count(*) as n from "BePokaYoke"
            where "plantId" = :p and "isDeleted" = false
              and status in ('VERIFIED','ACTIVE','DEGRADED')
              and "nextVerificationDueAt" < now()''',
        p=PLANT,
    )[0]["n"]
    print(f"  {'—' * 18}\n  open bypasses  {openb}   (of {allb} episodes logged)")
    print(f"  overdue checks {overdue}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wipe", action="store_true",
                    help="soft-delete this script's devices, then re-seed")
    args = ap.parse_args()

    print(f"Poka Yoke demo seed → {BASE}")
    print(f"Plant: Meridian North Works ({PLANT})\n")

    if args.wipe:
        n = wipe()
        print(f"Soft-deleted {n} previously seeded device(s).\n")

    rc = seed()
    report()
    return rc


if __name__ == "__main__":
    sys.exit(main())
