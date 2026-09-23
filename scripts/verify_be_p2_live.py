"""BE Phase 2 — live verification (sections A–F).

The corrected form of the "BE Phase 2 Live Verification" diagnostic. The version
that circulated was written against the polymorphic `be_submissions` design that
was proposed and then REJECTED on 2026-08-24 in favour of extending natively, so
it named tables and endpoints that do not exist. Run this instead.

  python scripts/verify_be_p2_live.py

Environment:
  API_BASE          e.g. https://safeops-api-main.epfassist.com   (required for B–E)
  TOKEN_USER        bearer token for a NON-admin user
  TOKEN_ADMIN       bearer token for an admin
  TOKEN_BLOCKED     bearer token for a user who SHOULD be refused by section E —
                    a QCC circle member, or a SIP owner/sponsor
  DATABASE_URL      falls back to the backend's own settings if unset

Sections A, B, D and F are strictly read-only.

⚠ SECTIONS C AND E WRITE IF AND ONLY IF THE THING THEY TEST IS BROKEN.
They are negative tests: C tries to skip a stage gate, E tries to validate a
benefit as somebody who should be blocked. If the guard holds, the API refuses
and nothing changes — that is a PASS. If the guard is missing, the request
SUCCEEDS and mutates the record, which is the failure being reported. There is no
way to test "does this actually block" without attempting the thing. Run them
against staging, or against a record you are willing to have advanced, and pass
--allow-writes to acknowledge it. Without that flag they are reported SKIPPED.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# Windows consoles default to cp1252, which cannot encode the arrows and warning
# marks in the evidence lines — the run would die on print(), not on a check.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── The REAL schema. Quoted camelCase, mirroring Prisma. ────────────────────
P2_TABLES = [
    "BeSuggestion",
    "BeQccTeam",
    "BeQccTeamMember",
    "BeQccProject",
    "BeQccProjectStage",
    "BeSip",
    "BeSipMilestone",
    "BeBenefit",
    "BeBenefitReading",
]

P2_PERMISSION_MODULES = ["SUGGESTION", "QCC", "SIP", "BENEFIT"]
P2_WORKFLOW_MODULES = ["BE_SUGGESTION", "BE_QCC", "BE_SIP"]

#: Tables that SHOULD be empty on a fresh deploy. There is no demo seed for
#: Phase 2, and seed-be-p2-workflows.ts creates WorkflowDefinition rows only —
#: the circulated prompt called empty register tables a FAIL, which is wrong.
EMPTY_IS_EXPECTED = set(P2_TABLES)

results: dict[str, tuple[str, list[str]]] = {}


def record(section: str, status: str, evidence: list[str]) -> None:
    results[section] = (status, evidence)


def api(path: str) -> str:
    return f"{os.environ.get('API_BASE', '').rstrip('/')}{path}"


def hdr(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


# ═══════════════════════════════════════════════════════════════════════════
# A — schema, permissions, workflow definitions, and who can actually validate
# ═══════════════════════════════════════════════════════════════════════════
async def section_a(conn) -> dict[str, Any]:
    ev: list[str] = []
    ok = True

    present = set(
        (
            await conn.execute(
                text(
                    "select table_name from information_schema.tables "
                    "where table_name = any(:n)"
                ),
                {"n": P2_TABLES},
            )
        )
        .scalars()
        .all()
    )
    ev.append(f"tables present: {len(present)}/9")
    for t in P2_TABLES:
        ev.append(f"  {'EXISTS' if t in present else 'ABSENT'}  {t}")
    if len(present) != 9:
        ok = False

    counts: dict[str, int] = {}
    for t in sorted(present):
        n = (await conn.execute(text(f'select count(*) from "{t}"'))).scalar() or 0
        counts[t] = n
        # Empty is the CORRECT state for a fresh deploy, not a failure.
        note = "" if t not in EMPTY_IS_EXPECTED else "  (empty is expected on a fresh deploy)"
        ev.append(f"  {t}: {n} row(s){note}")

    perms = (
        await conn.execute(
            text('select count(*) from "Permission" where module = any(:m)'),
            {"m": P2_PERMISSION_MODULES},
        )
    ).scalar() or 0
    ev.append(f"permissions: {perms}/20")
    if perms != 20:
        ok = False

    wf = (
        await conn.execute(
            text('select count(*) from "WorkflowDefinition" where module = any(:m)'),
            {"m": P2_WORKFLOW_MODULES},
        )
    ).scalar() or 0
    ev.append(f"workflow definitions: {wf}/3")
    if wf != 3:
        ok = False

    # ROLE-BASED, not a user_permissions table. This is the check that matters:
    # if no real user can reach BENEFIT.VALIDATE, every benefit is permanently
    # stuck at PENDING_VALIDATION however green the deploy-time verifier was.
    rows = (
        await conn.execute(
            text(
                """
                select distinct u.id, u.email, r.code as role
                from "User" u
                join "UserRole" ur on ur."userId" = u.id
                join "Role" r on r.id = ur."roleId"
                join "RolePermission" rp on rp."roleId" = r.id
                join "Permission" p on p.id = rp."permissionId"
                where p.code = 'BENEFIT.VALIDATE'
                order by u.email
                """
            )
        )
    ).all()
    ev.append(f"users who can reach BENEFIT.VALIDATE: {len(rows)}")
    for r in rows[:10]:
        ev.append(f"  {r.email}  ({r.role})")
    if not rows:
        ok = False
        ev.append("  ⚠ zero — every benefit would be stuck at PENDING_VALIDATION")

    record("A", "PASS" if ok else "FAIL", ev)
    return {"present": present, "counts": counts, "validators": rows}


# ═══════════════════════════════════════════════════════════════════════════
# B — endpoint reachability, against the REAL paths
# ═══════════════════════════════════════════════════════════════════════════
async def section_b(client, conn, state) -> None:
    ev: list[str] = []
    ok = True

    for label, path in [
        ("Suggestion register", "/api/be/suggestions"),
        ("QCC circles", "/api/be/qcc/teams"),
        ("QCC projects", "/api/be/qcc/projects"),
        ("SIP portfolio", "/api/be/sip"),
        ("Benefits", "/api/be/benefits"),
        ("Dashboard", "/api/be/dashboards/summary"),
        ("Meta", "/api/be/meta/p2"),
    ]:
        try:
            r = await client.get(api(path), headers=hdr(os.environ.get("TOKEN_USER")))
            body = r.text[:180].replace("\n", " ")
            ev.append(f"GET {path} → {r.status_code}  {body}")
            if r.status_code >= 400:
                ok = False
            # A 200 carrying an empty shell is a FAIL, per the brief.
            elif path != "/api/be/meta/p2":
                try:
                    j = r.json()
                    if not isinstance(j, dict) or "items" not in j and "workflows" not in j:
                        ev.append(f"  ⚠ 200 but payload has no items/workflows key")
                        ok = False
                except Exception:
                    ev.append("  ⚠ 200 but body is not JSON")
                    ok = False
        except Exception as e:
            ev.append(f"GET {path} → EXCEPTION {type(e).__name__}: {e}")
            ok = False

    # Fetch one real record of each type by id, if any exist.
    for table, path in [
        ("BeSuggestion", "/api/be/suggestions"),
        ("BeQccProject", "/api/be/qcc/projects"),
        ("BeSip", "/api/be/sip"),
    ]:
        if table not in state["present"] or not state["counts"].get(table):
            ev.append(f"{table}: no rows — cannot fetch one by id")
            continue
        rid = (
            await conn.execute(text(f'select id from "{table}" limit 1'))
        ).scalar()
        r = await client.get(api(f"{path}/{rid}"), headers=hdr(os.environ.get("TOKEN_USER")))
        keys = list(r.json().keys())[:12] if r.status_code == 200 else []
        ev.append(f"GET {path}/{rid} → {r.status_code}  keys={keys}")
        if r.status_code != 200 or len(keys) < 5:
            ok = False

    record("B", "PASS" if ok else "FAIL", ev)



def _refused_by_rbac(r) -> bool:
    """True when the API stopped the request at the RBAC layer.

    C and E test DOMAIN gates — the stage sequence and separation of duties.
    A 403 "Missing permission 'X'" means the caller was stopped one layer
    EARLIER and the gate under test never ran. Counting that as a PASS is the
    exact false-positive these sections exist to catch: it reports the gate as
    working when nothing exercised it. Such a run is INCONCLUSIVE, not PASS.
    """
    try:
        return "Missing permission" in (r.json() or {}).get("detail", "")
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════════
# C — does the stage gate actually block?  ⚠ WRITES IF BROKEN
# ═══════════════════════════════════════════════════════════════════════════
async def section_c(client, conn, state, allow_writes: bool) -> None:
    if "BeQccProjectStage" not in state["present"] or not state["counts"].get("BeQccProjectStage"):
        record("C", "UNKNOWN", ["no BeQccProjectStage rows — no live project to test"])
        return
    if not allow_writes:
        record("C", "SKIPPED", ["negative test mutates if the gate is broken; pass --allow-writes"])
        return

    # Find a project with an un-signed-off EARLY gate, and target a LATER one.
    row = (
        await conn.execute(
            text(
                """
                select s."projectId", s.stage, s.sequence
                from "BeQccProjectStage" s
                join "BeQccProject" p on p.id = s."projectId"
                where s.status <> 'SIGNED_OFF' and p.status in ('CHARTERED','IN_PROGRESS')
                order by s.sequence desc limit 1
                """
            )
        )
    ).first()
    if row is None:
        record("C", "UNKNOWN", ["no project with an open later-stage gate"])
        return

    # Real endpoint: POST /api/be/qcc/projects/{pid}/stages/{stage}/signoff
    url = api(f"/api/be/qcc/projects/{row.projectId}/stages/{row.stage}/signoff")
    r = await client.post(url, headers=hdr(os.environ.get("TOKEN_USER")), json={"note": "gate probe"})
    ev = [
        f"POST {url} (skipping earlier gates) → {r.status_code}",
        f"  body: {r.text[:300]}",
    ]
    if _refused_by_rbac(r):
        ev.append("  ⚠ refused by RBAC, not by the gate — this caller lacks QCC.SIGNOFF,")
        ev.append("    so the stage-sequence rule was never exercised. Re-run with a")
        ev.append("    TOKEN_USER that HOLDS QCC.SIGNOFF to test the gate itself.")
        record("C", "INCONCLUSIVE", ev)
        return
    ok = r.status_code in (409, 422)
    if not ok:
        ev.append("  ⚠ the gate did NOT block — sign-off succeeded out of sequence")
    record("C", "PASS" if ok else "FAIL", ev)


# ═══════════════════════════════════════════════════════════════════════════
# D — is anonymity suppressed server-side, or only in the UI?  READ-ONLY
# ═══════════════════════════════════════════════════════════════════════════
async def section_d(client, conn, state) -> None:
    if "BeSuggestion" not in state["present"]:
        record("D", "UNKNOWN", ["BeSuggestion absent"])
        return

    row = (
        await conn.execute(
            text(
                'select id, "createdById", "isAnonymous" from "BeSuggestion" '
                'where "isAnonymous" = true limit 1'
            )
        )
    ).first()
    if row is None:
        record("D", "UNKNOWN", ["no anonymous suggestion exists to test"])
        return

    ev = [f'DB: id={row.id}  createdById={row.createdById}  isAnonymous={row.isAnonymous}']
    if not row.createdById:
        ev.append("  ⚠ createdById is empty — that is a different bug (it is NOT NULL by design)")

    # The admin token is the point: suppression must hold for administrators too.
    r = await client.get(
        api(f"/api/be/suggestions/{row.id}"), headers=hdr(os.environ.get("TOKEN_ADMIN"))
    )
    body = r.json() if r.status_code == 200 else {}
    submitted_by = body.get("submittedBy")
    raw = json.dumps(body)
    leaked = row.createdById and row.createdById in raw

    ev.append(f"GET /api/be/suggestions/{row.id} (ADMIN token) → {r.status_code}")
    ev.append(f"  submittedBy = {submitted_by!r}")
    ev.append(f"  createdById present anywhere in the raw payload: {bool(leaked)}")
    ok = r.status_code == 200 and submitted_by is None and not leaked
    if not ok:
        ev.append("  ⚠ the identity is reachable via the API — suppression is cosmetic")
    record("D", "PASS" if ok else "FAIL", ev)


# ═══════════════════════════════════════════════════════════════════════════
# E — is separation of duties wired into the endpoint?  ⚠ WRITES IF BROKEN
# ═══════════════════════════════════════════════════════════════════════════
async def section_e(client, conn, state, allow_writes: bool) -> None:
    if "BeBenefit" not in state["present"] or not state["counts"].get("BeBenefit"):
        record("E", "UNKNOWN", ["no BeBenefit rows to test"])
        return
    if not os.environ.get("TOKEN_BLOCKED"):
        record("E", "UNKNOWN", ["TOKEN_BLOCKED not set — needs a user the rule should refuse"])
        return
    if not allow_writes:
        record("E", "SKIPPED", ["negative test mutates if the guard is missing; pass --allow-writes"])
        return

    row = (
        await conn.execute(
            text(
                'select id, "sourceType", "sourceId" from "BeBenefit" '
                "where status = 'PENDING_VALIDATION' and \"sourceType\" in ('QCC','SIP') limit 1"
            )
        )
    ).first()
    if row is None:
        record("E", "UNKNOWN", ["no QCC/SIP benefit at PENDING_VALIDATION"])
        return

    # Real endpoint: POST /api/be/benefits/{bid}/validate
    url = api(f"/api/be/benefits/{row.id}/validate")
    r = await client.post(
        url, headers=hdr(os.environ.get("TOKEN_BLOCKED")), json={"accept": True, "note": "probe"}
    )
    ev = [
        f"benefit {row.id} (source {row.sourceType}/{row.sourceId})",
        f"POST {url} as the blocked user → {r.status_code}",
        f"  body: {r.text[:300]}",
    ]
    if _refused_by_rbac(r):
        ev.append("  ⚠ refused by RBAC, not by validation_blockers() — this caller lacks")
        ev.append("    BENEFIT.VALIDATE, so the separation-of-duties rule never ran.")
        ev.append("    Re-run with a TOKEN_BLOCKED that HOLDS BENEFIT.VALIDATE but is the")
        ev.append("    record's owner/sponsor/recorder, which is the case that matters.")
        record("E", "INCONCLUSIVE", ev)
        return
    ok = r.status_code in (403, 409)
    if not ok:
        ev.append("  ⚠ validation succeeded — validation_blockers() is not wired into this route")
    record("E", "PASS" if ok else "FAIL", ev)


# ═══════════════════════════════════════════════════════════════════════════
# F — frontend wiring. Static; a HAR capture needs a browser session.
# ═══════════════════════════════════════════════════════════════════════════
def section_f(repo_root: str) -> None:
    import re
    from pathlib import Path

    be = Path(repo_root) / "safeops_360" / "src" / "app" / "(dashboard)" / "business-excellence"
    if not be.exists():
        record("F", "UNKNOWN", [f"frontend not found at {be}"])
        return

    bad: list[str] = []
    paths: set[str] = set()
    for f in be.rglob("*.tsx"):
        src = f.read_text(encoding="utf-8", errors="replace")
        # A-Z matters: a template literal like `${projectId}` would otherwise
        # truncate at the capital and the {id} substitution below would never
        # fire, leaving "/api/be/qcc/projects/${project" in the evidence.
        for m in re.finditer(r'["\`](/api/[A-Za-z0-9/{}$_.\-]*)', src):
            path = re.sub(r"\$\{[^}]*\}", "{id}", m.group(1)).rstrip("/")
            paths.add(path)
        if re.search(r"\b(localhost|127\.0\.0\.1|MOCK_|mockData|__mocks__)\b", src):
            bad.append(str(f.relative_to(be)))

    ev = [f"API paths referenced: {sorted(paths)}", f"mock/localhost hits: {bad or 'none'}"]
    ev.append("NOTE: static source check. A network trace needs a deployed build + browser session.")
    record("F", "PASS" if not bad else "FAIL", ev)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--allow-writes",
        action="store_true",
        help="permit sections C and E, which mutate ONLY if the guard they test is missing",
    )
    args = ap.parse_args()

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.path.insert(0, os.path.join(repo_root, "safeops_360_bakend"))
        from app.core.config import get_settings

        url = get_settings().async_database_url

    # Supabase exposes a session pooler (:5432, prepared statements OK) and a
    # transaction pooler (:6543, which does NOT support them and raises
    # DuplicatePreparedStatementError). Mirror app/core/db.py rather than
    # guessing — a diagnostic that cannot connect proves nothing.
    disable_cache = ":6543/" in url or url.endswith(":6543")
    eng = create_async_engine(
        url,
        echo=False,
        connect_args=(
            {"statement_cache_size": 0, "prepared_statement_cache_size": 0}
            if disable_cache
            else {}
        ),
    )
    async with eng.connect() as conn:
        state = await section_a(conn)

        if not os.environ.get("API_BASE"):
            for s in ("B", "C", "D", "E"):
                record(s, "UNKNOWN", ["API_BASE not set"])
        elif httpx is None:
            for s in ("B", "C", "D", "E"):
                record(s, "UNKNOWN", ["httpx not installed"])
        else:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                await section_b(client, conn, state)
                await section_c(client, conn, state, args.allow_writes)
                await section_d(client, conn, state)
                await section_e(client, conn, state, args.allow_writes)

    await eng.dispose()
    section_f(repo_root)

    print("\n" + "=" * 72)
    for s in "ABCDEF":
        status, ev = results.get(s, ("UNKNOWN", ["not run"]))
        print(f"\n[{s}] — {status}")
        print("Evidence:")
        for line in ev:
            print(f"  {line}")

    passed = sum(1 for s in "ABCDEF" if results.get(s, ("", []))[0] == "PASS")
    failed = [s for s in "ABCDEF" if results.get(s, ("", []))[0] == "FAIL"]
    print("\n" + "=" * 72)
    print(f"{passed}/6 PASS." + (f"  FAILED: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
