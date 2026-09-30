"""Index membership: Sharadar's S&P 500 constituents table, stored as delivered (task 9).

Rows are only ever added: the app role can insert and read but not change or delete, so the
history of what a provider said stays intact. A corrected row arrives as a review note, not an
overwrite.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "index_membership",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("index_name", sa.Text(), nullable=False),
        sa.Column("record_date", sa.Date(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("company_name", sa.Text()),
        sa.Column("contra_ticker", sa.Text()),
        sa.Column("contra_name", sa.Text()),
        sa.Column("note", sa.Text()),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("data_version", sa.Text(), nullable=False),
        sa.Column(
            "imported_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "index_name", "record_date", "action", "ticker", name="uq_index_membership_record"
        ),
        schema="hq",
    )
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON hq.index_membership FROM hq_app")


def downgrade() -> None:
    op.drop_table("index_membership", schema="hq")
