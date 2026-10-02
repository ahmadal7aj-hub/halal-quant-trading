"""Backtest run records (P2-6; PRD §18, §20; doc 03 R9).

Every run is recorded with the exact inputs (vintage, strategy, protocol, universe configuration,
costs, period), a content hash of its inputs (`run_key_sha256`, unique: running the same inputs
again must reproduce `result_sha256`), its NAV, its trades with the reason for each, and its
rebalances. The table doubles as the parameter log: every parameter set ever evaluated is here.
All four tables are add-only for the application role.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "backtest_run",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("run_key_sha256", sa.Text(), nullable=False),
        sa.Column("vintage_id", sa.Text(), nullable=False),
        sa.Column("strategy_version", sa.Text(), nullable=False),
        sa.Column("strategy_params", postgresql.JSONB(), nullable=False),
        sa.Column("strategy_sha256", sa.Text(), nullable=False),
        sa.Column("protocol_version", sa.Text(), nullable=False),
        sa.Column("protocol_sha256", sa.Text(), nullable=False),
        sa.Column("universe_version", sa.Text(), nullable=False),
        sa.Column("universe_sha256", sa.Text(), nullable=False),
        sa.Column("methodology", sa.Text(), nullable=False),
        sa.Column("slippage_case", sa.Text(), nullable=False),
        sa.Column("slippage_bps", sa.Numeric(10, 4), nullable=False),
        sa.Column("period_name", sa.Text(), nullable=False),
        sa.Column("first_day", sa.Date(), nullable=False),
        sa.Column("last_day", sa.Date(), nullable=False),
        sa.Column("final_test", sa.Boolean(), nullable=False),
        sa.Column("code_version", sa.Text(), nullable=False),
        sa.Column("result_sha256", sa.Text(), nullable=False),
        sa.Column("metrics", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["vintage_id"], ["hq.price_vintage.vintage_id"], name="fk_backtest_run_vintage"
        ),
        sa.UniqueConstraint("run_key_sha256", name="uq_backtest_run_key"),
        sa.CheckConstraint("last_day >= first_day", name="ck_backtest_run_period"),
        schema="hq",
    )
    op.create_table(
        "backtest_nav",
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("nav_date", sa.Date(), nullable=False),
        sa.Column("nav", sa.Numeric(24, 8), nullable=False),
        sa.PrimaryKeyConstraint("run_id", "nav_date", name="pk_backtest_nav"),
        sa.ForeignKeyConstraint(["run_id"], ["hq.backtest_run.id"], name="fk_backtest_nav_run"),
        schema="hq",
    )
    op.create_table(
        "backtest_trade",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("security_id", sa.BigInteger(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("shares", sa.Numeric(24, 4), nullable=False),
        sa.Column("price", sa.Numeric(30, 6), nullable=False),
        sa.Column("notional", sa.Numeric(24, 2), nullable=False),
        sa.Column("cost", sa.Numeric(24, 4), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["hq.backtest_run.id"], name="fk_backtest_trade_run"),
        sa.ForeignKeyConstraint(
            ["security_id"], ["hq.security.security_id"], name="fk_backtest_trade_security"
        ),
        sa.CheckConstraint("side IN ('BUY', 'SELL')", name="ck_backtest_trade_side"),
        schema="hq",
    )
    op.create_index(
        "ix_backtest_trade_run", "backtest_trade", ["run_id", "trade_date"], schema="hq"
    )
    op.create_table(
        "backtest_rebalance",
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("decision_date", sa.Date(), nullable=False),
        sa.Column("eligible", sa.Integer(), nullable=False),
        sa.Column("scored", sa.Integer(), nullable=False),
        sa.Column("selected", sa.Integer(), nullable=False),
        sa.Column("turnover", sa.Numeric(12, 6), nullable=False),
        sa.Column("costs", sa.Numeric(24, 4), nullable=False),
        sa.Column("nav_before", sa.Numeric(24, 8), nullable=False),
        sa.Column("nav_after", sa.Numeric(24, 8), nullable=False),
        sa.PrimaryKeyConstraint("run_id", "decision_date", name="pk_backtest_rebalance"),
        sa.ForeignKeyConstraint(
            ["run_id"], ["hq.backtest_run.id"], name="fk_backtest_rebalance_run"
        ),
        schema="hq",
    )
    for table in ("backtest_run", "backtest_nav", "backtest_trade", "backtest_rebalance"):
        op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON hq.{table} FROM hq_app")


def downgrade() -> None:
    op.drop_table("backtest_rebalance", schema="hq")
    op.drop_table("backtest_trade", schema="hq")
    op.drop_table("backtest_nav", schema="hq")
    op.drop_table("backtest_run", schema="hq")
