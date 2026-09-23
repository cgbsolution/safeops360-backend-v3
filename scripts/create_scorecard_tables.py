"""Create the EHS Scorecard table (hand DDL, idempotent).

Backend-only, like Signal / InsightSnapshot: deliberately NOT in schema.prisma,
which would put it in reach of `prisma db push` — that drops hand-DDL tables.

    .venv/Scripts/python.exe -m scripts.create_scorecard_tables
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import get_settings

DDL = [
    """
    CREATE TABLE IF NOT EXISTS "ScorecardPeriod" (
        id                          TEXT PRIMARY KEY,
        "tenantId"                  TEXT NOT NULL DEFAULT 'default',
        "siteId"                    TEXT NOT NULL,
        year                        INTEGER NOT NULL,
        month                       INTEGER NOT NULL,
        period                      TEXT NOT NULL,

        "employeeHours"             DOUBLE PRECISION,
        "contractorHours"           DOUBLE PRECISION,
        "totalHours"                DOUBLE PRECISION,
        headcount                   INTEGER,

        "observationsLogged"        INTEGER NOT NULL DEFAULT 0,
        "nearMissReported"          INTEGER NOT NULL DEFAULT 0,
        "ptwIssued"                 INTEGER NOT NULL DEFAULT 0,
        "ptwClosedProperly"         INTEGER NOT NULL DEFAULT 0,
        "ptwCompliancePct"          DOUBLE PRECISION,
        "trainingAssigned"          INTEGER NOT NULL DEFAULT 0,
        "trainingCompleted"         INTEGER NOT NULL DEFAULT 0,
        "trainingCompletionPct"     DOUBLE PRECISION,
        "inductionsConducted"       INTEGER NOT NULL DEFAULT 0,
        "leadershipWalksPlanned"    INTEGER NOT NULL DEFAULT 0,
        "leadershipWalksCompleted"  INTEGER NOT NULL DEFAULT 0,
        "leadershipWalksCompletedPct" DOUBLE PRECISION,
        "cultureStageScore"         DOUBLE PRECISION,
        "perceptionScore"           DOUBLE PRECISION,

        "incidentsTotal"            INTEGER NOT NULL DEFAULT 0,
        "ltiCount"                  INTEGER NOT NULL DEFAULT 0,
        "recordableCount"           INTEGER NOT NULL DEFAULT 0,
        "firstAidCount"             INTEGER NOT NULL DEFAULT 0,
        "highSeverityCount"         INTEGER NOT NULL DEFAULT 0,
        "lostDays"                  INTEGER NOT NULL DEFAULT 0,
        ltifr                       DOUBLE PRECISION,
        trir                        DOUBLE PRECISION,
        "severityRate"              DOUBLE PRECISION,

        gaps                        JSONB,
        sources                     JSONB,
        "computedAt"                TIMESTAMPTZ NOT NULL DEFAULT now(),
        notes                       TEXT,
        "createdAt"                 TIMESTAMPTZ NOT NULL DEFAULT now(),
        "updatedAt"                 TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # Identity of a plant-month. The rollup upserts on this, so a re-run
    # refreshes rather than duplicating — and a duplicated plant-month would
    # double every quarterly figure derived from it.
    'CREATE UNIQUE INDEX IF NOT EXISTS "uq_scorecard_site_period" '
    'ON "ScorecardPeriod" ("tenantId", "siteId", year, month)',
    'CREATE INDEX IF NOT EXISTS "ix_scorecard_period_site" '
    'ON "ScorecardPeriod" ("tenantId", period, "siteId")',
]

TABLES = ["ScorecardPeriod"]


def _present(s: Session) -> set[str]:
    return set(
        s.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name = ANY(:n)"
            ),
            {"n": TABLES},
        ).scalars().all()
    )


def main() -> int:
    engine = create_engine(get_settings().sync_database_url, future=True)
    with Session(engine) as s:
        before = _present(s)
        for stmt in DDL:
            s.execute(text(stmt))
        s.commit()
        after = _present(s)
        print(f"Created: {sorted(after - before) or '(all existed)'} | present: {sorted(after)}")
        return 0 if set(TABLES) <= after else 1


if __name__ == "__main__":
    raise SystemExit(main())
