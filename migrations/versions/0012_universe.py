"""Eligible-universe builds and their members (task 14).

A build records the date, the methodology and universe configuration it used, the data-quality run
it relied on, how many securities each rule excluded, and a content hash that identifies it: the
same inputs always give the same hash, so a universe can be reproduced and checked later. Members
are stored one row per security. Both tables are add-only for the app role.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "universe_build",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("methodology", sa.Text(), nullable=False),
        sa.Column("universe_version", sa.Text(), nullable=False),
        sa.Column("config_sha256", sa.Text(), nullable=False),
        sa.Column("data_quality_run_id", sa.BigInteger()),
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.Text(), nullable=False),
        sa.Column("exclusions", postgresql.JSONB(), nullable=False),
        sa.Column(
            "built_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("content_sha256", name="uq_universe_build_content"),
        schema="hq",
    )
    op.create_index("ix_universe_build_as_of", "universe_build", ["as_of"], schema="hq")
    op.create_table(
        "universe_member",
        sa.Column("build_id", sa.BigInteger(), nullable=False),
        sa.Column("security_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["build_id"], ["hq.universe_build.id"], name="fk_universe_member_build"
        ),
        sa.ForeignKeyConstraint(
            ["security_id"], ["hq.security.security_id"], name="fk_universe_member_security"
        ),
        sa.PrimaryKeyConstraint("build_id", "security_id", name="pk_universe_member"),
        sa.CheckConstraint("security_id > 0", name="ck_universe_member_security"),
        schema="hq",
    )
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON hq.universe_build FROM hq_app")
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON hq.universe_member FROM hq_app")


def downgrade() -> None:
    op.drop_table("universe_member", schema="hq")
    op.drop_table("universe_build", schema="hq")
