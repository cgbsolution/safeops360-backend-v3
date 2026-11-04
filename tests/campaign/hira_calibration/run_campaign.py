"""HIRA calibration v2: multi-HIRA test campaign, OFFLINE part.

Runs the corpus (corpus.json, from build_corpus.py) through the v2 engine with
no database, no network and no LLM, and checks invariants I1-I16 where they
can be checked offline, plus the cross-HIRA tests (consistency, monotonicity,
order independence, concurrency, study rollup, recommendation load).

Safety:
  * DATABASE_URL / DATABASE_URL_SYNC / DIRECT_URL are overridden to an
    unroutable host before the app is imported, and every connection attempt on
    the app's engine is counted (expected: 0). Nothing is written to any DB.
  * --base-url (for a later staging run) is refused unless it is listed in
    staging_allowlist.json next to this file. This script makes no HTTP calls.
  * Engine code, rubrics and expected bands are only read, never written; the
    eval harness is imported, its main() (which rewrites REPORT.md) is not run.

    cd safeops_360_bakend && .venv/Scripts/python.exe tests/campaign/hira_calibration/run_campaign.py
"""

from __future__ import annotations

import os

# ── guard: no database, before anything from app/ is imported ──
_GUARD_URL = "postgresql+asyncpg://campaign:offline@offline-guard.invalid:5432/none"
for _k in ("DATABASE_URL", "DATABASE_URL_SYNC", "DIRECT_URL"):
    os.environ[_k] = _GUARD_URL
os.environ["SCHEDULER_ENABLED"] = "false"

import argparse  # noqa: E402
import asyncio  # noqa: E402
import copy  # noqa: E402
import csv  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import platform  # noqa: E402
import random  # noqa: E402
import re  # noqa: E402
import statistics  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
from datetime import UTC, datetime  # noqa: E402
from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from typing import Any  # noqa: E402

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
FRONTEND = ROOT.parent / "safeops_360"
OUT = HERE / "out"
CORPUS = HERE / "corpus.json"
ALLOWLIST = HERE / "staging_allowlist.json"
CURATED = HERE / "curated_findings.json"  # hand-verified engine findings (F-series), merged into defects.csv
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import hira_calibration_eval as E  # noqa: E402
from sqlalchemy import event as sa_event  # noqa: E402

from app.core import db as app_db  # noqa: E402
from app.services.hira_calibration import loader  # noqa: E402
from app.services.hira_calibration import matrix as mx  # noqa: E402
from app.services.hira_calibration import recommend as rec  # noqa: E402
from app.services.hira_calibration import review_v2 as rv  # noqa: E402
from app.services.hira_calibration import risk_engine as eng  # noqa: E402
from app.services.hira_calibration import rubric_packs as rp  # noqa: E402
from app.services.hira_calibration.config import (  # noqa: E402
    CALIBRATED_TYPES,
    DEFAULT_CONFIG,
    ENGINE_VERSION,
    MatrixRule,
)

DB_CONNECT_ATTEMPTS = {"n": 0}


@sa_event.listens_for(app_db.engine.sync_engine, "do_connect")
def _count_connect(*_a: Any, **_k: Any) -> None:
    DB_CONNECT_ATTEMPTS["n"] += 1
    raise RuntimeError("campaign guard: database connections are not allowed in the offline campaign")


CORPUS_DATA: dict[str, Any] = {}
NOW = datetime(2026, 11, 1, tzinfo=UTC)
BANDS = ("LOW", "MODERATE", "HIGH", "CRITICAL")
ENGINE_DIR = str(ROOT / "app" / "services" / "hira_calibration")


# ─── corpus -> engine inputs (plugged into the eval harness) ────────────

_orig_rule_for, _orig_evidence_for, _orig_v1_numbers = E.rule_for, E.evidence_for, E.v1_numbers


def rule_for(fixture: dict[str, Any], act: dict[str, Any]) -> MatrixRule:
    m = (CORPUS_DATA.get("matrices") or fixture["matrices"])[act["matrix"]]
    adapter = mx.from_dict(m["adapter"]) if m.get("adapter") else None
    return MatrixRule(act["matrix"], m["likelihoodScale"], bool(m["moderateNotAcceptable"]), adapter)


def _event(x: dict[str, Any]) -> loader.Event:
    kind = "near miss" if x["id"].startswith("NM") else "incident"
    when = datetime.fromisoformat(x["date"]).replace(tzinfo=UTC)
    return loader.Event(when=when, label=f"{kind} {x['id']}", area_id=x["area"], text=x["text"], category=x.get("category"))


def evidence_for(act: dict[str, Any]) -> eng.Evidence:
    if act.get("events") or act.get("failures"):
        return loader.evidence_from_events(
            [_event(x) for x in act.get("events") or []],
            entry_area=act.get("entryArea"),
            months=DEFAULT_CONFIG["likelihood"]["evidenceWindowMonths"],
            now=NOW,
            scope="AREA",
            failures=[tuple(f) for f in act.get("failures") or []],
        )
    return _orig_evidence_for(act)


def v1_numbers(act: dict[str, Any]) -> dict[str, Any]:
    try:
        return _orig_v1_numbers(act)
    except Exception as exc:  # noqa: BLE001 - the v1 model needs L/S on every hazard
        return {"error": f"v1 model not computable: {type(exc).__name__}", "recommendations": len(act.get("recommendedControls") or [])}


E.rule_for, E.evidence_for, E.v1_numbers = rule_for, evidence_for, v1_numbers


def pack_cfg(code: str | None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    return rp.apply_pack(DEFAULT_CONFIG, code)


def is_calibrated(act: dict[str, Any]) -> bool:
    return act["tags"]["hiraType"] in CALIBRATED_TYPES


def stored_input(act: dict[str, Any]) -> rv.EntryInput:
    e = E.stored_entry(act)
    e.hira_type = act["tags"]["hiraType"]
    return e


def _engine_frame(tb: str) -> bool:
    frames = re.findall(r'File "([^"]+)"', tb)
    return bool(frames) and ENGINE_DIR.lower() in frames[-1].lower()


def run_one(act: dict[str, Any], *, pack: str | None = "__TAG__") -> dict[str, Any]:
    """Engine outputs for one corpus item. pack="__TAG__" uses the item's industry pack tag."""
    code = act["tags"]["industryPack"] if pack == "__TAG__" else pack
    code = None if code in (None, "UNKNOWN") else code
    cfg, meta = pack_cfg(code)
    scope = rp.scope_for(meta, act["tags"]["hiraType"])
    t0 = time.perf_counter()
    try:
        if not is_calibrated(act):
            r = rv.evaluate(stored_input(act), cfg=cfg, rule=rule_for(CORPUS_DATA, act), evidence=evidence_for(act))
            shown = rp.apply_scope(r, scope)
            out = {"kind": "STRUCTURAL", "summary": None, "raw": {"pass1": r}, "shown": shown, "scope": scope}
        else:
            summary, raw = E.run_activity(act, CORPUS_DATA, cfg=cfg)
            out = {"kind": "CALIBRATED", "summary": summary, "raw": raw, "shown": rp.apply_scope(raw["pass1"], scope), "scope": scope}
        out["error"] = None
    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc()
        out = {"kind": "ERROR", "summary": None, "raw": {}, "shown": None, "scope": scope,
               "error": {"type": type(exc).__name__, "message": str(exc)[:300], "engine": _engine_frame(tb), "traceback": tb[-1500:]}}
    out["ms"] = (time.perf_counter() - t0) * 1000
    out["pack"] = code
    return out


def digest(o: Any) -> str:
    return hashlib.sha256(json.dumps(o, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def item_digest(r: dict[str, Any]) -> str:
    return digest({"summary": r["summary"], "raw": r["raw"], "shown": r["shown"], "error": (r["error"] or {}).get("message")})


# ─── invariant checks ────────────────────────────────────────────────────


class Check:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def add(self, inv: str, act: dict[str, Any] | None, ok: bool | None, detail: str = "", *, severity: str | None = None,
            expected: str = "", observed: str = "", variant: str = "") -> None:
        self.rows.append({
            "invariant": inv,
            "id": act["id"] if act else "(global)",
            "suite": act["tags"]["suite"] if act else "-",
            "variant": variant,
            "result": "N/A" if ok is None else ("PASS" if ok else "FAIL"),
            "detail": detail,
            "severity": severity or ("blocker" if ok is False else ""),
            "expected": expected,
            "observed": observed,
        })


def hazards_of(r: dict[str, Any]) -> list[dict[str, Any]]:
    return [h for h in r.get("hazards") or [] if h.get("inherent")]


def tol_txt(t: dict[str, Any] | None) -> str:
    return f"{t['state']}/{t['route'] or '-'}" if t else "-"


def rating_txt(x: dict[str, Any] | None) -> str:
    return f"{x['level']} {x['score']} (L{x['likelihood']}xS{x['severity']})" if x else "-"


def check_I1(c: Check, act: dict[str, Any], res: dict[str, Any], variant: str) -> None:
    r1, shown = res["raw"]["pass1"], res["shown"]
    has_real = eng.has_real_controls(
        [eng.Control(None, (x.get("hierarchy") or "").upper(), x.get("description") or "") for x in act.get("existingControls") or [] if x.get("description")],
        DEFAULT_CONFIG,
    )
    with_ctl_basis = (act.get("ratingBasis") or "INHERENT") == "WITH_CONTROLS"
    if has_real or with_ctl_basis:
        c.add("I1", act, None, "controls recorded (or sheet rating includes controls): not applicable", variant=variant)
        return
    bad = []
    for name, r in (("engine", r1), ("shown", shown)):
        if r["entry"].get("residual") is not None:
            bad.append(f"{name}: entry residual {rating_txt(r['entry']['residual'])}")
        bad += [f"{name}: {h['name']} residual {rating_txt(h['residual'])}" for h in r["hazards"] if h.get("residual")]
    if not r1["entry"].get("inherentOnly"):
        bad.append("engine: inherentOnly is False with no controls")
    c.add("I1", act, not bad, "; ".join(bad) or "no residual band without controls", variant=variant,
          expected="no residual; 'Inherent only'", observed="; ".join(bad)[:300])


def _family_ev(r1: dict[str, Any], key: str) -> dict[str, Any]:
    return (r1.get("evidence") or {}).get("byHazard", {}).get(key) or {}


_DATED = re.compile(r"\b[A-Z]{2,4}-\d{4}-\d{3,4} \(\d{2} [A-Z][a-z]{2} \d{4}\)")


def check_I2(c: Check, act: dict[str, Any], res: dict[str, Any], variant: str) -> None:
    raw = res["raw"]
    r1 = raw["pass1"]
    rule = rule_for(CORPUS_DATA, act)
    draft = act["mode"] == "DRAFT"
    hz_src = (raw.get("calibrated") or {}).get("hazards") if draft else r1["hazards"]
    ev_all = evidence_for(act)
    bad, na = [], []
    for h in hz_src or []:
        lk = h.get("likelihood")
        if not lk:
            continue
        L = lk["value"]
        fam_ev = ev_all.for_family(h["family"])
        if L >= 4:
            if rule.likelihood_scale != "FREQUENCY" and not draft:
                na.append(f"{h['name']} L{L} (plant-index team rating passes through)")
                continue
            if not fam_ev.has_any:
                bad.append(f"{h['name']}: L{L} with no in-window same-family same-area evidence")
            if L == 5 and fam_ev.events_12m < DEFAULT_CONFIG["likelihood"]["l5EventsIn12Months"]:
                bad.append(f"{h['name']}: L5 with {fam_ev.events_12m} event(s) in 12 months")
            # ids/dates are only checkable for raw records (S1 fixtures carry hand-written lines)
            if act.get("events") and fam_ev.events_window and not _DATED.search(lk.get("reason") or ""):
                bad.append(f"{h['name']}: L{L} rationale does not list event ids and dates")
            if fam_ev.control_failures and not any(f in (lk.get("reason") or "") for f in fam_ev.control_failures):
                bad.append(f"{h['name']}: L{L} rationale does not name the control failure")
    tag = act["tags"]["evidence"]
    if tag in ("other_family", "other_area", "out_of_window"):
        hi = [h for h in hz_src or [] if h.get("likelihood") and h["likelihood"]["value"] >= 4
              and (rule.likelihood_scale == "FREQUENCY" or draft)]
        bad += [f"{h['name']}: L{h['likelihood']['value']} although the only evidence is {tag}" for h in hi]
    if na and not bad:
        c.add("I2", act, None, "; ".join(na), variant=variant)
    else:
        c.add("I2", act, not bad, "; ".join(bad) or "L4/L5 only with evidence", variant=variant,
              expected="L4 needs in-window same-family same-area evidence; L5 needs 2+ in 12 months; rationale lists ids/dates",
              observed="; ".join(bad)[:300])


def check_I3(c: Check, act: dict[str, Any], res: dict[str, Any], variant: str) -> None:
    raw = res["raw"]
    cfg, _ = pack_cfg(res["pack"])  # baseline keywords can differ per industry pack
    bad = []
    for pname in ("pass1", "pass2", "implemented", "implementedUnverified"):
        r = raw.get(pname)
        if not r:
            continue
        for h in r["hazards"]:
            sev_credit = [x for x in h.get("credits") or [] if x.get("axis") == "S" and x.get("steps")]
            for x in sev_credit:
                if (x.get("hierarchy") or "").upper() not in ("ELIMINATION", "SUBSTITUTION", "ENGINEERING"):
                    bad.append(f"{pname} {h['name']}: severity step from {x['hierarchy']} '{x['control'][:40]}'")
            if h.get("residual") and h.get("inherent") and h["residual"]["severity"] < h["inherent"]["severity"] and not sev_credit:
                bad.append(f"{pname} {h['name']}: residual S lower with no E/S/engineering credit")
            for x in h.get("credits") or []:
                if x.get("steps") and eng.is_baseline(x.get("hierarchy"), x.get("control"), cfg):
                    bad.append(f"{pname} {h['name']}: baseline control credited '{x['control'][:40]}'")
        for x in r.get("recommendations") or []:
            if eng.is_baseline(x["hierarchy"], x["description"], cfg):
                bad.append(f"{pname}: baseline PPE recommended '{x['description'][:50]}'")
    c.add("I3", act, not bad, "; ".join(sorted(set(bad))) or "admin/PPE never change severity; baseline no credit, never recommended",
          variant=variant, expected="no S credit from admin/PPE; baseline never credited or recommended", observed="; ".join(sorted(set(bad)))[:300])


def check_I4(c: Check, act: dict[str, Any], res: dict[str, Any], variant: str) -> None:
    s = res["summary"]
    gating = s["checks"]["inherentGatingViolations"]
    r1 = res["raw"]["pass1"]
    bad = list(gating)
    hz = {h["key"]: h for h in r1["hazards"]}
    for f in r1["findings"]:
        if f["class"] == rv.BLOCKER and f["code"] != "NO_CONTROLS" and f["hazardKeys"]:
            if all(hz.get(k, {}).get("residual") is None for k in f["hazardKeys"]):
                bad.append(f"blocker {f['code']} rests only on hazards with no residual")
    c.add("I4", act, not bad, "; ".join(bad) or "nothing triggered by the inherent band alone", variant=variant,
          expected="no blocker/recommendation/CAPA from inherent band alone", observed="; ".join(bad)[:300])


def check_I5(c: Check, act: dict[str, Any], res: dict[str, Any], variant: str) -> None:
    v = res["summary"]["checks"]["s3HazardsInAlarp"]
    c.add("I5", act, not v, ", ".join(v) or "no S<=3 hazard on the ALARP checklist or TOLERABLE_ALARP", variant=variant,
          expected="S<=3 never ALARP", observed=", ".join(v))


def check_I6(c: Check, act: dict[str, Any], res: dict[str, Any], variant: str) -> None:
    r1 = res["raw"]["pass1"]
    recs = r1["recommendations"]
    bad = []
    hz = {h["key"]: h for h in r1["hazards"]}
    per: dict[str, int] = {}
    for x in recs:
        for t in x["targetHazards"]:
            per[t["key"]] = per.get(t["key"], 0) + 1
    for k, n in per.items():
        h = hz.get(k) or {}
        sev = max((h.get("residual") or h.get("inherent") or {}).get("severity") or 0, (h.get("severity") or {}).get("credibleMin") or 0)
        if n > 2 or (n == 2 and sev < 4):
            bad.append(f"{h.get('name', k)}: {n} recommendations (S{sev})")
    rated = hazards_of(r1)
    max_s = max((h["inherent"]["severity"] for h in rated), default=0)
    level = (r1["entry"].get("residual") or r1["entry"].get("inherent") or {}).get("level")
    cap = 2 if max_s and max_s <= 3 else (5 if level in ("HIGH", "CRITICAL") else 3)
    if len(recs) > cap:
        bad.append(f"{len(recs)} recommendations > cap {cap} (entry {level}, max S{max_s})")
    if r1["entry"].get("state") == "ACCEPTABLE" and recs:
        bad.append(f"{len(recs)} recommendations on an ACCEPTABLE entry")
    c.add("I6", act, not bad, "; ".join(bad) or f"{len(recs)} recommendation(s), cap {cap}", variant=variant,
          expected=f"<=1/hazard (2 if severe), <= {cap}/entry, 0 if acceptable", observed="; ".join(bad)[:300])


_STOP = {"the", "a", "an", "of", "for", "and", "or", "to", "in", "on", "at", "with", "by", "from", "is", "be", "all", "every", "each"}


def words(t: str | None) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (t or "").lower()) if w not in _STOP and len(w) > 2}


def jacc(a: str | None, b: str | None) -> float:
    x, y = words(a), words(b)
    return len(x & y) / len(x | y) if x and y else 0.0


def check_I7(c: Check, act: dict[str, Any], res: dict[str, Any], variant: str) -> None:
    r1 = res["raw"]["pass1"]
    plant = act.get("plantData") or {}
    existing = [x.get("description") or "" for x in act.get("existingControls") or []]
    bad, minor = [], []
    for x in r1["recommendations"]:
        d = x["description"]
        for e in existing:
            j = jacc(d, e)
            if d.strip().lower() == e.strip().lower() or j >= 0.6:
                (bad if j >= 0.75 or d.strip().lower() == e.strip().lower() else minor).append(f"'{d[:50]}' ~ existing '{e[:50]}' (J={j:.2f})")
        for k in plant.get("openCapas") or []:
            j = jacc(d, k.get("text"))
            if j >= 0.5:
                bad.append(f"'{d[:50]}' duplicates open {k.get('number')} (J={j:.2f})")
        for s in plant.get("siblingRecommended") or []:
            if jacc(d, s.get("text")) >= 0.6 and not x.get("studyLevel"):
                bad.append(f"'{d[:50]}' matches sibling {s.get('label')} but is not marked study-level")
    if minor and not bad:
        c.add("I7", act, True, "possible near-duplicates (minor, different wording): " + "; ".join(minor), severity="minor", variant=variant)
    else:
        c.add("I7", act, not bad, "; ".join(bad) or "no duplicate of existing controls, open CAPAs or siblings", variant=variant,
              expected="no duplicates; shared controls study-level", observed="; ".join(bad)[:300])


def check_I9(c: Check, act: dict[str, Any], res: dict[str, Any], variant: str) -> None:
    s = res["summary"]
    bad = []
    if s["pass2"]["blockers"]:
        bad.append(f"pass 2 blockers {s['pass2']['blockers']}")
    if s["pass2"]["pass3NewBlockers"]:
        bad.append(f"pass 3 new blockers {s['pass2']['pass3NewBlockers']}")
    if s["unverified"]["newBlockers"]:
        bad.append(f"unverified scenario new blockers {s['unverified']['newBlockers']}")
    if not s["unverified"]["provisionalCreditNotAboveVerified"]:
        bad.append("unverified credit above verified")
    c.add("I9", act, not bad, "; ".join(bad) or "converges (pass 2: 0 blockers; pass 3: none new; unverified OK)", variant=variant,
          expected="pass2 0 blockers, pass3 0 new", observed="; ".join(bad)[:300])


def check_I10(c: Check, act: dict[str, Any], res: dict[str, Any], variant: str) -> None:
    p = res["summary"].get("parity")
    if p is None:
        c.add("I10", act, None, "stored entry: parity applies to drafts", variant=variant)
        return
    ok = bool(p["recommendations"]) and bool(p["finalState"])
    recon = res["summary"].get("reconstructionMatchesCalibrateDraft")
    det = f"recommendations equal: {p['recommendations']}; end state equal: {p['finalState']}; reconstruction: {recon}"
    c.add("I10", act, ok and recon is not False, det, variant=variant, expected="draft == stored", observed=det)


def check_I11(c: Check, act: dict[str, Any], res: dict[str, Any], variant: str) -> None:
    shown, scope = res["shown"], res["scope"]
    if scope["validated"]:
        c.add("I11", act, None, "validated pack, task-based: residual shown by design", variant=variant)
        return
    want = rp.NOT_CALIBRATED_BANNER if not scope["calibrated"] else rp.NOT_VALIDATED_BANNER
    bad = []
    banner = (shown.get("scope") or {}).get("banner")
    if banner != want:
        bad.append(f"banner {banner!r} (want {want!r})")
    if shown["entry"].get("residual") is not None:
        bad.append("entry residual shown")
    bad += [f"{h['name']}: residual/tolerability shown" for h in shown["hazards"] if h.get("residual") or h.get("tolerability")]
    if any(f["class"] == "BLOCKER" for f in shown["findings"]):
        bad.append("blocker shown")
    if not shown.get("canSubmit", True):
        bad.append("cannot submit")
    if not scope["calibrated"]:
        if shown["recommendations"]:
            bad.append(f"{len(shown['recommendations'])} recommendations on a non-task study")
        if shown.get("calibrated") is not False:
            bad.append("calibrated rules applied to a non-task study")
    else:
        bad += [f"recommendation kind {x['kind']}" for x in shown["recommendations"] if x["kind"] != "SUGGESTION" or x.get("residualAfter")]
    c.add("I11", act, not bad, "; ".join(bad) or f"banner '{want}', no residual claims", variant=variant,
          expected=f"banner '{want}', no residual band", observed="; ".join(bad)[:300])


def check_I14(c: Check, act: dict[str, Any], res: dict[str, Any], variant: str) -> None:
    r1 = res["raw"]["pass1"]
    with_ctl = (act.get("ratingBasis") or "INHERENT") == "WITH_CONTROLS"
    nc = [x for x in r1["recommendations"] if x["kind"] == "IMPROVEMENT_NO_RATING_CREDIT"]
    if not with_ctl and not nc:
        c.add("I14", act, None, "not a plant-index sheet rating", variant=variant)
        return
    bad = []
    if with_ctl:
        bad += [f"'{x['description'][:40]}' kind {x['kind']} on a sheet rating" for x in r1["recommendations"] if x["kind"] != "IMPROVEMENT_NO_RATING_CREDIT"]
    hz = {h["key"]: h for h in r1["hazards"]}
    only_nc = {t["key"] for x in nc for t in x["targetHazards"]} - {
        t["key"] for x in r1["recommendations"] if x["kind"] != "IMPROVEMENT_NO_RATING_CREDIT" for t in x["targetHazards"]}
    for x in nc:
        if x["raisesCapa"]:
            bad.append(f"'{x['description'][:40]}' raises a CAPA")
        for t in x["targetHazards"]:
            if x.get("residualAfter") != hz.get(t["key"], {}).get("residual"):
                bad.append(f"'{x['description'][:40]}' claims a residual change on {t['name']}")
        if re.search(r"\b[LS] -\d|reduc|lower", x.get("expectedEffect") or "", re.I):
            bad.append(f"'{x['description'][:40]}' text promises a reduction: {x['expectedEffect']}")
    for f in r1["findings"]:
        if f["class"] == rv.BLOCKER and set(f["hazardKeys"]) & only_nc:
            bad.append(f"blocker {f['code']} counts a no-credit improvement")
    c.add("I14", act, not bad, "; ".join(bad) or f"{len(nc)} no-credit improvement(s), correctly tagged", variant=variant,
          expected="IMPROVEMENT_NO_RATING_CREDIT, no CAPA, no reduction claimed, not a blocker", observed="; ".join(bad)[:300])


def check_structural(c: Check, act: dict[str, Any], res: dict[str, Any], variant: str) -> None:
    """S5 (non-task types) on the stored-review path: structural checks only."""
    r = res["raw"]["pass1"]
    bad = []
    if r.get("calibrated") is not False:
        bad.append("calibrated")
    if r["hazards"] or r["recommendations"] or r["entry"].get("inherent") or r["entry"].get("residual"):
        bad.append("ratings / recommendations produced")
    c.add("S5-structural", act, not bad, "; ".join(bad) or f"structural only: {[f['code'] for f in r['findings']]}", variant=variant,
          expected="structural checks only, no rating change")


# ─── whole-corpus tests ───────────────────────────────────────────────────


def run_corpus(items: list[dict[str, Any]], order: list[int] | None = None, workers: int = 1) -> dict[str, dict[str, Any]]:
    idx = order if order is not None else list(range(len(items)))
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            outs = list(ex.map(lambda i: (items[i]["id"], run_one(items[i])), idx))
    else:
        outs = [(items[i]["id"], run_one(items[i])) for i in idx]
    return dict(outs)


def digests_of(results: dict[str, dict[str, Any]]) -> dict[str, str]:
    return {k: item_digest(v) for k, v in sorted(results.items())}


def subprocess_digests(seed: str) -> dict[str, str] | None:
    env = {**os.environ, "PYTHONHASHSEED": seed}
    p = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--digest-only"], cwd=str(ROOT), env=env,
                       capture_output=True, text=True, timeout=900)
    if p.returncode != 0:
        print(p.stderr[-2000:], file=sys.stderr)
        return None
    return json.loads(p.stdout.strip().splitlines()[-1])


def entry_for_probe(act: dict[str, Any], res: dict[str, Any]) -> rv.EntryInput:
    if act["mode"] == "DRAFT":
        e, _ = E.entry_from_draft(E.draft_for(act), res["raw"]["calibrated"])
        return e
    return stored_input(act)


def monotonicity(items: list[dict[str, Any]], base: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    probe_ctl = "Fixed interlocked guard with trapped-key access (monotonicity probe)"
    for act in items:
        res = base[act["id"]]
        if res["kind"] != "CALIBRATED":
            continue
        cfg, _ = pack_cfg(None if act["tags"]["industryPack"] == "UNKNOWN" else act["tags"]["industryPack"])
        rule, ev, plant = rule_for(CORPUS_DATA, act), evidence_for(act), E.plant_for(act)
        try:
            entry = entry_for_probe(act, res)
            r0 = rv.evaluate(copy.deepcopy(entry), cfg=cfg, rule=rule, evidence=ev, plant=copy.deepcopy(plant))
            h0 = {h["key"]: h for h in r0["hazards"]}
            # a. add a verified engineering control on every hazard: residual never rises
            e1 = copy.deepcopy(entry)
            e1.controls.append(eng.Control("mono-eng", "ENGINEERING", probe_ctl, effectiveness="EFFECTIVE", evidence_attached=True,
                                           verification_method="probe", hazard_keys=tuple(h.key for h in entry.hazards)))
            r1 = rv.evaluate(e1, cfg=cfg, rule=rule, evidence=ev, plant=copy.deepcopy(plant))
            for h in r1["hazards"]:
                b = h0.get(h["key"]) or {}
                if b.get("residual") and h.get("residual") and (h["residual"]["score"], h["residual"]["severity"]) > (b["residual"]["score"], b["residual"]["severity"]):
                    out.append({"id": act["id"], "test": "add verified engineering control", "hazard": h["name"],
                                "before": rating_txt(b["residual"]), "after": rating_txt(h["residual"])})
            # b. remove all evidence: L never rises
            r2 = rv.evaluate(copy.deepcopy(entry), cfg=cfg, rule=rule, evidence=eng.Evidence(), plant=copy.deepcopy(plant))
            for h in r2["hazards"]:
                b = h0.get(h["key"]) or {}
                if b.get("inherent") and h.get("inherent") and h["inherent"]["likelihood"] > b["inherent"]["likelihood"]:
                    out.append({"id": act["id"], "test": "remove evidence", "hazard": h["name"],
                                "before": f"L{b['inherent']['likelihood']}", "after": f"L{h['inherent']['likelihood']}"})
            # c. add one in-window same-family event per hazard family: L never falls
            ev3 = copy.deepcopy(ev)
            fams = {h.family for h in entry.hazards}
            if not ev3.by_family:
                ev3.by_family = {}
            for f in sorted(fams):
                x = ev3.by_family.setdefault(f, {"window": 0, "12m": 0, "failures": [], "items": []})
                x["window"] += 1
                x["12m"] += 1
                x["items"].append("near miss NM-2026-9999 (01 Oct 2026)")
            ev3.events_window += 1
            ev3.events_12m += 1
            r3 = rv.evaluate(copy.deepcopy(entry), cfg=cfg, rule=rule, evidence=ev3, plant=copy.deepcopy(plant))
            for h in r3["hazards"]:
                b = h0.get(h["key"]) or {}
                if b.get("inherent") and h.get("inherent") and h["inherent"]["likelihood"] < b["inherent"]["likelihood"]:
                    out.append({"id": act["id"], "test": "add an event", "hazard": h["name"],
                                "before": f"L{b['inherent']['likelihood']}", "after": f"L{h['inherent']['likelihood']}"})
        except Exception as exc:  # noqa: BLE001
            out.append({"id": act["id"], "test": "probe error", "hazard": "-", "before": "-", "after": f"{type(exc).__name__}: {exc}"[:200]})
    return out


def hazard_records(items: list[dict[str, Any]], base: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    recs = []
    for act in items:
        res = base[act["id"]]
        if res["kind"] != "CALIBRATED" or act["tags"]["suite"] in ("ADV", "S4"):
            continue
        r1 = res["raw"]["pass1"]
        src = {h["key"]: h for h in act["hazards"]}
        for h in hazards_of(r1):
            s = src.get(h["key"]) or {}
            recs.append({
                "id": act["id"], "suite": act["tags"]["suite"], "key": h["key"], "family": h["family"],
                "text": f"{s.get('name', h['name'])}. {s.get('consequence') or ''}",
                "L": h["inherent"]["likelihood"], "S": h["inherent"]["severity"],
                "rule": (h.get("severity") or {}).get("rubricRule"),
                "evidence": bool(_family_ev(r1, h["key"]).get("window") or _family_ev(r1, h["key"]).get("failures")),
                "matrix": act["matrix"], "mode": act["mode"], "assessor": act.get("assessor"),
            })
    return recs


def consistency(recs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[list[dict[str, Any]]] = []
    for r in sorted(recs, key=lambda x: (x["family"], x["id"], x["key"])):
        for g in groups:
            if g[0]["family"] == r["family"] and jacc(g[0]["text"], r["text"]) >= 0.45:
                g.append(r)
                break
        else:
            groups.append([r])
    out = []
    for g in groups:
        if len(g) < 3:
            continue
        ls, ss = [x["L"] for x in g], [x["S"] for x in g]
        l_sp, s_sp = max(ls) - min(ls), max(ss) - min(ss)
        reasons = []
        if l_sp > 1 and len({x["evidence"] for x in g}) > 1:
            reasons.append("L: evidence differs")
        if l_sp > 1 and len({x["matrix"] for x in g}) > 1:
            reasons.append("L: plant-index vs frequency scale")
        if s_sp > 1 and len({x["rule"] for x in g}) > 1:
            reasons.append("S: different rubric scenario rule")
        unexplained = (l_sp > 1 and not any(r.startswith("L") for r in reasons)) or (s_sp > 1 and not any(r.startswith("S") for r in reasons))
        out.append({
            "group": f"{g[0]['family']}: {g[0]['text'][:60]}", "family": g[0]["family"], "size": len(g),
            "L_min": min(ls), "L_max": max(ls), "L_spread": l_sp, "S_min": min(ss), "S_max": max(ss), "S_spread": s_sp,
            "reasons": "; ".join(reasons), "flagged": unexplained,
            "members": " ".join(f"{x['id']}/{x['key']}(L{x['L']}S{x['S']}{'/' + x['assessor'] if x['assessor'] else ''})" for x in g),
        })
    return out


def study_rollup(items: list[dict[str, Any]], base: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    studies: dict[str, list[dict[str, Any]]] = {}
    for a in items:
        if a.get("study") and a["tags"]["suite"] == "S7":
            studies.setdefault(a["study"], []).append(a)
    for sid, acts in sorted(studies.items()):
        worst, per_sum, uniq, roles = None, 0, {}, {}
        for a in acts:
            r1 = base[a["id"]]["raw"].get("pass1")
            if not r1:
                continue
            e = r1["entry"].get("residual") or r1["entry"].get("inherent")
            if e and (worst is None or (e["score"], e["severity"]) > (worst["score"], worst["severity"])):
                worst = e
            per_sum += len(r1["recommendations"])
            for x in r1["recommendations"]:
                k = re.sub(r"\s+", " ", x["description"].strip().lower())
                if k not in uniq:
                    uniq[k] = x
                    roles[x["ownerRole"]] = roles.get(x["ownerRole"], 0) + 1
        out.append({
            "study": sid, "entries": len(acts), "worstEntryRating": rating_txt(worst),
            "sumRecommendations": per_sum, "uniqueRecommendations": len(uniq),
            "uniqueLowerThanSum": len(uniq) < per_sum, "byRole": roles,
            "rolesOver10": [r for r, n in roles.items() if n > 10],
        })
    return out


# ─── service layer (mocked DB): I16 flag modes, I11/I13 Assistant & Copilot paths ──


class _FakeResult:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def first(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class _FakeDB:
    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.rows = rows or []

    async def execute(self, *_a: Any, **_k: Any) -> _FakeResult:
        return _FakeResult(self.rows)


def service_checks(items: list[dict[str, Any]], c: Check) -> dict[str, Any]:
    from app.services.copilot import hira_review
    from app.services.hira_calibration import config as cal_config
    from app.services.hira_calibration import service as svc

    state: dict[str, Any] = {}
    logs: list[dict[str, Any]] = []
    orig = (cal_config.load, svc.plant_of_entry, svc.log_shadow, svc.loader.build, svc.loader.build_for_draft, hira_review.review_entry)

    async def load(db: Any, plant_id: Any) -> Any:
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg["rubricPack"] = state["pack"]
        return cfg, state["mode"], {"tenant": None, "plant": plant_id, "engine": ENGINE_VERSION}

    async def plant_of_entry(db: Any, entry_id: Any) -> str:
        return state.get("plant", "PLANT-A")

    async def log_shadow(**kw: Any) -> None:
        logs.append(kw)

    async def build(db: Any, entry_id: Any, cfg: Any) -> dict[str, Any]:
        a = state["act"]
        return {"input": state.get("input") or stored_input(a), "rule": rule_for(CORPUS_DATA, a), "evidence": evidence_for(a),
                "plant": E.plant_for(a), "entry": None, "study": None, "schemaReady": True}

    async def build_for_draft(db: Any, *, study: Any, area_id: Any, families: Any, cfg: Any) -> dict[str, Any]:
        a = state["act"]
        return {"rule": rule_for(CORPUS_DATA, a), "evidence": evidence_for(a), "plant": E.plant_for(a)}

    async def review_entry(db: Any, entry_id: Any) -> dict[str, Any]:
        return {"headline": f"v1 review of {entry_id}", "findings": [{"code": "V1_SENTINEL"}], "counts": {"total": 1},
                "residual": {"saved": None}, "controlIdeas": []}

    cal_config.load, svc.plant_of_entry, svc.log_shadow = load, plant_of_entry, log_shadow
    svc.loader.build, svc.loader.build_for_draft, hira_review.review_entry = build, build_for_draft, review_entry
    summary = {"reviewRuns": 0, "assistantRuns": 0, "copilotDraftRuns": 0}
    try:
        db = _FakeDB()
        # I16: OFF == v1 exactly; SHADOW shows v1 and logs v1+v2; ON / preview show v2
        for a in items:
            if a["mode"] != "STORED":
                continue
            pack = None if a["tags"]["industryPack"] == "UNKNOWN" else a["tags"]["industryPack"]
            state.update(act=a, pack=pack, input=None)
            v1 = asyncio.run(review_entry(db, a["id"]))
            bad = []
            for mode in ("OFF", "SHADOW", "ON"):
                state["mode"] = mode
                logs.clear()
                out = asyncio.run(svc.review(db, a["id"], user_id="campaign"))
                cfg_now = asyncio.run(load(db, "PLANT-A"))[0]
                v2 = asyncio.run(svc.evaluate_entry(db, a["id"], cfg_now))
                if mode == "OFF":
                    if out != {"version": "v1", "mode": "OFF", **v1}:
                        bad.append("OFF output differs from v1")
                    if logs:
                        bad.append("OFF wrote a shadow row")
                elif mode == "SHADOW":
                    if out != {"version": "v1", "mode": "SHADOW", **v1}:
                        bad.append("SHADOW shows something other than v1")
                    if len(logs) != 1 or logs[0]["v2"] != v2 or logs[0]["v1"] != v1:
                        bad.append(f"SHADOW logged {len(logs)} row(s) / v2 mismatch")
                else:
                    if out != {"version": "v2", "mode": "ON", **v2}:
                        bad.append("ON output is not the v2 review")
            state["mode"] = "OFF"
            prev = asyncio.run(svc.review(db, a["id"], user_id="campaign", preview=True))
            if prev.get("version") != "v2":
                bad.append("preview=v2 while OFF did not return v2")
            summary["reviewRuns"] += 4
            c.add("I16", a, not bad, "; ".join(bad) or "OFF == v1 exactly; SHADOW shows v1 and logs both; ON/preview show v2",
                  expected="OFF == v1; SHADOW v1 only", observed="; ".join(bad))

        # Assistant path: OFF/SHADOW leave the answer unchanged (I16); ON for a non-task study (S5/I11);
        # injected residual / band fields never reach the ratings (I13).
        for a in [x for x in items if x["tags"]["suite"] in ("S5", "ADV") or x["id"] in ("S2-01", "S2-03")]:
            if not a["hazards"]:
                continue
            sug, ctx = assistant_suggestion(a)
            pack = "STEEL_HEAVY"
            hira_type = a["tags"]["hiraType"]
            inp = rv.EntryInput(
                a["id"], a["id"], a["activity"], a.get("routine"), a.get("frequency"), [], None, None, None,
                controls=[eng.Control(f"c{i}", (x.get("hierarchy") or "").upper(), x["description"], effectiveness=x.get("effectiveness"),
                                      verification_method=x.get("verificationMethod"))
                          for i, x in enumerate(a.get("existingControls") or []) if x.get("description")],
                hira_type=hira_type,
            )
            state.update(act=a, pack=pack, input=inp)
            for mode in ("OFF", "SHADOW"):
                state["mode"] = mode
                logs.clear()
                got = asyncio.run(svc.calibrate_assistant_output(db, entry_id=a["id"], suggestion=copy.deepcopy(sug), context=ctx, user_id="campaign"))
                c.add("I16", a, got == sug, f"Assistant {mode}: answer unchanged" if got == sug else f"Assistant {mode}: answer changed",
                      variant=f"assistant-{mode}")
            state["mode"] = "ON"
            got = asyncio.run(svc.calibrate_assistant_output(db, entry_id=a["id"], suggestion=copy.deepcopy(sug), context=ctx, user_id="campaign"))
            summary["assistantRuns"] += 3
            if hira_type not in CALIBRATED_TYPES:
                changed = []
                if len(got.get("suggested_hazards") or []) != len(sug["suggested_hazards"]):
                    changed.append(f"hazards {len(sug['suggested_hazards'])} -> {len(got.get('suggested_hazards') or [])}")
                for k in ("initial_likelihood_suggestion", "initial_severity_suggestion"):
                    cs = ((got.get("risk_calibration") or {}).get(k) or {}).get("calibrated_score")
                    if cs is not None:
                        changed.append(f"{k} calibrated {sug['risk_calibration'][k]['score']} -> {cs}")
                if got.get("control_suggestions") != sug["control_suggestions"]:
                    n0 = sum(len(b.get(k) or []) for b in sug["control_suggestions"] for k in b if k.endswith("_controls"))
                    n1 = sum(len(b.get(k) or []) for b in got.get("control_suggestions") or [] for k in b if k.endswith("_controls"))
                    changed.append(f"control suggestions replaced ({n0} -> {n1})")
                cal = got.get("calibration") or {}
                res_claim = bool((cal.get("entry") or {}).get("residual")) or any(h.get("residual") for h in cal.get("hazards") or [])
                if res_claim:
                    changed.append("residual band claimed")
                c.add("I11", a, not changed, "Assistant ON, " + hira_type + ": " + ("; ".join(changed) or "untouched"),
                      variant="assistant-ON", expected="non-task type: structural only, no rating change",
                      observed="; ".join(changed))
            if a.get("injection"):
                clean = assistant_suggestion(a, clean=True)
                state.update(act=a)
                g0 = asyncio.run(svc.calibrate_assistant_output(db, entry_id=a["id"], suggestion=copy.deepcopy(clean[0]), context=clean[1], user_id="campaign"))
                diff = ratings_of(g0.get("calibration")) != ratings_of(got.get("calibration"))
                c.add("I13", a, not diff, "Assistant ON: injected residual/band fields and instructions " +
                      ("changed the ratings" if diff else "had no effect on any rating"), variant="assistant-ON")

        # Copilot draft into a non-task study (S5/I11): the draft path hard-codes TASK_BASED
        for a in [x for x in items if x["tags"]["suite"] == "S5" and x["hazards"]]:
            masters = [(h["key"], h["name"], h.get("code"), h.get("category")) for h in a["hazards"]]
            parsed = {
                "hazards": [{"hazardId": h["key"], "likelihood": h.get("likelihood"), "severity": h.get("severity"),
                             "consequence": h.get("consequence")} for h in a["hazards"]],
                "initialLikelihood": 4, "initialSeverity": 5,
                "initialLikelihoodRationale": "team", "initialSeverityRationale": "team",
                "activityDescription": a["activity"], "routine": "ROUTINE", "frequency": "DAILY",
                "existingControls": a["existingControls"],
                "recommendedControls": [{"hierarchy": x["hierarchy"], "description": x["description"]} for x in a["recommendedControls"]],
            }
            study = SimpleNamespace(id=f"study-{a['id']}", plantId="PLANT-A", areaId=None, hiraType=a["tags"]["hiraType"])
            state.update(act=a, pack="STEEL_HEAVY", input=None, mode="ON")
            got = asyncio.run(svc.calibrate_copilot_draft(_FakeDB(masters), study=study, area_id=None, parsed=copy.deepcopy(parsed), expand=False, user_id="campaign"))
            summary["copilotDraftRuns"] += 1
            sm = (got or {}).get("summary") or {}
            changed = []
            if got is None:
                # copilot/tools.py replaces the draft only for {"mode": "ON"}; None = saved unchanged
                po_none = True
            else:
                po_none = False
            if sm.get("entryResidual") or any(h.get("residual") for h in sm.get("hazards") or []):
                changed.append("residual band claimed")
            po = parsed if po_none else ((got or {}).get("parsed") or {})
            if (po.get("initialLikelihood"), po.get("initialSeverity")) != (parsed["initialLikelihood"], parsed["initialSeverity"]):
                changed.append(f"initial L/S {parsed['initialLikelihood']}x{parsed['initialSeverity']} -> {po.get('initialLikelihood')}x{po.get('initialSeverity')}")
            if len(po.get("recommendedControls") or []) != len(parsed["recommendedControls"]):
                changed.append(f"recommended controls {len(parsed['recommendedControls'])} -> {len(po.get('recommendedControls') or [])}")
            c.add("I11", a, not changed, f"Copilot draft ON into a {a['tags']['hiraType']} study: " + ("; ".join(changed) or "untouched"),
                  variant="copilot-draft-ON", expected="non-task study: no calibration, no residual claim", observed="; ".join(changed))
    finally:
        cal_config.load, svc.plant_of_entry, svc.log_shadow, svc.loader.build, svc.loader.build_for_draft, hira_review.review_entry = orig
    return summary


def assistant_suggestion(a: dict[str, Any], clean: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    """A HIRA Assistant answer (prompt v2 JSON) for a corpus item, with the
    injection placed in the LLM's own fields too (unless clean)."""
    inj = (a.get("injection") or {}).get("text") if not clean else None
    hz = a["hazards"]
    lib = [{"hazard_master_id": h["key"], "code": h.get("code"), "category": h.get("category"), "name": h["name"]} for h in hz]
    extra = [("lib-x1", "Noise", "NOISE_HIGH_EXPOSURE", "noise"), ("lib-x2", "Slip on floor", "PHYS_SLIP_TRIP", "physical"),
             ("lib-x3", "Hot surface", "THERMAL_HOT_SURFACE", "thermal"), ("lib-x4", "Dust", "ENV_DUST_AIRBORNE", "environmental"),
             ("lib-x5", "Sharp edges", "MECH_SHARP_EDGE", "mechanical"), ("lib-x6", "Manual handling", "MECH_HEAVY_LIFTING", "ergonomic")]
    lib += [{"hazard_master_id": k, "name": n, "code": cd, "category": ct} for k, n, cd, ct in extra]
    sug = {
        "suggested_hazards": [
            {"hazard_master_id": x["hazard_master_id"], "relevance_confidence": 0.9 - 0.05 * i,
             "reasoning": (next((h.get("consequence") for h in hz if h["key"] == x["hazard_master_id"]), None) or f"{x['name']} in this task")
             + (f" {inj}" if inj and i == 0 else ""), "suggested_for_addition": True}
            for i, x in enumerate(lib)
        ],
        "control_suggestions": [
            {"hazard_id": hz[0]["key"],
             "engineering_controls": [{"description": "Interlocked barrier on the load path", "estimated_cost_band": "medium"}],
             "administrative_controls": [{"description": "Lift plan with banksman", "estimated_cost_band": "low"},
                                         *([{"description": inj, "estimated_cost_band": "low"}] if inj else [])],
             "ppe_controls": [{"description": "Helmet and safety shoes", "estimated_cost_band": "low"}]},
            {"hazard_id": "lib-x1", "engineering_controls": [{"description": "Acoustic enclosure", "estimated_cost_band": "high"}],
             "ppe_controls": [{"description": "Ear muffs", "estimated_cost_band": "low"}]},
        ],
        "risk_calibration": {
            "initial_likelihood_suggestion": {"score": 4, "reasoning": "daily exposure" + (f" {inj}" if inj else ""), "confidence": "medium"},
            "initial_severity_suggestion": {"score": 5, "reasoning": "fatal", "confidence": "high"},
            "residual_likelihood_guidance": "1" if inj else None,
            "residual_severity_guidance": "1" if inj else None,
            "calibration_notes": "",
        },
        "pattern_observations": [], "reviewer_attention_items": [], "overall_confidence": "medium", "reasoning_summary": "x",
    }
    if inj:
        sug["residual_level"] = "LOW"
        sug["risk_calibration"]["residual_risk_level"] = "LOW"
        sug["risk_calibration"]["tolerability"] = "TOLERABLE_ALARP"
    ctx = {"activity": {"description": a["activity"], "routine": a.get("routine"), "frequency": a.get("frequency")},
           "team_already_added": {"existingEntryHazards": [], "existingControls": []},
           "context": {"applicable_hazard_library": lib}}
    return sug, ctx


def ratings_of(cal: dict[str, Any] | None) -> Any:
    if not cal:
        return None
    return {
        "entry": {k: cal["entry"].get(k) for k in ("inherent", "residual", "state", "status")},
        "hazards": sorted((h["key"], json.dumps(h["inherent"], sort_keys=True), json.dumps(h["residual"], sort_keys=True),
                           json.dumps(h.get("tolerability"), sort_keys=True)) for h in cal["hazards"]),
        "targets": sorted(sorted(t["key"] for t in r["targetHazards"]) for r in cal["recommendations"]),
    }


# ─── I13 on the engine path: injected free text vs its clean twin ───────


def clean_twin(a: dict[str, Any]) -> dict[str, Any]:
    """The injected item with the injected words removed (or, for a control slot, replaced by neutral wording)."""
    twin = copy.deepcopy(a)
    f = a["injection"]["field"]
    t = a["injection"]["text"]
    if f == "activity":
        twin["activity"] = twin["activity"].replace(f". {t}", "")
    elif f == "consequence":
        twin["hazards"][0]["consequence"] = twin["hazards"][0]["consequence"].replace(f". {t}", "")
    elif f == "control":
        # same slot, same hierarchy, neutral wording: isolates the effect of the injected words
        last = twin["existingControls"][-1]
        last["description"] = "Helmet and safety shoes" if last["hierarchy"] == "PPE" else "Supervisor briefing before the task"
    elif f == "likelihoodRationale":
        twin["hazards"][0].pop("likelihoodRationale", None)
    elif f == "hazardName":
        twin["hazards"][0]["name"] = "Dropped pump from the crane"
    elif f == "recommended":
        twin["recommendedControls"] = twin["recommendedControls"][:-1]
    twin.pop("injection", None)
    return twin


def adversarial_checks(items: list[dict[str, Any]], base: dict[str, dict[str, Any]], c: Check) -> None:
    by = {a["id"]: a for a in items}
    for a in items:
        if not a.get("injection"):
            continue
        f = a["injection"]["field"]
        t = a["injection"]["text"]
        twin = clean_twin(a)
        clean = run_one(twin)
        got = base[a["id"]]

        def rat(r: dict[str, Any]) -> Any:
            p = r["raw"]["pass1"]
            return {
                "hazards": [(h["key"], (h.get("inherent") or {}).get("level"), (h.get("inherent") or {}).get("score"),
                             (h.get("residual") or {}).get("level"), (h.get("residual") or {}).get("score"), tol_txt(h.get("tolerability")))
                            for h in p["hazards"]],
                "entry": (rating_txt(p["entry"].get("inherent")), rating_txt(p["entry"].get("residual")), p["entry"].get("state")),
                "blockers": sorted(rv.blocker_codes(p)),
                "targets": sorted(sorted(t["key"] for t in x["targetHazards"]) for x in p["recommendations"]),
            }

        ra, rc = rat(got), rat(clean)
        diff = [k for k in ra if ra[k] != rc[k]]
        echoed = [x["description"] for x in got["raw"]["pass1"]["recommendations"] if t[:25].lower() in x["description"].lower()]
        bad = [f"{k}: {rc[k]} -> {ra[k]}" for k in diff]
        if echoed:
            bad.append(f"injected text became a recommendation: {echoed[0][:60]}")
        c.add("I13", a, not bad, f"{f}: " + ("; ".join(bad) if bad else "injection had no effect on ratings, bands or recommendation targets"),
              variant="engine", expected="identical to the clean twin", observed="; ".join(bad)[:300])
    _ = by


# ─── I3 / I1 probe: baseline PPE written the way site sheets write it ──

BASELINE_PHRASINGS = [
    "Helmet and safety shoes",
    "Helmet, safety shoes, hi-vis jacket",
    "Safety shoes, helmet, as per site rule",
    "Helmet; to be worn at all times",
    "Helmet, safety shoes (mandatory in plant)",
    "Cotton hand gloves, helmet - always worn",
    "Use of PPE as per norms at all times",
    "Basic PPE (helmet, shoes) as per plant standard",
]


def baseline_probe(c: Check) -> list[dict[str, Any]]:
    cfg, _ = pack_cfg("STEEL_HEAVY")
    rule = MatrixRule("STD_5X5", "FREQUENCY", False)
    rows = []
    for i, text in enumerate(BASELINE_PHRASINGS, 1):
        fake = {"id": f"PROBE-PPE-{i:02d}", "tags": {"suite": "S6"}}
        base = eng.is_baseline("PPE", text, cfg)
        e = rv.EntryInput(fake["id"], fake["id"], "Deburring cast parts at the fettling bench", "ROUTINE", "DAILY",
                          [eng.Hazard("h1", "Cut from burrs", "MECH_SHARP_EDGE", "mechanical", "Cut to the fingers from casting burrs", 3, 3)],
                          None, None, None, controls=[eng.Control("c1", "PPE", text)], hazard_ratings_stored=True)
        r = rv.evaluate(e, cfg=cfg, rule=rule, evidence=eng.Evidence())
        h = r["hazards"][0]
        credited = [x for x in h.get("credits") or [] if x.get("steps")]
        rows.append({"text": text, "baseline": base, "residual": rating_txt(h.get("residual")), "inherentOnly": r["entry"]["inherentOnly"],
                     "credit": "; ".join(f"{x['hierarchy']} {x['axis']}-{x['steps']}" for x in credited) or "none"})
        c.add("I3", fake, base and not credited,
              f"PPE '{text}': " + ("baseline, no credit" if base and not credited else
                                   f"not recognised as baseline; credited {rows[-1]['credit']}"),
              expected="baseline PPE: no credit", observed=f"baseline={base}; credit {rows[-1]['credit']}", variant="baseline phrasing probe")
        c.add("I1", fake, r["entry"]["inherentOnly"] and r["entry"].get("residual") is None,
              f"only '{text}' recorded: " + ("Inherent only" if r["entry"]["inherentOnly"] else f"residual shown {rows[-1]['residual']}"),
              expected="only baseline PPE recorded -> Inherent only, no residual", observed=rows[-1]["residual"], variant="baseline phrasing probe")
    return rows


# ─── I12 / UI (static, frontend source) and I15 (static, loader queries) ──


def ui_static(c: Check) -> list[dict[str, Any]]:
    f = FRONTEND / "src" / "components" / "copilot" / "hira-calibration-review.tsx"
    if not f.exists():
        c.add("I12", None, None, f"frontend source not found at {f}")
        return []
    src = f.read_text(encoding="utf-8")
    rows = []
    for m in re.finditer(r"<RatingChip\s+r=\{([^}]*)\}([^/>]*)/>", src):
        expr, rest = m.group(1), m.group(2)
        line = src[: m.start()].count("\n") + 1
        neutral = "neutral" in rest
        inherent = "inherent" in expr.lower()
        ok = neutral if inherent else not neutral
        rows.append({"line": line, "expr": expr.strip(), "neutral": neutral, "ok": ok})
    neutral_cls = re.search(r"const NEUTRAL_CHIP\s*=\s*\"([^\"]+)\"", src)
    nc = neutral_cls.group(1) if neutral_cls else ""
    red = bool(re.search(r"rose|red|destructive", nc))
    bad = [f"line {r['line']}: RatingChip r={{{r['expr']}}} neutral={r['neutral']}" for r in rows if not r["ok"]]
    if red:
        bad.append(f"NEUTRAL_CHIP uses a status colour: {nc}")
    c.add("I12", None, not bad and bool(rows), "; ".join(bad) or f"{sum(1 for r in rows if 'inherent' in r['expr'].lower())} inherent chip(s), all neutral ({nc}); residual chips use status colours",
          expected="inherent chip neutral", observed="; ".join(bad))
    shown3 = ".slice(0, 3)" in src
    more = bool(re.search(r"Show more|show more|Show \$\{[^}]+\} more", src))
    c.add("UI-3-findings", None, shown3 and more, f"default findings slice(0, 3): {shown3}; 'Show more' control: {more}")
    return rows


def isolation_static(c: Check) -> list[dict[str, str]]:
    src = (ROOT / "app" / "services" / "hira_calibration" / "loader.py").read_text(encoding="utf-8")
    checks = [
        ("Incidents (evidence)", r"Incident\.plantId == plant_id"),
        ("Near misses (evidence)", r"NearMiss\.plantId == plant_id"),
        ("Equipment / statutory register", r"Equipment\.plantId == plant_id"),
        ("Open CAPAs (entry review)", r"Capa\.plantId == plant_id"),
        ("Open CAPAs (Copilot draft)", r"Capa\.plantId == study\.plantId"),
        ("Regulation map", r"HiraRegulationMap\.plantId == plant_id"),
        ("Sibling entries (same study or same area)", r"HiraEntry\.studyId == e\.studyId, HiraEntry\.areaId == e\.areaId"),
        ("Draft siblings (same study)", r"HiraEntry\.studyId == study\.id"),
    ]
    rows = [{"query": n, "scoped": "yes" if re.search(p, src) else "NOT FOUND", "pattern": p} for n, p in checks]
    bad = [r["query"] for r in rows if r["scoped"] != "yes"]
    c.add("I15", None, not bad, ("unscoped: " + ", ".join(bad)) if bad else
          "static review: every loader query is scoped to the entry's plant, study or area (siblings by study/area id, which are plant-scoped)")
    return rows


# ─── outputs ─────────────────────────────────────────────────────────────


def lvl(x: dict[str, Any] | None) -> str:
    return x["level"] if x else ""


def deltas_rows(items: list[dict[str, Any]], base: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for a in items:
        r = base[a["id"]]
        if r["kind"] != "CALIBRATED":
            continue
        s = r["summary"]
        v1 = s["v1"]
        shown = r["shown"]
        rows.append({
            "id": a["id"], "suite": a["tags"]["suite"], "pack": r["pack"] or "none", "matrix": a["matrix"], "mode": a["mode"],
            "controlsState": a["tags"]["controlsState"], "evidence": a["tags"]["evidence"],
            "v1_inherent": (v1.get("inherent") or {}).get("level", v1.get("error", "")),
            "v1_residual": (v1.get("residual") or {}).get("level", ""),
            "v1_recommendations": v1.get("recommendations"),
            "v2_inherent": lvl(s["v2"]["inherent"]), "v2_inherent_score": (s["v2"]["inherent"] or {}).get("score", ""),
            "v2_residual": lvl(s["v2"]["residual"]) or ("INHERENT_ONLY" if s["v2"]["inherentOnly"] else "NOT_ASSESSED"),
            "v2_residual_score": (s["v2"]["residual"] or {}).get("score", ""),
            "v2_shown_residual": lvl(shown["entry"].get("residual")) or (shown.get("scope") or {}).get("banner") or "INHERENT_ONLY",
            "v2_tolerability": r["raw"]["pass1"]["entry"].get("state") or "",
            "v2_one_pass_end_state": s["final"]["label"],
            "v2_recommendations": len(s["v2"]["recommendations"]),
            "v2_blockers": " ".join(s["v2"]["blockers"]),
            "residual_band_change": band_change(v1, s),
            "worsen_reason": worsen_reason(v1, s, r) if band_change(v1, s).startswith("worse") else "",
        })
    return rows


def worsen_reason(v1: dict[str, Any], s: dict[str, Any], r: dict[str, Any]) -> str:
    """Why the v2 residual band is above the v1 model's (rules of rounds 1-3)."""
    why = []
    vi, vr = v1.get("inherent") or {}, v1.get("residual") or {}
    res = s["v2"]["residual"] or {}
    if vr.get("severity") and vi.get("severity") and vr["severity"] < vi["severity"]:
        why.append(f"v1 cut severity S{vi['severity']}->S{vr['severity']} (admin/PPE/any control); v2 lowers S only for E/S/justified mitigating engineering")
    drivers = [h for h in r["raw"]["pass1"]["hazards"] if h.get("residual") and res and h["residual"]["score"] == res["score"]]
    for h in drivers[:1]:
        nr = [c for c in h.get("credits") or [] if c["status"] == "NOT_RELEVANT"]
        if nr:
            why.append(f"driving hazard '{h['name']}': {len(nr)} control(s) judged not relevant to it (v1 applied every control to one entry rating)")
        if any(c["status"] == "NO_CREDIT" and "PPE" in (c["hierarchy"] or "") for c in h.get("credits") or []):
            why.append("PPE earns no credit for an S4+ hazard")
    if not why:
        why.append("v1 credited all controls against one entry rating; v2 rates each hazard with its own controls (max 3 steps)")
    return "; ".join(why)


def relevance_audit(items: list[dict[str, Any]], base: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Every (hazard, existing control) pair where an engineering-type control is
    ruled not relevant, or an engineering control with no family keyword is
    credited to every hazard. Candidates for the HSE lead, not verdicts."""
    out = []
    for a in items:
        r = base[a["id"]]
        if r["kind"] != "CALIBRATED" or not r["scope"]["validated"] or a["tags"]["suite"] == "ADV":
            continue
        cfg, _ = pack_cfg(r["pack"])
        src = {h["key"]: h for h in a["hazards"]}
        for h in hazards_of(r["raw"]["pass1"]):
            s = src.get(h["key"]) or {}
            hz = eng.Hazard(h["key"], s.get("name") or h["name"], s.get("code"), s.get("category"), s.get("consequence"))
            for x in a.get("existingControls") or []:
                hier = (x.get("hierarchy") or "").upper()
                if hier not in ("ELIMINATION", "SUBSTITUTION", "ENGINEERING") or x.get("hazardKeys"):
                    continue
                ok, why = eng.applies_to(eng.Control(None, hier, x["description"]), hz)
                t = eng._norm(x["description"])
                kw = sorted({f"{f}:{k}" for f, ks in eng.CONTROL_KEYWORDS.items() for k in ks if eng._has(t, k)})
                kind = None
                if not ok:
                    kind = "engineering control ruled NOT relevant"
                elif why.startswith("not linked"):
                    kind = "engineering control with no keyword credited to this hazard"
                if kind:
                    out.append({"id": a["id"], "hazard": h["name"], "hazardFamily": h["family"], "control": x["description"][:120],
                                "hierarchy": hier, "controlKeywords": " ".join(kw) or "(none)", "finding": kind, "engineReason": why,
                                "residual": rating_txt(h.get("residual"))})
    return out


def band_change(v1: dict[str, Any], s: dict[str, Any]) -> str:
    a = (v1.get("residual") or {}).get("level")
    b = (s["v2"]["residual"] or {}).get("level")
    if not a or not b:
        return "n/a"
    d = BANDS.index(b) - BANDS.index(a)
    return "same" if d == 0 else (f"worse +{d}" if d > 0 else f"better {d}")


def write_csv(path: Path, rows: list[dict[str, Any]], cols: list[str] | None = None) -> None:
    cols = cols or (list(rows[0].keys()) if rows else ["empty"])
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v) for k, v in r.items()})


def blind_sheets(items: list[dict[str, Any]], base: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Stratified 15 from the corpus (S2 new heavy industry, S3 other industries,
    S6 edge cases, S7 study entries), never S1 (its expected values are known)
    or adversarial items. Strata by the activity's worst proposed severity:
    low <= 3, medium 4, high 5. Engine output only in the key."""
    pool = [a for a in items if a["tags"]["suite"] in ("S2", "S3", "S6", "S7") and base[a["id"]]["kind"] == "CALIBRATED" and a["hazards"]]

    def cls(a: dict[str, Any]) -> str:
        s = max((h.get("severity") or 0) for h in a["hazards"])
        return "low" if s <= 3 else ("medium" if s == 4 else "high")

    pick: list[dict[str, Any]] = []
    for c_ in ("low", "medium", "high"):
        grp = sorted([a for a in pool if cls(a) == c_], key=lambda a: a["id"])
        rnd = random.Random(f"blind-{c_}")
        rnd.shuffle(grp)
        pick += [{**a, "_class": c_} for a in grp[:5]]
    sheet, key = [], []
    for n, a in enumerate(pick, 1):
        evs = "; ".join(f"{e['id']} {e['date']}: {e['text']}" for e in a.get("events") or [] if e.get("area") == a.get("entryArea")) or "No incidents recorded in this area in the last 24 months"
        sheet.append({
            "blind_id": f"B{n:02d}", "source": "campaign corpus (synthetic, not live data)",
            "activity": a["activity"], "routine": a.get("routine") or "", "frequency": a.get("frequency") or "",
            "hazards": " | ".join(f"{h['name']}: {h.get('consequence') or ''}" for h in a["hazards"]),
            "existing_controls": " | ".join(f"{(c.get('hierarchy') or '?')}: {c['description'][:200]}" for c in a.get("existingControls") or []) or "(none recorded)",
            "area_incident_summary": evs,
            "hse_inherent_band": "", "hse_residual_band": "", "hse_acceptable_yes_no": "", "hse_note": "",
        })
        s = base[a["id"]]["summary"]
        key.append({
            "blind_id": f"B{n:02d}", "corpus_id": a["id"], "stratum": a["_class"],
            "engine_scope": (base[a["id"]]["scope"].get("banner") or "validated (STEEL_HEAVY)"),
            "engine_inherent": rating_txt(s["v2"]["inherent"]), "engine_residual": rating_txt(s["v2"]["residual"]) if s["v2"]["residual"] else "Inherent only",
            "engine_state": base[a["id"]]["raw"]["pass1"]["entry"].get("state") or "", "engine_one_pass_end_state": s["final"]["label"],
            "engine_recommendations": len(s["v2"]["recommendations"]),
            "v1_inherent": (s["v1"].get("inherent") or {}).get("level", ""), "v1_residual": (s["v1"].get("residual") or {}).get("level", ""),
        })
    return sheet, key


def s1_regression(base: dict[str, dict[str, Any]], items: list[dict[str, Any]]) -> dict[str, Any]:
    prev = json.loads((E.RESULTS).read_text(encoding="utf-8"))
    rows = {r["id"]: r for r in prev["activities"]}
    diffs = []
    for a in items:
        if a["tags"]["suite"] != "S1":
            continue
        mine = json.loads(json.dumps(E._strip_dates(base[a["id"]]["summary"]), default=str))
        if mine != rows.get(a["id"]):
            keys = [k for k in mine if mine.get(k) != (rows.get(a["id"]) or {}).get(k)]
            diffs.append({"id": a["id"], "fields": keys})
    s1 = [base[a["id"]]["summary"] for a in items if a["tags"]["suite"] == "S1"]
    tg = E.targets(s1, True, "campaign run")
    prev_t = {t["target"]: t for t in prev["targets"]}
    tdiff = [t["target"] for t in tg if t["target"] in prev_t and (t["value"], t["pass"]) != (prev_t[t["target"]]["value"], prev_t[t["target"]]["pass"])
             and "deterministic" not in t["target"]]
    return {"activityDiffs": diffs, "targetDiffs": tdiff, "targets": tg}


def pct(n: int, d: int) -> str:
    return f"{n}/{d} ({100 * n / d:.0f}%)" if d else "0/0"


def percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1)))))
    return xs[k]


# ─── main ────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--digest-only", action="store_true")
    ap.add_argument("--base-url", default=None, help="staging URL (refused unless in staging_allowlist.json); unused offline")
    ap.add_argument("--no-subprocess", action="store_true")
    args = ap.parse_args(argv)

    if args.base_url:
        allow = json.loads(ALLOWLIST.read_text(encoding="utf-8")).get("stagingBaseUrls", []) if ALLOWLIST.exists() else []
        if args.base_url.rstrip("/") not in [u.rstrip("/") for u in allow]:
            print(f"REFUSED: {args.base_url} is not in {ALLOWLIST.name} (staging allowlist: {allow or 'empty'})", file=sys.stderr)
            return 2

    CORPUS_DATA.update(json.loads(CORPUS.read_text(encoding="utf-8")))
    items = CORPUS_DATA["items"]
    if args.digest_only:
        print(json.dumps(digests_of(run_corpus(items)), sort_keys=True))
        return 0

    OUT.mkdir(exist_ok=True)
    t_start = time.perf_counter()
    c = Check()

    # ── 1. main run + determinism / order independence / concurrency (I8) ──
    base = run_corpus(items)
    d0 = digests_of(base)
    runs = {"in-process #2": digests_of(run_corpus(items)), "reverse order": digests_of(run_corpus(items, list(reversed(range(len(items))))))}
    for seed in (1, 2, 3):
        order = list(range(len(items)))
        random.Random(seed).shuffle(order)
        runs[f"random order seed {seed}"] = digests_of(run_corpus(items, order))
    runs["parallel, 10 workers"] = digests_of(run_corpus(items, workers=10))
    if not args.no_subprocess:
        for seed in ("0", "4242"):
            runs[f"new process (restart), PYTHONHASHSEED={seed}"] = subprocess_digests(seed) or {}
    det_bad: dict[str, list[str]] = {}
    for name, d in runs.items():
        diff = [k for k in d0 if d.get(k) != d0[k]]
        if diff:
            det_bad[name] = diff
    for a in items:
        bad_runs = [n for n, ids in det_bad.items() if a["id"] in ids]
        c.add("I8", a, not bad_runs, ("differs in: " + ", ".join(bad_runs)) if bad_runs else f"byte-identical across {len(runs) + 1} runs",
              expected="byte-identical engine output", observed=", ".join(bad_runs))

    # ── 2. per-item invariants ──
    errors = []
    for a in items:
        r = base[a["id"]]
        if r["kind"] == "ERROR":
            errors.append({"id": a["id"], **r["error"]})
            c.add("CRASH", a, False, f"{r['error']['type']}: {r['error']['message']}", severity="blocker" if r["error"]["engine"] else "major",
                  expected="no crash", observed=r["error"]["message"])
            continue
        if r["kind"] == "STRUCTURAL":
            check_structural(c, a, r, "")
            check_I11(c, a, r, "")
            continue
        for fn in (check_I1, check_I2, check_I3, check_I4, check_I5, check_I6, check_I7, check_I9, check_I10, check_I11, check_I14):
            try:
                fn(c, a, r, "")
            except Exception as exc:  # noqa: BLE001
                c.add(fn.__name__.replace("check_", ""), a, False, f"checker error {type(exc).__name__}: {exc}", severity="major")
        # unknown industry: the same S3 entry with no pack at all
        if a["tags"]["suite"] == "S3":
            r_none = run_one(a, pack=None)
            check_I11(c, a, r_none, "no pack (unknown industry)")

    # ── 3. I13 (engine path), service layer (I16 / I11 / I13), static I12 / I15 ──
    adversarial_checks(items, base, c)
    svc_summary = service_checks(items, c)
    ui_rows = ui_static(c)
    iso_rows = isolation_static(c)
    ppe_rows = baseline_probe(c)
    (OUT / "baseline_ppe_probe.json").write_text(json.dumps(ppe_rows, indent=1), encoding="utf-8")
    live = live_llm(c)

    # ── 4. cross-HIRA ──
    mono = monotonicity(items, base)
    c.add("MONOTONIC", None, not mono, f"{len(mono)} violation(s)" if mono else "adding a verified engineering control never raised a residual; removing evidence never raised L; adding an event never lowered L")
    cons = consistency(hazard_records(items, base))
    s4 = s4_ordering(items, base)
    for row in s4:
        c.add("S4-ordering", {"id": row["matrix"], "tags": {"suite": "S4"}}, row["ok"], row["detail"], severity="major" if not row["ok"] else "")
    roll = study_rollup(items, base)
    s1r = s1_regression(base, items)
    c.add("S1-regression", None, not s1r["activityDiffs"] and not s1r["targetDiffs"],
          "S1 reproduces results.json / REPORT.md exactly" if not s1r["activityDiffs"] and not s1r["targetDiffs"]
          else f"differs: {[d['id'] for d in s1r['activityDiffs']]} targets {s1r['targetDiffs']}")

    # ── 5. performance (engine only) ──
    perf = performance(items)

    # ── 6. outputs ──
    deltas = deltas_rows(items, base)
    write_csv(OUT / "v1_vs_v2_deltas.csv", deltas)
    audit = relevance_audit(items, base)
    write_csv(OUT / "control_relevance_audit.csv", audit)
    manifest_extra = {"controlRelevanceAuditRows": len(audit)}
    write_csv(OUT / "cross_hira_consistency.csv", cons, ["group", "family", "size", "L_min", "L_max", "L_spread", "S_min", "S_max", "S_spread", "reasons", "flagged", "members"])
    sheet, key = blind_sheets(items, base)
    write_csv(OUT / "blind_scoring_sheet.csv", sheet)
    write_csv(OUT / "blind_scoring_key.csv", key)
    defects = defect_rows(c, items)
    curated = json.loads(CURATED.read_text(encoding="utf-8"))["findings"] if CURATED.exists() else []
    for n, f in enumerate(curated, 1):
        defects.append({"defect_id": f"F{n:02d}", "hira_id": " ".join(f["hiraIds"]), "invariant": f["category"],
                        "observed": f["observed"], "expected": f["expected"], "severity": f["severity"], "repro": f["repro"]})
    write_csv(OUT / "defects.csv", defects, ["defect_id", "hira_id", "invariant", "observed", "expected", "severity", "repro"])
    write_csv(OUT / "invariant_results.csv", c.rows)
    (OUT / "monotonicity.json").write_text(json.dumps(mono, indent=1), encoding="utf-8")
    (OUT / "study_rollup.json").write_text(json.dumps(roll, indent=1), encoding="utf-8")
    (OUT / "db_row_count_diff.txt").write_text(db_note(), encoding="utf-8")
    (OUT / "live_shadow_report.md").write_text(live_note(), encoding="utf-8")
    manifest = {
        "campaign": "HIRA calibration v2, offline part",
        "generatedAt": datetime.now(UTC).isoformat(timespec="seconds"),
        "engineVersion": ENGINE_VERSION,
        "corpus": {"file": str(CORPUS.relative_to(ROOT)), "sha256": hashlib.sha256(CORPUS.read_bytes()).hexdigest(), "items": len(items),
                   "now": CORPUS_DATA["now"]},
        "packs": {p["code"]: p["status"] for p in rp.list_packs()},
        "python": platform.python_version(),
        "seeds": {"randomOrders": [1, 2, 3], "subprocessPYTHONHASHSEED": [] if args.no_subprocess else ["0", "4242"], "blindSheet": "blind-<stratum>"},
        "determinismRuns": list(runs),
        "dbConnectionAttempts": DB_CONNECT_ATTEMPTS["n"],
        "llm": {"calls": 0, "note": "offline engine run: no LLM calls (see llm_probe_report.md for the separate probe)",
                "liveProbe": {k: live["manifest"].get(k) for k in ("generatedAt", "analysedAt", "model", "temperature", "promptSha256",
                                                                   "engineVersion", "calls", "spentUsd", "budgetUsd", "usage", "latencyMs",
                                                                   "costPer100EntriesUsd", "costPer1000EntriesUsd", "callsOver30s")}
                if live else None},
        "timings": {"totalSeconds": round(time.perf_counter() - t_start, 1), **perf},
        "serviceHarness": svc_summary,
        **manifest_extra,
    }
    (OUT / "run_manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    report = render(items, base, c, defects, deltas, cons, roll, s1r, mono, s4, perf, ui_rows, iso_rows, runs, det_bad, errors, manifest,
                    audit, curated, live)
    (OUT / "campaign_report.md").write_text(report, encoding="utf-8")
    fails = [r for r in c.rows if r["result"] == "FAIL"]
    print(f"{len(items)} items, {len(c.rows)} checks, {len(fails)} FAIL, {len(defects)} defects; DB connection attempts: {DB_CONNECT_ATTEMPTS['n']}")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


def s4_ordering(items: list[dict[str, Any]], base: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    refs = {r["s4Base"]: run_one(r) for r in CORPUS_DATA["references"]}

    def key(res: dict[str, Any], which: str) -> tuple[int, int]:
        e = res["raw"]["pass1"]["entry"].get(which)
        return (BANDS.index(e["level"]), e["score"]) if e else (-1, 0)

    out = []
    by_m: dict[str, dict[str, dict[str, Any]]] = {}
    for a in items:
        if a["tags"]["suite"] == "S4":
            by_m.setdefault(a["matrix"], {})[a["s4Base"]] = base[a["id"]]
    for m, acts in sorted(by_m.items()):
        inv = []
        for which in ("inherent", "residual"):
            ks = sorted(acts)
            for i in ks:
                for j in ks:
                    if key(refs[i], which) > key(refs[j], which) and key(acts[i], which)[0] < key(acts[j], which)[0]:
                        inv.append(f"{which}: {i} > {j} on STD_5X5 but {i} < {j} on {m}")
        seq = " ".join(f"{k}={lvl(acts[k]['raw']['pass1']['entry'].get('inherent'))}/{lvl(acts[k]['raw']['pass1']['entry'].get('residual')) or 'NA'}" for k in sorted(acts))
        ref = " ".join(f"{k}={lvl(refs[k]['raw']['pass1']['entry'].get('inherent'))}/{lvl(refs[k]['raw']['pass1']['entry'].get('residual')) or 'NA'}" for k in sorted(refs))
        out.append({"matrix": m, "ok": not inv, "detail": ("; ".join(inv) if inv else "relative order preserved") + f" | {m}: {seq} | STD_5X5: {ref}"})
    return out


def performance(items: list[dict[str, Any]]) -> dict[str, Any]:
    drafts = [a for a in items if a["mode"] == "DRAFT" and is_calibrated(a) and a["tags"]["suite"] in ("S1", "S2")]
    stored = [a for a in items if a["mode"] == "STORED" and is_calibrated(a)]
    cfg, _ = pack_cfg("STEEL_HEAVY")
    t_draft, t_review = [], []
    for _ in range(3):
        for a in drafts:
            t0 = time.perf_counter()
            from app.services.hira_calibration import assistant_v2 as av
            av.calibrate_draft(E.draft_for(a), cfg=cfg, rule=rule_for(CORPUS_DATA, a), evidence=evidence_for(a), plant=E.plant_for(a))
            t_draft.append((time.perf_counter() - t0) * 1000)
        for a in stored:
            t0 = time.perf_counter()
            rv.evaluate(stored_input(a), cfg=cfg, rule=rule_for(CORPUS_DATA, a), evidence=evidence_for(a), plant=E.plant_for(a))
            t_review.append((time.perf_counter() - t0) * 1000)
    studies: dict[str, list[dict[str, Any]]] = {}
    for a in items:
        if a.get("study"):
            studies.setdefault(a["study"], []).append(a)
    t_study = []
    for acts in studies.values():
        t0 = time.perf_counter()
        for a in acts[:10]:
            run_one(a)
        t_study.append((time.perf_counter() - t0) * 1000)

    def st(xs: list[float]) -> dict[str, float]:
        return {"n": len(xs), "p50": round(statistics.median(xs), 1) if xs else 0, "p95": round(percentile(xs, 95), 1), "max": round(max(xs), 1) if xs else 0}

    return {"engineDraftMs": st(t_draft), "engineReviewMs": st(t_review), "engineStudyRunMs": st(t_study),
            "note": "engine only (calibrate_draft / evaluate / full multi-pass run); no LLM, no DB"}


def defect_rows(c: Check, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    n = 0
    seen = set()
    for r in c.rows:
        if r["result"] != "FAIL":
            continue
        k = (r["invariant"], r["id"], r["variant"], r["detail"][:80])
        if k in seen:
            continue
        seen.add(k)
        n += 1
        out.append({
            "defect_id": f"D{n:03d}", "hira_id": r["id"], "invariant": r["invariant"] + (f" ({r['variant']})" if r["variant"] else ""),
            "observed": r["observed"] or r["detail"], "expected": r["expected"], "severity": r["severity"] or "blocker",
            "repro": (f"python tests/campaign/hira_calibration/llm_probe.py --reanalyse (saved answers in out/llm_probe_runs.json, {r['id']}; "
                      "a fresh live run costs about $0.45)") if r["variant"] == "live LLM" else
            f"python tests/campaign/hira_calibration/run_campaign.py; see out/invariant_results.csv row {r['invariant']} / {r['id']}"
            + (f" / {r['variant']}" if r["variant"] else ""),
        })
    return out


def db_note() -> str:
    return (
        "DB row-count diff: NOT RUN against a database.\n\n"
        "This was the offline part of the campaign. No staging environment exists yet, and the only database in .env is the\n"
        "production Supabase project with the real JSL Hisar data, so no query was made.\n\n"
        f"Guard: DATABASE_URL / DATABASE_URL_SYNC / DIRECT_URL were overridden to an unroutable host before the app was imported,\n"
        f"and every connection attempt on the app's engine was counted and refused.\n"
        f"Connection attempts during this run: {DB_CONNECT_ATTEMPTS['n']}\n\n"
        "Tables to count before/after the staging run: HiraStudy, HiraEntry, HiraEntryHazard, HiraEntryExistingControl,\n"
        "HiraEntryRecommendedControl, HiraEntryVersion (or the version table in use), Capa, HiraCalibrationConfig,\n"
        "hira_copilot_shadow_runs (expected to grow), and the ZZ-CAL-TEST-<date> study (expected to appear, then be emptied).\n"
    )


def live_note() -> str:
    return (
        "# Live shadow report: NOT RUN\n\n"
        "The live-data shadow run (section 5 of the campaign brief) needs a staging backend with the v2 code deployed, the\n"
        "calibration DDL applied, and a copy of plant data. None of these exist yet:\n\n"
        "- the backend deploys only `main` (to Azure); `hira-calibration-v2` is not deployed anywhere;\n"
        "- the Vercel preview of the branch is frontend only;\n"
        "- the only database configured is the production Supabase project (real JSL Hisar data).\n\n"
        "Nothing was read from or written to it. When staging exists, run section 5 against it; the offline corpus results are in\n"
        "campaign_report.md. The blind scoring sheet in this folder is drawn from the synthetic campaign corpus, not live data.\n"
    )


def render(items, base, c, defects, deltas, cons, roll, s1r, mono, s4, perf, ui_rows, iso_rows, runs, det_bad, errors, manifest,  # noqa: ANN001, PLR0913
           audit, curated, live=None) -> str:
    L: list[str] = []
    lm = (live or {}).get("manifest") or {}
    inv_ids = ["I1", "I2", "I3", "I4", "I5", "I6", "I7", "I8", "I9", "I10", "I11", "I12", "I13", "I14", "I15", "I16"]
    suites = ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "ADV"]
    tab: dict[tuple[str, str], list[int]] = {}
    for r in c.rows:
        key = (r["suite"], r["invariant"])
        p, f, na = tab.get(key, [0, 0, 0])
        tab[key] = [p + (r["result"] == "PASS"), f + (r["result"] == "FAIL"), na + (r["result"] == "N/A")]
    calibrated = [a for a in items if base[a["id"]]["kind"] == "CALIBRATED"]
    validated = [a for a in calibrated if base[a["id"]]["scope"]["validated"]]
    recs = [len(base[a["id"]]["raw"]["pass1"]["recommendations"]) for a in validated]
    crit_res = [a["id"] for a in validated if (base[a["id"]]["raw"]["pass1"]["entry"].get("residual") or {}).get("level") == "CRITICAL"]
    crit_no_ev = [a["id"] for a in validated if any(not h["evidence"] for h in base[a["id"]]["summary"]["v2"]["criticalResidualHazards"])]
    inv_fail = {i: sum(1 for r in c.rows if r["invariant"] == i and r["result"] == "FAIL") for i in inv_ids}
    flagged = [g for g in cons if g["flagged"]]

    def gate(name: str, ok: bool | None, value: str) -> str:
        return f"| {name} | {'PASS' if ok else ('NOT RUN' if ok is None else 'FAIL')} | {value} |"

    worsen = [d for d in deltas if d["residual_band_change"].startswith("worse")]
    worsen2 = [d for d in worsen if int(d["residual_band_change"].split("+")[1]) >= 2]
    reached = [a for a in validated if base[a["id"]]["summary"]["final"]["reached"]]
    low = [a for a in validated if a["id"].startswith("LOW-")]
    low_r = [a for a in low if base[a["id"]]["summary"]["final"]["reached"]]
    high = [a for a in validated if not a["id"].startswith("LOW-") and a["tags"]["suite"] in ("S1", "S2")]
    high_r = [a for a in high if base[a["id"]]["summary"]["final"]["reached"]]
    L += [
        "# HIRA calibration v2: test campaign report (offline part" + (" + live LLM probe)" if live else ")"),
        "",
        f"Engine {ENGINE_VERSION}; corpus {len(items)} entries ({manifest['corpus']['sha256'][:12]}); generated {manifest['generatedAt']}.",
        "The engine run uses no database and no network (DB connection attempts: "
        f"{manifest['dbConnectionAttempts']})."
        + (f" The live LLM probe ({lm['calls']} Assistant calls to `{lm['model']}` at temperature 0, ${lm['spentUsd']:.2f} of the "
           f"${lm['budgetUsd']:.2f} cap, {lm['generatedAt'][:10]}) is merged in as the I8 / I13 rows marked 'live LLM'." if live
           else " No LLM was called.")
        + " Staging items (live sample, UI screenshots, DB diff, flag toggling) were not run: there is no staging environment yet.",
        "",
        "## Gate results",
        "",
        "| Gate | Result | Value |",
        "|---|---|---|",
        gate("Invariants I1-I16 on 100% of the S1-S7 corpus (offline checks" + (" + live LLM probe)" if live else ")"),
             not any(inv_fail.values()), ", ".join(f"{k}: {v} fail" for k, v in inv_fail.items() if v) or "no failures"),
        gate("Live sample: no Critical residual without evidence; avg recs <= 3.0, none > 5", None, "needs staging"),
        gate("Live sample: <= 10% worsen; no 2-band worsening unexplained", None, "needs staging (corpus: "
             f"{len(worsen)} worsen, {len(worsen2)} by 2+ bands vs the v1 model)"),
        gate("Convergence and determinism 100%", inv_fail["I8"] == 0 and inv_fail["I9"] == 0, f"I8 fails {inv_fail['I8']}, I9 fails {inv_fail['I9']}"
             + (f" (includes {len(live['i8'])} items re-run live through the Assistant)" if live else "")),
        gate("UI 12/12", None, "browser run needs staging; static source checks: see I12 / UI rows"),
        gate("DB diff: no unexpected changes", None, f"no DB touched ({manifest['dbConnectionAttempts']} connection attempts)"),
        gate("Cross-HIRA: unexplained spread > 1 level in <= 5% of groups", len(flagged) <= 0.05 * max(1, len(cons)),
             f"{len(flagged)}/{len(cons)} groups flagged"),
        gate("Corpus: Critical residual <= 5% and only with evidence", len(crit_res) <= 0.05 * max(1, len(validated)) and not crit_no_ev,
             f"{pct(len(crit_res), len(validated))}; without evidence: {len(crit_no_ev)}"),
        gate("Corpus: avg recommendations <= 3.0, none > 5", (sum(recs) / max(1, len(recs))) <= 3.0 and max(recs or [0]) <= 5,
             f"avg {sum(recs) / max(1, len(recs)):.2f}, max {max(recs or [0])}"),
        gate("One-pass resolution: LOW-* 100%", len(low_r) == len(low), pct(len(low_r), len(low))),
        gate("One-pass resolution: high-hazard (S1 HH/BENCH + S2) >= 70%", len(high_r) >= 0.7 * max(1, len(high)), pct(len(high_r), len(high))),
        gate("One-pass resolution overall (validated corpus) >= 80%", len(reached) >= 0.8 * max(1, len(validated)), pct(len(reached), len(validated))),
        gate("S1 reproduces REPORT.md / results.json", not s1r["activityDiffs"] and not s1r["targetDiffs"],
             "identical" if not s1r["activityDiffs"] and not s1r["targetDiffs"] else f"{len(s1r['activityDiffs'])} activities differ"),
        "",
        "## Suite x invariant (pass / fail / not applicable)",
        "",
        "| Suite | " + " | ".join(inv_ids) + " |",
        "|---|" + "---|" * len(inv_ids),
    ]
    for s in suites:
        cells = []
        for i in inv_ids:
            p, f, na = tab.get((s, i), [0, 0, 0])
            cells.append("-" if not (p or f or na) else (f"**{p}/{f}**/{na}" if f else f"{p}/{f}/{na}"))
        L.append(f"| {s} | " + " | ".join(cells) + " |")
    glob_rows = [r for r in c.rows if r["id"] == "(global)"]
    L += ["", "Global checks:", ""]
    L += [f"- **{r['invariant']}** {r['result']}: {r['detail']}" for r in glob_rows]
    if curated:
        L += ["", "## Engine findings verified by hand (F-series)", "",
              "Found while triaging the automated results; each was reproduced directly against the engine. They sit outside the "
              "literal I1-I16 wording but change what users see.", ""]
        for n, f in enumerate(curated, 1):
            L += [f"### F{n:02d} [{f['severity']}] {f['title']}", "", f"- Entries: {', '.join(f['hiraIds'])}", f"- Observed: {f['observed']}",
                  f"- Expected: {f['expected']}", f"- Likely cause: {f.get('cause', '-')}", f"- Repro: {f['repro']}",
                  *([f"- Fix: {f['fix']}"] if f.get("fix") else []), ""]
    crit = [(a["id"], h) for a in items if base[a["id"]]["kind"] == "CALIBRATED" and base[a["id"]]["scope"]["validated"]
            for h in base[a["id"]]["summary"]["v2"]["criticalResidualHazards"]]
    if crit:
        L += ["", "## Critical residuals on the corpus, by cause", ""]
        for aid, h in crit:
            hz = next(x for x in base[aid]["raw"]["pass1"]["hazards"] if x["key"] == h["key"])
            nr = [x for x in hz.get("credits") or [] if x["status"] == "NOT_RELEVANT"]
            ppe = [x for x in hz.get("credits") or [] if x["status"] == "NO_CREDIT" and x["hierarchy"] == "PPE"]
            cause = ("controls recorded but ruled not relevant to this hazard: " + "; ".join(f"'{x['control'][:45]}' ({x['reason'][:40]})" for x in nr[:2])) if nr else (
                "only PPE recorded for an S4+ hazard (no credit by rule)" if ppe else "no control applies to this hazard")
            L.append(f"- {aid} {h['name']} {rating_txt(h['rating'])}, evidence: {'yes' if h['evidence'] else 'no'}. {cause}")
    if audit:
        nr = [x for x in audit if "NOT relevant" in x["finding"]]
        kwless = [x for x in audit if "no keyword" in x["finding"]]
        L += ["", "## Control relevance audit (control_relevance_audit.csv)", "",
              f"{len(nr)} engineering-control/hazard pairs ruled not relevant, {len(kwless)} keyword-less engineering controls credited "
              "to a hazard. Candidates for the HSE lead; the clear keyword collisions are in the F-series above.", ""]
    L += ["", "## Defects", ""]
    if defects:
        L += ["| ID | HIRA | Invariant | Severity | Observed |", "|---|---|---|---|---|"]
        L += [f"| {d['defect_id']} | {d['hira_id']} | {d['invariant']} | {d['severity']} | {str(d['observed'])[:220].replace('|', '/')} |" for d in defects]
    else:
        L.append("None.")
    na_i2 = [r for r in c.rows if r["invariant"] == "I2" and r["result"] == "N/A" and "plant-index" in r["detail"]]
    if na_i2:
        L += ["", f"I2 not applied ({len(na_i2)} entries): a team's stored rating on the plant-index (JSL) scale passes through without the "
              "evidence cap, by design. Listed as an open question.", ""]
        L += [f"- {r['id']}: {r['detail']}" for r in na_i2]
    if errors:
        L += ["", "## Crashes", ""] + [f"- {e['id']}: {e['type']} {e['message']} (engine frame: {e['engine']})" for e in errors]
    L += ["", "## Determinism and order independence (I8)", ""]
    L += [f"- {n}: {'identical' if n not in det_bad else 'DIFFERS for ' + ', '.join(det_bad[n][:8])}" for n in runs]
    if live:
        i13_fail = sorted({x["id"] for x in live["i13"] if not x["ok"]})
        L += ["", "## Live LLM probe (Assistant, flag ON path)", "",
              f"`{lm['model']}`, temperature 0, system prompt sha256 {lm['promptSha256'][:16]}..., engine {lm['engineVersion']}; "
              f"{lm['calls']} calls, ${lm['spentUsd']:.4f} spent. Each answer went through the same path the flag-ON Assistant uses "
              "(from_assistant_suggestion -> calibrate_draft, STEEL_HEAVY). Full tables: llm_probe_report.md.", "",
              f"- I8, repeats: {sum(x['engineIdentical'] for x in live['i8'])} of {len(live['i8'])} items gave identical engine output; "
              f"LLM text identical in {sum(x['textIdentical'] for x in live['i8'])} of {len(live['i8'])}.",
              f"- I13, injections: {len({x['id'] for x in live['i13']}) - len(i13_fail)} of {len({x['id'] for x in live['i13']})} injected items "
              "changed no rating. Each injected run is compared with the clean runs on the same clean record, so an extra control slot is "
              "not counted as an effect." + (f" **Failed: {', '.join(i13_fail)}.**" if i13_fail else "")]
        for x in live["i13"]:
            if not x["ok"] and x["rep"] == 1:
                L += [f"  - {x['id']} ({x['field']}): '{x['injection']}'. The LLM proposed L{x['llmInitial'][0]} (clean runs: "
                      f"L{x['cleanLlmInitial'][0][0]}) and the engine took it: entry {x['cleanEntries'][0][0]} -> {x['entry'][0]}, "
                      f"residual {x['cleanEntries'][0][1]} -> {x['entry'][1]}.",
                      f"  - Cause: {LIVE_CAUSE}"]
        echo = sorted({x["id"] for x in live["i13"] if x.get("llmObeyed") and x["ok"]})
        if echo:
            L.append(f"- The LLM quoted the injected band words in its own text for {', '.join(echo)} (it flagged them as not a technical "
                     "calibration); no rating moved.")
        L.append("- The LLM wrote residual numbers in its residual_*_guidance text in every run (the prompt asks for that field); with the flag "
                 "ON the service replaces both fields with the engine's own text, so they do not reach the user.")
    L += ["", "## Cross-HIRA consistency", "", f"{len(cons)} groups of 3+ similar hazards; flagged (unexplained spread > 1): {len(flagged)}.", ""]
    L += ["| Group | Size | L spread | S spread | Reason | Flagged |", "|---|---|---|---|---|---|"]
    for g in sorted(cons, key=lambda g: (-max(g["L_spread"], g["S_spread"]), g["group"]))[:12]:
        L.append(f"| {g['group'].replace('|', '/')} | {g['size']} | {g['L_min']}-{g['L_max']} | {g['S_min']}-{g['S_max']} | {g['reasons'] or '-'} | {'yes' if g['flagged'] else ''} |")
    L += ["", "Monotonicity: " + (f"{len(mono)} violation(s):" if mono else "no violations (verified engineering control never raised a residual; removing evidence never raised L; adding an event never lowered L).")]
    L += [f"- {m['id']} {m['test']} {m['hazard']}: {m['before']} -> {m['after']}" for m in mono[:20]]
    L += ["", "## S4 matrix shapes (relative order vs STD_5X5)", ""] + [f"- {'PASS' if r['ok'] else 'FAIL'} {r['detail']}" for r in s4]
    L += ["", "## S7 study rollup and recommendation load", "",
          "The v2 engine has no study-level risk summary, so 'study summary equals the worst-hazard rollup' cannot be checked; "
          "the harness rollup is listed for the HSE lead.", "",
          "| Study | Entries | Worst entry rating | Sum of recs | Unique recs | Unique < sum | Recs by owner role | Roles > 10 |", "|---|---|---|---|---|---|---|---|"]
    for r in roll:
        L.append(f"| {r['study']} | {r['entries']} | {r['worstEntryRating']} | {r['sumRecommendations']} | {r['uniqueRecommendations']} | "
                 f"{'yes' if r['uniqueLowerThanSum'] else 'no'} | {', '.join(f'{k}: {v}' for k, v in sorted(r['byRole'].items()))} | {', '.join(r['rolesOver10']) or '-'} |")
    L += ["", "## v1 vs v2 on the corpus (validated, task-based entries)", ""]
    vd = [d for d in deltas if base[d["id"]]["scope"]["validated"]]
    for k in ("v1_inherent", "v2_inherent", "v1_residual", "v2_residual"):
        cnt: dict[str, int] = {}
        for d in vd:
            cnt[str(d[k]) or "-"] = cnt.get(str(d[k]) or "-", 0) + 1
        L.append(f"- {k}: " + ", ".join(f"{a} {b}" for a, b in sorted(cnt.items())))
    L.append(f"- recommendations per entry: v1 {sum(int(d['v1_recommendations'] or 0) for d in vd) / max(1, len(vd)):.2f}, "
             f"v2 {sum(d['v2_recommendations'] for d in vd) / max(1, len(vd)):.2f}")
    L.append(f"- residual band worse under v2: {len(worsen)} ({', '.join(d['id'] for d in worsen[:12])}); 2+ bands: {len(worsen2)}")
    L += ["", "## Performance (engine only, no LLM)", ""]
    for k in ("engineDraftMs", "engineReviewMs", "engineStudyRunMs"):
        p = perf[k]
        L.append(f"- {k}: n={p['n']}, p50 {p['p50']} ms, p95 {p['p95']} ms, max {p['max']} ms")
    if live:
        u, lat = lm["usage"], lm["latencyMs"]
        L += ["", "### LLM (live probe, one Assistant call per entry)", "",
              f"- Latency: p50 {lat['p50'] / 1000:.1f} s, p95 {lat['p95'] / 1000:.1f} s, max {lat['max'] / 1000:.1f} s. "
              f"**Flagged (over 30 s): {len(lm['callsOver30s'])} of {lm['calls']} calls.** Output length drives it "
              f"(avg {u['output_tokens'] // max(1, lm['calls'])} output tokens per call).",
              f"- Tokens: input {u['input_tokens']}, output {u['output_tokens']}, cache write {u['cache_creation_input_tokens']}, "
              f"cache read {u['cache_read_input_tokens']}.",
              f"- Cost: ${lm['spentUsd'] / max(1, lm['calls']):.4f} per entry, about **${lm['costPer100EntriesUsd']:.2f} per 100 entries** and "
              f"${lm['costPer1000EntriesUsd']:.2f} per 1,000 (warm prompt cache). The engine itself adds no cost.",
              "- A 10-entry study through the Assistant is about 10 calls: roughly 6 minutes if run one after another, at about $0.21."]
    L += ["", "## UI static checks (frontend source)", ""]
    L += [f"- line {r['line']}: RatingChip r={{{r['expr']}}} neutral={r['neutral']} {'ok' if r['ok'] else 'WRONG'}" for r in ui_rows]
    L += ["", "## I15 tenant isolation (static review of loader queries)", ""]
    L += [f"- {r['query']}: {r['scoped']}" for r in iso_rows]
    L += ["", "## Not run, and why", "",
          "- Live shadow run (section 5), blind scoring on live entries, DB row counts, flag OFF/SHADOW/ON per real plant, UI screenshots "
          "(section 8)" + (", end-to-end latency through the deployed API" if live else ", live latency and LLM cost per 100/1,000 entries")
          + ": no staging environment exists (backend `hira-calibration-v2` is not deployed; the only DB is production).",
          "- I15 dynamic check (plant A never reads plant B): static review only; the engine itself is pure and takes no plant id.",
          ("- Live LLM probe: small by design (10 items, 21 calls, Haiku 4.5, seed prompt file with no tools). Production loads the prompt "
           "from the AgentPrompt table and runs with tools, so a staging run should repeat it." if live
           else "- I8 for LLM wording and the live half of I13: " + llm_status()),
          "- Section 7 'study summary equals worst-hazard rollup': v2 has no study-level summary to compare against.",
          ""]
    return "\n".join(L) + "\n"


def llm_status() -> str:
    f = OUT / "llm_probe_report.md"
    if not f.exists():
        return "llm_probe.py has not been run."
    first = f.read_text(encoding="utf-8").splitlines()[0]
    if "NOT RUN" in first:
        return ("NOT RUN: the backend's ANTHROPIC_API_KEY is rejected by the API (401), $0 spent; see llm_probe_report.md. "
                "The offline half (injected text through the real v2 code, engine and Assistant paths) is above.")
    return "see llm_probe_report.md."


LIVE_CAUSE = ("risk_engine.calibrate_likelihood only caps a proposed likelihood from above (L3 without evidence, L4/L5 need events); "
              "a lower proposal from the Assistant (here L1, justified in its own text by 'well controlled') passes with no floor "
              "and no check of its reason, so the inherent rating takes it.")


def live_llm(c: Check) -> dict[str, Any] | None:
    """Results of the live probe (llm_probe.py), when it has run with a valid key: adds I8 / I13 rows (variant 'live LLM')."""
    rep, f = OUT / "llm_probe_report.md", OUT / "llm_probe_runs.json"
    if not rep.exists() or not f.exists() or "NOT RUN" in rep.read_text(encoding="utf-8").splitlines()[0]:
        return None
    d = json.loads(f.read_text(encoding="utf-8"))

    def act(aid: str) -> dict[str, Any]:
        return {"id": aid, "tags": {"suite": "ADV" if aid.startswith("ADV") else aid.split("-")[0]}}

    for x in d["i8"]:
        c.add("I8", act(x["id"]), x["engineIdentical"],
              f"{x['runs']} Assistant calls at temperature 0: engine output "
              + ("identical" if x["engineIdentical"] else "differs in " + ", ".join(x["engineDiffers"]))
              + ("; LLM text identical" if x["textIdentical"] else "; LLM wording differs"),
              variant="live LLM", expected="identical ratings, bands and recommendation targets", observed=", ".join(x["engineDiffers"]))
    by: dict[str, list[dict[str, Any]]] = {}
    for x in d["i13"]:
        by.setdefault(x["id"], []).append(x)
    for aid, xs in by.items():
        x = xs[0]
        bad = sorted({b for y in xs for b in y["directEffectOfInjectedWords"] + y["ratingsMovedVsCleanRuns"]
                      + [f"hazard dropped: {h}" for h in y["hazardsDroppedVsCleanRuns"]]})
        ok = all(y["ok"] for y in xs)
        ls = f"LLM proposed L{x['llmInitial'][0]}/S{x['llmInitial'][1]} (clean runs L{x['cleanLlmInitial'][0][0]}/S{x['cleanLlmInitial'][0][1]})"
        c.add("I13", act(aid), ok,
              f"injection in {x['field']}, {len(xs)} runs: " + ("; ".join(bad) if bad else "no rating, band or target changed") + f"; {ls}",
              variant="live LLM", expected="the injected words change no rating (I13)" + ("" if ok else f". Cause: {LIVE_CAUSE}"),
              observed=(f"'{x['injection']}' -> {ls}; " + "; ".join(bad))[:600] if bad else "")
    return d


if __name__ == "__main__":
    raise SystemExit(main())
