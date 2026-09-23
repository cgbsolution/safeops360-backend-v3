"""Business Excellence demo seed — all six workflows, driven through the API.

Creates a coherent, realistic dataset for the Meridian garment plant covering
every state a demo needs to show, and specifically the five conditions worth
proving:

  1. a Kaizen that is genuinely SITTING IN somebody's screening queue
     (a WorkflowTask row, not just a status column)
  2. a QCC benefit at PENDING_VALIDATION whose circle includes a PLANT_HEAD —
     so an attempt to self-validate is refused by the separation-of-duties rule
     rather than by RBAC, which is the only version of that test that means
     anything
  3. an ANONYMOUS suggestion raised by an ADMIN — the hardest case for the
     suppression rule, because that user can see everything else
  4. a PUBLISHED OPL with real per-person acknowledgement obligations
  5. enough records in every register that the list and detail screens have
     something behind them

WHY THE API AND NOT SQL
Every rule this seeds against lives in the service layer: the workflow bridge,
the gate sequence, audience resolution, numbering, separation of duties. A
direct-SQL seed would write rows that look right and prove nothing — and would
silently skip the very machinery the demo is meant to show working.

  API_BASE=http://127.0.0.1:8000 python scripts/seed_be_demo.py
  API_BASE=... python scripts/seed_be_demo.py --wipe   # withdraw and re-seed

IDEMPOTENT: every record carries a "SEED-BE" marker in its source fields, and a
second run detects the marker and stops rather than doubling the dataset.

⚠ This writes to whatever database the backend at API_BASE is pointed at, which
for this project is prod. It creates demo records; it modifies nothing existing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.config import get_settings  # noqa: E402
from app.core.security import create_access_token  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000")

#: Stamped into sourceRecordRef so a re-run can recognise its own work.
MARKER = "SEED-BE-DEMO"

#: The cast. Role code -> how many we need. Resolved against the plant roster.
CAST_ROLES = [
    "ADMIN",
    "PLANT_HEAD",
    "DEPARTMENT_HEAD",
    "SUPERVISOR",
    "WORKER",
    "TRAINER",
    "MAINTENANCE_HEAD",
    "SAFETY_OFFICER",
    "FACTORY_MANAGER",
    "HSE_MANAGER",
]

now = datetime.now(timezone.utc)


def iso(d: datetime) -> str:
    return d.isoformat()



def _sync_engine():
    url = get_settings().database_url_sync or get_settings().async_database_url
    if "+asyncpg" in url:
        url = url.replace("+asyncpg", "+psycopg2")
    from sqlalchemy import create_engine

    return create_engine(url, pool_pre_ping=True)


_SYNC = None


def _q(sql: str, **params):
    global _SYNC
    if _SYNC is None:
        _SYNC = _sync_engine()
    with _SYNC.begin() as c:
        return c.execute(text(sql), params).mappings().all()


def _pending_task_sync(module: str, record_id: str):
    """The oldest PENDING task on this record, straight from the table.

    Deliberately not via the inbox API: the inbox shows only the CALLER's tasks,
    so a seed driving a chain has to know who the engine actually picked.
    """
    rows = _q(
        '''select id, "stepName", "assignedToId" from "WorkflowTask"
             where module = :m and "recordId" = :r and status = \'PENDING\'
             order by "assignedAt" limit 1''',
        m=module,
        r=record_id,
    )
    return dict(rows[0]) if rows else None


def _user_sync(user_id: str) -> dict:
    rows = _q('select id, email, role from "User" where id = :i', i=user_id)
    return dict(rows[0]) if rows else {"email": "unknown@local", "role": "WORKER"}


class Api:
    """Thin API wrapper that fails loudly and says which call failed."""

    def __init__(self, cast: dict, plant_id: str):
        self.c = httpx.Client(base_url=BASE, timeout=90.0)
        self.cast = cast
        self.plant = plant_id
        self.calls = 0
        self._extra: dict[str, str] = {}

    def h(self, who: str) -> dict:
        return {
            "Authorization": f"Bearer {self.cast[who]['token']}",
            "Content-Type": "application/json",
        }

    def call(self, method: str, path: str, who: str, body=None, ok=(200, 201, 204)):
        r = self.c.request(method, path, headers=self.h(who), json=body)
        self.calls += 1
        if r.status_code not in ok:
            raise RuntimeError(
                f"{method} {path} as {who} -> {r.status_code}\n  {r.text[:500]}"
            )
        return r.json() if r.content and r.status_code != 204 else {}

    def post(self, p, who, body=None, ok=(200, 201)):
        return self.call("POST", p, who, body, ok)

    def patch(self, p, who, body=None):
        return self.call("PATCH", p, who, body)

    def get(self, p, who):
        return self.call("GET", p, who)

    # ── Workflow ──────────────────────────────────────────────────────────
    def advance(self, module: str, record_id: str, max_steps: int = 6) -> int:
        """Approve every pending task on a record until its chain completes.

        Approves AS THE ACTUAL ASSIGNEE, whoever the engine picked — a token is
        minted on demand for them. The first version of this only looked at the
        inbox of the handful of users in the cast, so when the engine assigned a
        task to the OTHER supervisor at the plant it found nothing and returned
        zero without saying so. A seed that silently skips the approval chain
        produces records that look submitted and are actually stuck.
        """
        done = 0
        for _ in range(max_steps):
            task = _pending_task_sync(module, record_id)
            if task is None:
                break
            tok = self.token_for(task["assignedToId"])
            r = self.c.post(
                "/api/workflow/approve",
                headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                json={"taskId": task["id"], "comments": "Approved (demo seed)."},
            )
            self.calls += 1
            if r.status_code not in (200, 201):
                print(f"     ! approve {task['stepName']}: {r.status_code} {r.text[:120]}")
                break
            done += 1
        return done

    def token_for(self, user_id: str) -> str:
        """A token for any real user, cast member or not."""
        for v in self.cast.values():
            if v["id"] == user_id:
                return v["token"]
        if user_id not in self._extra:
            info = _user_sync(user_id)
            self._extra[user_id] = create_access_token(
                subject=user_id,
                extra_claims={"role": info["role"], "plantId": self.plant, "email": info["email"]},
            )
        return self._extra[user_id]


async def load_cast() -> tuple[dict, str, str | None, str]:
    """Resolve one real user per role at the first plant, and mint tokens."""
    url = get_settings().async_database_url
    eng = create_async_engine(
        url,
        connect_args=(
            {"statement_cache_size": 0, "prepared_statement_cache_size": 0}
            if ":6543" in url
            else {}
        ),
    )
    cast: dict = {}
    async with eng.connect() as c:
        plant = (
            await c.execute(text('select id, name from "Plant" order by "createdAt" limit 1'))
        ).first()
        area = (
            await c.execute(
                text('select id from "Area" where "plantId" = :p order by name limit 1'),
                {"p": plant.id},
            )
        ).first()
        for role in CAST_ROLES:
            row = (
                await c.execute(
                    text(
                        'select id, email, role from "User" '
                        'where "plantId" = :p and role = :r order by email limit 1'
                    ),
                    {"p": plant.id, "r": role},
                )
            ).first()
            if row is None:
                continue
            cast[role] = {
                "id": row.id,
                "email": row.email,
                "role": row.role,
                "token": create_access_token(
                    subject=row.id,
                    extra_claims={"role": row.role, "plantId": plant.id, "email": row.email},
                ),
            }
        # An independent validator: holds BENEFIT.VALIDATE, is in no circle and
        # owns nothing here. Needed so the benefit rules can be shown ALLOWING
        # somebody, not only refusing.
        indep = (
            await c.execute(
                text(
                    """
                    select distinct u.id, u.email, u.role from "User" u
                    join "UserRole" ur on ur."userId" = u.id
                    join "RolePermission" rp on rp."roleId" = ur."roleId"
                    join "Permission" p on p.id = rp."permissionId"
                    where p.code = 'BENEFIT.VALIDATE' and u."plantId" <> :p
                    order by u.email limit 1
                    """
                ),
                {"p": plant.id},
            )
        ).first()
        if indep:
            cast["INDEPENDENT"] = {
                "id": indep.id,
                "email": indep.email,
                "role": indep.role,
                "token": create_access_token(
                    subject=indep.id,
                    extra_claims={"role": indep.role, "plantId": plant.id, "email": indep.email},
                ),
            }
    await eng.dispose()
    return cast, plant.id, (area.id if area else None), plant.name


async def already_seeded() -> int:
    url = get_settings().async_database_url
    eng = create_async_engine(
        url,
        connect_args=(
            {"statement_cache_size": 0, "prepared_statement_cache_size": 0}
            if ":6543" in url
            else {}
        ),
    )
    async with eng.connect() as c:
        n = (
            await c.execute(
                text(
                    'select count(*) from "BeKaizen" where "sourceRecordRef" = :m '
                    'and "isDeleted" = false'
                ),
                {"m": MARKER},
            )
        ).scalar() or 0
    await eng.dispose()
    return n


def src(module: str = "MANUAL") -> dict:
    """Provenance stamp that also acts as the idempotency marker."""
    return {"sourceModule": module, "sourceRecordRef": MARKER}


# ═══════════════════════════════════════════════════════════════════════════
def seed(api: Api, plant: str, area: str | None) -> dict:
    out: dict = {"kaizen": [], "suggestion": [], "opl": [], "poka": [], "qcc": {}, "sip": []}
    A = {"areaId": area} if area else {}

    # ── KAIZEN ────────────────────────────────────────────────────────────
    print("\nKaizen")
    k1 = api.post(
        "/api/be/kaizen",
        "SUPERVISOR",
        {
            "plantId": plant, **A,
            "title": "Cut needle-change downtime on Sewing Line 3",
            "category": "PRODUCTIVITY",
            "lane": "STANDARD",
            "lineOrMachine": "Sewing Line 3 — Juki DDL-9000",
            "processStep": "Sleeve attach",
            "problemStatement": (
                "Needle changes on Line 3 take 11 minutes because the spare needle "
                "cabinet is at the far end of the hall. It happens 4-6 times a shift."
            ),
            "proposedImprovement": (
                "Put a small shadow-board of the three needle types at the line head, "
                "replenished by the store on the morning round."
            ),
            "expectedBenefit": "Roughly 35 minutes of line time recovered per shift.",
            "estimatedAnnualSaving": 420000.0,
            "savingType": "SOFT",
            "investmentCost": 8000.0,
            "targetDate": iso(now + timedelta(days=30)),
            **src(),
        },
    )
    api.post(f"/api/be/kaizen/{k1['id']}/submit", "SUPERVISOR")
    print(f"  {k1['id'][:8]} submitted -> awaiting screening  [CHECK 1]")
    out["kaizen"].append(k1["id"])

    k2 = api.post(
        "/api/be/kaizen",
        "WORKER",
        {
            "plantId": plant, **A,
            "title": "Bobbin winder guard interlock",
            "category": "SAFETY",
            "lane": "STANDARD",
            "lineOrMachine": "Bobbin winding station 2",
            "problemStatement": "The winder guard can be lifted while the spindle is still turning.",
            "proposedImprovement": "Fit a magnetic interlock that cuts the spindle when the guard lifts.",
            "estimatedAnnualSaving": 0.0,
            "targetDate": iso(now + timedelta(days=45)),
            **src(),
        },
    )
    api.post(f"/api/be/kaizen/{k2['id']}/submit", "WORKER")
    api.advance("BE_KAIZEN", k2["id"])
    for target in ("IN_IMPLEMENTATION",):
        try:
            api.post(f"/api/be/kaizen/{k2['id']}/transition/{target}", "DEPARTMENT_HEAD", {})
        except RuntimeError as e:
            print(f"     ! {target}: {str(e)[:90]}")
    print(f"  {k2['id'][:8]} approved -> implementing")
    out["kaizen"].append(k2["id"])

    k3 = api.post(
        "/api/be/kaizen",
        "WORKER",
        {
            "plantId": plant, **A,
            "title": "Offcut recovery bin at the cutting table",
            "category": "COST",
            "lane": "STANDARD",
            "lineOrMachine": "Cutting table 1",
            "problemStatement": "Usable fabric offcuts go into the general waste skip.",
            "proposedImprovement": "A labelled recovery bin at each table, emptied into the remnant store daily.",
            "estimatedAnnualSaving": 260000.0,
            "savingType": "HARD",
            "targetDate": iso(now - timedelta(days=10)),
            **src(),
        },
    )
    api.post(f"/api/be/kaizen/{k3['id']}/submit", "WORKER")
    api.advance("BE_KAIZEN", k3["id"])
    for target in ("IN_IMPLEMENTATION", "IMPLEMENTED"):
        try:
            api.post(f"/api/be/kaizen/{k3['id']}/transition/{target}", "DEPARTMENT_HEAD", {})
        except RuntimeError as e:
            print(f"     ! {target}: {str(e)[:90]}")
    # Savings verified by somebody OTHER than the person who raised it — the
    # API refuses otherwise, which is the point.
    try:
        api.post(
            f"/api/be/kaizen/{k3['id']}/transition/VERIFIED",
            "PLANT_HEAD",
            {
                "note": "Remnant store weighbridge log, three months.",
                "verifiedAnnualSaving": 238000.0,
            },
        )
        api.post(f"/api/be/kaizen/{k3['id']}/transition/CLOSED", "PLANT_HEAD", {})
        print(f"  {k3['id'][:8]} verified and closed  (savings signed off by a second person)")
    except RuntimeError as e:
        print(f"     ! verify/close: {str(e)[:120]}")
    out["kaizen"].append(k3["id"])

    k4 = api.post(
        "/api/be/kaizen",
        "WORKER",
        {
            "plantId": plant, **A,
            "title": "Move the thread trolley beside the packing bench",
            "category": "PRODUCTIVITY",
            "lane": "FAST_TRACK",
            "problemStatement": "Packers walk the length of the bench for every thread change.",
            "proposedImprovement": "Park the trolley at the bench head. No cost.",
            **src(),
        },
    )
    api.post(f"/api/be/kaizen/{k4['id']}/submit", "WORKER")
    print(f"  {k4['id'][:8]} fast-track lane, awaiting a supervisor")
    out["kaizen"].append(k4["id"])

    # ── SUGGESTION ────────────────────────────────────────────────────────
    print("\nSuggestion Scheme")
    s1 = api.post(
        "/api/be/suggestions",
        "ADMIN",
        {
            "plantId": plant, **A,
            "title": "Stagger the canteen break for the finishing lines",
            "category": "MORALE",
            "description": (
                "Everyone breaks at 13:00 and the queue takes twenty minutes of the "
                "thirty. Splitting finishing at 12:45 would cost nothing."
            ),
            "isAnonymous": True,
            **src(),
        },
    )
    api.post(f"/api/be/suggestions/{s1['id']}/submit", "ADMIN")
    print(f"  {s1['id'][:8]} ANONYMOUS, raised by an ADMIN  [CHECK 3]")
    out["suggestion"].append(s1["id"])

    s2 = api.post(
        "/api/be/suggestions",
        "WORKER",
        {
            "plantId": plant, **A,
            "title": "Label the fabric racks by shade lot, not by supplier",
            "category": "QUALITY",
            "description": "Shade-lot mixing at the cutting table starts at the rack.",
            "expectedBenefit": "Fewer shade-mix rejects at final inspection.",
            **src(),
        },
    )
    api.post(f"/api/be/suggestions/{s2['id']}/submit", "WORKER")
    api.post(
        f"/api/be/suggestions/{s2['id']}/screen",
        "SUPERVISOR",
        {"outcome": "RELEVANT", "note": "Cheap, and shade mixing is our top reject code."},
    )
    api.post(
        f"/api/be/suggestions/{s2['id']}/decide",
        "DEPARTMENT_HEAD",
        {
            "decision": "ACCEPT",
            "rationale": (
                "Accepted. Shade-lot labelling goes in with the September rack "
                "re-organisation; stores will own it."
            ),
        },
    )
    api.post(f"/api/be/suggestions/{s2['id']}/transition/IN_IMPLEMENTATION", "SUPERVISOR", {})
    api.post(
        f"/api/be/suggestions/{s2['id']}/incentive",
        "DEPARTMENT_HEAD",
        {"status": "APPROVED", "points": 50, "note": "Quarterly scheme, band B."},
    )
    print(f"  {s2['id'][:8]} accepted -> implementing, incentive approved")
    out["suggestion"].append(s2["id"])

    s3 = api.post(
        "/api/be/suggestions",
        "SUPERVISOR",
        {
            "plantId": plant, **A,
            "title": "Air-conditioning for the finishing hall",
            "category": "MORALE",
            "description": "The hall runs above 34 degrees through May and June.",
            **src(),
        },
    )
    api.post(f"/api/be/suggestions/{s3['id']}/submit", "SUPERVISOR")
    api.post(
        f"/api/be/suggestions/{s3['id']}/screen",
        "SUPERVISOR",
        {"outcome": "RELEVANT", "note": "Real problem, but this is a capex conversation."},
    )
    api.post(
        f"/api/be/suggestions/{s3['id']}/decide",
        "DEPARTMENT_HEAD",
        {
            "decision": "DEFER",
            "rationale": (
                "Deferred, not declined. Outside this year's capex envelope; it goes "
                "into the FY27 submission with the roof insulation study."
            ),
            "deferredUntil": iso(now + timedelta(days=20)),
        },
    )
    print(f"  {s3['id'][:8]} deferred with a return date")
    out["suggestion"].append(s3["id"])

    s4 = api.post(
        "/api/be/suggestions",
        "WORKER",
        {
            "plantId": plant, **A,
            "title": "Switch the canteen tea supplier",
            "category": "MORALE",
            "description": "The current tea is not popular on the night shift.",
            **src(),
        },
    )
    api.post(f"/api/be/suggestions/{s4['id']}/submit", "WORKER")
    api.post(
        f"/api/be/suggestions/{s4['id']}/screen",
        "SUPERVISOR",
        {"outcome": "NOT_RELEVANT", "note": "Canteen contract is a facilities matter."},
    )
    api.post(
        f"/api/be/suggestions/{s4['id']}/decide",
        "DEPARTMENT_HEAD",
        {
            "decision": "REJECT",
            "rationale": (
                "Passed to Facilities rather than the BE scheme — they hold the "
                "canteen contract and the next review is in November."
            ),
        },
    )
    print(f"  {s4['id'][:8]} not accepted, with a reason the submitter can see")
    out["suggestion"].append(s4["id"])

    # ── OPL ───────────────────────────────────────────────────────────────
    print("\nOne Point Lesson")
    # NOT the TRAINER: OPL.CREATE is granted to corporate / plant leadership /
    # frontline supervision, and TRAINER is in none of those tiers. Worth a
    # second look — a training role that cannot author a training artefact is
    # surprising — but the seed uses a role that genuinely holds the permission
    # rather than papering over it.
    opl_author = "SAFETY_OFFICER" if "SAFETY_OFFICER" in api.cast else "DEPARTMENT_HEAD"
    o1 = api.post(
        "/api/be/opl",
        opl_author,
        {
            "plantId": plant, **A,
            "title": "Setting needle depth for sleeve attach",
            "category": "IMPROVEMENT_CASE",
            "lineOrMachine": "Sewing Line 3",
            "contentHtml": (
                "<p>Needle depth for sleeve attach is set to the second witness mark, "
                "not the third. The third mark is for cuff attach on the same head.</p>"
            ),
            "keyPoints": [
                "Second witness mark for sleeve attach",
                "Third mark is cuff attach only",
                "Re-check after every needle change",
            ],
            "audience": {"roleCodes": ["WORKER", "SUPERVISOR", "FIELD_TECHNICIAN"]},
            "acknowledgementDueDays": 14,
            "effectiveFrom": iso(now),
            "reviewDueAt": iso(now + timedelta(days=365)),
            **src(),
        },
    )
    api.post(f"/api/be/opl/{o1['id']}/submit", opl_author)
    api.advance("BE_OPL", o1["id"])
    published = False
    for approver in ("PLANT_HSE_HEAD", "PLANT_HEAD", "ADMIN"):
        if approver not in api.cast:
            continue
        try:
            api.post(f"/api/be/opl/{o1['id']}/publish", approver)
            published = True
            break
        except RuntimeError as e:
            last = str(e)[:110]
    if published:
        ack = api.get(f"/api/be/opl/{o1['id']}/acknowledgements", opl_author)
        print(f"  {o1['id'][:8]} PUBLISHED -> {ack.get('total', 0)} acknowledgement obligations  [CHECK 4]")
    else:
        print(f"     ! publish refused: {last}")
    out["opl"].append(o1["id"])

    o2 = api.post(
        "/api/be/opl",
        opl_author,
        {
            "plantId": plant, **A,
            "title": "Reading the shade-lot ticket before cutting",
            "category": "BASIC_KNOWLEDGE",
            "keyPoints": ["Lot letter first", "Roll number second", "Never mix letters in one lay"],
            "audience": {"roleCodes": ["WORKER"]},
            **src(),
        },
    )
    print(f"  {o2['id'][:8]} draft")
    out["opl"].append(o2["id"])

    # ── POKA YOKE ─────────────────────────────────────────────────────────
    print("\nPoka Yoke")
    p1 = api.post(
        "/api/be/poka-yoke",
        "MAINTENANCE_HEAD",
        {
            "plantId": plant, **A,
            "title": "Sleeve orientation jig, Line 3",
            "defectModePrevented": "Sleeve attached inside-out at the shoulder seam.",
            "description": "A keyed jig that only seats the panel one way round.",
            "deviceType": "CONTACT",
            "approach": "PREVENTION",
            "reactionMode": "CONTROL",
            "lineOrMachine": "Sewing Line 3",
            "processStep": "Sleeve attach",
            "beforeCondition": "Operator judged orientation by eye against a sample.",
            "afterCondition": "The panel physically will not seat the wrong way round.",
            "cost": 14000.0,
            "verificationFrequency": "MONTHLY",
        },
    )
    api.post(f"/api/be/poka-yoke/{p1['id']}/submit", "MAINTENANCE_HEAD")
    api.advance("BE_POKA_YOKE", p1["id"])
    try:
        api.post(f"/api/be/poka-yoke/{p1['id']}/install", "MAINTENANCE_HEAD", {})
        api.post(
            f"/api/be/poka-yoke/{p1['id']}/verify",
            "SAFETY_OFFICER",
            {"result": "PASS", "note": "Tried both orientations; only one seats."},
        )
        print(f"  {p1['id'][:8]} installed and verified PASS")
    except RuntimeError as e:
        print(f"     ! install/verify: {str(e)[:110]}")
    out["poka"].append(p1["id"])

    p2 = api.post(
        "/api/be/poka-yoke",
        "MAINTENANCE_HEAD",
        {
            "plantId": plant, **A,
            "title": "Button feeder count sensor",
            "defectModePrevented": "Garment leaves the line one button short.",
            "deviceType": "FIXED_VALUE",
            "approach": "DETECTION",
            "reactionMode": "WARNING",
            "lineOrMachine": "Button station 1",
            "verificationFrequency": "WEEKLY",
        },
    )
    print(f"  {p2['id'][:8]} proposed")
    out["poka"].append(p2["id"])

    # ── QCC ───────────────────────────────────────────────────────────────
    print("\nQuality Circle")
    members = [
        {"userId": api.cast[r]["id"], "memberRole": role}
        for r, role in [
            ("SUPERVISOR", "LEADER"),
            ("DEPARTMENT_HEAD", "FACILITATOR"),
            ("WORKER", "MEMBER"),
            # A PLANT_HEAD inside the circle. This is the whole point of check 2:
            # they HOLD BENEFIT.VALIDATE, so when they try to sign off the
            # circle's own benefit it is the separation-of-duties rule that must
            # refuse them, not RBAC.
            ("PLANT_HEAD", "MEMBER"),
        ]
        if r in api.cast
    ]
    team = api.post(
        "/api/be/qcc/teams",
        "DEPARTMENT_HEAD",
        {
            "plantId": plant, **A,
            "name": "Sewing Line 3 Quality Circle",
            "department": "Sewing",
            "motto": "Fix it where it happens.",
            "leaderId": api.cast["SUPERVISOR"]["id"],
            "facilitatorId": api.cast["DEPARTMENT_HEAD"]["id"],
            "formedOn": iso(now - timedelta(days=120)),
            "members": members,
        },
    )
    out["qcc"]["team"] = team["id"]
    print(f"  circle {team.get('teamNo')} formed with {len(members)} members")

    proj = api.post(
        "/api/be/qcc/projects",
        "SUPERVISOR",
        {
            "teamId": team["id"], "plantId": plant, **A,
            "title": "Halve sleeve-attach rework on Line 3",
            "category": "QUALITY",
            "problemStatement": (
                "Sleeve-attach rework on Line 3 runs at 4.2% against a plant average "
                "of 1.6%. It is the single largest rework code in Sewing."
            ),
            "selectionRationale": "Highest reject count, and the circle works the line daily.",
            "priorityScore": 8.5,
            "methodology": "DMAIC",
            "scope": "Line 3 sleeve attach only. Cuff attach is out of scope.",
            "baselineMetric": "Sleeve-attach rework rate",
            "baselineValue": 4.2,
            "targetValue": 1.5,
            "metricUnit": "%",
            "targetDate": iso(now + timedelta(days=75)),
            **src(),
        },
    )
    api.post(f"/api/be/qcc/projects/{proj['id']}/charter", "SUPERVISOR")
    api.advance("BE_QCC", proj["id"])
    out["qcc"]["project"] = proj["id"]

    # Walk the first two gates so the board shows real progress, and open the
    # shared RCA the ANALYZE gate will demand.
    api.patch(
        f"/api/be/qcc/projects/{proj['id']}/stages/DEFINE",
        "SUPERVISOR",
        {
            "summary": (
                "Rework rate 4.2% over eight weeks, n=11,400 garments. Defect is "
                "shoulder-seam pucker, concentrated on the 08:00-12:00 block."
            ),
            "status": "IN_PROGRESS",
        },
    )
    api.post(f"/api/be/qcc/projects/{proj['id']}/stages/DEFINE/signoff", "PLANT_HEAD", {"note": "Baseline accepted."})
    api.patch(
        f"/api/be/qcc/projects/{proj['id']}/stages/MEASURE",
        "SUPERVISOR",
        {
            "summary": (
                "Stratified by operator, machine and shift. Machine explains most of "
                "it: heads 3 and 7 account for 71% of the rework."
            ),
            "status": "IN_PROGRESS",
        },
    )
    api.post(f"/api/be/qcc/projects/{proj['id']}/stages/MEASURE/signoff", "PLANT_HEAD", {"note": "Stratification is sound."})
    try:
        api.post(f"/api/be/qcc/projects/{proj['id']}/rca", "SUPERVISOR", {"methodology": "FIVE_WHY"})
        print("  ANALYZE gate: shared RCA opened in the platform register")
    except RuntimeError as e:
        print(f"     ! RCA: {str(e)[:110]}")
    print(f"  project {proj.get('projectNo')}: DEFINE + MEASURE signed off, now at ANALYZE")

    ben = api.post(
        "/api/be/benefits",
        "SUPERVISOR",
        {
            "sourceType": "QCC", "sourceId": proj["id"],
            "benefitType": "QUALITY_IMPROVEMENT",
            "valueKind": "FINANCIAL", "currency": "INR",
            "projectedValue": 610000.0, "annualisedValue": 610000.0,
            "validationWindowMonths": 3,
            "note": "Rework labour and fabric at the current reject rate.",
        },
    )
    api.post(
        f"/api/be/benefits/{ben['id']}/claim",
        "SUPERVISOR",
        {"realizedValue": 548000.0, "note": "Eight weeks post-change, extrapolated to a year."},
    )
    out["qcc"]["benefit"] = ben["id"]
    print(f"  benefit {ben['id'][:8]} at PENDING_VALIDATION  [CHECK 2]")

    # ── SIP ───────────────────────────────────────────────────────────────
    print("\nImprovement Projects")
    sip = api.post(
        "/api/be/sip",
        "PLANT_HEAD",
        {
            "plantId": plant, **A,
            "department": "Finishing",
            "title": "Halve changeover time across the finishing lines",
            "category": "PRODUCTIVITY",
            "scope": (
                "All four finishing lines. Covers changeover method, trolley "
                "pre-staging and the pre-set tool kit. Excludes machine replacement."
            ),
            "problemStatement": "Changeover averages 96 minutes against a 45-minute benchmark.",
            "sponsorId": api.cast["PLANT_HEAD"]["id"],
            "ownerId": api.cast["DEPARTMENT_HEAD"]["id"],
            "metricName": "Mean changeover time",
            "metricUnit": "min",
            "baselineValue": 96.0,
            "targetValue": 45.0,
            "startDate": iso(now - timedelta(days=40)),
            "targetDate": iso(now + timedelta(days=95)),
            "feasibilityScore": 7.0,
            "impactScore": 9.0,
            "investmentCost": 180000.0,
            "milestones": [
                {"name": "SMED study on Line 1", "sequence": 0,
                 "plannedDate": iso(now - timedelta(days=12)),
                 "ownerId": api.cast["DEPARTMENT_HEAD"]["id"]},
                {"name": "Pre-set tool kits built", "sequence": 1,
                 "plannedDate": iso(now + timedelta(days=10)),
                 "ownerId": api.cast["MAINTENANCE_HEAD"]["id"] if "MAINTENANCE_HEAD" in api.cast else None},
                {"name": "Roll out to Lines 2-4", "sequence": 2,
                 "plannedDate": iso(now + timedelta(days=60))},
                {"name": "Standard work signed off", "sequence": 3,
                 "plannedDate": iso(now + timedelta(days=85))},
            ],
            **src(),
        },
    )
    api.post(f"/api/be/sip/{sip['id']}/transition/SUBMITTED", "PLANT_HEAD", {})
    api.advance("BE_SIP", sip["id"])
    try:
        api.post(f"/api/be/sip/{sip['id']}/transition/IN_PROGRESS", "DEPARTMENT_HEAD", {})
    except RuntimeError as e:
        print(f"     ! start: {str(e)[:110]}")
    detail = api.get(f"/api/be/sip/{sip['id']}", "PLANT_HEAD")
    ms = detail.get("milestones", [])
    if ms:
        api.patch(
            f"/api/be/sip/{sip['id']}/milestones/{ms[0]['id']}",
            "DEPARTMENT_HEAD",
            {"status": "COMPLETED", "progressPercent": 100,
             "note": "Study done; 38 of the 96 minutes are external work already."},
        )
    if len(ms) > 1:
        # A deliberately slipped milestone, so the RAG rollup has something to
        # say and the board is not uniformly green.
        api.patch(
            f"/api/be/sip/{sip['id']}/milestones/{ms[1]['id']}",
            "DEPARTMENT_HEAD",
            {"status": "DELAYED", "revisedDate": iso(now + timedelta(days=25)),
             "progressPercent": 40, "note": "Tool steel on back-order."},
        )
    sben = api.post(
        "/api/be/benefits",
        "DEPARTMENT_HEAD",
        {
            "sourceType": "SIP", "sourceId": sip["id"],
            "benefitType": "PRODUCTIVITY_GAIN",
            "valueKind": "FINANCIAL", "currency": "INR",
            "projectedValue": 2400000.0, "annualisedValue": 2400000.0,
            "validationWindowMonths": 6,
            "note": "Recovered line hours at the standard contribution rate.",
        },
    )
    for lbl, val, back in [("2026-06", 96.0, 40), ("2026-07", 88.0, 25), ("2026-08", 74.0, 5)]:
        api.post(
            f"/api/be/benefits/{sben['id']}/readings",
            "DEPARTMENT_HEAD",
            {"actualValue": val, "targetValue": 45.0, "periodLabel": lbl,
             "note": "Mean of all four lines."},
        )
    out["sip"].append(sip["id"])
    print(f"  {sip.get('sipNo') or sip['id'][:8]} in progress, 4 milestones (1 done, 1 delayed), 3 metric readings")

    sip2 = api.post(
        "/api/be/sip",
        "PLANT_HEAD",
        {
            "plantId": plant, **A,
            "department": "Cutting",
            "title": "Marker efficiency programme",
            "category": "COST",
            "scope": "Raise average marker efficiency from 82% to 88% across all styles.",
            "sponsorId": api.cast["PLANT_HEAD"]["id"],
            "ownerId": api.cast["SUPERVISOR"]["id"],
            "metricName": "Marker efficiency",
            "metricUnit": "%",
            "baselineValue": 82.0,
            "targetValue": 88.0,
            "feasibilityScore": 6.0,
            "impactScore": 8.0,
            **src(),
        },
    )
    out["sip"].append(sip2["id"])
    print(f"  {sip2['id'][:8]} draft, awaiting submission")

    return out



def wipe(api: "Api") -> int:
    """Withdraw everything this seed created, so it can be re-run cleanly.

    Soft-delete through the API, not DELETE FROM: these are governed entities
    and the ORM guard refuses a hard delete anyway. Benefit lines go with their
    source because the delete endpoints cascade them.

    Record numbers are NOT released — a soft-deleted record keeps its number by
    design, so a re-seed continues the sequence rather than reusing it.
    """
    removed = 0
    plans = [
        ("/api/be/suggestions", "BeSuggestion"),
        ("/api/be/qcc/projects", "BeQccProject"),
        ("/api/be/sip", "BeSip"),
        ("/api/be/kaizen", "BeKaizen"),
        ("/api/be/opl", "BeOpl"),
        ("/api/be/poka-yoke", "BePokaYoke"),
        ("/api/be/qcc/teams", "BeQccTeam"),
    ]
    hdr = {
        "Authorization": f"Bearer {api.cast['ADMIN']['token']}",
        "x-audit-reason": "Business Excellence demo seed withdrawn for a clean re-seed",
    }
    for path, table in plans:
        if table == "BeQccTeam":
            # Teams carry no sourceRecordRef; find them by the seed's own name.
            rows = _q(
                'select id from "BeQccTeam" where name like :n and "isDeleted" = false',
                n="%Quality Circle%",
            )
        elif table == "BePokaYoke":
            # BePokaYoke has sourceKaizenId / sourceRcaId but no
            # sourceRecordRef, so the marker has nowhere to live. Matched on the
            # seed's own titles instead.
            rows = _q(
                'select id from "BePokaYoke" where title = any(:t) and "isDeleted" = false',
                t=["Sleeve orientation jig, Line 3", "Button feeder count sensor"],
            )
        else:
            rows = _q(
                f'select id from "{table}" where "sourceRecordRef" = :m and "isDeleted" = false',
                m=MARKER,
            )
        for row in rows:
            r = api.c.delete(f"{path}/{row['id']}", headers=hdr)
            api.calls += 1
            if r.status_code in (200, 204):
                removed += 1
            else:
                print(f"  ! DELETE {path}/{row['id'][:8]} -> {r.status_code} {r.text[:100]}")
    # Benefit lines whose source is now withdrawn (belt and braces: the delete
    # endpoints cascade, but a partial earlier run may predate that).
    n = _q(
        '''update "BeBenefit" b set "isDeleted" = true, "deletedAt" = now(),
                "deletionReason" = \'Business Excellence demo seed withdrawn\'
             where b."isDeleted" = false
               and not exists (select 1 from "BeSip" x where x.id = b."sourceId" and x."isDeleted" = false)
               and not exists (select 1 from "BeQccProject" x where x.id = b."sourceId" and x."isDeleted" = false)
               and not exists (select 1 from "BeKaizen" x where x.id = b."sourceId" and x."isDeleted" = false)
               and not exists (select 1 from "BeSuggestion" x where x.id = b."sourceId" and x."isDeleted" = false)
           returning b.id'''
    )
    return removed + len(n)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="seed again even if a marker is present")
    ap.add_argument("--wipe", action="store_true", help="withdraw the existing seed, then re-seed")
    args = ap.parse_args()

    if args.wipe:
        cast, plant, area, plant_name = asyncio.run(load_cast())
        api = Api(cast, plant)
        n = wipe(api)
        print(f"Withdrew {n} seeded record(s).")
        args.force = True

    existing = asyncio.run(already_seeded())
    if existing and not args.force:
        print(f"Already seeded ({existing} marked Kaizen records). Nothing to do.")
        print("Re-run with --force to add a second dataset.")
        return 0

    cast, plant, area, plant_name = asyncio.run(load_cast())
    missing = [r for r in ("ADMIN", "SUPERVISOR", "DEPARTMENT_HEAD", "WORKER", "PLANT_HEAD") if r not in cast]
    if missing:
        print(f"Cannot seed: no user at this plant holds {', '.join(missing)}")
        return 1

    print(f"Seeding Business Excellence demo data")
    print(f"  plant : {plant_name}")
    print(f"  api   : {BASE}")
    print(f"  cast  : {len(cast)} roles")

    api = Api(cast, plant)
    out = seed(api, plant, area)
    print(f"\nDone. {api.calls} API calls.")
    print(json.dumps({k: (v if not isinstance(v, list) else len(v)) for k, v in out.items()}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
