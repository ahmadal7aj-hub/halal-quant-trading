"""Widen the price columns so heavily split-adjusted history fits (found loading 2004-08).

Sharadar adjusts old prices for every later split. A company that reverse-split many times (TOP
Ships, ticker TOPS) has adjusted prices around 2.8e14 for 2004, which does not fit numeric(19,6)
(limit 1e13) and made the price import fail. The values are correct as delivered, so the columns
are widened instead of the rows being dropped. Raising the precision with the same scale does not
rewrite the table in PostgreSQL.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PRICE_COLUMNS = ("open", "high", "low", "close", "adjusted_close", "close_unadjusted")


def upgrade() -> None:
    for column in PRICE_COLUMNS:
        op.alter_column(
            "daily_price",
            column,
            type_=sa.Numeric(30, 6),
            existing_type=sa.Numeric(19, 6),
            existing_nullable=False,
            schema="hq",
        )


def downgrade() -> None:
    for column in PRICE_COLUMNS:
        op.alter_column(
            "daily_price",
            column,
            type_=sa.Numeric(19, 6),
            existing_type=sa.Numeric(30, 6),
            existing_nullable=False,
            schema="hq",
        )
