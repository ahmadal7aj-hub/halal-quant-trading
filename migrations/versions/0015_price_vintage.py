"""Price vintages: one consistent download of every price series (Phase 2; G8 OI-16).

`price_vintage` is the register (building -> complete); `vintage_price` and
`vintage_benchmark_price` hold the rows. Rows are add-only for the application role. The
register may only have its completion columns updated, through column-level UPDATE.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRICE = sa.Numeric(30, 6)


def upgrade() -> None:
    op.create_table(
        "price_vintage",
        sa.Column("vintage_id", sa.Text(), primary_key=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("stock_rows", sa.BigInteger()),
        sa.Column("benchmark_rows", sa.BigInteger()),
        sa.CheckConstraint("status IN ('building', 'complete')", name="ck_price_vintage_status"),
        sa.CheckConstraint("vintage_id <> ''", name="ck_price_vintage_id"),
        schema="hq",
    )
    op.create_table(
        "vintage_price",
        sa.Column("vintage_id", sa.Text(), nullable=False),
        sa.Column("security_id", sa.BigInteger(), nullable=False),
        sa.Column("price_date", sa.Date(), nullable=False),
        sa.Column("close_unadjusted", PRICE, nullable=False),
        sa.Column("adjusted_close", PRICE, nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("vintage_id", "security_id", "price_date", name="pk_vintage_price"),
        sa.ForeignKeyConstraint(
            ["vintage_id"], ["hq.price_vintage.vintage_id"], name="fk_vintage_price_vintage"
        ),
        sa.ForeignKeyConstraint(
            ["security_id"], ["hq.security.security_id"], name="fk_vintage_price_security"
        ),
        sa.CheckConstraint(
            "close_unadjusted > 0 AND adjusted_close > 0", name="ck_vintage_price_positive"
        ),
        sa.CheckConstraint("volume >= 0", name="ck_vintage_price_volume"),
        schema="hq",
    )
    op.create_index(
        "ix_vintage_price_date", "vintage_price", ["vintage_id", "price_date"], schema="hq"
    )
    op.create_table(
        "vintage_benchmark_price",
        sa.Column("vintage_id", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("price_date", sa.Date(), nullable=False),
        sa.Column("close_unadjusted", PRICE, nullable=False),
        sa.Column("adjusted_close", PRICE, nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "vintage_id", "symbol", "price_date", name="pk_vintage_benchmark_price"
        ),
        sa.ForeignKeyConstraint(
            ["vintage_id"], ["hq.price_vintage.vintage_id"], name="fk_vintage_benchmark_vintage"
        ),
        sa.CheckConstraint(
            "close_unadjusted > 0 AND adjusted_close > 0", name="ck_vintage_benchmark_positive"
        ),
        schema="hq",
    )
    for table in ("vintage_price", "vintage_benchmark_price"):
        op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON hq.{table} FROM hq_app")
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON hq.price_vintage FROM hq_app")
    completion = "status, finished_at, stock_rows, benchmark_rows"
    op.execute(f"GRANT UPDATE ({completion}) ON hq.price_vintage TO hq_app")


def downgrade() -> None:
    op.drop_table("vintage_benchmark_price", schema="hq")
    op.drop_table("vintage_price", schema="hq")
    op.drop_table("price_vintage", schema="hq")
