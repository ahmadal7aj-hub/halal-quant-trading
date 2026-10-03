"""Read-only reporting views for the dashboard (Phase 6).

Three views over the add-only history tables, so the dashboard's queries are plain static SQL:
`order_current_state` (latest state of each broker order), `proposal_current_status` (latest status
of each trade proposal) and `fill_signed` (every fill with its signed quantity: buys positive,
sells negative). Views hold no data of their own.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0019"
down_revision: str | Sequence[str] | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VIEWS = ("order_current_state", "proposal_current_status", "fill_signed")


def upgrade() -> None:
    op.execute(
        """
        CREATE VIEW hq.order_current_state AS
        SELECT DISTINCT ON (order_id) order_id, state, created_at
        FROM hq.order_state_event ORDER BY order_id, id DESC
        """
    )
    op.execute(
        """
        CREATE VIEW hq.proposal_current_status AS
        SELECT DISTINCT ON (proposal_id) proposal_id, status, created_at
        FROM hq.proposal_event ORDER BY proposal_id, id DESC
        """
    )
    op.execute(
        """
        CREATE VIEW hq.fill_signed AS
        SELECT o.id AS order_id, o.symbol,
               CASE WHEN o.side = 'BUY' THEN f.quantity ELSE -f.quantity END AS signed_quantity
        FROM hq.broker_order o JOIN hq.order_fill f ON f.order_id = o.id
        """
    )
    for view in VIEWS:
        op.execute(f"GRANT SELECT ON hq.{view} TO hq_app, hq_readonly")


def downgrade() -> None:
    for view in VIEWS:
        op.execute(f"DROP VIEW IF EXISTS hq.{view}")
