"""Kill switch events and trade proposals with their approval trail (Phase 4; PRD §12-§14).

Three add-only tables: `kill_switch_event` (engage/release log; the latest row is the state),
`trade_proposal` (an order as the risk engine judged it) and `proposal_event` (its status history:
risk-rejected, awaiting approval, approved, rejected by the owner). The application role cannot
UPDATE, DELETE or TRUNCATE any of them.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUS_CHECK = "status IN ('RISK_REJECTED', 'AWAITING_APPROVAL', 'APPROVED', 'REJECTED_BY_OWNER')"


def upgrade() -> None:
    op.create_table(
        "kill_switch_event",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("engaged", sa.Boolean(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("actor <> '' AND reason <> ''", name="ck_kill_switch_event_text"),
        schema="hq",
    )
    op.create_table(
        "trade_proposal",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("proposed_by", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Numeric(24, 4), nullable=False),
        sa.Column("ref_price", sa.Numeric(30, 6), nullable=False),
        sa.Column("notional", sa.Numeric(24, 2), nullable=False),
        sa.Column("strategy_reason", sa.Text(), nullable=False),
        sa.Column("risk_limits_version", sa.Text(), nullable=False),
        sa.Column("risk_result", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.CheckConstraint("side IN ('BUY', 'SELL')", name="ck_trade_proposal_side"),
        sa.CheckConstraint(
            "risk_result IN ('APPROVED', 'REJECTED', 'REQUIRES_MANUAL_REVIEW')",
            name="ck_trade_proposal_result",
        ),
        sa.CheckConstraint("quantity > 0 AND ref_price > 0", name="ck_trade_proposal_positive"),
        schema="hq",
    )
    op.create_table(
        "proposal_event",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("proposal_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("cause", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["hq.trade_proposal.id"], name="fk_proposal_event_proposal"
        ),
        sa.CheckConstraint(STATUS_CHECK, name="ck_proposal_event_status"),
        sa.CheckConstraint("actor <> '' AND cause <> ''", name="ck_proposal_event_text"),
        schema="hq",
    )
    op.create_index(
        "ix_proposal_event_proposal", "proposal_event", ["proposal_id", "id"], schema="hq"
    )
    for table in ("kill_switch_event", "trade_proposal", "proposal_event"):
        op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON hq.{table} FROM hq_app")


def downgrade() -> None:
    op.drop_table("proposal_event", schema="hq")
    op.drop_table("trade_proposal", schema="hq")
    op.drop_table("kill_switch_event", schema="hq")
