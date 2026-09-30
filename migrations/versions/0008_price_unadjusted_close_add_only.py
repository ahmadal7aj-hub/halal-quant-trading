"""Task 10: keep the as-traded close, and make prices and corporate actions add-only.

Sharadar's open, high, low, close and volume are split-adjusted, and only `closeunadj` is the
price actually paid on the day, so the as-traded close needs its own column. Stored market
data is never changed or deleted by the app role: a provider's restated value arrives as a
review note, so a backtest can always be tied to the data it used.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NOT NULL without a default: fails loudly if prices were ever loaded before this change.
    op.add_column(
        "daily_price",
        sa.Column("close_unadjusted", sa.Numeric(19, 6), nullable=False),
        schema="hq",
    )
    op.create_check_constraint(
        "ck_daily_price_close_unadjusted", "daily_price", "close_unadjusted > 0", schema="hq"
    )
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON hq.daily_price FROM hq_app")
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON hq.corporate_action FROM hq_app")


def downgrade() -> None:
    op.execute("GRANT UPDATE, DELETE ON hq.daily_price, hq.corporate_action TO hq_app")
    op.drop_constraint("ck_daily_price_close_unadjusted", "daily_price", schema="hq")
    op.drop_column("daily_price", "close_unadjusted", schema="hq")
