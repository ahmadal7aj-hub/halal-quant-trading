from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from halal_quant.data.market_data import (
    CorporateAction,
    DailyPrice,
    add_corporate_actions,
    add_prices,
)


def price(**overrides: Any) -> DailyPrice:
    fields: dict[str, Any] = {
        "security_id": 1,
        "price_date": date(2024, 3, 4),
        "open": Decimal("10"),
        "high": Decimal("11"),
        "low": Decimal("9.5"),
        "close": Decimal("10.5"),
        "adjusted_close": Decimal("10.4"),
        "volume": 1000,
        "source": "test",
        "data_version": "v-test",
    }
    return DailyPrice(**{**fields, **overrides})


@pytest.mark.parametrize("field", ["open", "high", "low", "close", "adjusted_close"])
@pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity")])
def test_non_positive_or_non_finite_prices_are_refused(field: str, bad: Decimal) -> None:
    with pytest.raises(ValidationError):
        price(**{field: bad})


def test_negative_volume_and_missing_lineage_are_refused() -> None:
    with pytest.raises(ValidationError):
        price(volume=-1)
    with pytest.raises(ValidationError):
        price(source="")
    with pytest.raises(ValidationError):
        price(data_version="")


def test_split_and_dividend_need_a_positive_value() -> None:
    base: dict[str, Any] = {
        "security_id": 1,
        "effective_date": date(2024, 3, 4),
        "source": "test",
        "data_version": "v-test",
    }
    assert CorporateAction(**base, action_type="SPLIT", value=Decimal("2")).value == 2
    assert CorporateAction(**base, action_type="DELISTING").value is None
    with pytest.raises(ValidationError):
        CorporateAction(**base, action_type="DIVIDEND", value=Decimal("-0.1"))
    for missing in (None, Decimal("0")):
        with pytest.raises(ValidationError, match="needs a value"):
            CorporateAction(**base, action_type="SPLIT", value=missing)
    with pytest.raises(ValidationError):
        CorporateAction(**base, action_type="SPINOFF")  # type: ignore[arg-type]


class Capture:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    def execute(self, statement: Any, params: Any = None) -> None:
        self.calls.append(params)


def test_empty_batches_do_nothing() -> None:
    conn = Capture()
    add_prices(conn, [])  # type: ignore[arg-type]
    add_corporate_actions(conn, [])  # type: ignore[arg-type]
    assert conn.calls == []


def test_prices_are_sent_as_one_batch() -> None:
    conn = Capture()
    add_prices(conn, [price(), price(price_date=date(2024, 3, 5))])  # type: ignore[arg-type]
    [rows] = conn.calls
    assert [r["price_date"] for r in rows] == [date(2024, 3, 4), date(2024, 3, 5)]
