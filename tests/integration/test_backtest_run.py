"""P2-6: a stored backtest run is reproducible, guarded and audited (made-up data)."""

import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, func, select

from halal_quant.audit import audit_event_table
from halal_quant.backtest.config import ProtocolError, load_momentum, load_protocol
from halal_quant.backtest.records import (
    ReproducibilityError,
    backtest_nav_table,
    backtest_rebalance_table,
    backtest_run_table,
    backtest_trade_table,
)
from halal_quant.backtest.run import RunOutcome, decision_dates, describe, execute_run
from halal_quant.core.config import UniverseConfig, load_config
from halal_quant.core.settings import DbRole
from halal_quant.data.security_master import SecurityInfo, create_security
from halal_quant.data.vintage import VintageError, finish_vintage, start_vintage
from tests.test_backtest_engine import FakePrices, FakeUniverse, path

FIRST, LAST = date(2010, 2, 1), date(2010, 4, 30)  # inside the in-sample period

PROTOCOL = load_protocol(Path("config/research/protocol_v1.yaml"))
STRATEGY = load_momentum(Path("config/strategy/momentum_v1.yaml"))
UNIVERSE = load_config(Path("config/universe/default.yaml"), UniverseConfig)


@pytest.fixture
def conn(engines: dict[DbRole, Engine]) -> Iterator[Connection]:
    with engines[DbRole.APP].connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


def make_security(conn: Connection) -> int:
    info = SecurityInfo(source="test", source_id=uuid.uuid4().hex, company_name="Backtest Corp")
    ticker = "ZB" + uuid.uuid4().hex[:6].upper()
    return create_security(conn, info, ticker, date(2009, 1, 2), actor="test", reason="test")


@pytest.fixture
def vintage(conn: Connection) -> str:
    vintage_id = "t" + uuid.uuid4().hex[:10]
    start_vintage(conn, vintage_id, "backtest test vintage")
    finish_vintage(conn, vintage_id)
    return vintage_id


def sources(conn: Connection) -> tuple[FakePrices, FakeUniverse, list[int]]:
    ids = [make_security(conn), make_security(conn)]
    first = date(2009, 6, 1)
    prices = FakePrices(
        {ids[0]: path("100", "1.002", first=first), ids[1]: path("100", "1.001", first=first)}
    )
    universe = FakeUniverse({d: list(ids) for d in decision_dates(FIRST, LAST)})
    return prices, universe, ids


def run_once(
    conn: Connection,
    vintage: str,
    prices: FakePrices,
    universe: FakeUniverse,
    first: date = FIRST,
    last: date = LAST,
    final_test: bool = False,
) -> RunOutcome:
    return execute_run(
        conn,
        vintage,
        STRATEGY,
        PROTOCOL,
        UNIVERSE,
        first,
        last,
        "base",
        final_test,
        prices=prices,
        universe_source=universe,
        actor="test",
    )


def test_a_run_is_stored_with_nav_trades_rebalances_and_an_audit_event(
    conn: Connection, vintage: str
) -> None:
    prices, universe, _ = sources(conn)
    outcome = run_once(conn, vintage, prices, universe)
    assert outcome.reused is False and outcome.period_name == "in_sample"
    run = conn.execute(
        select(backtest_run_table).where(backtest_run_table.c.id == outcome.run_id)
    ).one()
    assert run.vintage_id == vintage and run.result_sha256 == outcome.result_sha256
    assert run.slippage_bps == Decimal("10") and run.final_test is False
    for table in (backtest_nav_table, backtest_trade_table, backtest_rebalance_table):
        count = conn.execute(
            select(func.count()).select_from(table).where(table.c.run_id == outcome.run_id)
        ).scalar_one()
        assert count > 0
    audited = conn.execute(
        select(func.count()).where(
            audit_event_table.c.action == "backtest.run",
            audit_event_table.c.entity_id == str(outcome.run_id),
        )
    ).scalar_one()
    assert audited == 1
    assert "stored" in describe(outcome)


def test_running_the_same_inputs_again_reproduces_the_stored_result(
    conn: Connection, vintage: str
) -> None:
    prices, universe, _ = sources(conn)
    first = run_once(conn, vintage, prices, universe)
    again = run_once(conn, vintage, prices, universe)
    assert again.reused is True
    assert (again.run_id, again.result_sha256) == (first.run_id, first.result_sha256)
    assert "REPRODUCED" in describe(again)


def test_a_different_result_for_the_same_inputs_fails_loudly(
    conn: Connection, vintage: str
) -> None:
    prices, universe, ids = sources(conn)
    run_once(conn, vintage, prices, universe)
    for day in list(prices.data[ids[0]]):
        if day >= date(2010, 3, 1):
            adjusted, unadjusted = prices.data[ids[0]][day]
            prices.data[ids[0]][day] = (adjusted * 2, unadjusted * 2)  # the data changed
    with pytest.raises(ReproducibilityError):
        run_once(conn, vintage, prices, universe)


def test_a_vintage_that_is_not_complete_is_refused(conn: Connection) -> None:
    building = "t" + uuid.uuid4().hex[:10]
    start_vintage(conn, building, "still downloading")
    prices, universe, _ = sources(conn)
    with pytest.raises(VintageError, match="complete"):
        run_once(conn, building, prices, universe)
    with pytest.raises(VintageError, match="None"):
        run_once(conn, "no-such-vintage", prices, universe)


def test_out_of_sample_is_refused_and_nothing_is_stored(conn: Connection, vintage: str) -> None:
    prices, universe, _ = sources(conn)
    before = conn.execute(select(func.count()).select_from(backtest_run_table)).scalar_one()
    with pytest.raises(ProtocolError):
        run_once(conn, vintage, prices, universe, date(2019, 1, 2), date(2019, 3, 29))
    after = conn.execute(select(func.count()).select_from(backtest_run_table)).scalar_one()
    assert after == before
