"""Pydantic schemas for the Form & Workflow Engine.

The deliberate asymmetry here: field *schemas* are typed loosely (`list[Any]`)
and validated by `services/form_engine/schema.py`, not by pydantic. Two reasons.
A pydantic model of the field grammar would have to be kept in lockstep with the
publish gate, and the gate is what the Builder calls — so a mismatch would let
the API accept a definition the Builder rejects, or worse the reverse. And the
gate returns EVERY error at once with field keys attached, which is the
authoring loop the Builder needs; pydantic stops at the shape.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

DefinitionStatus = Literal["DRAFT", "PUBLISHED", "ARCHIVED"]
RecordStatus = Literal["DRAFT", "SUBMITTED", "IN_REVIEW", "APPROVED", "REJECTED", "CLOSED"]


# ── Definitions ─────────────────────────────────────────────────────────────


class DefinitionCreate(BaseModel):
    key: str = Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    title: str = Field(min_length=1, max_length=200)
    module: str = Field(min_length=1, max_length=64)
    description: str | None = None
    schemaJson: list[Any] | None = None
    uiSchemaJson: dict[str, Any] | None = None
    workflowModule: str | None = None
    workflowRecordType: str | None = None
    numberPattern: str | None = None
    storageBinding: dict[str, Any] | None = None
    orgScope: dict[str, Any] | None = None
    permissionPrefix: str = "FORMS"


class DefinitionUpdate(BaseModel):
    """Every field optional — a PATCH. Only a DRAFT accepts one; editing a
    PUBLISHED definition goes through POST /{key}/new-version instead."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    schemaJson: list[Any] | None = None
    uiSchemaJson: dict[str, Any] | None = None
    workflowModule: str | None = None
    workflowRecordType: str | None = None
    numberPattern: str | None = None
    storageBinding: dict[str, Any] | None = None
    orgScope: dict[str, Any] | None = None
    permissionPrefix: str | None = None


class DefinitionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    key: str
    version: int
    status: DefinitionStatus
    title: str
    description: str | None
    module: str
    schemaJson: list[Any] | None
    uiSchemaJson: dict[str, Any] | None
    workflowModule: str | None
    workflowRecordType: str | None
    numberPattern: str | None
    storageBinding: dict[str, Any] | None
    orgScope: dict[str, Any] | None
    permissionPrefix: str
    createdById: str | None
    createdAt: datetime
    updatedAt: datetime
    publishedById: str | None
    publishedAt: datetime | None


class DefinitionSummary(BaseModel):
    """Register-row shape. Carries the published version's identity so a list
    screen never has to fetch each definition to know what to render."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    key: str
    version: int
    status: DefinitionStatus
    title: str
    module: str
    workflowModule: str | None
    recordCount: int = 0
    updatedAt: datetime


# ── Records ─────────────────────────────────────────────────────────────────


class RecordCreate(BaseModel):
    definitionKey: str
    siteId: str
    areaId: str | None = None
    data: dict[str, Any] | None = None
    # Create-and-submit in one call. The wizard's last step uses it so a
    # submitted record never exists as an orphan draft if the second call fails.
    submit: bool = False


class RecordUpdate(BaseModel):
    data: dict[str, Any] | None = None
    areaId: str | None = None


class RecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    definitionId: str
    definitionKey: str
    formVersion: int
    module: str
    siteId: str
    siteName: str | None
    areaId: str | None
    dataJson: dict[str, Any] | None
    computedJson: dict[str, Any] | None
    status: RecordStatus
    referenceNo: str | None
    workflowInstanceId: str | None
    createdById: str
    createdAt: datetime
    updatedById: str | None
    updatedAt: datetime
    submittedById: str | None
    submittedAt: datetime | None
    closedAt: datetime | None


class RecordDetail(RecordOut):
    """Detail view. `definition` is the PINNED version's schema, not the latest
    published one — the renderer must draw the form the record was created
    under or a v2 record would render against a v3 layout."""

    definition: DefinitionOut
    # Required visible fields still blank. Lets the UI show submit-readiness
    # without the user pressing submit to find out.
    missingRequired: list[str] = []
    canEdit: bool = True


class RecordListOut(BaseModel):
    items: list[RecordOut]
    total: int


class SubmitOut(BaseModel):
    ok: bool = True
    recordId: str
    referenceNo: str | None
    status: RecordStatus
    workflowInstanceId: str | None
