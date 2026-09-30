"""Sharia classification records (task 13; PRD §6).

One record per security, provider, methodology and screening date. A record says which status
applies from the next trading day after the screening date through the next screening date, and
why. Records are add-only for the app role: a new methodology version adds records and rewrites
nothing (BRD §9).

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "classification",
        sa.Column("classification_id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("security_id", sa.BigInteger(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("methodology", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=False),
        sa.Column("screening_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        sa.Column("source_reference", postgresql.JSONB(), nullable=False),
        sa.Column(
            "imported_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["security_id"], ["hq.security.security_id"], name="fk_classification_security"
        ),
        sa.UniqueConstraint(
            "security_id",
            "provider",
            "methodology",
            "screening_date",
            name="uq_classification_screen",
        ),
        sa.CheckConstraint(
            "status IN ('HALAL', 'NON_HALAL', 'UNKNOWN', 'PENDING_REVIEW')",
            name="ck_classification_status",
        ),
        sa.CheckConstraint(
            "effective_from > screening_date", name="ck_classification_after_screening"
        ),
        sa.CheckConstraint("effective_to >= effective_from", name="ck_classification_period"),
        schema="hq",
    )
    op.create_index(
        "ix_classification_lookup",
        "classification",
        ["security_id", "methodology", "effective_from"],
        schema="hq",
    )
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON hq.classification FROM hq_app")


def downgrade() -> None:
    op.drop_table("classification", schema="hq")
