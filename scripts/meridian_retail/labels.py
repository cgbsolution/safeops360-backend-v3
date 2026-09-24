"""Meridian Retail — display-label overrides (profile RETAIL).

Generated from the code, not hand-listed: every `L("key", "fallback")` call in
the frontend and every `label(labels, "key", "default")` call in the backend is
collected, the Retail vocabulary is applied to the fallback, and a DisplayLabel
row is written only where the result differs. A new call site therefore gets a
Retail label the next time this runs, instead of leaking "Plant".

Vocabulary: Plant→Store, Factory→Distribution Center, Site→Store,
Plant Head / Shift Supervisor→Store Manager, Production Line→Aisle / Zone,
plus explicit nav relabels (PTW/LOTO → "Facilities & DC Maintenance Permits",
EPC → "Contractor Safety Management", CAMS inspections → "Fire Safety Audits").

    python -m scripts.meridian_retail.labels            # dry run: prints the table
    python -m scripts.meridian_retail.labels --commit
"""

from __future__ import annotations

import os
import re
import sys

from scripts.meridian_retail.common import PROFILE, ROOT, conn, new_id

FRONTEND_SRC = os.path.join(os.path.dirname(ROOT), "safeops360-frontend", "src")
BACKEND_APP = os.path.join(ROOT, "app")

# Explicit labels (win over the generated vocabulary).
EXPLICIT: dict[str, str] = {
    # Nav — relabels through the override layer, not forks.
    "nav./ptw": "Facilities & DC Maintenance Permits",
    "nav./loto": "DC Lockout / Tagout (LOTO)",
    "nav./epc": "Contractor Safety Dashboard",
    "nav.section.epc": "Contractor Safety Management",
    "nav./epc/sites": "Fit-out & Renovation Projects",
    "nav./epc/contractors": "Contractor Companies",
    "nav./epc/workers": "Contractor Workers",
    "nav./epc/mobilization": "Contractor Mobilisation",
    "nav./epc/inductions/new": "Record Store Safety Induction",
    "nav./epc/gate": "Store Entry Gate Check",
    "nav./cams/engagements": "Fire Safety Audits",
    "nav./cams/findings": "Audit Findings",
    "nav.section.cams": "Fire Safety Audits (CAMS)",
    "nav.section.operational": "Store Safety Operations",
    "nav./field-reports": "Field Reports (Store Floor)",
    "nav./capture": "Report from the Floor",
    # Vocabulary cores.
    "term.plant": "Store",
    "term.plants": "Stores",
    "term.all_plants": "All Stores",
    "term.factory": "Distribution Center",
    "term.factories": "Distribution Centers",
    "term.shift_supervisor": "Store Manager",
    "term.production_line": "Aisle / Zone",
    "term.plant_head": "Store Manager",
    "term.plant_manager": "Store manager",
    "term.site": "Store",
    "term.sites": "Stores",
    "term.all_sites": "All stores",
    # PTW close-out PDF step label (dynamic key ptw.action.<ACTION>).
    "ptw.action.APPROVE_PLANT_HEAD": "Store Manager Approval",
    # EPC "site" is a fit-out location at a store.
    "term.construction_site": "Fit-out Site",
    "term.construction_sites": "Fit-out Sites",
}

# Ordered whole-word replacements applied to every other fallback.
RULES: list[tuple[str, str]] = [
    (r"\bPlant Heads?\b", "Store Manager"),
    (r"\bplant heads?\b", "store manager"),
    (r"\bPlant [Mm]anager\b", "Store manager"),
    (r"\bShift Supervisors?\b", "Store Manager"),
    (r"\bProduction Lines?\b", "Aisle / Zone"),
    (r"\bFactories\b", "Distribution Centers"),
    (r"\bFactory\b", "Distribution Center"),
    (r"\bfactories\b", "distribution centers"),
    (r"\bfactory\b", "distribution center"),
    (r"\bPlants\b", "Stores"),
    (r"\bPlant\b", "Store"),
    (r"\bplants\b", "stores"),
    (r"\bplant\(s\)", "store(s)"),
    (r"\bplant\b", "store"),
    (r"\bSites\b", "Stores"),
    (r"\bSite\b", "Store"),
    (r"\bsites\b", "stores"),
    (r"\bsite\b", "store"),
]

_STR = r'"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\'|`([^`$]*)`'
FE_CALL = re.compile(r"\bL\(\s*(" + r"TERM\.\w+|navKey\([^)]*\)|navSectionKey\([^)]*\)|" + _STR + r")\s*,\s*(" + _STR + r")\s*\)")
BE_CALL = re.compile(r"\b(?:label|_lbl)\(\s*\w+\s*,\s*(" + _STR + r")\s*,\s*(" + _STR + r")\s*\)")
TERM_DEF = re.compile(r"(\w+):\s*\"(term\.[\w.]+)\"")


def _s(m_groups: tuple) -> str | None:
    for g in m_groups:
        if g is not None:
            return g
    return None


def collect() -> dict[str, str]:
    """key → fallback, from every call site. Dynamic keys (nav.<href>) are
    covered by EXPLICIT; `navKey(item.href)` call sites carry no literal."""
    core = open(os.path.join(FRONTEND_SRC, "lib", "labels", "core.ts"), encoding="utf-8").read()
    terms = dict(TERM_DEF.findall(core))
    found: dict[str, str] = {}
    extra = [p for p in os.environ.get("LABELS_EXTRA_SRC", "").split(";") if p]
    roots = [(FRONTEND_SRC, FE_CALL), *[(p, FE_CALL) for p in extra], (BACKEND_APP, BE_CALL)]
    for base, rx in roots:
        for dirpath, _, files in os.walk(base):
            if "node_modules" in dirpath:
                continue
            for f in files:
                if not f.endswith((".ts", ".tsx", ".py")):
                    continue
                text = open(os.path.join(dirpath, f), encoding="utf-8", errors="ignore").read()
                for m in rx.finditer(text):
                    if rx is FE_CALL:
                        keytok = m.group(1)
                        if keytok.startswith("TERM."):
                            key = terms.get(keytok[5:])
                        elif keytok.startswith("nav"):
                            continue
                        else:
                            key = _s(m.groups()[1:4])
                        fallback = _s(m.groups()[5:8])
                    else:
                        key = _s(m.groups()[1:4])
                        fallback = _s(m.groups()[5:8])
                    if key and fallback is not None:
                        found.setdefault(key, fallback)
    return found


def retailise(text: str) -> str:
    out = text
    for pat, rep in RULES:
        out = re.sub(pat, rep, out)
    return out


def workflow_step_keys() -> dict[str, str]:
    """Workflow step names live in the DB ("Plant Head Final Close") and are
    rendered through <StepName> as `workflow.step.<name>`."""
    c = conn()
    cur = c.cursor()
    cur.execute('select distinct name from "WorkflowStep" where name is not null')
    names = [r[0] for r in cur.fetchall()]
    c.close()
    return {f"workflow.step.{n}": n for n in names}


def build() -> dict[str, str]:
    labels: dict[str, str] = {}
    for key, fallback in {**collect(), **workflow_step_keys()}.items():
        value = EXPLICIT.get(key, retailise(fallback))
        if value != fallback:
            labels[key] = value
    for key, value in EXPLICIT.items():
        labels.setdefault(key, value)
    return dict(sorted(labels.items()))


def main(commit: bool) -> None:
    labels = build()
    width = max(len(k) for k in labels)
    for k, v in labels.items():
        print(f"  {k:<{width}}  {v}")
    c = conn()
    cur = c.cursor()
    for k, v in labels.items():
        cur.execute(
            'insert into "DisplayLabel"(id, "profileCode", key, label) values (%s,%s,%s,%s) '
            'on conflict ("profileCode", key) do update set label = excluded.label',
            (new_id(), PROFILE, k, v),
        )
    # Keys no longer produced by the code are removed so the profile stays exact.
    cur.execute('delete from "DisplayLabel" where "profileCode"=%s and not (key = any(%s))', (PROFILE, list(labels)))
    print(f"{len(labels)} labels for profile {PROFILE}; {cur.rowcount} stale removed")
    if commit:
        c.commit()
        print("committed")
    else:
        c.rollback()
        print("dry run — rolled back (pass --commit)")


if __name__ == "__main__":
    main("--commit" in sys.argv)
