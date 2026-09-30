"""Data-quality runs and findings (task 12).

A run records the range checked and the thresholds used; a finding names the security, date and
problem. Both are add-only for the app role: a finding is history, and a later run adds new rows
instead of rewriting old ones. The universe builder (task 14) reads findings to fail closed.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "data_quality_run",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("first_date", sa.Date(), nullable=False),
        sa.Column("last_date", sa.Date(), nullable=False),
        sa.Column("as_of", sa.Date()),
        sa.Column("thresholds", postgresql.JSONB(), nullable=False),
        sa.Column(
            "checked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        schema="hq",
    )
    op.create_table(
        "data_quality_finding",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("security_id", sa.BigInteger(), nullable=False),
        sa.Column("check_name", sa.Text(), nullable=False),
        sa.Column("data_date", sa.Date(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["hq.data_quality_run.id"], name="fk_data_quality_finding_run"
        ),
        sa.ForeignKeyConstraint(
            ["security_id"], ["hq.security.security_id"], name="fk_data_quality_finding_security"
        ),
        schema="hq",
    )
    op.create_index(
        "ix_data_quality_finding_lookup",
        "data_quality_finding",
        ["security_id", "data_date"],
        schema="hq",
    )
    op.create_index(
        "ix_data_quality_finding_run",
        "data_quality_finding",
        ["run_id", "check_name"],
        schema="hq",
    )
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON hq.data_quality_run FROM hq_app")
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON hq.data_quality_finding FROM hq_app")


def downgrade() -> None:
    op.drop_table("data_quality_finding", schema="hq")
    op.drop_table("data_quality_run", schema="hq")
