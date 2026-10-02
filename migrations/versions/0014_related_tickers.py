"""Related tickers on the security master (G8 OI-19).

Sharadar lists, for each security, the tickers of the company's other securities (other share
classes, units). The universe builder uses it to keep one share class per company instead of
guessing from the company name. NULL means "not imported yet"; an empty string means the provider
lists none.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("security", sa.Column("related_tickers", sa.Text()), schema="hq")


def downgrade() -> None:
    op.drop_column("security", "related_tickers", schema="hq")
