"""Point-in-time fundamentals and daily market value (task 11).

Both tables are add-only for the app role, like prices: what a provider said stays intact, and a
later correction arrives as a review note, not an overwrite.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AMOUNT = sa.Numeric(24, 4)
AMOUNT_COLUMNS = (
    "debt",
    "debt_current",
    "debt_noncurrent",
    "cash_and_equivalents",
    "investments",
    "investments_current",
    "investments_noncurrent",
    "receivables",
    "assets",
    "revenue",
    "interest_expense",
    "ebit",
    "ebt",
    "operating_income",
    "net_income",
    "market_cap",
    "shares_basic",
    "shares_weighted_avg",
    "price",
)


def upgrade() -> None:
    op.create_table(
        "fundamental",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("security_id", sa.BigInteger(), nullable=False),
        sa.Column("dimension", sa.Text(), nullable=False),
        sa.Column("calendar_date", sa.Date(), nullable=False),
        sa.Column("filing_date", sa.Date(), nullable=False),
        sa.Column("report_period", sa.Date()),
        sa.Column("fiscal_period", sa.Text()),
        *(sa.Column(name, AMOUNT) for name in AMOUNT_COLUMNS),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("data_version", sa.Text(), nullable=False),
        sa.Column(
            "imported_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["hq.security.security_id"],
            name="fk_fundamental_security",
        ),
        sa.UniqueConstraint(
            "security_id", "dimension", "calendar_date", "filing_date", name="uq_fundamental_filing"
        ),
        sa.CheckConstraint("dimension IN ('ARQ', 'ARY', 'ART')", name="ck_fundamental_dimension"),
        sa.CheckConstraint("source <> '' AND data_version <> ''", name="ck_fundamental_lineage"),
        schema="hq",
    )
    op.create_index(
        "ix_fundamental_lookup",
        "fundamental",
        ["security_id", "dimension", "filing_date"],
        schema="hq",
    )
    op.create_table(
        "daily_market_cap",
        sa.Column("security_id", sa.BigInteger(), nullable=False),
        sa.Column("cap_date", sa.Date(), nullable=False),
        sa.Column("market_cap_usd", AMOUNT, nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("data_version", sa.Text(), nullable=False),
        sa.Column(
            "imported_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["hq.security.security_id"],
            name="fk_daily_market_cap_security",
        ),
        sa.PrimaryKeyConstraint("security_id", "cap_date", name="pk_daily_market_cap"),
        sa.CheckConstraint(
            "source <> '' AND data_version <> ''", name="ck_daily_market_cap_lineage"
        ),
        schema="hq",
    )
    op.create_index("ix_daily_market_cap_cap_date", "daily_market_cap", ["cap_date"], schema="hq")
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON hq.fundamental FROM hq_app")
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON hq.daily_market_cap FROM hq_app")


def downgrade() -> None:
    op.drop_table("daily_market_cap", schema="hq")
    op.drop_table("fundamental", schema="hq")
