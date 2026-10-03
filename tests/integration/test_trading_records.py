"""Phase 4: the kill switch and the proposal approval trail are add-only (made-up data)."""

from collections.abc import Iterator
from decimal import Decimal

import pytest
from sqlalchemy import Connection, Engine, func, select, text
from sqlalchemy.exc import ProgrammingError

from halal_quant.audit import audit_event_table
from halal_quant.core.settings import DbRole
from halal_quant.trading import risk
from halal_quant.trading.records import (
    APPROVED_STATUS,
    AWAITING_APPROVAL,
    REJECTED_BY_OWNER,
    RISK_REJECTED,
    ProposalError,
    decide_proposal,
    engage_kill_switch,
    kill_switch_engaged,
    kill_switch_event_table,
    proposal_event_table,
    proposal_status,
    release_kill_switch,
    submit_proposal,
    trade_proposal_table,
)

D = Decimal


@pytest.fixture
def conn(engines: dict[DbRole, Engine]) -> Iterator[Connection]:
    with engines[DbRole.APP].connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


def order(shares: int = 100, price: int = 100) -> risk.OrderRequest:
    return risk.OrderRequest("SPUS", "BUY", D(shares), D(price), "fund core top-up")


MANUAL = risk.RiskDecision(risk.REQUIRES_MANUAL_REVIEW, risk.MANUAL_REVIEW_SIZE, "over the limit")
OK = risk.RiskDecision(risk.APPROVED, risk.OK, "All risk checks passed.")
REFUSED = risk.RiskDecision(risk.REJECTED, risk.STALE_PRICE, "price too old")


def test_the_kill_switch_state_is_the_latest_event_and_every_change_is_audited(
    conn: Connection,
) -> None:
    start = kill_switch_engaged(conn)  # whatever the real database holds; restored by the rollback
    engage_kill_switch(conn, "owner", "testing the switch")
    assert kill_switch_engaged(conn) is True
    release_kill_switch(conn, "owner", "test over")
    assert kill_switch_engaged(conn) is False
    engage_kill_switch(conn, "owner", "again")
    assert kill_switch_engaged(conn) is True
    log = conn.execute(select(func.count()).select_from(kill_switch_event_table)).scalar_one()
    assert log >= 3
    audited = conn.execute(
        select(func.count()).where(audit_event_table.c.action.like("kill_switch.%"))
    ).scalar_one()
    assert audited >= 3
    assert start in (True, False)


def test_the_kill_switch_needs_an_actor_and_a_reason(conn: Connection) -> None:
    with pytest.raises(ValueError, match="actor and a reason"):
        engage_kill_switch(conn, "owner", "  ")
    with pytest.raises(ValueError, match="actor and a reason"):
        release_kill_switch(conn, "", "because")


def test_a_manual_review_proposal_waits_then_the_owner_approves_it(conn: Connection) -> None:
    proposal_id, status = submit_proposal(conn, order(), MANUAL, "risk-v1", "system:test")
    assert status == AWAITING_APPROVAL and proposal_status(conn, proposal_id) == AWAITING_APPROVAL
    assert decide_proposal(conn, proposal_id, True, "owner", "looks right") == APPROVED_STATUS
    assert proposal_status(conn, proposal_id) == APPROVED_STATUS
    with pytest.raises(ProposalError, match="awaiting approval"):
        decide_proposal(conn, proposal_id, False, "owner", "changed my mind")  # decided once only
    events = (
        conn.execute(
            select(proposal_event_table.c.status)
            .where(proposal_event_table.c.proposal_id == proposal_id)
            .order_by(proposal_event_table.c.id)
        )
        .scalars()
        .all()
    )
    assert events == [AWAITING_APPROVAL, APPROVED_STATUS]


def test_the_owner_can_reject_and_a_reason_is_required(conn: Connection) -> None:
    proposal_id, _ = submit_proposal(conn, order(), MANUAL, "risk-v1", "system:test")
    with pytest.raises(ValueError, match="needs a reason"):
        decide_proposal(conn, proposal_id, False, "owner", " ")
    assert decide_proposal(conn, proposal_id, False, "owner", "not now") == REJECTED_BY_OWNER


def test_a_risk_rejected_order_is_stored_and_can_never_be_approved_by_hand(
    conn: Connection,
) -> None:
    proposal_id, status = submit_proposal(conn, order(), REFUSED, "risk-v1", "system:test")
    assert status == RISK_REJECTED
    with pytest.raises(ProposalError):
        decide_proposal(conn, proposal_id, True, "owner", "override")
    row = conn.execute(
        select(trade_proposal_table).where(trade_proposal_table.c.id == proposal_id)
    ).one()
    assert (row.risk_result, row.reason_code, row.notional) == (
        "REJECTED",
        "STALE_PRICE",
        D("10000.00"),
    )


def test_an_automatically_approved_order_starts_approved(conn: Connection) -> None:
    proposal_id, status = submit_proposal(conn, order(10), OK, "risk-v1", "system:test")
    assert status == APPROVED_STATUS == proposal_status(conn, proposal_id)


def test_nothing_can_be_approved_while_the_kill_switch_is_engaged(conn: Connection) -> None:
    proposal_id, _ = submit_proposal(conn, order(), MANUAL, "risk-v1", "system:test")
    engage_kill_switch(conn, "owner", "stop everything")
    with pytest.raises(ProposalError, match="kill switch"):
        decide_proposal(conn, proposal_id, True, "owner", "approve")
    assert decide_proposal(conn, proposal_id, False, "owner", "reject is still fine") == (
        REJECTED_BY_OWNER
    )


def test_the_application_role_cannot_rewrite_history(engines: dict[DbRole, Engine]) -> None:
    for sql in (
        "UPDATE hq.trade_proposal SET quantity = 1",
        "DELETE FROM hq.proposal_event",
        "UPDATE hq.kill_switch_event SET engaged = false",
        "DELETE FROM hq.kill_switch_event",
        "TRUNCATE hq.trade_proposal CASCADE",
    ):
        with engines[DbRole.APP].connect() as connection, pytest.raises(ProgrammingError):
            connection.execute(text(sql))
