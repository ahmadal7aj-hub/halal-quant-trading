"""The kill switch and trade proposals with their approval trail (Phase 4; PRD §12-§14, §37 test 6).

Everything here is add-only. The kill switch is a log of engage/release events; the current state
is the latest event (no events means not engaged). A trade proposal is stored once, exactly as the
risk engine judged it; what happens to it afterwards is a list of status events, so the history of
who approved what, and why, can never be rewritten. Each change also writes an audit event.
"""

from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Connection,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Numeric,
    Table,
    Text,
    func,
    select,
)

from halal_quant.audit import AuditEvent, record_event
from halal_quant.core.logging import get_correlation_id
from halal_quant.db.engine import metadata
from halal_quant.trading.risk import (
    APPROVED,
    REJECTED,
    REQUIRES_MANUAL_REVIEW,
    OrderRequest,
    RiskDecision,
)

# Proposal statuses (a subset of the PRD §14 order states; execution states come with Phase 5).
RISK_REJECTED = "RISK_REJECTED"
AWAITING_APPROVAL = "AWAITING_APPROVAL"
APPROVED_STATUS = "APPROVED"
REJECTED_BY_OWNER = "REJECTED_BY_OWNER"
STATUSES = (RISK_REJECTED, AWAITING_APPROVAL, APPROVED_STATUS, REJECTED_BY_OWNER)

kill_switch_event_table = Table(
    "kill_switch_event",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("engaged", Boolean, nullable=False),
    Column("actor", Text, nullable=False),
    Column("reason", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("actor <> '' AND reason <> ''", name="ck_kill_switch_event_text"),
)

trade_proposal_table = Table(
    "trade_proposal",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("correlation_id", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("proposed_by", Text, nullable=False),
    Column("symbol", Text, nullable=False),
    Column("side", Text, nullable=False),
    Column("quantity", Numeric(24, 4), nullable=False),
    Column("ref_price", Numeric(30, 6), nullable=False),
    Column("notional", Numeric(24, 2), nullable=False),
    Column("strategy_reason", Text, nullable=False),
    Column("risk_limits_version", Text, nullable=False),
    Column("risk_result", Text, nullable=False),
    Column("reason_code", Text, nullable=False),
    Column("explanation", Text, nullable=False),
    CheckConstraint("side IN ('BUY', 'SELL')", name="ck_trade_proposal_side"),
    CheckConstraint(
        "risk_result IN ('APPROVED', 'REJECTED', 'REQUIRES_MANUAL_REVIEW')",
        name="ck_trade_proposal_result",
    ),
    CheckConstraint("quantity > 0 AND ref_price > 0", name="ck_trade_proposal_positive"),
)

proposal_event_table = Table(
    "proposal_event",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column(
        "proposal_id",
        BigInteger,
        ForeignKey("trade_proposal.id", name="fk_proposal_event_proposal"),
        nullable=False,
    ),
    Column("status", Text, nullable=False),
    Column("actor", Text, nullable=False),
    Column("cause", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "status IN ('RISK_REJECTED', 'AWAITING_APPROVAL', 'APPROVED', 'REJECTED_BY_OWNER')",
        name="ck_proposal_event_status",
    ),
    CheckConstraint("actor <> '' AND cause <> ''", name="ck_proposal_event_text"),
    Index("ix_proposal_event_proposal", "proposal_id", "id"),
)


class ProposalError(Exception):
    """A proposal action is not allowed from its current state."""


def kill_switch_engaged(conn: Connection) -> bool:
    """True when the latest kill switch event is an engage (no events: not engaged)."""
    k = kill_switch_event_table.c
    latest = conn.execute(select(k.engaged).order_by(k.id.desc()).limit(1)).scalar_one_or_none()
    return bool(latest)


def _set_kill_switch(conn: Connection, engaged: bool, actor: str, reason: str) -> None:
    if not reason.strip() or not actor.strip():
        raise ValueError("The kill switch needs an actor and a reason.")
    conn.execute(
        kill_switch_event_table.insert().values(engaged=engaged, actor=actor, reason=reason)
    )
    record_event(
        conn,
        AuditEvent(
            actor=actor,
            action="kill_switch.engaged" if engaged else "kill_switch.released",
            entity_type="kill_switch",
            entity_id="global",
            reason=reason,
            source=__name__,
        ),
    )


def engage_kill_switch(conn: Connection, actor: str, reason: str) -> None:
    """Stop all order proposals from being approved. Always allowed, any time."""
    _set_kill_switch(conn, True, actor, reason)


def release_kill_switch(conn: Connection, actor: str, reason: str) -> None:
    """Allow trading again. A human actor and a reason are recorded; nothing is deleted."""
    _set_kill_switch(conn, False, actor, reason)


def _add_event(conn: Connection, proposal_id: int, status: str, actor: str, cause: str) -> None:
    conn.execute(
        proposal_event_table.insert().values(
            proposal_id=proposal_id, status=status, actor=actor, cause=cause
        )
    )


def proposal_status(conn: Connection, proposal_id: int) -> str | None:
    e = proposal_event_table.c
    return conn.execute(
        select(e.status).where(e.proposal_id == proposal_id).order_by(e.id.desc()).limit(1)
    ).scalar_one_or_none()


def submit_proposal(
    conn: Connection,
    order: OrderRequest,
    decision: RiskDecision,
    limits_version: str,
    actor: str,
) -> tuple[int, str]:
    """Store a proposed order with the risk engine's decision; returns (id, first status).

    Rejected orders are stored too (a record of what was refused and why). Approved orders wait for
    nothing further; orders that need the owner start as AWAITING_APPROVAL.
    """
    status = {
        APPROVED: APPROVED_STATUS,
        REQUIRES_MANUAL_REVIEW: AWAITING_APPROVAL,
        REJECTED: RISK_REJECTED,
    }[decision.result]
    proposal_id: int = conn.execute(
        trade_proposal_table.insert()
        .values(
            correlation_id=get_correlation_id() or "none",
            proposed_by=actor,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            ref_price=order.price,
            notional=order.notional.quantize(Decimal("0.01")),
            strategy_reason=order.reason or "(none given)",
            risk_limits_version=limits_version,
            risk_result=decision.result,
            reason_code=decision.reason_code,
            explanation=decision.explanation,
        )
        .returning(trade_proposal_table.c.id)
    ).scalar_one()
    _add_event(conn, proposal_id, status, actor, f"{decision.reason_code}: {decision.explanation}")
    record_event(
        conn,
        AuditEvent(
            actor=actor,
            action="trade_proposal.created",
            entity_type="trade_proposal",
            entity_id=str(proposal_id),
            reason=decision.explanation,
            source=__name__,
            details={
                "symbol": order.symbol,
                "side": order.side,
                "quantity": order.quantity,
                "notional": order.notional,
                "result": decision.result,
                "reason_code": decision.reason_code,
                "status": status,
            },
        ),
    )
    return proposal_id, status


def decide_proposal(
    conn: Connection, proposal_id: int, approve: bool, actor: str, reason: str
) -> str:
    """The owner approves or rejects a proposal that is awaiting approval; returns the new status.

    Refused when the proposal is in any other state (a risk-rejected order can never be approved by
    hand) or when the kill switch is engaged.
    """
    if not reason.strip():
        raise ValueError("A decision needs a reason.")
    current = proposal_status(conn, proposal_id)
    if current != AWAITING_APPROVAL:
        raise ProposalError(
            f"Proposal {proposal_id} is {current!r}; only one awaiting approval can be decided."
        )
    if approve and kill_switch_engaged(conn):
        raise ProposalError("The kill switch is engaged: nothing can be approved.")
    status = APPROVED_STATUS if approve else REJECTED_BY_OWNER
    _add_event(conn, proposal_id, status, actor, reason)
    record_event(
        conn,
        AuditEvent(
            actor=actor,
            action="trade_proposal.approved" if approve else "trade_proposal.rejected",
            entity_type="trade_proposal",
            entity_id=str(proposal_id),
            reason=reason,
            source=__name__,
        ),
    )
    return status
