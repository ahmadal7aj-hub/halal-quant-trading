"""Phase 5 critical tests 7, 8 and 9 on the order machinery, with a simulated broker."""

from collections.abc import Iterator
from decimal import Decimal

import pytest
from sqlalchemy import Connection, Engine, func, select, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from halal_quant.core.settings import DbRole
from halal_quant.trading import risk
from halal_quant.trading.broker import (
    LiveTradingRefused,
    SimulatedBroker,
)
from halal_quant.trading.orders import (
    ACKNOWLEDGED,
    APPROVED,
    FAILED,
    FILLED,
    PARTIALLY_FILLED,
    REJECTED_BY_BROKER,
    SUBMISSION_UNKNOWN,
    ExecutionRefused,
    InvalidTransition,
    broker_order_table,
    expected_positions,
    idempotency_key,
    order_fill_table,
    order_state,
    order_state_event_table,
    reconcile,
    record_state,
    resolve_unknown,
    submit_approved_proposal,
    sync_order,
    unresolved_orders,
)
from halal_quant.trading.records import (
    engage_kill_switch,
    kill_switch_engaged,
    submit_proposal,
)

D = Decimal
OK = risk.RiskDecision(risk.APPROVED, risk.OK, "All risk checks passed.")
MANUAL = risk.RiskDecision(risk.REQUIRES_MANUAL_REVIEW, risk.MANUAL_REVIEW_SIZE, "big")


@pytest.fixture
def conn(engines: dict[DbRole, Engine]) -> Iterator[Connection]:
    with engines[DbRole.APP].connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


def approved(conn: Connection, symbol: str = "SPUS", qty: int = 10, side: str = "BUY") -> int:
    order = risk.OrderRequest(symbol, side, D(qty), D(100), "test")  # type: ignore[arg-type]
    proposal_id, status = submit_proposal(conn, order, OK, "risk-v1", "system:test")
    assert status == "APPROVED"
    return proposal_id


def test_an_approved_proposal_becomes_one_filled_order_with_a_full_history(
    conn: Connection,
) -> None:
    broker = SimulatedBroker()
    proposal_id = approved(conn)
    outcome = submit_approved_proposal(conn, broker, proposal_id)
    assert outcome.state == FILLED and outcome.already_existed is False
    assert broker.positions() == {"SPUS": D(10)} and broker.submissions == 1
    states = (
        conn.execute(
            select(order_state_event_table.c.state)
            .where(order_state_event_table.c.order_id == outcome.order_id)
            .order_by(order_state_event_table.c.id)
        )
        .scalars()
        .all()
    )
    assert states == [APPROVED, "SUBMITTED", FILLED]
    row = conn.execute(
        select(broker_order_table).where(broker_order_table.c.id == outcome.order_id)
    ).one()
    assert row.idempotency_key == idempotency_key(proposal_id) and row.account_id == "DU0000001"


def test_test7_a_duplicate_request_does_not_create_a_duplicate_trade(conn: Connection) -> None:
    broker = SimulatedBroker()
    proposal_id = approved(conn)
    first = submit_approved_proposal(conn, broker, proposal_id)
    again = submit_approved_proposal(conn, broker, proposal_id)
    third = submit_approved_proposal(conn, broker, proposal_id)
    assert again.already_existed and third.already_existed
    assert first.order_id == again.order_id == third.order_id
    assert broker.submissions == 1 and broker.positions() == {"SPUS": D(10)}
    count = conn.execute(select(func.count()).select_from(broker_order_table)).scalar_one()
    assert count >= 1


def test_the_database_itself_refuses_a_second_order_for_the_same_key(conn: Connection) -> None:
    proposal_id = approved(conn)
    submit_approved_proposal(conn, SimulatedBroker(), proposal_id)
    with pytest.raises(IntegrityError), conn.begin_nested():
        conn.execute(
            broker_order_table.insert().values(
                proposal_id=proposal_id,
                idempotency_key=idempotency_key(proposal_id),
                symbol="SPUS",
                side="BUY",
                quantity=D(1),
                limit_price=D(1),
                account_id="DU1",
            )
        )


def test_test8_a_disconnected_broker_means_nothing_is_sent_and_no_order_exists(
    conn: Connection,
) -> None:
    broker = SimulatedBroker(connected=False)
    proposal_id = approved(conn)
    with pytest.raises(ExecutionRefused, match="not connected"):
        submit_approved_proposal(conn, broker, proposal_id)
    assert broker.submissions == 0
    broker.connected = True  # after reconnecting the same approved proposal can go out, once
    assert submit_approved_proposal(conn, broker, proposal_id).state == FILLED
    assert broker.submissions == 1


def test_test8_a_connection_lost_mid_send_is_never_resent_and_blocks_new_orders(
    conn: Connection,
) -> None:
    broker = SimulatedBroker(mode="lose_ack")
    proposal_id = approved(conn)
    outcome = submit_approved_proposal(conn, broker, proposal_id)
    assert outcome.state == SUBMISSION_UNKNOWN
    assert unresolved_orders(conn) == [outcome.order_id]
    # asking again must NOT send it again
    again = submit_approved_proposal(conn, broker, proposal_id)
    assert again.already_existed and broker.submissions == 1
    # and nothing new may be sent while an order is in doubt
    broker.mode = "normal"
    with pytest.raises(ExecutionRefused, match="in doubt"):
        submit_approved_proposal(conn, broker, approved(conn, "HLAL"))
    assert broker.submissions == 1
    # settling it by asking the broker (it did arrive and fill) clears the block
    assert resolve_unknown(conn, broker, outcome.order_id) == FILLED
    assert unresolved_orders(conn) == [] and broker.positions() == {"SPUS": D(10)}
    assert submit_approved_proposal(conn, broker, approved(conn, "HLAL")).state == FILLED


def test_an_order_in_doubt_that_the_broker_never_saw_is_closed_as_failed(
    conn: Connection,
) -> None:
    class Forgetful(SimulatedBroker):
        def find_order(self, client_ref: str):  # type: ignore[no-untyped-def]
            return None  # the broker has no record of it

    broker = Forgetful(mode="lose_ack")
    outcome = submit_approved_proposal(conn, broker, approved(conn))
    assert resolve_unknown(conn, broker, outcome.order_id) == FAILED
    with pytest.raises(ExecutionRefused, match="not in doubt"):
        resolve_unknown(conn, broker, outcome.order_id)


def test_a_broker_rejection_is_recorded_and_not_retried(conn: Connection) -> None:
    broker = SimulatedBroker(mode="reject")
    proposal_id = approved(conn)
    outcome = submit_approved_proposal(conn, broker, proposal_id)
    assert outcome.state == REJECTED_BY_BROKER
    assert submit_approved_proposal(conn, broker, proposal_id).already_existed
    assert broker.submissions == 0


def test_only_approved_proposals_are_sent_and_never_while_the_kill_switch_is_engaged(
    conn: Connection,
) -> None:
    waiting, _ = submit_proposal(
        conn, risk.OrderRequest("SPUS", "BUY", D(1), D(100), "t"), MANUAL, "risk-v1", "system:test"
    )
    refused, _ = submit_proposal(
        conn,
        risk.OrderRequest("SPUS", "BUY", D(1), D(100), "t"),
        risk.RiskDecision(risk.REJECTED, risk.STALE_PRICE, "old"),
        "risk-v1",
        "system:test",
    )
    broker = SimulatedBroker()
    for proposal_id in (waiting, refused):
        with pytest.raises(ExecutionRefused, match="not approved"):
            submit_approved_proposal(conn, broker, proposal_id)
    ready = approved(conn)
    engage_kill_switch(conn, "owner", "stop")
    with pytest.raises(ExecutionRefused, match="kill switch"):
        submit_approved_proposal(conn, broker, ready)
    assert broker.submissions == 0


def test_live_looking_accounts_are_refused_before_anything_is_sent(conn: Connection) -> None:
    broker = SimulatedBroker(account="U1234567")
    proposal_id = approved(conn)
    with pytest.raises(LiveTradingRefused):
        submit_approved_proposal(conn, broker, proposal_id)
    assert broker.submissions == 0


def test_partial_fills_are_tracked_and_completed_without_double_counting(
    conn: Connection,
) -> None:
    broker = SimulatedBroker(mode="partial")
    outcome = submit_approved_proposal(conn, broker, approved(conn, qty=10))
    assert outcome.state == PARTIALLY_FILLED
    assert sync_order(conn, broker, outcome.order_id) == PARTIALLY_FILLED  # nothing new
    ref = broker.find_order(idempotency_key(1)) or next(iter(broker._orders.values()))  # noqa: SLF001
    broker.fill_rest(ref.broker_ref)
    assert sync_order(conn, broker, outcome.order_id) == FILLED
    assert sync_order(conn, broker, outcome.order_id) == FILLED  # repeating adds nothing
    fills = conn.execute(
        select(func.sum(order_fill_table.c.quantity)).where(
            order_fill_table.c.order_id == outcome.order_id
        )
    ).scalar_one()
    assert fills == D(10)


def test_the_state_machine_refuses_impossible_moves(conn: Connection) -> None:
    outcome = submit_approved_proposal(conn, SimulatedBroker(), approved(conn))
    assert order_state(conn, outcome.order_id) == FILLED
    with pytest.raises(InvalidTransition):
        record_state(conn, outcome.order_id, ACKNOWLEDGED, "x", "going backwards")
    with pytest.raises(InvalidTransition):
        record_state(conn, outcome.order_id, FILLED, "x", "twice")


def test_test9_reconciliation_passes_when_records_and_broker_agree(conn: Connection) -> None:
    broker = SimulatedBroker()
    submit_approved_proposal(conn, broker, approved(conn, "SPUS", 10))
    submit_approved_proposal(conn, broker, approved(conn, "HLAL", 5))
    submit_approved_proposal(conn, broker, approved(conn, "SPUS", 4, "SELL"))
    result = reconcile(conn, broker)
    assert result.ok and expected_positions(conn) == {"SPUS": D(6), "HLAL": D(5)}
    assert kill_switch_engaged(conn) is False


def test_test9_a_position_mismatch_stops_new_trading(conn: Connection) -> None:
    broker = SimulatedBroker()
    submit_approved_proposal(conn, broker, approved(conn, "SPUS", 10))
    broker.tamper_position("SPUS", D(25))  # the broker says something we never did
    result = reconcile(conn, broker)
    assert not result.ok and result.position_mismatches == {"SPUS": (D(10), D(25))}
    assert kill_switch_engaged(conn) is True  # STOP_NEW_TRADING
    with pytest.raises(ExecutionRefused, match="kill switch"):
        submit_approved_proposal(conn, broker, approved(conn, "HLAL", 1))


def test_test9_a_position_nobody_asked_for_is_a_mismatch(conn: Connection) -> None:
    broker = SimulatedBroker()
    broker.tamper_position("QQQ", D(3))
    result = reconcile(conn, broker)
    assert result.position_mismatches == {"QQQ": (D(0), D(3))} and kill_switch_engaged(conn)


def test_test9_an_open_order_we_did_not_send_is_a_mismatch(conn: Connection) -> None:
    from halal_quant.trading.broker import BrokerOrder

    broker = SimulatedBroker(mode="partial")
    broker.submit(BrokerOrder("someone-else", "SPUS", "BUY", D(10), D(100)))  # outside our records
    result = reconcile(conn, broker, baseline={"SPUS": D(5)})  # even with the fill matched...
    assert result.unknown_orders == ["someone-else"] and not result.ok and kill_switch_engaged(conn)


def test_the_application_role_cannot_rewrite_order_history(engines: dict[DbRole, Engine]) -> None:
    for sql in (
        "UPDATE hq.broker_order SET quantity = 1",
        "DELETE FROM hq.order_state_event",
        "UPDATE hq.order_fill SET quantity = 1",
        "TRUNCATE hq.order_fill",
    ):
        with engines[DbRole.APP].connect() as connection, pytest.raises(ProgrammingError):
            connection.execute(text(sql))
