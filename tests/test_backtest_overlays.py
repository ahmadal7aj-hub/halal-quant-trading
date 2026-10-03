"""Stage A2 overlays: low-volatility ranking and the trend filter (made-up data)."""

from datetime import date
from decimal import Decimal

from halal_quant.backtest.config import OverlayConfig
from halal_quant.backtest.data import above_average
from halal_quant.backtest.engine import START_NAV, rank, run_backtest, volatility_scores
from halal_quant.data.calendar import trading_days
from tests.test_backtest_engine import D1, D2, D3, END, ZERO, FakePrices, FakeUniverse, path


def lowvol(top_n: int = 1, lookback: int = 5) -> OverlayConfig:
    return OverlayConfig(
        config_type="strategy_overlay",
        version="test-lowvol",
        status="proposed",
        lookback_trading_days=lookback,
        skip_trading_days=0,
        top_n=top_n,
        weighting="equal",
        rebalance="first_trading_day_of_month",
        ranking="low_volatility",
        trend_filter_days=200,
    )


def swinging(amount: str) -> dict[date, tuple[Decimal, Decimal]]:
    """A path that alternates up and down by `amount` each trading day."""
    out, price, up = {}, Decimal(100), True
    for day in trading_days(date(2019, 9, 2), date(2020, 5, 29)):
        out[day] = (price, price)
        price *= (1 + Decimal(amount)) if up else (1 - Decimal(amount))
        up = not up
    return out


PRICES = {1: path("100", "1.001"), 2: swinging("0.02"), 3: swinging("0.05")}


def test_the_calmest_stock_ranks_first_and_the_window_ends_before_the_decision() -> None:
    scores, start, end = volatility_scores([1, 2, 3], D2, lowvol(), FakePrices(PRICES))
    assert rank(scores) == [1, 2, 3]
    assert end < D2 and start < end
    assert scores[1] == 0  # a steady climb has no spread at all


def test_a_stock_with_too_little_history_is_not_called_calm() -> None:
    thin = {d: p for d, p in PRICES[1].items() if d >= date(2020, 2, 27)}  # 2 days in the window
    scores, _, _ = volatility_scores([1, 2], D2, lowvol(), FakePrices({1: thin, 2: PRICES[2]}))
    assert 1 not in scores and 2 in scores


def test_the_calmest_stocks_are_the_ones_bought_and_the_reason_says_so() -> None:
    result = run_backtest(
        [D1], D1, FakeUniverse({D1: [1, 2, 3]}), FakePrices(PRICES), lowvol(top_n=1), ZERO,
        Decimal(0), Decimal(0),
    )  # fmt: skip
    assert [t.security_id for t in result.trades] == [1]
    assert "volatility" in result.trades[0].reason and "ranked 1 of 3" in result.trades[0].reason


def test_when_the_trend_filter_is_off_everything_is_sold_and_cash_is_held() -> None:
    flat = {1: path("100", "1.0"), 2: path("100", "1.0")}
    result = run_backtest(
        [D1, D2, D3], END, FakeUniverse({d: [1, 2] for d in (D1, D2, D3)}), FakePrices(flat),
        lowvol(top_n=2), ZERO, Decimal(0), Decimal(0), risk_on=lambda day: day != D2,
    )  # fmt: skip
    march = [t for t in result.trades if t.trade_date == D2]
    assert {t.side for t in march} == {"SELL"} and all(
        "trend filter off" in t.reason for t in march
    )
    assert [r.selected for r in result.rebalances] == [2, 0, 2]  # back in when the filter is on
    assert {v for d, v in result.nav if D2 <= d < D3} == {START_NAV}  # cash on a flat market


def test_without_a_filter_nothing_changes() -> None:
    flat = {1: path("100", "1.0"), 2: path("100", "1.0")}
    kwargs = dict(universe=FakeUniverse({D1: [1, 2]}), prices=FakePrices(flat))
    on = run_backtest([D1], D2, kwargs["universe"], kwargs["prices"], lowvol(2), ZERO,  # type: ignore[arg-type]
                      Decimal(0), Decimal(0), risk_on=lambda day: True)  # fmt: skip
    off = run_backtest([D1], D2, kwargs["universe"], kwargs["prices"], lowvol(2), ZERO,  # type: ignore[arg-type]
                       Decimal(0), Decimal(0))  # fmt: skip
    assert on.nav == off.nav and on.trades == off.trades


def test_above_average_compares_the_newest_close_with_the_mean() -> None:
    assert above_average([Decimal(110), Decimal(100), Decimal(100)])
    assert not above_average([Decimal(90), Decimal(100), Decimal(100)])
    assert not above_average([Decimal(100), Decimal(100)])  # equal is not above


def by_cap(top_n: int = 1) -> OverlayConfig:
    return OverlayConfig(
        config_type="strategy_overlay",
        version="test-cap",
        status="proposed",
        lookback_trading_days=5,
        skip_trading_days=0,
        top_n=top_n,
        weighting="equal",
        rebalance="first_trading_day_of_month",
        ranking="market_cap",
    )


def test_the_largest_companies_are_bought_using_the_value_before_the_decision_day() -> None:
    asked: list[date] = []

    def caps(ids: object, day: date) -> dict[int, Decimal]:
        asked.append(day)
        return {1: Decimal(5_000_000_000), 2: Decimal(80_000_000_000), 3: Decimal(0)}

    result = run_backtest(
        [D1], D1, FakeUniverse({D1: [1, 2, 3]}), FakePrices(PRICES), by_cap(top_n=2), ZERO,
        Decimal(0), Decimal(0), market_caps=caps,
    )  # fmt: skip
    assert sorted(t.security_id for t in result.trades) == [1, 2]  # 3 has no usable value
    assert asked == [date(2020, 1, 31)]  # the day before 3 Feb 2020 (D1), never D1 itself
    assert "ranked 1 of 2" in next(t for t in result.trades if t.security_id == 2).reason
    assert "$80.0 billion" in next(t for t in result.trades if t.security_id == 2).reason


def test_a_market_value_ranking_without_a_source_fails_loudly() -> None:
    import pytest

    with pytest.raises(ValueError, match="market value source"):
        run_backtest(
            [D1], D1, FakeUniverse({D1: [1]}), FakePrices(PRICES), by_cap(), ZERO,
            Decimal(0), Decimal(0),
        )  # fmt: skip
