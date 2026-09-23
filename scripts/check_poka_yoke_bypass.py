"""Live verification of the Poka Yoke bypass log (gap-closure §1).

Proves the two things the build claims, against the REAL database and the REAL
API — not a fixture:

  1. Logging a bypass makes the device stop reading as Active on the register
     AND on the dashboard tile, immediately.
  2. Restoring it puts the device back and KEEPS the episode in history as a
     distinct entry — which is the whole point, because the previous
     implementation erased it.

Plus the three guards that make those two claims safe: the log (not the flag)
is what the tile counts, a second bypass on an already-bypassed device is
refused, and a restore of an already-restored episode is refused.

  API_BASE=http://127.0.0.1:8000 python scripts/check_poka_yoke_bypass.py

⚠ This WRITES. It picks a live device, bypasses it, restores it, and leaves the
closed episode behind as history. That is deliberate — a read-only check cannot
prove a write path — but it means the device it touches ends at DEGRADED owing a
verification, exactly as a real restore would leave it. It never touches a
device that already has an open bypass.
"""

from __future__ import annotations

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


def check(name: str, ok: bool, *evidence: str) -> None:
    results.append((name, ok, list(evidence)))
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    for e in evidence:
        print(f"        {e}")


def q(sql: str, **kw):
    with ENG.connect() as c:
        return c.execute(text(sql), kw).fetchall()


def main() -> int:
    # ── Pick an actor who can actually do this, and a device to do it to ─────
    device = q(
        """
        SELECT d."id", d."deviceNo", d."title", d."status", d."plantId", d."createdById"
          FROM "BePokaYoke" d
         WHERE d."isDeleted" = false
           AND d."status" IN ('VERIFIED','ACTIVE')
           AND NOT EXISTS (
                 SELECT 1 FROM "BePokaYokeBypass" b
                  WHERE b."deviceId" = d."id" AND b."restoredAt" IS NULL)
         ORDER BY d."createdAt" DESC
         LIMIT 1
        """
    )
    if not device:
        check(
            "A bypassable device exists",
            False,
            "No live device is at VERIFIED/ACTIVE without an open bypass. "
            "Only such a device can be bypassed (allowed_poka_yoke_actions), so "
            "there is nothing to verify against.",
        )
        return 1

    did, dno, dtitle, dstatus, plant_id, creator = device[0]
    check(
        "A bypassable device exists",
        True,
        f"{dno or did} — {dtitle!r}, status={dstatus}, plant={plant_id}",
    )

    actor = q(
        """
        SELECT u."id", u."name", u."email" FROM "User" u
         WHERE u."id" = :c LIMIT 1
        """,
        c=creator,
    )
    if not actor:
        check("An actor to bypass as", False, f"User {creator} not found")
        return 1
    uid, uname, uemail = actor[0]
    H = {"Authorization": f"Bearer {create_access_token(uid)}"}
    check("An actor to bypass as", True, f"{uname} <{uemail}>")

    def tile_count() -> int:
        r = C.get("/api/be/poka-yoke", params={"plantId": plant_id}, headers=H)
        r.raise_for_status()
        return r.json().get("activeBypasses", -1)

    before_tile = tile_count()

    # ── 1. Bypass ───────────────────────────────────────────────────────────
    REASON = "Verification check — sensor awaiting replacement, 100% manual inspection in place."
    r = C.post(f"/api/be/poka-yoke/{did}/bypass", json={"reason": REASON}, headers=H)
    if r.status_code != 200:
        check("POST /bypass succeeds", False, f"HTTP {r.status_code}: {r.text[:300]}")
        return 1
    body = r.json()
    check("POST /bypass succeeds", True, f"HTTP 200, isBypassed={body['isBypassed']}")

    # The row exists in the LOG, not just as a flag.
    rows = q(
        'SELECT "id","reason","bypassedById","statusAtBypass","restoredAt" '
        'FROM "BePokaYokeBypass" WHERE "deviceId"=:d AND "restoredAt" IS NULL',
        d=did,
    )
    check(
        "The bypass is a row in BePokaYokeBypass, not just a flag",
        len(rows) == 1 and rows[0][1] == REASON,
        f"{len(rows)} open episode(s); reason stored={rows[0][1][:60]!r}" if rows else "no row",
    )
    bypass_id = rows[0][0] if rows else None
    check(
        "It recorded what the device WAS before the bypass",
        bool(rows) and rows[0][3] == dstatus,
        f"statusAtBypass={rows[0][3]!r} (device was {dstatus})" if rows else "-",
    )

    # The status the SCREEN shows changed — not buried in a note.
    check(
        "displayStatus flips to BYPASSED while status is preserved",
        body.get("displayStatus") == "BYPASSED" and body.get("status") == dstatus,
        f"displayStatus={body.get('displayStatus')!r}, status={body.get('status')!r} "
        "(the lifecycle column is untouched, so 'what was it before?' survives)",
    )

    # The dashboard tile moved, immediately, from a scope-wide count.
    after_tile = tile_count()
    check(
        "The ACTIVE BYPASSES tile increments immediately",
        after_tile == before_tile + 1,
        f"activeBypasses {before_tile} → {after_tile}",
    )

    # The register row itself reads Bypassed.
    lr = C.get("/api/be/poka-yoke", params={"plantId": plant_id, "bypassed": True}, headers=H)
    listed = [i for i in lr.json().get("items", []) if i["id"] == did]
    check(
        "The device appears under the ?bypassed=1 filter, reading Bypassed",
        bool(listed) and listed[0]["displayStatus"] == "BYPASSED",
        f"row displayStatus={listed[0]['displayStatus']!r}, "
        f"bypassOpenHours={listed[0]['bypassOpenHours']}" if listed else "not in the filtered list",
    )

    # ── 2. Guards ───────────────────────────────────────────────────────────
    r2 = C.post(f"/api/be/poka-yoke/{did}/bypass", json={"reason": REASON}, headers=H)
    check(
        "A second bypass on an already-bypassed device is refused",
        r2.status_code == 409,
        f"HTTP {r2.status_code} — {r2.json().get('detail', '')[:80]!r}",
    )

    dup = q(
        'SELECT count(*) FROM "BePokaYokeBypass" WHERE "deviceId"=:d AND "restoredAt" IS NULL',
        d=did,
    )[0][0]
    check(
        "…and no second episode was created",
        dup == 1,
        f"{dup} open episode(s) — the partial unique index holds",
    )

    # ── 3. Restore ──────────────────────────────────────────────────────────
    r3 = C.post(
        f"/api/be/poka-yoke/bypasses/{bypass_id}/restore",
        json={"note": "Part fitted and refitted; device back in circuit."},
        headers=H,
    )
    if r3.status_code != 200:
        check("POST /bypasses/{id}/restore succeeds", False, f"HTTP {r3.status_code}: {r3.text[:300]}")
        return 1
    restored = r3.json()
    check("POST /bypasses/{id}/restore succeeds", True, f"HTTP 200")

    check(
        "The device returns to its correct status and owes a check",
        restored["status"] == "DEGRADED"
        and restored["displayStatus"] == "DEGRADED"
        and restored["isBypassed"] is False,
        f"status={restored['status']}, displayStatus={restored['displayStatus']}, "
        f"nextVerificationDueAt={restored['nextVerificationDueAt']}",
    )

    # THE point of the whole change.
    ep = q(
        'SELECT "reason","bypassedById","bypassedAt","restoredById","restoredAt","restoreNote" '
        'FROM "BePokaYokeBypass" WHERE "id"=:b',
        b=bypass_id,
    )
    survived = bool(ep) and all(
        v is not None for v in (ep[0][0], ep[0][1], ep[0][2], ep[0][3], ep[0][4])
    )
    check(
        "The episode SURVIVES the restore with who/when intact",
        survived,
        f"reason kept={bool(ep and ep[0][0])}, bypassedBy kept={bool(ep and ep[0][1])}, "
        f"bypassedAt kept={bool(ep and ep[0][2])}, restoredBy stamped={bool(ep and ep[0][3])}, "
        f"restoredAt stamped={bool(ep and ep[0][4])}",
        "This is the defect being closed: the previous /restore NULLed "
        "bypassedAt, bypassedById and bypassApprovedById on the device row.",
    )

    check(
        "The episode is in the detail payload as a distinct closed entry",
        any(b["id"] == bypass_id and not b["isOpen"] for b in restored.get("bypasses", []))
        and restored.get("activeBypass") is None,
        f"{len(restored.get('bypasses', []))} episode(s) in history; "
        f"activeBypass={restored.get('activeBypass')}",
    )

    dur = next(
        (b["durationHours"] for b in restored.get("bypasses", []) if b["id"] == bypass_id), None
    )
    check(
        "The closed episode carries a measured duration",
        dur is not None,
        f"durationHours={dur}",
    )

    final_tile = tile_count()
    check(
        "The ACTIVE BYPASSES tile decrements on restore",
        final_tile == before_tile,
        f"activeBypasses {after_tile} → {final_tile} (started at {before_tile})",
    )

    r4 = C.post(f"/api/be/poka-yoke/bypasses/{bypass_id}/restore", json={}, headers=H)
    check(
        "Restoring an already-restored episode is refused",
        r4.status_code == 409,
        f"HTTP {r4.status_code} — {r4.json().get('detail', '')[:80]!r}",
    )

    # ── Summary ─────────────────────────────────────────────────────────────
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n{'=' * 60}\n{passed}/{len(results)} checks passed")
    if passed != len(results):
        print("\nFailed:")
        for name, ok, _ in results:
            if not ok:
                print(f"  ✗ {name}")
        return 1
    print(f"\nDevice {dno or did} is left at DEGRADED owing a verification, with one\n"
          f"closed bypass episode in its history — exactly as a real restore leaves it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
