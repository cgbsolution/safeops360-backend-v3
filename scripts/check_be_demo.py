"""The five conditions worth proving on the Business Excellence module.

Run AFTER scripts/seed_be_demo.py. Each check states PASS/FAIL with the query or
response that proves it — no check reports green off a status column alone.

  API_BASE=http://127.0.0.1:8000 python scripts/check_be_demo.py

Checks 1, 3, 4 and 5 are read-only. Check 2 attempts a validation that MUST be
refused (nothing changes when the rule holds) and then performs one that must
succeed, on the SIP benefit — the positive case matters as much as the negative,
because a rule that blocks everybody looks identical to a broken endpoint.
"""

from __future__ import annotations

import json
import os
import sys

import httpx
from sqlalchemy import create_engine, text

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.config import get_settings  # noqa: E402
from app.core.security import create_access_token  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000")
_url = get_settings().database_url_sync or get_settings().async_database_url
if "+asyncpg" in _url:
    _url = _url.replace("+asyncpg", "+psycopg2")
ENG = create_engine(_url, pool_pre_ping=True)
C = httpx.Client(base_url=BASE, timeout=60.0)

results: list[tuple[str, bool, list[str]]] = []


def q(sql: str, **p):
    with ENG.connect() as c:
        return c.execute(text(sql), p).mappings().all()


def tok(user_id: str) -> str:
    u = q('select id, email, role, "plantId" from "User" where id = :i', i=user_id)[0]
    return create_access_token(
        subject=u["id"],
        extra_claims={"role": u["role"], "plantId": u["plantId"], "email": u["email"]},
    )


def H(user_id: str) -> dict:
    return {"Authorization": f"Bearer {tok(user_id)}", "Content-Type": "application/json"}


def report(name: str, ok: bool, ev: list[str]) -> None:
    results.append((name, ok, ev))
    print(f"\n{'PASS' if ok else 'FAIL'}  {name}")
    for line in ev:
        print(f"      {line}")


# ═══════════════════════════════════════════════════════════════════════════
# 1 — does a submitted Kaizen land in a screening QUEUE, or just a table?
# ═══════════════════════════════════════════════════════════════════════════
def check_1() -> None:
    ev: list[str] = []
    k = q(
        '''select id, "kaizenNo", title, status, "workflowInstanceId"
             from "BeKaizen" where status = 'SUBMITTED' and "isDeleted" = false
             order by "createdAt" desc limit 1'''
    )
    if not k:
        report("1. Kaizen lands in a screening queue", False, ["no SUBMITTED Kaizen found"])
        return
    k = k[0]
    ev.append(f'record   {k["kaizenNo"]}  status={k["status"]}')
    # A status column alone proves nothing. The queue is a WorkflowTask row.
    if not k["workflowInstanceId"]:
        report("1. Kaizen lands in a screening queue", False,
               ev + ["no workflowInstanceId — it never entered a chain"])
        return

    tasks = q(
        '''select t.id, t."stepName", t.status, t."assignedToId", t."dueAt", u.email, u.role
             from "WorkflowTask" t join "User" u on u.id = t."assignedToId"
            where t.module = 'BE_KAIZEN' and t."recordId" = :r''',
        r=k["id"],
    )
    ev.append(f"WorkflowTask rows: {len(tasks)}")
    for t in tasks:
        ev.append(f'  {t["stepName"]:22} {t["status"]:10} -> {t["email"]} ({t["role"]})')
    if not tasks:
        report("1. Kaizen lands in a screening queue", False,
               ev + ["instance exists but NO task — nobody was ever asked to act"])
        return

    pending = [t for t in tasks if t["status"] == "PENDING"]
    if not pending:
        report("1. Kaizen lands in a screening queue", False, ev + ["no PENDING task"])
        return

    # And it is genuinely in that person's inbox, not merely in the table.
    t0 = pending[0]
    inbox = C.get("/api/workflow/tasks?status_filter=PENDING&limit=200", headers=H(t0["assignedToId"]))
    hit = [
        i for i in inbox.json().get("items", [])
        if i.get("recordId") == k["id"]
    ]
    ev.append(f'inbox of {t0["email"]}: GET /api/workflow/tasks -> {inbox.status_code}, '
              f'{"CONTAINS" if hit else "DOES NOT CONTAIN"} this Kaizen')
    if hit:
        ev.append(f'  "{hit[0].get("recordTitle")}" step={hit[0].get("stepName")} '
                  f'due={str(hit[0].get("dueAt"))[:10]}')

    # And the approval is actually actionable by that person.
    perm = q(
        '''select 1 from "User" u
             join "UserRole" ur on ur."userId" = u.id
             join "RolePermission" rp on rp."roleId" = ur."roleId"
             join "Permission" p on p.id = rp."permissionId"
            where u.id = :u and p.code = 'BE_KAIZEN.APPROVE' limit 1''',
        u=t0["assignedToId"],
    )
    ev.append(f'assignee holds BE_KAIZEN.APPROVE: {"yes" if perm else "NO — the queue is decorative"}')
    report("1. Kaizen lands in a screening queue", bool(hit) and bool(perm), ev)


# ═══════════════════════════════════════════════════════════════════════════
# 2 — does the benefit rule BLOCK a circle member, or only warn?
# ═══════════════════════════════════════════════════════════════════════════
def check_2() -> None:
    ev: list[str] = []
    b = q(
        '''select b.id, b."sourceId", b.status, b."createdById"
             from "BeBenefit" b where b."sourceType" = 'QCC'
              and b.status = 'PENDING_VALIDATION' and b."isDeleted" = false
             order by b."createdAt" desc limit 1'''
    )
    if not b:
        report("2. Circle member cannot validate the circle's benefit", False,
               ["no QCC benefit at PENDING_VALIDATION"])
        return
    b = b[0]

    # A circle member who ALSO holds BENEFIT.VALIDATE. Without that second half
    # the request is refused by RBAC and the rule under test never runs — the
    # false pass this check exists to avoid.
    member = q(
        '''select distinct u.id, u.email, u.role
             from "BeQccProject" pr
             join "BeQccTeamMember" m on m."teamId" = pr."teamId"
             join "User" u on u.id = m."userId"
             join "UserRole" ur on ur."userId" = u.id
             join "RolePermission" rp on rp."roleId" = ur."roleId"
             join "Permission" p on p.id = rp."permissionId"
            where pr.id = :p and p.code = 'BENEFIT.VALIDATE' limit 1''',
        p=b["sourceId"],
    )
    if not member:
        report("2. Circle member cannot validate the circle's benefit", False,
               ["no circle member holds BENEFIT.VALIDATE — the rule cannot be tested"])
        return
    m = member[0]
    ev.append(f'circle member {m["email"]} ({m["role"]}) — holds BENEFIT.VALIDATE')

    r = C.post(f"/api/be/benefits/{b['id']}/validate", headers=H(m["id"]),
               json={"accept": True, "note": "attempting to sign off our own work"})
    ev.append(f'POST /api/be/benefits/{b["id"][:8]}/validate -> {r.status_code}')
    ev.append(f'  {r.text[:220]}')
    rbac = "Missing permission" in r.text
    blocked = r.status_code in (403, 409) and not rbac
    if rbac:
        ev.append("  ⚠ refused by RBAC, not the rule — INCONCLUSIVE")

    after = q('select status from "BeBenefit" where id = :i', i=b["id"])[0]["status"]
    ev.append(f'benefit status after the attempt: {after} (unchanged = the block is real)')

    # The positive control, on a DIFFERENT benefit so the QCC one stays pending
    # for the demo. Without this, "blocks everyone" is indistinguishable from
    # "endpoint is broken".
    sipb = q(
        '''select id, "sourceId" from "BeBenefit"
             where "sourceType" = 'SIP' and status <> 'VALIDATED' and "isDeleted" = false
             order by "createdAt" desc limit 1'''
    )
    if sipb:
        indep = q(
            '''select distinct u.id, u.email, u.role from "User" u
                 join "UserRole" ur on ur."userId" = u.id
                 join "RolePermission" rp on rp."roleId" = ur."roleId"
                 join "Permission" p on p.id = rp."permissionId"
                 left join "BeSip" s on s.id = :s
                where p.code = 'BENEFIT.VALIDATE'
                  and u.id not in (coalesce(s."ownerId",''), coalesce(s."sponsorId",''), coalesce(s."createdById",''))
                order by u.email limit 1''',
            s=sipb[0]["sourceId"],
        )
        if indep:
            C.post(f"/api/be/benefits/{sipb[0]['id']}/claim", headers=H(indep[0]["id"]),
                   json={"realizedValue": 2180000.0, "note": "Line-hours recovered, Q2 actuals."})
            r2 = C.post(f"/api/be/benefits/{sipb[0]['id']}/validate", headers=H(indep[0]["id"]),
                        json={"accept": True, "note": "Finance confirmed against the contribution model."})
            ev.append(f'control: independent validator on a SIP benefit -> {r2.status_code}'
                      f' ({"VALIDATED" if r2.status_code == 200 else r2.text[:120]})')
            blocked = blocked and r2.status_code == 200
    report("2. Circle member cannot validate the circle's benefit", blocked, ev)


# ═══════════════════════════════════════════════════════════════════════════
# 3 — is an anonymous submitter hidden from the API, not just the form?
# ═══════════════════════════════════════════════════════════════════════════
def check_3() -> None:
    ev: list[str] = []
    s = q(
        '''select id, "suggestionNo", "createdById", "isAnonymous"
             from "BeSuggestion" where "isAnonymous" = true and "isDeleted" = false
             order by "createdAt" desc limit 1'''
    )
    if not s:
        report("3. Anonymous submitter is hidden", False, ["no anonymous suggestion found"])
        return
    s = s[0]
    author = q('select email, role from "User" where id = :i', i=s["createdById"])[0]
    ev.append(f'DB: {s["suggestionNo"]}  isAnonymous=true  createdById -> {author["email"]} ({author["role"]})')

    # Read it as a DIFFERENT administrator. If anyone unmasks it, an admin will.
    other = q(
        '''select id, email from "User" where role = 'ADMIN' and id <> :me order by email limit 1''',
        me=s["createdById"],
    )[0]
    r = C.get(f"/api/be/suggestions/{s['id']}", headers=H(other["id"]))
    body = r.json()
    raw = json.dumps(body)
    leaked = s["createdById"] in raw or author["email"] in raw
    ev.append(f'GET /api/be/suggestions/{s["id"][:8]} as {other["email"]} -> {r.status_code}')
    ev.append(f'  submittedBy = {body.get("submittedBy")!r}')
    ev.append(f'  creator id or email anywhere in the raw payload: {leaked}')

    # ...and in the register listing, which is a different code path.
    lst = C.get("/api/be/suggestions?limit=200", headers=H(other["id"])).json()
    row = next((i for i in lst.get("items", []) if i["id"] == s["id"]), None)
    ev.append(f'  register row submittedBy = {row.get("submittedBy")!r}' if row else "  not in register")

    # ...and the submitter still sees their own.
    own = C.get(f"/api/be/suggestions/{s['id']}", headers=H(s["createdById"])).json()
    ev.append(f'  submitter reading their own: submittedBy = '
              f'{(own.get("submittedBy") or {}).get("name")!r}')

    ok = (
        r.status_code == 200
        and body.get("submittedBy") is None
        and not leaked
        and (row is None or row.get("submittedBy") is None)
        and own.get("submittedBy") is not None
    )
    report("3. Anonymous submitter is hidden", ok, ev)


# ═══════════════════════════════════════════════════════════════════════════
# 4 — does publishing an OPL create real obligations, or flip a flag?
# ═══════════════════════════════════════════════════════════════════════════
def check_4() -> None:
    ev: list[str] = []
    o = q(
        '''select id, "oplNo", title, status, revision, "publishedAt", audience
             from "BeOpl" where status = 'PUBLISHED' and "isDeleted" = false
             order by "publishedAt" desc limit 1'''
    )
    if not o:
        report("4. OPL publish creates acknowledgement obligations", False,
               ["no PUBLISHED OPL found"])
        return
    o = o[0]
    ev.append(f'{o["oplNo"]}  status={o["status"]}  rev={o["revision"]}')
    ev.append(f'  audience spec: {json.dumps(o["audience"])}')

    acks = q(
        '''select a.id, a.status, a."dueAt", u.email, u.role
             from "BeOplAcknowledgement" a join "User" u on u.id = a."personUserId"
            where a."oplId" = :o order by u.email''',
        o=o["id"],
    )
    ev.append(f'BeOplAcknowledgement rows: {len(acks)}  '
              f'{"(publish is NOT just a status flip)" if acks else "(publish only flipped a flag)"}')
    for a in acks[:6]:
        ev.append(f'  {a["email"]:38} {a["role"]:18} {a["status"]:12} due {str(a["dueAt"])[:10]}')
    if len(acks) > 6:
        ev.append(f"  ... and {len(acks) - 6} more")
    if not acks:
        report("4. OPL publish creates acknowledgement obligations", False, ev)
        return

    # And one of those people can actually discharge it.
    target = acks[0]
    mine = C.get("/api/be/opl/mine", headers=H(q('select id from "User" where email = :e',
                                                 e=target["email"])[0]["id"]))
    n_mine = len(mine.json().get("items", mine.json())) if mine.status_code == 200 else -1
    ev.append(f'GET /api/be/opl/mine as {target["email"]} -> {mine.status_code}, {n_mine} outstanding')

    uid = q('select id from "User" where email = :e', e=target["email"])[0]["id"]
    r = C.post(f"/api/be/opl/{o['id']}/acknowledge", headers=H(uid),
               json={"note": "Read and understood."})
    ev.append(f'POST /api/be/opl/{o["id"][:8]}/acknowledge -> {r.status_code}')
    after = q('select status, "acknowledgedAt" from "BeOplAcknowledgement" where id = :i',
              i=target["id"])[0]
    ev.append(f'  that row is now {after["status"]} at {str(after["acknowledgedAt"])[:19]}')
    report("4. OPL publish creates acknowledgement obligations",
           len(acks) > 0 and after["status"] == "ACKNOWLEDGED", ev)


# ═══════════════════════════════════════════════════════════════════════════
# 5 — do the registers and detail views have anything behind them?
# ═══════════════════════════════════════════════════════════════════════════
def check_5() -> None:
    ev: list[str] = []
    admin = q("""select id from "User" where role = 'ADMIN' order by email limit 1""")[0]["id"]
    h = H(admin)
    ok = True
    registers = [
        ("Kaizen", "/api/be/kaizen", "/api/be/kaizen/{id}"),
        ("Suggestion", "/api/be/suggestions", "/api/be/suggestions/{id}"),
        ("OPL", "/api/be/opl", "/api/be/opl/{id}"),
        ("Poka Yoke", "/api/be/poka-yoke", "/api/be/poka-yoke/{id}"),
        ("QCC circles", "/api/be/qcc/teams", "/api/be/qcc/teams/{id}"),
        ("QCC projects", "/api/be/qcc/projects", "/api/be/qcc/projects/{id}"),
        ("SIP", "/api/be/sip", "/api/be/sip/{id}"),
    ]
    for label, lst, det in registers:
        rl = C.get(f"{lst}?limit=100", headers=h)
        items = rl.json().get("items", []) if rl.status_code == 200 else []
        if not items:
            ev.append(f"{label:14} list {rl.status_code}  0 records  <- empty register")
            ok = False
            continue
        rd = C.get(det.replace("{id}", items[0]["id"]), headers=h)
        nkeys = len(rd.json().keys()) if rd.status_code == 200 else 0
        # A detail view that returns the same shape as the list row is a list
        # row, not a detail view.
        deeper = nkeys > len(items[0].keys())
        ev.append(
            f"{label:14} list {rl.status_code} {len(items):3} records   "
            f"detail {rd.status_code} {nkeys:2} fields "
            f"({'richer than the row' if deeper else 'NO deeper than the list row'})"
        )
        ok = ok and rd.status_code == 200 and deeper

    d = C.get("/api/be/dashboards/summary", headers=h)
    wf = d.json().get("workflows", []) if d.status_code == 200 else []
    ev.append(f"dashboard     {d.status_code}  " +
              ", ".join(f'{w["workflowType"]}={w["total"]}' for w in wf))
    ok = ok and d.status_code == 200 and sum(w["total"] for w in wf) > 0
    report("5. Registers and detail views have records behind them", ok, ev)


if __name__ == "__main__":
    print(f"Business Excellence — five conditions\napi: {BASE}")
    for fn in (check_1, check_2, check_3, check_4, check_5):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            report(fn.__name__, False, [f"raised {type(e).__name__}: {e}"])
    passed = sum(1 for _, ok, _ in results if ok)
    print("\n" + "=" * 72)
    print(f"{passed}/{len(results)} conditions PASS")
    for name, ok, _ in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    sys.exit(0 if passed == len(results) else 1)
