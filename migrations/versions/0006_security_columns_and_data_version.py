"""Security category and SIC code, and the data-version register (task 9).

`security.category` (e.g. "Domestic Common Stock") and `security.sic_code` are inputs to the
universe filter and the Sharia business-activity screen. `data_version` records every dataset
download the platform has used, so prices, fundamentals and classifications can name the exact
data behind them (BRD §6.4). It is append-only for the app role.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("security", sa.Column("category", sa.Text()), schema="hq")
    op.add_column("security", sa.Column("sic_code", sa.Text()), schema="hq")

    op.create_table(
        "data_version",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("version", sa.Text(), nullable=False, unique=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("dataset", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column("downloaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("dataset", "sha256", name="uq_data_version_dataset_sha256"),
        sa.CheckConstraint("row_count >= 0", name="ck_data_version_row_count"),
        sa.CheckConstraint("source <> '' AND dataset <> ''", name="ck_data_version_names"),
        schema="hq",
    )
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON hq.data_version FROM hq_app")


def downgrade() -> None:
    op.drop_table("data_version", schema="hq")
    op.drop_column("security", "sic_code", schema="hq")
    op.drop_column("security", "category", schema="hq")
