"""External-system integration framework (JSL Hisar: "integrate with JSL SAS portal").

The JSL SAS API specification has NOT been supplied yet, so nothing here knows
what SAS expects. Instead the connector is configuration:

  IntegrationEndpoint   one external system + direction + where to call + how
                        to authenticate + how SafeOps fields map onto its
                        fields. Secrets are never stored: `secretEnvVar` /
                        `inboundSecretEnvVar` hold the NAME of an environment
                        variable (INTEGRATION_* only) that holds the secret.
  IntegrationSyncLog    one outbound push or inbound receipt: payload, attempts,
                        back-off, HTTP status, response excerpt, external id.
                        It is both the outbox and the audit trail.

Tables are created by safeops_360/prisma/apply-integrations-ddl.ts (hand DDL,
additive). Statuses are TEXT, not native enums (house rule).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, JSON, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models._base import Base, IdMixin


def _c():
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _u():
    return mapped_column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)


class IntegrationEndpoint(Base, IdMixin):
    __tablename__ = "IntegrationEndpoint"

    code: Mapped[str] = mapped_column(String, unique=True, nullable=False)  # e.g. JSL_SAS
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    entityType: Mapped[str] = mapped_column(String, nullable=False, default="OBSERVATION")
    direction: Mapped[str] = mapped_column(String, nullable=False, default="OUTBOUND")  # OUTBOUND | INBOUND | BOTH
    baseUrl: Mapped[str | None] = mapped_column(String)
    path: Mapped[str | None] = mapped_column(String)
    method: Mapped[str] = mapped_column(String, nullable=False, default="POST")  # POST | PUT | PATCH
    authType: Mapped[str] = mapped_column(String, nullable=False, default="NONE")  # NONE | API_KEY | BEARER | BASIC
    authHeaderName: Mapped[str | None] = mapped_column(String)  # API_KEY header, default X-API-Key
    secretEnvVar: Mapped[str | None] = mapped_column(String)  # NAME of env var with the outbound secret
    inboundSecretEnvVar: Mapped[str | None] = mapped_column(String)  # NAME of env var with the inbound API key
    headers: Mapped[dict | None] = mapped_column(JSON)  # extra static headers (no secrets)
    fieldMapping: Mapped[list | None] = mapped_column(JSON)  # [{source, target, transform, arg, direction}]
    triggerEvents: Mapped[list | None] = mapped_column(JSON)  # ["observation.created", "observation.status_changed"]
    plantIds: Mapped[list | None] = mapped_column(JSON)  # null = every plant
    responseIdPath: Mapped[str | None] = mapped_column(String)  # where the external id sits in the response JSON
    timeoutSeconds: Mapped[int] = mapped_column(Integer, nullable=False, default=15)
    serviceUserId: Mapped[str | None] = mapped_column(String)  # identity inbound records are created under
    defaultPlantId: Mapped[str | None] = mapped_column(String)  # inbound plant when the payload names none
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dryRun: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    cursorAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # change-capture watermark
    lastTestAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lastTestOk: Mapped[bool | None] = mapped_column(Boolean)
    lastTestMessage: Mapped[str | None] = mapped_column(Text)
    isDeleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    createdAt: Mapped[datetime] = _c()
    createdBy: Mapped[str | None] = mapped_column(String)
    updatedAt: Mapped[datetime] = _u()
    updatedBy: Mapped[str | None] = mapped_column(String)


class IntegrationSyncLog(Base, IdMixin):
    __tablename__ = "IntegrationSyncLog"

    endpointId: Mapped[str] = mapped_column(String, nullable=False)
    entityType: Mapped[str] = mapped_column(String, nullable=False)  # OBSERVATION
    entityId: Mapped[str | None] = mapped_column(String)
    entityRef: Mapped[str | None] = mapped_column(String)  # human number, e.g. SO-2026-HSR-00042
    eventType: Mapped[str | None] = mapped_column(String)  # observation.created | observation.status_changed | inbound.create
    entityStatus: Mapped[str | None] = mapped_column(String)  # record status the event describes
    direction: Mapped[str] = mapped_column(String, nullable=False)  # OUTBOUND | INBOUND
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")  # PENDING | SENT | FAILED | SKIPPED | RECEIVED
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    nextAttemptAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lastAttemptAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    httpStatus: Mapped[int | None] = mapped_column(Integer)
    responseExcerpt: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict | None] = mapped_column(JSON)
    externalId: Mapped[str | None] = mapped_column(String)
    # One row per (endpoint, state version) outbound and per (endpoint, externalId)
    # for a received inbound record. NULL on failed inbound attempts so a retry
    # by the sender is never blocked by its own earlier failure.
    dedupeKey: Mapped[str | None] = mapped_column(String)
    sentAt: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    createdAt: Mapped[datetime] = _c()
    updatedAt: Mapped[datetime] = _u()

    __table_args__ = (
        Index("ix_IntegrationSyncLog_due", "status", "nextAttemptAt"),
        Index("ix_IntegrationSyncLog_endpoint_created", "endpointId", "createdAt"),
        Index("ix_IntegrationSyncLog_entity", "entityType", "entityId"),
        Index(
            "uq_IntegrationSyncLog_endpoint_dedupe", "endpointId", "dedupeKey", unique=True,
            postgresql_where=text('"dedupeKey" IS NOT NULL'), sqlite_where=text('"dedupeKey" IS NOT NULL'),
        ),
    )


__all__ = ["IntegrationEndpoint", "IntegrationSyncLog"]
