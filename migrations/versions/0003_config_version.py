"""config_version table: every configuration version ever used (PRD section 29).

History is kept: the app role may add and read versions but never change or remove them.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "config_version",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("config_type", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("registered_by", sa.Text(), nullable=False),
        sa.UniqueConstraint("config_type", "version", name="uq_config_version_type_version"),
        schema="hq",
    )
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON hq.config_version FROM hq_app")


def downgrade() -> None:
    op.drop_table("config_version", schema="hq")
