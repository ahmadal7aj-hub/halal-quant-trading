"""Task 8 acceptance: the database rejects duplicate and non-positive prices.

Made-up data only. Every test runs in a rolled-back transaction.
"""

import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, insert
from sqlalchemy.exc import IntegrityError

from halal_quant.core.settings import DbRole
from halal_quant.data.market_data import (
    CorporateAction,
    DailyPrice,
    add_corporate_actions,
    add_prices,
    corporate_action_table,
    daily_price_table,
    price_history,
)
from halal_quant.data.security_master import SecurityInfo, create_security

DAY = date(2024, 3, 4)


@pytest.fixture
def conn(engines: dict[DbRole, Engine]) -> Iterator[Connection]:
    with engines[DbRole.APP].connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


@pytest.fixture
def security_id(conn: Connection) -> int:
    info = SecurityInfo(source="test", source_id=uuid.uuid4().hex, company_name="Price Corp")
    return create_security(conn, info, "ZQMD", date(2020, 1, 2), actor="test", reason="test")


def raw_price(security_id: int, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "security_id": security_id,
        "price_date": DAY,
        "open": Decimal("10"),
        "high": Decimal("11"),
        "low": Decimal("9.5"),
        "close": Decimal("10.5"),
        "adjusted_close": Decimal("10.4"),
        "volume": 1000,
        "source": "test",
        "data_version": "v-test",
    }
    return {**row, **overrides}


def raw_action(security_id: int, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "security_id": security_id,
        "action_type": "SPLIT",
        "effective_date": DAY,
        "value": Decimal("2"),
        "source": "test",
        "data_version": "v-test",
    }
    return {**row, **overrides}


def test_prices_round_trip_in_date_order(conn: Connection, security_id: int) -> None:
    later = DailyPrice.model_validate(raw_price(security_id, price_date=date(2024, 3, 5)))
    earlier = DailyPrice.model_validate(raw_price(security_id))
    add_prices(conn, [later, earlier])
    assert price_history(conn, security_id, DAY, date(2024, 3, 5)) == [earlier, later]
    assert price_history(conn, security_id, date(2024, 3, 5), date(2024, 3, 5)) == [later]
    assert price_history(conn, security_id, date(2024, 3, 6), date(2024, 3, 8)) == []


def test_database_rejects_a_duplicate_price_row(conn: Connection, security_id: int) -> None:
    add_prices(conn, [DailyPrice.model_validate(raw_price(security_id))])
    with pytest.raises(IntegrityError, match="pk_daily_price"), conn.begin_nested():
        add_prices(conn, [DailyPrice.model_validate(raw_price(security_id, close=Decimal("99")))])


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"open": Decimal("0")}, "ck_daily_price_ohlc"),
        ({"high": Decimal("-1")}, "ck_daily_price_ohlc"),
        ({"low": Decimal("0")}, "ck_daily_price_ohlc"),
        ({"close": Decimal("-0.01")}, "ck_daily_price_ohlc"),
        ({"adjusted_close": Decimal("0")}, "ck_daily_price_adjusted_close"),
        ({"volume": -1}, "ck_daily_price_volume"),
        ({"source": ""}, "ck_daily_price_lineage"),
        ({"data_version": ""}, "ck_daily_price_lineage"),
    ],
)
def test_database_rejects_bad_prices_even_without_the_python_checks(
    conn: Connection, security_id: int, overrides: dict[str, Any], constraint: str
) -> None:
    with pytest.raises(IntegrityError, match=constraint), conn.begin_nested():
        conn.execute(insert(daily_price_table).values(**raw_price(security_id, **overrides)))


def test_price_for_an_unknown_security_is_rejected(conn: Connection) -> None:
    with pytest.raises(IntegrityError, match="fk_daily_price_security"), conn.begin_nested():
        conn.execute(insert(daily_price_table).values(**raw_price(-1)))


def test_prices_that_are_only_suspicious_are_stored_for_the_quality_report(
    conn: Connection, security_id: int
) -> None:
    # high below low is a data-quality finding (task 12), not something to refuse or lose.
    odd = DailyPrice.model_validate(raw_price(security_id, high=Decimal("9"), low=Decimal("12")))
    add_prices(conn, [odd])
    assert price_history(conn, security_id, DAY, DAY) == [odd]


def test_corporate_actions_of_every_type_are_stored(conn: Connection, security_id: int) -> None:
    base = {"security_id": security_id, "effective_date": DAY, "source": "t", "data_version": "v"}
    add_corporate_actions(
        conn,
        [
            CorporateAction(**base, action_type="SPLIT", value=Decimal("0.1")),
            CorporateAction(**base, action_type="DIVIDEND", value=Decimal("0.25")),
            CorporateAction(**base, action_type="MERGER", details={"acquirer": "Other Corp"}),
            CorporateAction(**base, action_type="SYMBOL_CHANGE", details={"new_ticker": "ZQ2"}),
            CorporateAction(**base, action_type="DELISTING", details={"reason": "acquired"}),
        ],
    )
    count = conn.execute(corporate_action_table.select()).all()
    assert len(count) == 5


def test_database_rejects_the_same_corporate_action_twice(
    conn: Connection, security_id: int
) -> None:
    add_corporate_actions(conn, [CorporateAction.model_validate(raw_action(security_id))])
    with pytest.raises(IntegrityError, match="uq_corporate_action_event"), conn.begin_nested():
        add_corporate_actions(conn, [CorporateAction.model_validate(raw_action(security_id))])


def test_duplicate_valueless_actions_are_also_rejected(conn: Connection, security_id: int) -> None:
    delisting = raw_action(security_id, action_type="DELISTING", value=None)
    add_corporate_actions(conn, [CorporateAction.model_validate(delisting)])
    with pytest.raises(IntegrityError, match="uq_corporate_action_event"), conn.begin_nested():
        add_corporate_actions(conn, [CorporateAction.model_validate(delisting)])


def test_two_different_dividends_on_one_day_are_allowed(conn: Connection, security_id: int) -> None:
    regular = raw_action(security_id, action_type="DIVIDEND", value=Decimal("0.25"))
    special = raw_action(security_id, action_type="DIVIDEND", value=Decimal("1.00"))
    add_corporate_actions(conn, [CorporateAction.model_validate(x) for x in (regular, special)])


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"action_type": "SPLIT", "value": Decimal("0")}, "ck_corporate_action_value"),
        ({"action_type": "DIVIDEND", "value": None}, "ck_corporate_action_value"),
        ({"action_type": "MERGER", "value": Decimal("-1")}, "ck_corporate_action_value_sign"),
        ({"action_type": "SPINOFF"}, "ck_corporate_action_type"),
        ({"source": ""}, "ck_corporate_action_lineage"),
    ],
)
def test_database_rejects_bad_corporate_actions(
    conn: Connection, security_id: int, overrides: dict[str, Any], constraint: str
) -> None:
    with pytest.raises(IntegrityError, match=constraint), conn.begin_nested():
        conn.execute(insert(corporate_action_table).values(**raw_action(security_id, **overrides)))
