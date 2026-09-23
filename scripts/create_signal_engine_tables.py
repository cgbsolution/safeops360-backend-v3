"""One-off: create the Signal Engine tables (Signal Engine spec §2.1, Stream 1).

Backend-only tables (not in schema.prisma) — reached only through FastAPI, the
same policy as InsightSnapshot and Attachment. Adding them to schema.prisma
would put them in reach of `prisma db push`, which drops hand-DDL tables.

Idempotent (CREATE TABLE / INDEX IF NOT EXISTS). Run from safeops_360_bakend:

    python -m scripts.create_signal_engine_tables

Until this runs, every Signal Engine endpoint 500s and the nightly job logs a
failed run — it does NOT degrade silently, by design: a data-quality engine that
quietly does nothing is the exact failure mode it exists to detect.
"""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.core.config import get_settings

DDL = [
    """
    CREATE TABLE IF NOT EXISTS "SignalRule" (
        "id" TEXT PRIMARY KEY,
        "code" TEXT NOT NULL UNIQUE,
        "name" TEXT NOT NULL,
        "description" TEXT NOT NULL DEFAULT '',
        "category" TEXT NOT NULL,
        "defaultSeverity" TEXT NOT NULL DEFAULT 'INFO',
        "sourceModules" JSONB,
        "windowDays" INTEGER NOT NULL DEFAULT 30,
        "defaultThresholds" JSONB,
        "enabled" BOOLEAN NOT NULL DEFAULT true,
        "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS "SignalRuleOverride" (
        "id" TEXT PRIMARY KEY,
        "tenantId" TEXT NOT NULL DEFAULT 'default',
        "ruleId" TEXT NOT NULL REFERENCES "SignalRule"("id") ON DELETE CASCADE,
        "enabled" BOOLEAN,
        "thresholdJson" JSONB,
        "severityOverride" TEXT,
        "updatedBy" TEXT,
        "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    'CREATE UNIQUE INDEX IF NOT EXISTS "ux_SignalRuleOverride_tenant_rule" '
    'ON "SignalRuleOverride" ("tenantId", "ruleId")',
    'CREATE INDEX IF NOT EXISTS "ix_SignalRuleOverride_tenant" ON "SignalRuleOverride" ("tenantId")',
    """
    CREATE TABLE IF NOT EXISTS "Signal" (
        "id" TEXT PRIMARY KEY,
        "tenantId" TEXT NOT NULL DEFAULT 'default',
        "siteId" TEXT,
        "areaId" TEXT,
        "ruleId" TEXT NOT NULL REFERENCES "SignalRule"("id"),
        "ruleCode" TEXT NOT NULL,
        "signalKey" TEXT NOT NULL,
        "severity" TEXT NOT NULL DEFAULT 'INFO',
        "status" TEXT NOT NULL DEFAULT 'OPEN',
        "category" TEXT NOT NULL,
        "confidence" DOUBLE PRECISION NOT NULL DEFAULT 0,
        "narrativeTemplate" TEXT NOT NULL,
        "narrativeLLM" TEXT,
        "recommendedAction" TEXT NOT NULL,
        "windowStart" TIMESTAMPTZ NOT NULL,
        "windowEnd" TIMESTAMPTZ NOT NULL,
        "thresholdSnapshot" JSONB,
        "computedAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "firstSeenAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "lastSeenAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "occurrenceCount" INTEGER NOT NULL DEFAULT 1,
        "expiresAt" TIMESTAMPTZ,
        "acknowledgedBy" TEXT,
        "acknowledgedAt" TIMESTAMPTZ,
        "dismissedBy" TEXT,
        "dismissedAt" TIMESTAMPTZ,
        "dismissedReason" TEXT,
        "linkedCapaId" TEXT,
        "linkedTrainingId" TEXT,
        "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "updatedAt" TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    # The identity that makes re-runs idempotent. Deliberately excludes
    # windowStart — see app/models/signal_engine.py, deviation 3.
    'CREATE UNIQUE INDEX IF NOT EXISTS "ux_Signal_identity" '
    'ON "Signal" ("tenantId", "ruleCode", "signalKey")',
    'CREATE INDEX IF NOT EXISTS "ix_Signal_feed" ON "Signal" ("tenantId", "siteId", "status", "severity")',
    'CREATE INDEX IF NOT EXISTS "ix_Signal_rule_window" ON "Signal" ("tenantId", "ruleCode", "windowStart")',
    'CREATE INDEX IF NOT EXISTS "ix_Signal_category_status" ON "Signal" ("tenantId", "category", "status")',
    'CREATE INDEX IF NOT EXISTS "ix_Signal_tenant" ON "Signal" ("tenantId")',
    'CREATE INDEX IF NOT EXISTS "ix_Signal_ruleId" ON "Signal" ("ruleId")',
    'CREATE INDEX IF NOT EXISTS "ix_Signal_ruleCode" ON "Signal" ("ruleCode")',
    """
    CREATE TABLE IF NOT EXISTS "SignalEvidence" (
        "id" TEXT PRIMARY KEY,
        "signalId" TEXT NOT NULL REFERENCES "Signal"("id") ON DELETE CASCADE,
        "sourceModule" TEXT NOT NULL,
        "sourceRecordId" TEXT NOT NULL,
        "sourceRecordRef" TEXT,
        "weight" DOUBLE PRECISION NOT NULL DEFAULT 1,
        "snapshotJson" JSONB,
        "createdAt" TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    'CREATE INDEX IF NOT EXISTS "ix_SignalEvidence_signal" ON "SignalEvidence" ("signalId")',
    'CREATE INDEX IF NOT EXISTS "ix_SignalEvidence_source" ON "SignalEvidence" ("sourceModule", "sourceRecordId")',
    """
    CREATE TABLE IF NOT EXISTS "SignalRunLog" (
        "id" TEXT PRIMARY KEY,
        "tenantId" TEXT NOT NULL DEFAULT 'default',
        "runType" TEXT NOT NULL DEFAULT 'SCHEDULED',
        "triggerEvent" TEXT,
        "startedAt" TIMESTAMPTZ NOT NULL DEFAULT now(),
        "completedAt" TIMESTAMPTZ,
        "durationMs" INTEGER,
        "rulesRun" INTEGER NOT NULL DEFAULT 0,
        "signalsEmitted" INTEGER NOT NULL DEFAULT 0,
        "signalsUpdated" INTEGER NOT NULL DEFAULT 0,
        "signalsExpired" INTEGER NOT NULL DEFAULT 0,
        "errorCount" INTEGER NOT NULL DEFAULT 0,
        "errorDetail" JSONB,
        "ruleDetail" JSONB
    )
    """,
    'CREATE INDEX IF NOT EXISTS "ix_SignalRunLog_tenant_started" ON "SignalRunLog" ("tenantId", "startedAt")',
    'CREATE INDEX IF NOT EXISTS "ix_SignalRunLog_tenant" ON "SignalRunLog" ("tenantId")',
]

# Additive migrations applied after the CREATEs. Kept separate and IF NOT EXISTS
# so this script stays idempotent and safe to re-run against a database that
# already has the tables — which is the normal case now that Stream 1's DDL is
# live in prod.
ALTERS = [
    # Stream 1 (rule expansion): the rule's computation CLASS, distinct from the
    # business `category` it already carries. See app/models/signal_engine.py.
    ('ALTER TABLE "SignalRule" ADD COLUMN IF NOT EXISTS "ruleClass" '
     "TEXT NOT NULL DEFAULT 'CORRELATION'"),
]

TABLES = ["Signal", "SignalEvidence", "SignalRule", "SignalRuleOverride", "SignalRunLog"]


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
        for stmt in ALTERS:
            s.execute(text(stmt))
        s.commit()
        after = _present(s)
        print(f"Created: {sorted(after - before) or '(all existed)'} | present: {sorted(after)}")
        return 0 if set(TABLES) <= after else 1


if __name__ == "__main__":
    raise SystemExit(main())
