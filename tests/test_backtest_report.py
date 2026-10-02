"""Benchmark alignment and the report text (made-up data)."""

from datetime import date
from decimal import Decimal

import pytest

from halal_quant.backtest.engine import START_NAV
from halal_quant.backtest.report import common_window, rebase, render_report, yearly_returns


def series(*points: tuple[str, str]) -> list[tuple[date, Decimal]]:
    return [(date.fromisoformat(d), Decimal(v)) for d, v in points]


def test_rebase_starts_at_the_starting_value_and_keeps_the_shape() -> None:
    prices = series(("2010-01-04", "50"), ("2010-01-05", "55"), ("2010-02-01", "40"))
    out = rebase(prices, date(2010, 1, 1), date(2010, 1, 31))
    assert [v for _, v in out] == [START_NAV, START_NAV * Decimal("1.1")]
    assert rebase(prices, date(2011, 1, 1), date(2011, 12, 31)) == []  # no data: nothing invented


def test_common_window_cuts_every_series_to_the_shared_days_and_restarts_them() -> None:
    strategy = series(("2010-01-04", "100"), ("2010-01-05", "110"), ("2010-01-06", "121"))
    fund = series(("2010-01-05", "20"), ("2010-01-06", "22"), ("2010-01-07", "30"))
    cut = common_window({"strategy": strategy, "fund": fund, "none": []})
    assert set(cut) == {"strategy", "fund"}  # an empty series is dropped
    assert [d for d, _ in cut["strategy"]] == [date(2010, 1, 5), date(2010, 1, 6)]
    assert cut["strategy"][0][1] == cut["fund"][0][1] == START_NAV
    assert cut["strategy"][1][1] == pytest.approx(START_NAV * Decimal("1.1"))
    assert common_window({}) == {}


def test_yearly_returns_are_measured_from_the_previous_year_end() -> None:
    nav = series(("2010-06-01", "100"), ("2010-12-31", "110"), ("2011-12-30", "99"))
    years = yearly_returns(nav)
    assert years[2010] == pytest.approx(0.10)
    assert years[2011] == pytest.approx(-0.10)


def test_the_report_names_every_series_and_says_what_has_no_data() -> None:
    nav = series(("2010-01-04", "100000"), ("2010-12-31", "110000"))
    text = render_report(
        "Backtest report: in_sample",
        "Window note.",
        {"Momentum V1 (base slippage)": nav, "SPY (buy and hold)": nav},
        ["SPUS: no price history in 2004-01-01 to 2013-12-31"],
        ["run 1: momentum-v1, slippage base, result hash abcdef123456"],
    )
    assert "| Momentum V1 (base slippage) | 10.0% |" in text
    assert "SPY (buy and hold)" in text and "## Calendar-year returns" in text
    assert "SPUS: no price history" in text and "run 1: momentum-v1" in text
    assert "not a forecast and not advice" in text
    assert "qualified scholar" in text and "has not" in text
