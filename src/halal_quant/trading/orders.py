"""Order state machine, safe submission and reconciliation (Phase 5; PRD §14-§17; tests 7, 8, 9).

Rules enforced here:
- An approved proposal becomes AT MOST ONE broker order: the idempotency key is unique in the
  database, so re-processing the same request returns the existing order and never reaches the
  broker twice (test 7).
- The SUBMITTED state is written BEFORE the broker is called. If the connection drops while the
  order is in flight, the order is SUBMISSION_UNKNOWN and is NEVER re-sent; it is resolved only by
  asking the broker for the order by its client reference. While any order is in doubt, no new
  order is submitted (test 8). A disconnect before sending creates no order at all.
- Every state change is an add-only event with cause and correlation id.
- Reconciliation compares our fills with the broker's positions and open orders; any mismatch
  engages the kill switch (STOP_NEW_TRADING) and writes an audit event (test 9).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
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
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert

from halal_quant.audit import AuditEvent, record_event
from halal_quant.core.logging import get_correlation_id
from halal_quant.db.engine import metadata
from halal_quant.trading.broker import (
    AmbiguousSubmit,
    Broker,
    BrokerNotConnected,
    BrokerOrder,
    BrokerOrderInfo,
    BrokerRejected,
    assert_paper_account,
)
from halal_quant.trading.records import (
    APPROVED_STATUS,
    engage_kill_switch,
    kill_switch_engaged,
    proposal_status,
    trade_proposal_table,
)

APPROVED = "APPROVED"
SUBMITTED = "SUBMITTED"
SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"  # beyond the PRD list: an order in doubt is never re-sent
ACKNOWLEDGED = "ACKNOWLEDGED"
PARTIALLY_FILLED = "PARTIALLY_FILLED"
FILLED = "FILLED"
CANCELLED = "CANCELLED"
REJECTED_BY_BROKER = "REJECTED_BY_BROKER"
FAILED = "FAILED"

_AFTER_SEND = {ACKNOWLEDGED, PARTIALLY_FILLED, FILLED, REJECTED_BY_BROKER, CANCELLED}
TRANSITIONS: dict[str, set[str]] = {
    APPROVED: {SUBMITTED, FAILED, CANCELLED},
    SUBMITTED: {*_AFTER_SEND, SUBMISSION_UNKNOWN, FAILED},
    SUBMISSION_UNKNOWN: {*_AFTER_SEND, FAILED},
    ACKNOWLEDGED: {PARTIALLY_FILLED, FILLED, CANCELLED, REJECTED_BY_BROKER},
    PARTIALLY_FILLED: {PARTIALLY_FILLED, FILLED, CANCELLED},
}

broker_order_table = Table(
    "broker_order",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column(
        "proposal_id",
        BigInteger,
        ForeignKey("trade_proposal.id", name="fk_broker_order_proposal"),
        nullable=False,
    ),
    Column("idempotency_key", Text, nullable=False),
    Column("symbol", Text, nullable=False),
    Column("side", Text, nullable=False),
    Column("quantity", Numeric(24, 4), nullable=False),
    Column("limit_price", Numeric(30, 6), nullable=False),
    Column("account_id", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("idempotency_key", name="uq_broker_order_idempotency_key"),
    UniqueConstraint("proposal_id", name="uq_broker_order_proposal"),
    CheckConstraint("side IN ('BUY', 'SELL')", name="ck_broker_order_side"),
    CheckConstraint("quantity > 0 AND limit_price > 0", name="ck_broker_order_positive"),
    CheckConstraint("account_id LIKE 'DU%'", name="ck_broker_order_paper_account"),
)

order_state_event_table = Table(
    "order_state_event",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column(
        "order_id",
        BigInteger,
        ForeignKey("broker_order.id", name="fk_order_state_event_order"),
        nullable=False,
    ),
    Column("state", Text, nullable=False),
    Column("actor", Text, nullable=False),
    Column("cause", Text, nullable=False),
    Column("broker_ref", Text),
    Column("correlation_id", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "state IN ('APPROVED', 'SUBMITTED', 'SUBMISSION_UNKNOWN', 'ACKNOWLEDGED', "
        "'PARTIALLY_FILLED', 'FILLED', 'CANCELLED', 'REJECTED_BY_BROKER', 'FAILED')",
        name="ck_order_state_event_state",
    ),
    CheckConstraint("actor <> '' AND cause <> ''", name="ck_order_state_event_text"),
    Index("ix_order_state_event_order", "order_id", "id"),
)

order_fill_table = Table(
    "order_fill",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column(
        "order_id",
        BigInteger,
        ForeignKey("broker_order.id", name="fk_order_fill_order"),
        nullable=False,
    ),
    Column("exec_id", Text, nullable=False),
    Column("quantity", Numeric(24, 4), nullable=False),
    Column("price", Numeric(30, 6), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("order_id", "exec_id", name="uq_order_fill_exec"),
    CheckConstraint("quantity > 0 AND price > 0", name="ck_order_fill_positive"),
)


class ExecutionRefused(Exception):
    """An order was not sent, and why (plain words)."""


class InvalidTransition(Exception):
    """A state change the order state machine does not allow."""


@dataclass(frozen=True)
class SubmitOutcome:
    order_id: int
    state: str
    already_existed: bool  # True when the proposal already had its one order


def idempotency_key(proposal_id: int) -> str:
    return f"proposal-{proposal_id}"


def order_state(conn: Connection, order_id: int) -> str | None:
    e = order_state_event_table.c
    return conn.execute(
        select(e.state).where(e.order_id == order_id).order_by(e.id.desc()).limit(1)
    ).scalar_one_or_none()


def record_state(
    conn: Connection,
    order_id: int,
    new_state: str,
    actor: str,
    cause: str,
    broker_ref: str | None = None,
) -> None:
    """Add a state event if the order state machine allows it from the current state."""
    current = order_state(conn, order_id)
    if current is not None and new_state not in TRANSITIONS.get(current, set()):
        raise InvalidTransition(f"Order {order_id}: {current} -> {new_state} is not allowed.")
    conn.execute(
        order_state_event_table.insert().values(
            order_id=order_id,
            state=new_state,
            actor=actor,
            cause=cause,
            broker_ref=broker_ref,
            correlation_id=get_correlation_id() or "none",
        )
    )
    record_event(
        conn,
        AuditEvent(
            actor=actor,
            action=f"order.{new_state.lower()}",
            entity_type="broker_order",
            entity_id=str(order_id),
            reason=cause,
            source=__name__,
            details={"from": current, "to": new_state, "broker_ref": broker_ref},
        ),
    )


def _existing(conn: Connection, key: str) -> int | None:
    o = broker_order_table.c
    return conn.execute(select(o.id).where(o.idempotency_key == key)).scalar_one_or_none()


def unresolved_orders(conn: Connection) -> list[int]:
    """Orders whose submission is in doubt: nothing new may be sent until they are resolved."""
    ids: list[int] = list(conn.execute(select(broker_order_table.c.id)).scalars().all())
    return [i for i in ids if order_state(conn, i) == SUBMISSION_UNKNOWN]


def _apply_info(conn: Connection, order_id: int, info: BrokerOrderInfo, actor: str) -> str:
    """Bring our records in line with the broker's report of this order; returns the new state."""
    new_fills = 0
    for fill in info.fills:
        inserted = conn.execute(
            pg_insert(order_fill_table)
            .values(
                order_id=order_id, exec_id=fill.exec_id, quantity=fill.quantity, price=fill.price
            )
            .on_conflict_do_nothing(constraint="uq_order_fill_exec")
            .returning(order_fill_table.c.id)
        ).scalar_one_or_none()
        new_fills += inserted is not None
    filled: Decimal = conn.execute(
        select(func.coalesce(func.sum(order_fill_table.c.quantity), 0)).where(
            order_fill_table.c.order_id == order_id
        )
    ).scalar_one()
    quantity: Decimal = conn.execute(
        select(broker_order_table.c.quantity).where(broker_order_table.c.id == order_id)
    ).scalar_one()
    if filled >= quantity:
        target = FILLED
    elif filled > 0:
        target = PARTIALLY_FILLED
    else:
        target = {"REJECTED": REJECTED_BY_BROKER, "CANCELLED": CANCELLED}.get(
            info.status, ACKNOWLEDGED
        )
    current = order_state(conn, order_id)
    if target != current or (target == PARTIALLY_FILLED and new_fills):
        record_state(
            conn, order_id, target, actor, f"broker reports {info.status}", info.broker_ref
        )
    return target


def submit_approved_proposal(
    conn: Connection, broker: Broker, proposal_id: int, actor: str = "system:executor"
) -> SubmitOutcome:
    """Send the one broker order for an approved proposal, safely (tests 7 and 8)."""
    key = idempotency_key(proposal_id)
    existing = _existing(conn, key)
    if existing is not None:  # re-processing the same request: no second order, no broker call
        return SubmitOutcome(existing, order_state(conn, existing) or APPROVED, True)
    if proposal_status(conn, proposal_id) != APPROVED_STATUS:
        raise ExecutionRefused(f"Proposal {proposal_id} is not approved: nothing is sent.")
    if kill_switch_engaged(conn):
        raise ExecutionRefused("The kill switch is engaged: no orders may be sent.")
    in_doubt = unresolved_orders(conn)
    if in_doubt:
        raise ExecutionRefused(
            f"Order(s) {in_doubt} are in doubt (sent, no answer): resolve them with the broker "
            "before sending anything new."
        )
    if not broker.is_connected():
        raise ExecutionRefused("The broker is not connected: nothing was sent.")
    assert_paper_account(broker.account_id())
    p = trade_proposal_table.c
    proposal = conn.execute(
        select(p.symbol, p.side, p.quantity, p.ref_price).where(p.id == proposal_id)
    ).one()
    order_id = conn.execute(
        pg_insert(broker_order_table)
        .values(
            proposal_id=proposal_id,
            idempotency_key=key,
            symbol=proposal.symbol,
            side=proposal.side,
            quantity=proposal.quantity,
            limit_price=proposal.ref_price,
            account_id=broker.account_id(),
        )
        .on_conflict_do_nothing(constraint="uq_broker_order_idempotency_key")
        .returning(broker_order_table.c.id)
    ).scalar_one_or_none()
    if order_id is None:  # lost a race with another process: that one owns the order
        raced = _existing(conn, key)
        if raced is None:
            raise ExecutionRefused("The order could not be created or found: nothing was sent.")
        return SubmitOutcome(raced, order_state(conn, raced) or APPROVED, True)
    record_state(conn, order_id, APPROVED, actor, "proposal approved; order created")
    record_state(conn, order_id, SUBMITTED, actor, "about to send to the broker")
    order = BrokerOrder(key, proposal.symbol, proposal.side, proposal.quantity, proposal.ref_price)
    try:
        info = broker.submit(order)
    except BrokerNotConnected:
        record_state(conn, order_id, FAILED, actor, "disconnected: the order was not sent")
        return SubmitOutcome(order_id, FAILED, False)
    except BrokerRejected as exc:
        record_state(conn, order_id, REJECTED_BY_BROKER, actor, str(exc))
        return SubmitOutcome(order_id, REJECTED_BY_BROKER, False)
    except AmbiguousSubmit as exc:
        record_state(
            conn,
            order_id,
            SUBMISSION_UNKNOWN,
            actor,
            f"{exc} The order may have reached the broker: it will NOT be re-sent.",
        )
        return SubmitOutcome(order_id, SUBMISSION_UNKNOWN, False)
    return SubmitOutcome(order_id, _apply_info(conn, order_id, info, actor), False)


def sync_order(
    conn: Connection, broker: Broker, order_id: int, actor: str = "system:executor"
) -> str:
    """Refresh one order from the broker (fills, partial fills, cancellations)."""
    key: str = conn.execute(
        select(broker_order_table.c.idempotency_key).where(broker_order_table.c.id == order_id)
    ).scalar_one()
    info = broker.find_order(key)
    state = order_state(conn, order_id) or APPROVED
    if info is None:
        return state
    return _apply_info(conn, order_id, info, actor)


def resolve_unknown(
    conn: Connection, broker: Broker, order_id: int, actor: str = "system:reconcile"
) -> str:
    """Settle an order in doubt by asking the broker for it. Never re-sends anything."""
    if order_state(conn, order_id) != SUBMISSION_UNKNOWN:
        raise ExecutionRefused(f"Order {order_id} is not in doubt.")
    key: str = conn.execute(
        select(broker_order_table.c.idempotency_key).where(broker_order_table.c.id == order_id)
    ).scalar_one()
    info = broker.find_order(key)  # raises BrokerNotConnected while the broker is unreachable
    if info is None:
        record_state(conn, order_id, FAILED, actor, "the broker never received it (looked up)")
        return FAILED
    return _apply_info(conn, order_id, info, actor)


@dataclass(frozen=True)
class Reconciliation:
    position_mismatches: dict[str, tuple[Decimal, Decimal]]  # symbol -> (expected, broker)
    unknown_orders: list[str]  # open at the broker, unknown to us (client references)

    @property
    def ok(self) -> bool:
        return not self.position_mismatches and not self.unknown_orders


def expected_positions(
    conn: Connection, baseline: Mapping[str, Decimal] | None = None
) -> dict[str, Decimal]:
    """Shares we expect to hold: the baseline plus every recorded fill, signed by side."""
    o, f = broker_order_table.c, order_fill_table.c
    rows = conn.execute(
        select(o.symbol, o.side, func.sum(f.quantity))
        .select_from(broker_order_table.join(order_fill_table, f.order_id == o.id))
        .group_by(o.symbol, o.side)
    )
    held: dict[str, Decimal] = dict(baseline or {})
    for symbol, side, quantity in rows:
        held[symbol] = held.get(symbol, Decimal(0)) + (quantity if side == "BUY" else -quantity)
    return {s: q for s, q in held.items() if q != 0}


def reconcile(
    conn: Connection,
    broker: Broker,
    baseline: Mapping[str, Decimal] | None = None,
    actor: str = "system:reconcile",
) -> Reconciliation:
    """Compare our records with the broker; on a mismatch STOP new trading and alert (test 9)."""
    expected = expected_positions(conn, baseline)
    actual = broker.positions()
    mismatches = {
        s: (expected.get(s, Decimal(0)), actual.get(s, Decimal(0)))
        for s in sorted(set(expected) | set(actual))
        if expected.get(s, Decimal(0)) != actual.get(s, Decimal(0))
    }
    known: set[str] = set(
        conn.execute(select(broker_order_table.c.idempotency_key)).scalars().all()
    )
    unknown = [o.client_ref for o in broker.open_orders() if o.client_ref not in known]
    result = Reconciliation(mismatches, unknown)
    if not result.ok:
        detail = "; ".join(f"{s}: expected {e}, broker {b}" for s, (e, b) in mismatches.items())
        if unknown:
            detail += f"; unknown open orders {unknown}"
        engage_kill_switch(conn, actor, f"Reconciliation mismatch: {detail}")
        record_event(
            conn,
            AuditEvent(
                actor=actor,
                action="reconciliation.mismatch",
                entity_type="reconciliation",
                entity_id="positions",
                reason=detail,
                source=__name__,
                details={"stop_new_trading": True, "alert_user": True},
            ),
        )
    return result
