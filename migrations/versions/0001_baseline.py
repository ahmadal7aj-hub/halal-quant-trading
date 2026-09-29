"""Baseline: protect Alembic's version table from the app role.

The bootstrap's default privileges give hq_app data access to every table the migrator
creates, including `alembic_version`. The app must never be able to change which migration
the database claims to be at, so those rights are taken back here.

Revision ID: 0001
Revises:
Create Date: 2026-09-29
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON hq.alembic_version FROM hq_app")


def downgrade() -> None:
    pass  # the version table is Alembic's own; nothing to undo
