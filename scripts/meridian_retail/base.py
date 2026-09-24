"""Meridian Retail — base: sites, areas, roles, users, module switches.

Isolation model (no Tenant table in this schema — the scope unit is Plant):
  * Every Retail user's home plant and PLANT-scoped UserRole rows are Retail
    sites only, so their accessible-plant set never includes another tenant.
  * Retail users hold ONLY the RETAIL_* roles created here. Those are clones of
    existing roles, filtered to the modules Retail uses, with every ALL_PLANTS
    grant downgraded to OWN_PLANT. (The stock WORKER / SUPERVISOR / PLANT_HEAD
    roles carry ALL_PLANTS on INCIDENT/NEAR_MISS/OBSERVATION — a store employee
    holding one would read Manufacturing's incidents.) No existing role or
    grant is modified.
  * Module switches: FactoryModuleEntitlement enabled=false rows on the 43 sites
    for everything outside the Retail edition (licensed codes and ungated ones,
    incl. CAMS_GENERAL → CAMS for Fire Safety audits only).

    python -m scripts.meridian_retail.base            # dry run
    python -m scripts.meridian_retail.base --commit
"""

from __future__ import annotations

import sys

from scripts.meridian_retail.common import (
    DC_AREAS,
    DCS,
    EMAIL_DOMAIN,
    RNG,
    STORE_AREAS,
    STORES,
    conn,
    dc_code,
    dc_name,
    new_id,
    password_hash,
    plant_map,
    store_code,
    store_name,
)

# Module permission prefixes the Retail edition uses.
RETAIL_PERMISSION_MODULES = {
    "FIRE", "CAMS", "CAPA", "INCIDENT", "NEAR_MISS", "CAPTURE", "PTW", "LOTO", "EPC", "ALERT",
}

# code → (name, source role to clone, extra modules to take from ANOTHER role)
RETAIL_ROLES: dict[str, tuple[str, str, str | None]] = {
    "RETAIL_OPS_ADMIN": ("Retail Ops & Safety Admin", "HSE_MANAGER", None),
    "RETAIL_STORE_MANAGER": ("Store Manager", "PLANT_HEAD", None),
    "RETAIL_FIRE_TECHNICIAN": ("Fire Safety Technician", "FIELD_TECHNICIAN", None),
    "RETAIL_FLOOR_STAFF": ("Store Floor Staff", "WORKER", None),
    "RETAIL_FIRE_AUDITOR": ("Fire Safety Lead Auditor", "LEAD_AUDITOR", None),
    "RETAIL_DC_MAINTENANCE": ("DC Maintenance Lead", "MAINTENANCE_HEAD", "PERMIT_ISSUER"),
    "RETAIL_PROJECTS": ("Store Projects & Contractor Coordinator", "CONTRACTOR_COORDINATOR", "HSE_MANAGER"),
}
# For the `extra` role only these modules are borrowed.
EXTRA_MODULES = {"RETAIL_DC_MAINTENANCE": {"PTW", "LOTO"}, "RETAIL_PROJECTS": {"EPC"}}

# Grants the cloned source roles don't carry but the Retail job needs — every
# role reads its Daily Brief; store managers see the contractors working in
# their store; DC maintenance reports incidents and near misses; the scorecard
# (gated on INCIDENT.READ) is visible to every role. All own-plant.
EXTRA_GRANTS: dict[str, list[str]] = {
    "RETAIL_OPS_ADMIN": ["ALERT.READ"],
    "RETAIL_STORE_MANAGER": ["ALERT.READ", "EPC.READ"],
    "RETAIL_FIRE_TECHNICIAN": ["ALERT.READ", "INCIDENT.READ", "NEAR_MISS.READ", "NEAR_MISS.CREATE"],
    "RETAIL_FLOOR_STAFF": ["ALERT.READ"],
    "RETAIL_FIRE_AUDITOR": ["ALERT.READ", "INCIDENT.READ"],
    "RETAIL_DC_MAINTENANCE": ["ALERT.READ", "INCIDENT.READ", "INCIDENT.CREATE", "NEAR_MISS.READ", "NEAR_MISS.CREATE"],
    "RETAIL_PROJECTS": ["ALERT.READ", "INCIDENT.READ", "EPC.READ"],
}

# Modules switched OFF on every Retail site. Licensed codes + ungated codes.
DISABLED_MODULES = [
    # spec: disabled
    "MOC", "BRSR", "ERM", "BCM", "BUSINESS_EXCELLENCE", "TRAINING_ENGINE",
    # the rest of the ERM family
    "KRI", "APPETITE", "ERM_COMPLIANCE", "LOSS", "CONTROL", "VENDOR", "INSURANCE",
    # not part of the Retail edition
    "HIRA", "EAI", "RISK_AGG", "STATUTORY_REGISTERS", "FACILITIES", "TRAINING", "COMPETENCY",
    "SCI", "PPE", "INSPECTION", "MANHOURS", "ANOMALIES", "AI_ASSIST", "OBSERVATION", "FLRA", "SIGNALS",
    # CAMS for Fire Safety engagements only
    "CAMS_GENERAL",
]

FIRST = ["Aarav", "Diya", "Rohan", "Ananya", "Kabir", "Isha", "Arjun", "Meera", "Vihaan", "Saanvi",
         "Aditya", "Kiara", "Reyansh", "Nisha", "Karthik", "Pooja", "Siddharth", "Riya", "Varun", "Sneha",
         "Harsh", "Tanvi", "Nikhil", "Aisha", "Manoj", "Lakshmi", "Suresh", "Farhan", "Gurpreet", "Deepa"]
LAST = ["Sharma", "Iyer", "Patel", "Reddy", "Nair", "Singh", "Menon", "Gupta", "Rao", "Kulkarni",
        "Das", "Joshi", "Khan", "Pillai", "Chatterjee", "Bhat", "Verma", "Shetty", "Malhotra", "Mishra"]


_NAMES: dict[int, str] = {}


def _name(i: int) -> str:
    """A stable, unique person name per seed slot. Unique matters: an auditor
    sharing a name with the store manager they audit would muddy the
    independence story on screen."""
    if i not in _NAMES:
        used = set(_NAMES.values())
        k = 0
        while True:
            # k walks all FIRST×LAST combinations, so a free one is always found.
            cand = f"{FIRST[(i * 7 + k) % len(FIRST)]} {LAST[(i * 11 + k // len(FIRST)) % len(LAST)]}"
            if cand not in used and cand not in ("Kavya Menon", "Imran Qureshi"):
                _NAMES[i] = cand
                break
            k += 1
    return _NAMES[i]


def seed_plants(cur) -> dict[str, str]:
    have = plant_map(cur)
    rows = [(store_code(i), store_name(i), f"{s[0]}, {s[1]}", s[2], f"Retail Store — {s[3]}") for i, s in enumerate(STORES, 1)]
    rows += [(dc_code(i), dc_name(i), f"{d[0]}, {d[1]}", d[2], "Distribution Center") for i, d in enumerate(DCS, 1)]
    added = 0
    for code, name, loc, state, unit in rows:
        if code in have:
            continue
        pid = new_id()
        cur.execute(
            'insert into "Plant"(id, code, name, location, state, "unitType") values (%s,%s,%s,%s,%s,%s)',
            (pid, code, name, loc, state, unit),
        )
        have[code] = pid
        added += 1
    print(f"plants: {added} added, {len(have)} total")
    # Areas
    cur.execute('select "plantId", name from "Area" where "plantId" = any(%s)', (list(have.values()),))
    existing = set(cur.fetchall())
    n = 0
    for code, pid in have.items():
        for a in DC_AREAS if code.startswith("MR-DC") else STORE_AREAS:
            if (pid, a) not in existing:
                cur.execute('insert into "Area"(id, name, "plantId") values (%s,%s,%s)', (new_id(), a, pid))
                n += 1
    print(f"areas: {n} added")
    return have


def seed_roles(cur) -> dict[str, str]:
    cur.execute('select code, id from "Role"')
    roles = dict(cur.fetchall())
    out: dict[str, str] = {}
    for i, (code, (name, source, extra)) in enumerate(RETAIL_ROLES.items()):
        if code in roles:
            rid = roles[code]
        else:
            rid = new_id()
            cur.execute(
                'insert into "Role"(id, code, name, description, "isSystem", "isActive", "sortOrder", "defaultLanding", "updatedAt") '
                'values (%s,%s,%s,%s,false,true,%s,%s,now())',
                (rid, code, name, f"Meridian Retail — cloned from {source}, own-plant scope only", 900 + i, "/dashboard/daily"),
            )
        out[code] = rid
        grants: dict[str, str] = {}
        for src, mods in ((source, RETAIL_PERMISSION_MODULES), (extra, EXTRA_MODULES.get(code, set()))):
            if not src:
                continue
            cur.execute(
                'select p.id, p.code, rp.scope from "RolePermission" rp join "Permission" p on p.id=rp."permissionId" '
                'join "Role" r on r.id=rp."roleId" where r.code=%s',
                (src,),
            )
            for pid, pcode, scope in cur.fetchall():
                if pcode.split(".")[0] not in mods:
                    continue
                grants[pid] = "OWN_PLANT" if scope == "ALL_PLANTS" else scope
        for pcode in EXTRA_GRANTS.get(code, []):
            cur.execute('select id from "Permission" where code=%s', (pcode,))
            row = cur.fetchone()
            assert row, f"permission {pcode} does not exist"
            grants.setdefault(row[0], "OWN_PLANT")
        cur.execute('select "permissionId" from "RolePermission" where "roleId"=%s', (rid,))
        held = {r[0] for r in cur.fetchall()}
        for pid, scope in grants.items():
            if pid not in held:
                cur.execute(
                    'insert into "RolePermission"(id, "roleId", "permissionId", scope) values (%s,%s,%s,%s)',
                    (new_id(), rid, pid, scope),
                )
        cur.execute(
            'select count(*) filter (where scope=%s), count(*) from "RolePermission" where "roleId"=%s',
            ("ALL_PLANTS", rid),
        )
        allp, total = cur.fetchone()
        assert allp == 0, f"{code} must not hold ALL_PLANTS grants"
        print(f"role {code}: {total} grants (0 ALL_PLANTS) from {source}{' + ' + extra if extra else ''}")
    return out


def _user(cur, users: dict[str, str], email: str, name: str, role: str, home: str, designation: str, department: str | None = None) -> str:
    if email in users:
        return users[email]
    uid = new_id()
    cur.execute(
        'insert into "User"(id, email, name, "passwordHash", role, "plantId", designation, department) '
        'values (%s,%s,%s,%s,%s,%s,%s,%s)',
        (uid, email, name, password_hash(), role, home, designation, department),
    )
    users[email] = uid
    return uid


def _grant(cur, uid: str, rid: str, plant_ids: list[str]) -> None:
    cur.execute('select "scopeValue" from "UserRole" where "userId"=%s and "roleId"=%s', (uid, rid))
    have = {r[0] for r in cur.fetchall()}
    for pid in plant_ids:
        if pid not in have:
            cur.execute(
                'insert into "UserRole"(id, "userId", "roleId", "scopeType", "scopeValue") values (%s,%s,%s,%s,%s)',
                (new_id(), uid, rid, "PLANT", pid),
            )


def seed_users(cur, plants: dict[str, str], roles: dict[str, str]) -> None:
    cur.execute('select email, id from "User" where email like %s', ("%@" + EMAIL_DOMAIN,))
    users = dict(cur.fetchall())
    stores = [plants[store_code(i)] for i in range(1, len(STORES) + 1)]
    dcs = [plants[dc_code(i)] for i in range(1, len(DCS) + 1)]
    everything = stores + dcs

    uid = _user(cur, users, f"store-ops.admin@{EMAIL_DOMAIN}", "Kavya Menon", "RETAIL_OPS_ADMIN",
                plants[dc_code(1)], "Head — Store Operations & Safety", "Operations")
    _grant(cur, uid, roles["RETAIL_OPS_ADMIN"], everything)

    for i in range(1, len(STORES) + 1):
        pid = plants[store_code(i)]
        uid = _user(cur, users, f"sm.s{i:03d}@{EMAIL_DOMAIN}", _name(i), "RETAIL_STORE_MANAGER", pid, "Store Manager", "Store Operations")
        _grant(cur, uid, roles["RETAIL_STORE_MANAGER"], [pid])

    # Floor staff at 14 stores — they file the mobile field reports.
    for i in range(1, 15):
        pid = plants[store_code(i)]
        for tag, desig in (("a", "Customer Service Associate"), ("b", "Stock Associate")):
            uid = _user(cur, users, f"floor.s{i:03d}.{tag}@{EMAIL_DOMAIN}", _name(100 + i * 2 + (tag == "b")),
                        "RETAIL_FLOOR_STAFF", pid, desig, "Store Floor")
            _grant(cur, uid, roles["RETAIL_FLOOR_STAFF"], [pid])

    # AMC fire technicians, 10 stores each; tech 1 also covers the DCs.
    for t in range(4):
        cover = stores[t * 10:(t + 1) * 10] + (dcs if t == 0 else [])
        uid = _user(cur, users, f"fire.tech{t + 1}@{EMAIL_DOMAIN}", _name(200 + t), "RETAIL_FIRE_TECHNICIAN",
                    cover[0], "Fire Safety Technician (AMC)", "Facilities")
        _grant(cur, uid, roles["RETAIL_FIRE_TECHNICIAN"], cover)

    # Lead auditors (independent of store operations), by region.
    for a, cover in enumerate((stores[:14], stores[14:28], stores[28:] + dcs), 1):
        uid = _user(cur, users, f"fire.auditor{a}@{EMAIL_DOMAIN}", _name(300 + a), "RETAIL_FIRE_AUDITOR",
                    cover[0], "Fire Safety Lead Auditor", "Internal Audit")
        _grant(cur, uid, roles["RETAIL_FIRE_AUDITOR"], cover)

    for d in range(1, len(DCS) + 1):
        pid = plants[dc_code(d)]
        uid = _user(cur, users, f"dc.manager.dc{d:02d}@{EMAIL_DOMAIN}", _name(400 + d), "RETAIL_STORE_MANAGER", pid, "DC Manager", "Distribution")
        _grant(cur, uid, roles["RETAIL_STORE_MANAGER"], [pid])
        uid = _user(cur, users, f"dc.maint.dc{d:02d}@{EMAIL_DOMAIN}", _name(410 + d), "RETAIL_DC_MAINTENANCE", pid, "DC Maintenance Lead", "Engineering")
        _grant(cur, uid, roles["RETAIL_DC_MAINTENANCE"], [pid])

    uid = _user(cur, users, f"projects@{EMAIL_DOMAIN}", "Imran Qureshi", "RETAIL_PROJECTS", stores[0],
                "Store Projects & Fit-out Manager", "Projects")
    _grant(cur, uid, roles["RETAIL_PROJECTS"], stores)
    print(f"users: {len(users)} Meridian Retail users")


def seed_module_switches(cur, plants: dict[str, str]) -> None:
    n = 0
    for pid in plants.values():
        cur.execute('select "moduleCode" from "FactoryModuleEntitlement" where "plantId"=%s', (pid,))
        have = {r[0] for r in cur.fetchall()}
        for code in DISABLED_MODULES:
            if code in have:
                continue
            cur.execute(
                'insert into "FactoryModuleEntitlement"(id, "plantId", "moduleCode", enabled, "updatedBy", "updatedAt") '
                "values (%s,%s,%s,false,%s,now())",
                (new_id(), pid, code, "seed:meridian-retail"),
            )
            n += 1
    print(f"module switches: {n} OFF rows added ({len(DISABLED_MODULES)} modules × {len(plants)} sites)")


def seed_label_profile(cur, plants: dict[str, str]) -> None:
    cur.execute('select 1 from "DisplayLabelProfile" where code=%s', ("RETAIL",))
    if not cur.fetchone():
        cur.execute(
            'insert into "DisplayLabelProfile"(id, code, name, description) values (%s,%s,%s,%s)',
            (new_id(), "RETAIL", "Meridian Retail", "Store / Distribution Center vocabulary"),
        )
    n = 0
    for pid in plants.values():
        cur.execute(
            'insert into "PlantDisplayProfile"("plantId", "profileCode") values (%s,%s) on conflict ("plantId") do nothing',
            (pid, "RETAIL"),
        )
        n += cur.rowcount
    print(f"label profile RETAIL: {n} plants mapped (labels: scripts/meridian_retail/labels.py)")


def main(commit: bool) -> None:
    c = conn()
    cur = c.cursor()
    RNG.seed(20260923)
    plants = seed_plants(cur)
    roles = seed_roles(cur)
    seed_users(cur, plants, roles)
    seed_module_switches(cur, plants)
    seed_label_profile(cur, plants)
    if commit:
        c.commit()
        print("committed")
    else:
        c.rollback()
        print("dry run — rolled back (pass --commit)")


if __name__ == "__main__":
    main("--commit" in sys.argv)
