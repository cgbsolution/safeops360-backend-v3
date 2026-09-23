"""Per-flow analytics engine — generic over `FlowSpec` (specs.py).

Answers the questions a register cannot: is this getting better or worse, where
is it concentrated, how old is the backlog, are we closing within target, and
who is carrying it. One implementation serves every flow, so the Incident and
CAPA screens cannot drift apart in what "overdue" or "closure rate" means.

Deterministic and airgap-safe, like the rest of the intelligence layer: pure SQL
reads plus arithmetic, no model call, no network.

**Aggregation happens in Python, not SQL.** Every flow here is in the hundreds
to low thousands of rows (the largest on prod today is 426 observations), and
one pass over that in Python is both faster than eight round trips and far
easier to keep consistent across flows with different column names. `MAX_ROWS`
guards the assumption: past it the response is marked `truncated` rather than
quietly analysing a subset, because a chart built on a silent sample is exactly
the kind of confidently-wrong artefact this layer exists to replace.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.analytics.specs import Dimension, FlowSpec

MAX_ROWS = 50_000
NOT_SET = "__NOT_SET__"
NOT_SET_LABEL = "Not recorded"

# Open-record age buckets. Chosen to mirror how the registers already talk about
# lateness (a week, a month, a quarter) rather than an even split.
AGE_BUCKETS: tuple[tuple[str, int | None], ...] = (
    ("0-7d", 7),
    ("8-30d", 30),
    ("31-90d", 90),
    (">90d", None),
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _scalar(v: Any) -> Any:
    """Unwrap a mapped Python enum to its stored value.

    Load-bearing. Most flows map their status/severity/type columns as
    `Enum(SomeStrEnum, native_enum=False)`, so the ORM hands back an enum member
    whose `str()` is "ObservationStatus.OPEN", not "OPEN". Comparing that to the
    spec's state tuples matched nothing, which classified every record as closed
    and reported open=0 on Observation, Incident and Near Miss — a screen that
    would have told a safety manager their entire backlog was clear.
    """
    return v.value if isinstance(v, Enum) else v


def _naive(v: Any) -> datetime | None:
    """Normalise to naive UTC. Prisma writes most date columns tz-naive while
    the newer SQLAlchemy tables write tz-aware, and comparing the two raises.
    `date` columns are widened to midnight."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.replace(tzinfo=None) if v.tzinfo is not None else v
    if isinstance(v, date_cls):
        return datetime(v.year, v.month, v.day)
    return None


def _month_key(d: datetime) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _month_range(start: datetime, end: datetime) -> list[str]:
    keys, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        keys.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return keys


def _pct(n: int, d: int) -> float | None:
    return round(n / d * 100, 1) if d else None


@dataclass
class _Row:
    id: str
    ref: str | None
    date: datetime | None
    status: str | None
    plant: str | None
    area: str | None
    closed_at: datetime | None
    due: datetime | None
    owner: str | None
    dims: dict[str, Any]
    # Values of the spec's `metric_columns`, kept separate from `dims` so a
    # column that feeds a published number is never mistaken for a breakdown
    # axis and rendered as a chart.
    metrics: dict[str, Any]
    is_open: bool


# ── Loading ──────────────────────────────────────────────────────────────────
async def _load(
    db: AsyncSession, spec: FlowSpec, *, plants_allowed: list[str] | None
) -> tuple[list[_Row], bool]:
    m = spec.model
    cols: dict[str, Any] = {"id": m.id}

    def add(name: str, attr: str | None) -> None:
        if attr and hasattr(m, attr):
            cols[name] = getattr(m, attr)

    add("ref", spec.ref_column)
    add("date", spec.date_column)
    add("status", spec.status_column)
    add("plant", spec.plant_column)
    add("area", spec.area_column)
    add("closed_at", spec.closed_at_column)
    add("due", spec.due_column)
    add("owner", spec.owner_column)
    for d in spec.dimensions:
        if hasattr(m, d.column):
            cols[f"dim__{d.key}"] = getattr(m, d.column)
    for column, _label, _effect in spec.metric_columns:
        if hasattr(m, column):
            cols[f"met__{column}"] = getattr(m, column)

    parent = None
    if spec.plant_via:
        fk, parent_model, parent_plant_col = spec.plant_via
        parent = parent_model
        cols["plant"] = getattr(parent_model, parent_plant_col)

    stmt = select(*[c.label(k) for k, c in cols.items()])
    if parent is not None:
        fk = spec.plant_via[0]
        stmt = stmt.join(parent, getattr(m, fk) == parent.id)
    if spec.soft_delete and hasattr(m, "isDeleted"):
        stmt = stmt.where(m.isDeleted.is_(False))
    # Plant scoping is fail-closed: a caller whose accessible-plant list is
    # known is restricted to it, and a flow with no plant column at all returns
    # nothing rather than everything. Silently widening scope on a screen that
    # aggregates is worse than on one that lists — a chart leaks the shape of
    # data the reader cannot open.
    if plants_allowed is not None:
        if "plant" not in cols:
            return ([], False)
        if not plants_allowed:
            return ([], False)
        stmt = stmt.where(cols["plant"].in_(plants_allowed))
    # The single-plant `?plant=` narrowing is applied in Python, NOT here. The
    # SegmentBar's site dropdown has to list every site the caller may look at,
    # with its record count — and it cannot do that from a result set the SQL
    # has already narrowed to one of them. `plants_allowed` stays in SQL because
    # it is the security boundary; `plant` is a user-chosen view of what they
    # are already entitled to see. Row counts here are in the hundreds, so the
    # extra rows cost nothing.

    rows = (await db.execute(stmt.limit(MAX_ROWS + 1))).mappings().all()
    truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]

    open_set = {s.upper() for s in spec.open_states} if spec.upper_values else set(spec.open_states)
    out: list[_Row] = []
    for r in rows:
        raw_status = _scalar(r.get("status"))
        status = str(raw_status) if raw_status is not None else None
        cmp_status = (status.upper() if (status and spec.upper_values) else status)
        out.append(_Row(
            id=str(r["id"]),
            ref=(str(_scalar(r["ref"])) if r.get("ref") is not None else None),
            date=_naive(r.get("date")),
            status=cmp_status,
            plant=(str(r["plant"]) if r.get("plant") else None),
            area=(str(r["area"]) if r.get("area") else None),
            closed_at=_naive(r.get("closed_at")),
            due=_naive(r.get("due")),
            owner=(str(r["owner"]) if r.get("owner") else None),
            dims={d.key: _scalar(r.get(f"dim__{d.key}")) for d in spec.dimensions},
            metrics={
                c: _scalar(r.get(f"met__{c}"))
                for c, _l, _e in spec.metric_columns
                if f"met__{c}" in cols
            },
            # Unknown states count as OPEN. Erring the other way would quietly
            # shrink the backlog every time someone adds a state to a workflow.
            is_open=(cmp_status in open_set) if cmp_status else True,
        ))
    return out, truncated


# ── Lookups ──────────────────────────────────────────────────────────────────
async def _lookups(db: AsyncSession, spec: FlowSpec, rows: list[_Row]) -> dict[str, dict[str, str]]:
    """id → display name per lookup family. House rule: never render a cuid."""
    need: dict[str, set[str]] = defaultdict(set)
    for d in spec.dimensions:
        if d.lookup:
            for r in rows:
                v = r.dims.get(d.key)
                if v:
                    need[d.lookup].add(str(v))
    if any(r.owner for r in rows):
        need["user"].update(r.owner for r in rows if r.owner)
    if spec.area_column:
        need["area"].update(r.area for r in rows if r.area)

    out: dict[str, dict[str, str]] = {}
    if need.get("plant"):
        from app.services.plant_directory import resolve_plant_names

        out["plant"] = await resolve_plant_names(db, need["plant"])
    if need.get("area"):
        from app.models.plant import Area

        res = (
            await db.execute(
                select(Area.id, Area.name, Area.plantId).where(Area.id.in_(need["area"]))
            )
        ).all()
        # Area names repeat across plants — "Process Area A" exists at both
        # Meridian sites, and two identically-labelled bars in one chart is
        # indistinguishable from a rendering fault. Disambiguate ONLY the names
        # that actually collide, so the common case stays short.
        name_counts = Counter(n for _, n, _ in res)
        plant_ids = {p for _, _, p in res if p}
        codes: dict[str, str] = {}
        if plant_ids and any(name_counts[n] > 1 for _, n, _ in res):
            from app.models.plant import Plant

            # The plant CODE, not its name: full names run to "Meridian North
            # Works — Integrated Manufacturing Unit", which as a chart axis
            # label pushes the area itself off the screen.
            codes = dict(
                (await db.execute(select(Plant.id, Plant.code).where(Plant.id.in_(plant_ids)))).all()
            )
        out["area"] = {
            i: (f"{n} · {codes.get(p, '?')}" if (name_counts[n] > 1 and p) else n)
            for i, n, p in res
        }
    if need.get("user"):
        from app.models.user import User

        res = (await db.execute(select(User.id, User.name).where(User.id.in_(need["user"])))).all()
        out["user"] = {i: n for i, n in res}
    if need.get("risk_category"):
        from app.models.erm import RiskCategory

        res = (
            await db.execute(
                select(RiskCategory.id, RiskCategory.name).where(RiskCategory.id.in_(need["risk_category"]))
            )
        ).all()
        out["risk_category"] = {i: n for i, n in res}
    return out


# Domain acronyms that must not be sentence-cased. Without this the Incident
# type chart reads "Lti / Mtc / Rwc" and the observation chart "Ppe", which an
# EHS reader sees as the product not knowing its own vocabulary.
_ACRONYMS = frozenset({
    "PPE", "LTI", "MTC", "RWC", "FAC", "HIPO", "ALARP", "CAPA", "MOC", "HIRA",
    "EAI", "PTW", "LOTO", "SLA", "NC", "RCA", "PSSR", "KRI", "ESG", "GHG",
    "QA", "QC", "HSE", "BBS", "JSA", "SOP", "MSDS", "SDS", "BC", "IT", "HR",
})


def _titleise(v: str) -> str:
    """UNSAFE_ACT → Unsafe act; implementation_in_progress → Implementation in
    progress; PPE → PPE. Enum tokens are not a display language."""
    if not v:
        return v
    words = v.replace("_", " ").split()
    if not words:
        return v
    out = []
    for i, w in enumerate(words):
        if w.upper() in _ACRONYMS:
            out.append(w.upper())
        elif i == 0:
            out.append(w.capitalize())
        else:
            out.append(w.lower())
    return " ".join(out)


def _field_label(col: str) -> str:
    """statutoryDeadline → "statutory deadline"; closedAt → "closed at".

    A data-quality flag names the column that is missing, and the reader is an
    EHS manager, not a developer. Showing them `statutoryDeadline` tells them a
    field is empty but not which one they have to go and fill in.
    """
    out: list[str] = []
    for i, ch in enumerate(col):
        if ch.isupper() and i:
            out.append(" ")
        out.append(ch.lower())
    return "".join(out).replace("_", " ").strip()


# Dimension keys that hold a severity / risk grading, in preference order. The
# SegmentBar's severity control binds to the first one a flow actually declares,
# so one control serves every flow without the frontend knowing column names.
_SEVERITY_DIM_KEYS: tuple[str, ...] = (
    "severity", "potentialSeverity", "riskLevel", "priority",
    "residualBand", "classification",
)


def severity_dimension(spec: FlowSpec) -> Dimension | None:
    """The dimension the severity filter binds to for this flow, or None."""
    by_key = {d.key: d for d in spec.dimensions}
    for k in _SEVERITY_DIM_KEYS:
        if k in by_key:
            return by_key[k]
    return None


# ── Computations ─────────────────────────────────────────────────────────────
def _trend(rows: list[_Row], months: int, now: datetime, *, has_closure: bool) -> list[dict[str, Any]]:
    """Opened per month, and — only where the flow records a closure date —
    closed per month and the running backlog.

    `has_closure` is not cosmetic. EnterpriseRisk, HiraEntry and EaiEntry carry
    no closure-date column, so counting closures off them yields zero for every
    month and a backlog line that rises forever. That chart says "nothing here
    is ever resolved", which is false — those flows simply record completion as
    a state change, not a timestamp. Better to draw no line than a wrong one.
    """
    start = (now.replace(day=1) - timedelta(days=31 * (months - 1))).replace(day=1)
    keys = _month_range(start, now)
    key_set = set(keys)
    opened: Counter[str] = Counter()
    closed: Counter[str] = Counter()
    for r in rows:
        if r.date:
            k = _month_key(r.date)
            if k in key_set:
                opened[k] += 1
        if has_closure and r.closed_at:
            k = _month_key(r.closed_at)
            if k in key_set:
                closed[k] += 1

    if not has_closure:
        return [{"period": k, "opened": opened[k], "closed": None, "backlog": None} for k in keys]

    # Backlog is the running open count at each month end — the line that shows
    # whether the flow is actually clearing, which neither bar alone reveals.
    backlog = sum(
        1 for r in rows
        if r.date and _month_key(r.date) < keys[0]
        and not (r.closed_at and _month_key(r.closed_at) < keys[0])
    )
    out = []
    for k in keys:
        backlog += opened[k] - closed[k]
        out.append({
            "period": k,
            "opened": opened[k],
            "closed": closed[k],
            "backlog": max(backlog, 0),
        })
    return out


def _breakdown(
    rows: list[_Row], d: Dimension, spec: FlowSpec, lookups: dict[str, dict[str, str]], limit: int = 12
) -> dict[str, Any]:
    total: Counter[str] = Counter()
    open_c: Counter[str] = Counter()
    for r in rows:
        raw = r.dims.get(d.key)
        v = str(raw) if raw not in (None, "") else NOT_SET
        if v != NOT_SET and spec.upper_values and not d.lookup:
            v = v.upper()
        total[v] += 1
        if r.is_open:
            open_c[v] += 1

    order_index = {v: i for i, v in enumerate(d.order)}

    def sort_key(kv: tuple[str, int]) -> tuple[int, int, int]:
        k, n = kv
        if k == NOT_SET:
            return (2, 0, 0)  # always last: it is an absence, not a category
        return (0, order_index.get(k, len(order_index)), -n)

    items = []
    for key, n in sorted(total.items(), key=sort_key)[:limit]:
        if key == NOT_SET:
            label = NOT_SET_LABEL
        elif d.lookup:
            label = lookups.get(d.lookup, {}).get(key) or "Unknown"
        else:
            label = _titleise(key)
        href = None
        if d.drill_param and key != NOT_SET:
            href = f"{spec.href}?{d.drill_param}={key}"
        items.append({
            "key": key, "label": label, "total": n,
            "open": open_c.get(key, 0), "href": href,
        })
    return {"key": d.key, "label": d.label, "items": items}


def _ageing(rows: list[_Row], spec: FlowSpec, now: datetime) -> list[dict[str, Any]]:
    buckets = {name: 0 for name, _ in AGE_BUCKETS}
    unknown = 0
    for r in rows:
        if not r.is_open:
            continue
        if r.date is None:
            unknown += 1
            continue
        age = (now - r.date).days
        for name, ceiling in AGE_BUCKETS:
            if ceiling is None or age <= ceiling:
                buckets[name] += 1
                break
    out = [{"bucket": name, "count": buckets[name]} for name, _ in AGE_BUCKETS]
    if unknown:
        out.append({"bucket": "No date", "count": unknown})
    return out


def _sla(rows: list[_Row], now: datetime) -> dict[str, Any]:
    overdue = due_soon = no_target = 0
    on_time = late = 0
    for r in rows:
        if r.is_open:
            if r.due is None:
                no_target += 1
            elif r.due < now:
                overdue += 1
            elif r.due <= now + timedelta(days=7):
                due_soon += 1
        elif r.due and r.closed_at:
            if r.closed_at <= r.due:
                on_time += 1
            else:
                late += 1
    return {
        "overdue": overdue,
        "dueIn7": due_soon,
        "openWithoutTarget": no_target,
        "closedOnTime": on_time,
        "closedLate": late,
        "onTimeClosurePct": _pct(on_time, on_time + late),
    }


def _contributors(
    rows: list[_Row], lookups: dict[str, dict[str, str]], now: datetime, limit: int = 8
) -> list[dict[str, Any]]:
    agg: dict[str, dict[str, int]] = defaultdict(lambda: {"open": 0, "total": 0, "overdue": 0})
    for r in rows:
        if not r.owner:
            continue
        a = agg[r.owner]
        a["total"] += 1
        if r.is_open:
            a["open"] += 1
            if r.due and r.due < now:
                a["overdue"] += 1
    ranked = sorted(agg.items(), key=lambda kv: (-kv[1]["open"], -kv[1]["overdue"]))[:limit]
    names = lookups.get("user", {})
    return [
        {"id": uid, "name": names.get(uid) or "Unknown user", **counts}
        for uid, counts in ranked
        if counts["open"] > 0
    ]


def _open_as_of(rows: list[_Row], t: datetime) -> int | None:
    """How many records were open at instant `t` — the comparator for the Open KPI.

    Reconstructed from dates, not from today's status, because status has no
    history. A record counts as open at `t` when it existed by then and had not
    yet been closed by then:

      • closed, with a closure date → open at t iff date <= t < closedAt
      • still open today            → open at t iff date <= t
      • closed, with NO closure date → we cannot know when it left the backlog.
        Those rows are excluded from the reconstruction and reported through
        `dataQuality` instead. Guessing (say, treating them as closed at t)
        would silently understate every historical backlog figure.

    Returns None when the population carries no usable date at all, so the
    caller renders "no prior period data" rather than a confident 0.
    """
    if not any(r.date for r in rows):
        return None
    n = 0
    for r in rows:
        if r.date is None or r.date > t:
            continue
        if r.closed_at is not None:
            if r.closed_at > t:
                n += 1
        elif r.is_open:
            n += 1
    return n


def _data_quality(
    rows: list[_Row],
    spec: FlowSpec,
    *,
    has_closure: bool,
    min_share: float = 0.20,
    metric_values: dict[str, list[Any]] | None = None,
) -> list[dict[str, Any]]:
    """Metrics that are structurally UNCOUNTABLE rather than genuinely zero.

    This is the fix for the single most dangerous class of bug on an analytics
    screen. Incident's Overdue tile read a clean, reassuring **0** — not because
    nothing was late, but because `statutoryDeadline` is null on every incident
    on prod, so lateness cannot be computed at all. A zero and an unmeasurable
    are opposite facts and must never share a rendering.

    Every flag below is computed off the loaded rows; nothing is hardcoded. A
    flag is raised when the missing share reaches `min_share`, or at any share
    when it makes the metric wholly uncountable (`blocking`).
    """
    flags: list[dict[str, Any]] = []
    metric_values = metric_values or {}

    def add(metric, field, incomplete, total, scope, effect):
        if not total or not incomplete:
            return
        share = incomplete / total
        blocking = incomplete >= total
        if share < min_share and not blocking:
            return
        flags.append({
            "metric": metric,
            "field": field,
            "fieldLabel": _field_label(field),
            "incompleteCount": incomplete,
            "totalCount": total,
            "scope": scope,
            "sharePct": round(share * 100, 1),
            "blocking": blocking,
            "reason": (
                f"{incomplete} of {total} {scope} have no {_field_label(field)}, "
                f"so {effect}"
            ),
        })

    open_rows = [r for r in rows if r.is_open]

    # 1. Overdue / target adherence — the flagship case.
    if spec.due_column and open_rows:
        add(
            "Overdue", spec.due_column,
            sum(1 for r in open_rows if r.due is None), len(open_rows),
            "open records",
            "lateness cannot be computed for them. The count shown is not a "
            "count of records that are on time.",
        )

    # 2. Avg days to close / the closed and backlog series.
    if has_closure:
        closed_rows = [r for r in rows if not r.is_open]
        add(
            "Avg days to close", spec.closed_at_column or "closedAt",
            sum(1 for r in closed_rows if r.closed_at is None or r.date is None),
            len(closed_rows),
            "closed records",
            "they are excluded from the average and from the monthly closed bar.",
        )

    # 3. Columns a PUBLISHED metric is derived from. Stricter than a breakdown
    #    dimension and reported at a lower threshold, because the failure is
    #    different in kind: a sparse dimension makes a chart thin, whereas an
    #    empty metric column makes a NUMBER on the screen mean something other
    #    than what it says. CAMS is the live example — `overallCompliancePct` is
    #    null on 13 of 23 audits, so the site benchmarking average is computed
    #    over the minority that happen to carry a score.
    for column, metric, effect in spec.metric_columns:
        vals = metric_values.get(column, [])
        if not vals:
            continue
        add(metric, column,
            sum(1 for v in vals if v in (None, "")), len(vals),
            "records", effect)

    # 4. Any breakdown dimension that is mostly empty. A distribution chart over
    #    a field nobody fills in is a chart about the form, not about safety.
    #
    #    A column already reported above as a metric column is skipped: the same
    #    emptiness stated twice under two names reads as two problems, and the
    #    metric framing ("Pass rate is computed over 14 of 21") is the more
    #    useful of the two.
    metric_fields = {c for c, _l, _e in spec.metric_columns}
    for d in spec.dimensions:
        if d.column in metric_fields:
            continue
        add(
            d.label, d.column,
            sum(1 for r in rows if r.dims.get(d.key) in (None, "")), len(rows),
            "records",
            f"the {d.label.lower()} breakdown and any filter on it cover only "
            f"the records that do carry one.",
        )

    # Blocking first, then by how much of the population is missing.
    flags.sort(key=lambda f: (not f["blocking"], -f["sharePct"]))
    return flags


def _filter_options(
    rows: list[_Row], spec: FlowSpec, lookups: dict[str, dict[str, str]]
) -> dict[str, Any]:
    """Choices for the SegmentBar, derived from the rows the caller may see.

    Computed BEFORE the site and severity narrowing is applied, so choosing one
    site does not empty the dropdown you chose it from. Counts come with the
    options: an option that would yield nothing says so before it is picked.
    """
    plants: Counter[str] = Counter()
    for r in rows:
        if r.plant:
            plants[r.plant] += 1
    names = lookups.get("plant", {})
    site_opts = sorted(
        ({"value": pid, "label": names.get(pid) or "Unknown site", "count": n}
         for pid, n in plants.items()),
        key=lambda o: (-o["count"], o["label"]),
    )

    sev_opts: list[dict[str, Any]] = []
    sev_dim = severity_dimension(spec)
    if sev_dim:
        c: Counter[str] = Counter()
        for r in rows:
            raw = r.dims.get(sev_dim.key)
            v = str(raw) if raw not in (None, "") else NOT_SET
            if v != NOT_SET and spec.upper_values and not sev_dim.lookup:
                v = v.upper()
            c[v] += 1
        order = {v: i for i, v in enumerate(sev_dim.order)}
        sev_opts = [
            {"value": k,
             "label": NOT_SET_LABEL if k == NOT_SET else _titleise(k),
             "count": n}
            for k, n in sorted(
                c.items(),
                key=lambda kv: (kv[0] == NOT_SET, order.get(kv[0], len(order)), -kv[1]),
            )
        ]

    return {
        "sites": site_opts,
        "severities": sev_opts,
        "severityLabel": sev_dim.label if sev_dim else None,
        "severityDimension": sev_dim.key if sev_dim else None,
    }


def _summary(
    rows: list[_Row],
    spec: FlowSpec,
    months: int,
    now: datetime,
    *,
    has_closure: bool,
    win_start: datetime,
    prior_start: datetime,
) -> dict[str, Any]:
    """Headline figures, each with the SAME figure computed over the immediately
    preceding window of equal length.

    Every KPI on an analytics screen is required to carry a comparator, and a
    comparator that is invented is worse than none: the prior-period values here
    are recomputed from the same rows over `[prior_start, win_start)`, never
    scaled, seeded or defaulted. Where the prior window genuinely holds nothing
    to compare against, the value is None and the UI says so out loud.
    """
    opened_win = sum(1 for r in rows if r.date and win_start <= r.date)
    opened_prior = sum(1 for r in rows if r.date and prior_start <= r.date < win_start)
    closed_win = sum(1 for r in rows if r.closed_at and r.closed_at >= win_start)
    closed_prior = sum(
        1 for r in rows if r.closed_at and prior_start <= r.closed_at < win_start
    )

    def avg_close(lo: datetime, hi: datetime | None) -> float | None:
        d = [
            (r.closed_at - r.date).days
            for r in rows
            if r.closed_at and r.date and r.closed_at >= r.date
            and r.closed_at >= lo and (hi is None or r.closed_at < hi)
        ]
        return round(sum(d) / len(d), 1) if (has_closure and d) else None

    open_n = sum(1 for r in rows if r.is_open)
    # Point-in-time backlog at the start of the current window — i.e. what the
    # Open tile would have read a period ago. Reconstructed from dates, since
    # status carries no history. See _open_as_of.
    open_prior = _open_as_of(rows, win_start)

    avg_now, avg_prior = avg_close(win_start, None), avg_close(prior_start, win_start)
    return {
        "total": len(rows),
        "open": open_n,
        "closed": len(rows) - open_n,
        # Comparator for the Open tile: the same count one window earlier.
        "openPrior": open_prior,
        "openDelta": (open_n - open_prior) if open_prior is not None else None,
        "openedInWindow": opened_win,
        "closedInWindow": closed_win,
        "openedPriorWindow": opened_prior,
        "closedPriorWindow": closed_prior,
        # None rather than 0 when there is no prior period to compare against —
        # "no change" and "nothing to compare" are different facts.
        "openedDeltaPct": _pct(opened_win - opened_prior, opened_prior) if opened_prior else None,
        # None, not 0, where the flow records no closure date — the metric is
        # unavailable, which is a different statement from "nothing closes".
        "avgDaysToClose": avg_now,
        "avgDaysToClosePrior": avg_prior,
        "avgDaysToCloseDelta": (
            round(avg_now - avg_prior, 1) if (avg_now is not None and avg_prior is not None) else None
        ),
        "closureRatePct": _pct(closed_win, opened_win) if (has_closure and opened_win) else None,
        "closureRatePriorPct": (
            _pct(closed_prior, opened_prior) if (has_closure and opened_prior) else None
        ),
    }


# ── Entry point ──────────────────────────────────────────────────────────────
def _window(
    now: datetime, months: int, date_from: datetime | None, date_to: datetime | None
) -> tuple[datetime, datetime, datetime, int]:
    """Resolve the analysis window and the equal-length window before it.

    Returns (win_start, win_end, prior_start, months_equivalent). An explicit
    from/to pair wins over the `months` preset; the comparator window is always
    exactly as long as the selected one, so a delta never compares thirty days
    against a year.
    """
    win_end = date_to or now
    if date_from:
        win_start = date_from
    else:
        win_start = win_end - timedelta(days=months * 30)
    if win_start > win_end:
        win_start, win_end = win_end, win_start
    span = max((win_end - win_start).days, 1)
    return win_start, win_end, win_start - timedelta(days=span), max(round(span / 30), 1)


async def compute_flow(
    db: AsyncSession,
    spec: FlowSpec,
    *,
    plant: str | None = None,
    plants_allowed: list[str] | None = None,
    months: int = 12,
    severities: list[str] | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> dict[str, Any]:
    """Compute one flow's analytics under the SegmentBar's three filters.

    The three filters do deliberately different things, and the difference is
    the whole reason the numbers can be trusted:

      • **site** and **severity** narrow the POPULATION. They are identity
        filters — "show me this subset of records" — so every figure on the
        screen is recomputed over that subset.
      • **date range** moves the analysis WINDOW. It governs the trend, the
        opened/closed counts, the closure average and every delta. It does NOT
        drop older records from the backlog: Open, ageing, target adherence and
        the data-quality flags stay point-in-time over the whole population,
        because a backlog figure that quietly forgets everything raised before
        the window is precisely the kind of reassuring, wrong number this layer
        exists to eliminate.
    """
    now = _now()
    win_start, win_end, prior_start, months_eq = _window(now, months, date_from, date_to)

    rows, truncated = await _load(db, spec, plants_allowed=plants_allowed)
    lookups = await _lookups(db, spec, rows)

    # Options come off the un-narrowed population so the controls keep offering
    # every choice the caller is entitled to, including the one they just left.
    filter_options = _filter_options(rows, spec, lookups)

    if plant:
        rows = [r for r in rows if r.plant == plant]
    sev_dim = severity_dimension(spec)
    applied_severities: list[str] = []
    if severities and sev_dim:
        wanted = {
            (v.upper() if (spec.upper_values and not sev_dim.lookup and v != NOT_SET) else v)
            for v in severities
        }
        applied_severities = sorted(wanted)

        def sev_of(r: _Row) -> str:
            raw = r.dims.get(sev_dim.key)
            if raw in (None, ""):
                return NOT_SET
            v = str(raw)
            return v.upper() if (spec.upper_values and not sev_dim.lookup) else v

        rows = [r for r in rows if sev_of(r) in wanted]

    has_closure = spec.closed_at_column is not None and any(r.closed_at for r in rows)

    plant_name = None
    if plant:
        from app.services.plant_directory import resolve_plant_names, site_label

        plant_name = site_label(await resolve_plant_names(db, [plant]), plant)

    return {
        "flow": spec.key,
        "label": spec.label,
        "href": spec.href,
        "plant": plant,
        "plantName": plant_name,
        "months": months_eq,
        "windowStart": win_start.isoformat(),
        "windowEnd": win_end.isoformat(),
        "priorWindowStart": prior_start.isoformat(),
        "severities": applied_severities,
        "filterOptions": filter_options,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "recordCount": len(rows),
        "truncated": truncated,
        "statusDrillParam": spec.status_drill_param,
        # Lets the UI hide the closed/backlog series (and the closure stats)
        # rather than render them as zeroes for a flow that never records one.
        "hasClosureData": has_closure,
        "hasTargetDates": spec.due_column is not None,
        # What this flow's Open tile actually counts — see FlowSpec.open_meaning.
        "openMeaning": spec.open_meaning,
        "summary": _summary(
            rows, spec, months_eq, now,
            has_closure=has_closure, win_start=win_start, prior_start=prior_start,
        ),
        "trend": _trend(rows, months_eq, win_end, has_closure=has_closure),
        "breakdowns": [_breakdown(rows, d, spec, lookups) for d in spec.dimensions],
        "ageing": _ageing(rows, spec, now),
        "sla": _sla(rows, now),
        # Which headline numbers are structurally uncountable rather than good.
        "dataQuality": _data_quality(
            rows, spec, has_closure=has_closure,
            metric_values={
                c: [r.metrics.get(c) for r in rows]
                for c, _l, _e in spec.metric_columns
            },
        ),
        "contributors": _contributors(rows, lookups, now),
        "notes": spec.notes or None,
    }


__all__ = ["MAX_ROWS", "compute_flow", "severity_dimension"]
