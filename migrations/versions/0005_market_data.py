"""Market data model: daily prices and corporate actions (PRD §7, BRD §10).

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRICE = sa.Numeric(19, 6)
ACTION_VALUE = sa.Numeric(19, 8)


def upgrade() -> None:
    op.create_table(
        "daily_price",
        sa.Column("security_id", sa.BigInteger(), nullable=False),
        sa.Column("price_date", sa.Date(), nullable=False),
        sa.Column("open", PRICE, nullable=False),
        sa.Column("high", PRICE, nullable=False),
        sa.Column("low", PRICE, nullable=False),
        sa.Column("close", PRICE, nullable=False),
        sa.Column("adjusted_close", PRICE, nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("data_version", sa.Text(), nullable=False),
        sa.Column(
            "imported_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("security_id", "price_date", name="pk_daily_price"),
        sa.ForeignKeyConstraint(
            ["security_id"], ["hq.security.security_id"], name="fk_daily_price_security"
        ),
        sa.CheckConstraint(
            "open > 0 AND high > 0 AND low > 0 AND close > 0", name="ck_daily_price_ohlc"
        ),
        sa.CheckConstraint("adjusted_close > 0", name="ck_daily_price_adjusted_close"),
        sa.CheckConstraint("volume >= 0", name="ck_daily_price_volume"),
        sa.CheckConstraint("source <> '' AND data_version <> ''", name="ck_daily_price_lineage"),
        schema="hq",
    )
    op.create_index("ix_daily_price_price_date", "daily_price", ["price_date"], schema="hq")

    op.create_table(
        "corporate_action",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("security_id", sa.BigInteger(), nullable=False),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("value", ACTION_VALUE),
        sa.Column("related_security_id", sa.BigInteger()),
        sa.Column("details", postgresql.JSONB()),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("data_version", sa.Text(), nullable=False),
        sa.Column(
            "imported_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["security_id"], ["hq.security.security_id"], name="fk_corporate_action_security"
        ),
        sa.ForeignKeyConstraint(
            ["related_security_id"],
            ["hq.security.security_id"],
            name="fk_corporate_action_related_security",
        ),
        sa.CheckConstraint(
            "action_type IN ('SPLIT', 'DIVIDEND', 'MERGER', 'SYMBOL_CHANGE', 'DELISTING')",
            name="ck_corporate_action_type",
        ),
        sa.CheckConstraint(
            "action_type NOT IN ('SPLIT', 'DIVIDEND') OR (value IS NOT NULL AND value > 0)",
            name="ck_corporate_action_value",
        ),
        sa.CheckConstraint("value IS NULL OR value >= 0", name="ck_corporate_action_value_sign"),
        sa.CheckConstraint(
            "source <> '' AND data_version <> ''", name="ck_corporate_action_lineage"
        ),
        sa.UniqueConstraint(
            "security_id",
            "action_type",
            "effective_date",
            "value",
            name="uq_corporate_action_event",
            postgresql_nulls_not_distinct=True,
        ),
        schema="hq",
    )
    op.create_index(
        "ix_corporate_action_security_date",
        "corporate_action",
        ["security_id", "effective_date"],
        schema="hq",
    )


def downgrade() -> None:
    op.drop_table("corporate_action", schema="hq")
    op.drop_table("daily_price", schema="hq")
