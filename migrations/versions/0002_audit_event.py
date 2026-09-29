"""Append-only audit_event table (PRD §30, BRD §15).

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_event",
        sa.Column("event_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "sequence", sa.BigInteger(), sa.Identity(always=True), nullable=False, unique=True
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text()),
        sa.Column("old_value", postgresql.JSONB()),
        sa.Column("new_value", postgresql.JSONB()),
        sa.Column("reason", sa.Text()),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column("details", postgresql.JSONB()),
        sa.CheckConstraint("actor <> ''", name="ck_audit_event_actor"),
        sa.CheckConstraint("action <> ''", name="ck_audit_event_action"),
        sa.CheckConstraint("entity_type <> ''", name="ck_audit_event_entity_type"),
        sa.CheckConstraint("source <> ''", name="ck_audit_event_source"),
        sa.CheckConstraint("correlation_id <> ''", name="ck_audit_event_correlation_id"),
        schema="hq",
    )
    op.create_index("ix_audit_event_occurred_at", "audit_event", ["occurred_at"], schema="hq")
    op.create_index("ix_audit_event_correlation_id", "audit_event", ["correlation_id"], schema="hq")
    op.create_index(
        "ix_audit_event_entity", "audit_event", ["entity_type", "entity_id"], schema="hq"
    )

    # Layer 1: the app role may only add and read events.
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON hq.audit_event FROM hq_app")

    # Layer 2: nobody, not even the table owner, can change or remove an event.
    op.execute(
        """
        CREATE FUNCTION hq.audit_event_reject_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit_event is append-only: % is not allowed', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER audit_event_no_update_delete BEFORE UPDATE OR DELETE ON hq.audit_event"
        " FOR EACH ROW EXECUTE FUNCTION hq.audit_event_reject_change()"
    )
    op.execute(
        "CREATE TRIGGER audit_event_no_truncate BEFORE TRUNCATE ON hq.audit_event"
        " FOR EACH STATEMENT EXECUTE FUNCTION hq.audit_event_reject_change()"
    )


def downgrade() -> None:
    op.drop_table("audit_event", schema="hq")  # drops its triggers and indexes too
    op.execute("DROP FUNCTION hq.audit_event_reject_change()")
