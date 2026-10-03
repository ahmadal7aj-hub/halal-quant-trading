"""The paper-only guard and the simulated broker (made-up data; no real broker is involved)."""

from decimal import Decimal

import pytest

from halal_quant.trading.broker import (
    AmbiguousSubmit,
    BrokerNotConnected,
    BrokerOrder,
    BrokerRejected,
    LiveTradingRefused,
    SimulatedBroker,
    assert_paper_account,
    assert_paper_only,
)

D = Decimal


@pytest.mark.parametrize("port", [4002, 7497])
def test_paper_account_on_a_paper_port_is_allowed(port: int) -> None:
    assert_paper_only("DU1234567", port)


@pytest.mark.parametrize("port", [4001, 7496])
def test_the_live_ports_are_always_refused(port: int) -> None:
    with pytest.raises(LiveTradingRefused, match="LIVE"):
        assert_paper_only("DU1234567", port)


def test_an_unknown_port_or_a_non_paper_account_is_refused() -> None:
    with pytest.raises(LiveTradingRefused, match="not a known paper port"):
        assert_paper_only("DU1234567", 5000)
    for account in ("U1234567", "I9999999", ""):
        with pytest.raises(LiveTradingRefused, match="not a paper account"):
            assert_paper_account(account)
        with pytest.raises(LiveTradingRefused):
            assert_paper_only(account, 4002)


def order(ref: str = "k1", side: str = "BUY", qty: int = 10) -> BrokerOrder:
    return BrokerOrder(ref, "SPUS", side, D(qty), D(100))  # type: ignore[arg-type]


def test_the_simulated_broker_fills_and_keeps_cash_and_positions() -> None:
    broker = SimulatedBroker()
    info = broker.submit(order())
    assert info.status == "FILLED" and broker.positions() == {"SPUS": D(10)}
    assert broker.cash() == D(100_000) - D(1_000) and broker.submissions == 1
    broker.submit(order("k2", "SELL", 10))
    assert broker.positions() == {} and broker.cash() == D(100_000)


def test_the_simulated_broker_does_not_dedupe_so_the_platform_must() -> None:
    broker = SimulatedBroker()
    broker.submit(order("same"))
    broker.submit(order("same"))
    assert broker.submissions == 2 and broker.positions() == {"SPUS": D(20)}


def test_failure_modes() -> None:
    broker = SimulatedBroker(connected=False)
    with pytest.raises(BrokerNotConnected):
        broker.submit(order())
    with pytest.raises(BrokerNotConnected):
        broker.find_order("k1")
    assert broker.submissions == 0
    broker = SimulatedBroker(mode="reject")
    with pytest.raises(BrokerRejected):
        broker.submit(order())
    assert broker.submissions == 0
    broker = SimulatedBroker(mode="lose_ack")
    with pytest.raises(AmbiguousSubmit):
        broker.submit(order("lost"))
    assert broker.submissions == 1 and broker.find_order("lost") is not None  # it DID arrive


def test_partial_fills_complete_later() -> None:
    broker = SimulatedBroker(mode="partial")
    info = broker.submit(order(qty=10))
    assert info.status == "PARTIALLY_FILLED" and broker.positions() == {"SPUS": D(5)}
    assert [o.client_ref for o in broker.open_orders()] == ["k1"]
    broker.fill_rest(info.broker_ref)
    done = broker.find_order("k1")
    assert done is not None and done.status == "FILLED" and broker.positions() == {"SPUS": D(10)}
