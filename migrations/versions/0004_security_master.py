"""Security master: permanent security IDs and dated ticker history (PRD §5, BRD §10).

`security` holds one row per security; its `security_id` never changes. `security_ticker`
holds which ticker the security traded under and when (`valid_from` inclusive, `valid_to`
exclusive, NULL = still current). Two exclusion constraints guarantee that a ticker belongs to
at most one security on any date, and a security has at most one ticker on any date.

History is kept: the app role cannot delete rows, and on `security_ticker` it may only set
`valid_to` (to close a period), never rewrite a ticker or its start date.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "security",
        sa.Column("security_id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("company_name", sa.Text(), nullable=False),
        sa.Column("exchange", sa.Text()),
        sa.Column("currency", sa.Text()),
        sa.Column("country", sa.Text()),
        sa.Column("sector", sa.Text()),
        sa.Column("industry", sa.Text()),
        sa.Column("isin", sa.Text()),
        sa.Column("cusip", sa.Text()),
        sa.Column("start_date", sa.Date()),
        sa.Column("end_date", sa.Date()),
        sa.Column("delisted_flag", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("active_flag", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("source", "source_id", name="uq_security_source_id"),
        sa.CheckConstraint("source <> '' AND source_id <> ''", name="ck_security_source"),
        sa.CheckConstraint("company_name <> ''", name="ck_security_company_name"),
        sa.CheckConstraint("end_date >= start_date", name="ck_security_lifecycle_dates"),
        sa.CheckConstraint(
            "NOT (delisted_flag AND active_flag)", name="ck_security_delisted_not_active"
        ),
        sa.CheckConstraint(
            "NOT delisted_flag OR end_date IS NOT NULL", name="ck_security_delisted_has_end"
        ),
        schema="hq",
    )
    op.create_table(
        "security_ticker",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column(
            "security_id",
            sa.BigInteger(),
            sa.ForeignKey("hq.security.security_id", name="fk_security_ticker_security"),
            nullable=False,
        ),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date()),
        sa.CheckConstraint(
            "ticker <> '' AND ticker = upper(btrim(ticker))", name="ck_security_ticker_ticker"
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from", name="ck_security_ticker_period"
        ),
        postgresql.ExcludeConstraint(
            (sa.column("ticker"), "="),
            (sa.text("daterange(valid_from, valid_to)"), "&&"),
            using="gist",
            name="ex_security_ticker_one_security_per_ticker",
        ),
        postgresql.ExcludeConstraint(
            (sa.column("security_id"), "="),
            (sa.text("daterange(valid_from, valid_to)"), "&&"),
            using="gist",
            name="ex_security_ticker_one_ticker_per_security",
        ),
        schema="hq",
    )

    op.execute("REVOKE DELETE, TRUNCATE ON hq.security FROM hq_app")
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON hq.security_ticker FROM hq_app")
    op.execute("GRANT UPDATE (valid_to) ON hq.security_ticker TO hq_app")


def downgrade() -> None:
    op.drop_table("security_ticker", schema="hq")
    op.drop_table("security", schema="hq")
