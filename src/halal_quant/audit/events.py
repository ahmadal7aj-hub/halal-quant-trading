"""Write audit events: one row per material action, never changed afterwards.

The table is append-only at two levels (migration 0002):
- the app role has INSERT and SELECT only (no UPDATE, DELETE or TRUNCATE);
- triggers reject UPDATE, DELETE and TRUNCATE for every role, including the table owner.

`record_event` writes on the caller's connection, so the event commits or rolls back together
with the action it describes.
"""

import logging
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Connection,
    DateTime,
    Identity,
    Index,
    Table,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

from halal_quant.core.logging import SecretMasker, get_correlation_id
from halal_quant.db.engine import metadata

log = logging.getLogger(__name__)

audit_event_table = Table(
    "audit_event",
    metadata,
    Column("event_id", Uuid, primary_key=True),
    Column("sequence", BigInteger, Identity(always=True), nullable=False, unique=True),
    Column("occurred_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("actor", Text, nullable=False),
    Column("action", Text, nullable=False),
    Column("entity_type", Text, nullable=False),
    Column("entity_id", Text),
    Column("old_value", JSONB),
    Column("new_value", JSONB),
    Column("reason", Text),
    Column("source", Text, nullable=False),
    Column("correlation_id", Text, nullable=False),
    Column("details", JSONB),
    CheckConstraint("actor <> ''", name="ck_audit_event_actor"),
    CheckConstraint("action <> ''", name="ck_audit_event_action"),
    CheckConstraint("entity_type <> ''", name="ck_audit_event_entity_type"),
    CheckConstraint("source <> ''", name="ck_audit_event_source"),
    CheckConstraint("correlation_id <> ''", name="ck_audit_event_correlation_id"),
    Index("ix_audit_event_occurred_at", "occurred_at"),
    Index("ix_audit_event_correlation_id", "correlation_id"),
    Index("ix_audit_event_entity", "entity_type", "entity_id"),
)


class AuditEvent(BaseModel):
    """One material action. `details` holds the BRD §15 extras (versions, inputs, outputs,
    decision, approval, broker response, error information)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    actor: str = Field(min_length=1)  # "owner", or "system:<component>"
    action: str = Field(min_length=1)  # e.g. "config.changed", "import.completed"
    entity_type: str = Field(min_length=1)
    entity_id: str | None = None
    old_value: Any = None
    new_value: Any = None
    reason: str | None = None
    source: str = Field(min_length=1)  # module or command that did the action
    details: dict[str, Any] | None = None


def _mask(value: Any, masker: SecretMasker) -> Any:
    if isinstance(value, str):
        return masker.mask(value)
    if isinstance(value, dict):
        return {masker.mask(str(k)): _mask(v, masker) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask(v, masker) for v in value]
    return value


def record_event(
    conn: Connection, event: AuditEvent, masker: SecretMasker | None = None
) -> uuid.UUID:
    """Insert one audit event and return its ID. Uses the current correlation ID, or a new one."""
    event_id = uuid.uuid4()
    correlation_id = get_correlation_id() or uuid.uuid4().hex
    # mode="json" turns Decimal, datetime, UUID etc. into JSON-safe values.
    row: dict[str, Any] = event.model_dump(mode="json")
    if masker is not None:
        row = _mask(row, masker)
    conn.execute(
        audit_event_table.insert().values(event_id=event_id, correlation_id=correlation_id, **row)
    )
    log.info(
        "audit event recorded",
        extra={"event_id": str(event_id), "audit_action": event.action},
    )
    return event_id
