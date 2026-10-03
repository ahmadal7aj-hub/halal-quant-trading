"""Broker orders, their state history and fills (Phase 5; PRD §14, §16; critical tests 7-9).

`broker_order` has a UNIQUE idempotency key (one per approved proposal), so re-processing the same
request can never create a second order (test 7). `order_state_event` is the add-only history of
each order's states with cause and correlation id; the current state is the latest event.
`order_fill` records executions; (order, exec id) is unique so a repeated report is harmless.
The application role cannot UPDATE, DELETE or TRUNCATE any of them.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATES = (
    "'APPROVED', 'SUBMITTED', 'SUBMISSION_UNKNOWN', 'ACKNOWLEDGED', 'PARTIALLY_FILLED', "
    "'FILLED', 'CANCELLED', 'REJECTED_BY_BROKER', 'FAILED'"
)


def upgrade() -> None:
    op.create_table(
        "broker_order",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("proposal_id", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Numeric(24, 4), nullable=False),
        sa.Column("limit_price", sa.Numeric(30, 6), nullable=False),
        sa.Column("account_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["hq.trade_proposal.id"], name="fk_broker_order_proposal"
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_broker_order_idempotency_key"),
        sa.UniqueConstraint("proposal_id", name="uq_broker_order_proposal"),
        sa.CheckConstraint("side IN ('BUY', 'SELL')", name="ck_broker_order_side"),
        sa.CheckConstraint("quantity > 0 AND limit_price > 0", name="ck_broker_order_positive"),
        sa.CheckConstraint("account_id LIKE 'DU%'", name="ck_broker_order_paper_account"),
        schema="hq",
    )
    op.create_table(
        "order_state_event",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("cause", sa.Text(), nullable=False),
        sa.Column("broker_ref", sa.Text()),
        sa.Column("correlation_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["hq.broker_order.id"], name="fk_order_state_event_order"
        ),
        sa.CheckConstraint(f"state IN ({STATES})", name="ck_order_state_event_state"),
        sa.CheckConstraint("actor <> '' AND cause <> ''", name="ck_order_state_event_text"),
        schema="hq",
    )
    op.create_index(
        "ix_order_state_event_order", "order_state_event", ["order_id", "id"], schema="hq"
    )
    op.create_table(
        "order_fill",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), primary_key=True),
        sa.Column("order_id", sa.BigInteger(), nullable=False),
        sa.Column("exec_id", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Numeric(24, 4), nullable=False),
        sa.Column("price", sa.Numeric(30, 6), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["order_id"], ["hq.broker_order.id"], name="fk_order_fill_order"),
        sa.UniqueConstraint("order_id", "exec_id", name="uq_order_fill_exec"),
        sa.CheckConstraint("quantity > 0 AND price > 0", name="ck_order_fill_positive"),
        schema="hq",
    )
    for table in ("broker_order", "order_state_event", "order_fill"):
        op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON hq.{table} FROM hq_app")


def downgrade() -> None:
    op.drop_table("order_fill", schema="hq")
    op.drop_table("order_state_event", schema="hq")
    op.drop_table("broker_order", schema="hq")
