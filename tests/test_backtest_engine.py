"""The backtest engine on made-up prices: selection, no look-ahead, weights, costs, delisting."""

from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal

from halal_quant.backtest.config import Costs, MomentumConfig
from halal_quant.backtest.engine import (
    START_NAV,
    PricePoint,
    order_cost,
    rank,
    run_backtest,
    score_members,
    shift_back,
)
from halal_quant.backtest.records import result_hash
from halal_quant.data.calendar import trading_days

D1, D2, D3 = date(2020, 2, 3), date(2020, 3, 2), date(2020, 4, 1)  # first trading days
END = date(2020, 4, 30)
ZERO = Costs(
    commission_per_share=Decimal(0),
    commission_min_per_order=Decimal(0),
    commission_max_fraction=Decimal(1),
    slippage_bps={"optimistic": Decimal(0), "base": Decimal(0), "stress": Decimal(0)},
)
REAL = Costs(
    commission_per_share=Decimal("0.005"),
    commission_min_per_order=Decimal("1.00"),
    commission_max_fraction=Decimal("0.01"),
    slippage_bps={"optimistic": Decimal(5), "base": Decimal(10), "stress": Decimal(25)},
)


def strategy(top_n: int = 2, lookback: int = 5, skip: int = 1) -> MomentumConfig:
    return MomentumConfig(
        config_type="strategy_momentum",
        version="test",
        status="proposed",
        lookback_trading_days=lookback,
        skip_trading_days=skip,
        top_n=top_n,
        weighting="equal",
        rebalance="first_trading_day_of_month",
    )


class FakePrices:
    def __init__(self, data: dict[int, dict[date, tuple[Decimal, Decimal]]]) -> None:
        self.data = data

    def on_or_before(
        self, security_ids: Sequence[int], day: date, max_age_days: int
    ) -> dict[int, PricePoint]:
        found = {}
        for sid in security_ids:
            days = [
                d for d in self.data.get(sid, {}) if day - timedelta(days=max_age_days) < d <= day
            ]
            if days:
                latest = max(days)
                found[sid] = PricePoint(latest, *self.data[sid][latest])
        return found

    def series(
        self, security_ids: Sequence[int], first: date, last: date
    ) -> dict[int, dict[date, tuple[Decimal, Decimal]]]:
        return {
            sid: {d: p for d, p in self.data.get(sid, {}).items() if first <= d <= last}
            for sid in security_ids
        }


class FakeUniverse:
    def __init__(self, members: dict[date, list[int]]) -> None:
        self.by_date = members

    def members(self, as_of: date) -> list[int]:
        return list(self.by_date.get(as_of, []))

    def describe(self, security_id: int, as_of: date) -> str:
        return f"Sharia screen: NON_HALAL (made-up reason for {security_id})"


def path(
    start: str, daily: str = "1", first: date = date(2019, 9, 2), last: date = date(2020, 5, 29),
    stop: date | None = None, unadjusted: str | None = None,
) -> dict[date, tuple[Decimal, Decimal]]:  # fmt: skip
    """A price path compounding by `daily` each trading day; adjusted and unadjusted equal."""
    out, price = {}, Decimal(start)
    for day in trading_days(first, last):
        if stop and day > stop:
            break
        out[day] = (price, Decimal(unadjusted) if unadjusted else price)
        price = price * Decimal(daily)
    return out


def run(
    prices: dict[int, dict[date, tuple[Decimal, Decimal]]],
    members: dict[date, list[int]],
    top_n: int = 2,
    costs: Costs = ZERO,
    slippage: str = "0",
    drag: str = "0",
    decisions: tuple[date, ...] = (D1, D2, D3),
    last: date = END,
) -> object:
    return run_backtest(
        list(decisions), last, FakeUniverse(members), FakePrices(prices), strategy(top_n),
        costs, Decimal(slippage), Decimal(drag),
    )  # fmt: skip


def test_the_score_window_ends_strictly_before_the_decision_day_so_nothing_later_is_read() -> None:
    honest = {1: path("100", "1.01"), 2: path("100", "1.00")}
    poisoned = {
        sid: {d: (p[0] * (1000 if d >= D2 else 1), p[1]) for d, p in series.items()}
        for sid, series in honest.items()
    }
    a, start_a, end_a = score_members([1, 2], D2, strategy(), FakePrices(honest))
    b, start_b, end_b = score_members([1, 2], D2, strategy(), FakePrices(poisoned))
    assert a == b and (start_a, end_a) == (start_b, end_b)
    assert end_a < D2  # strictly before the decision day (2 Mar 2020)


def test_the_window_is_shifted_by_the_skip_and_lookback_in_trading_days() -> None:
    assert shift_back(date(2020, 3, 2), 0) == date(2020, 3, 2)
    assert shift_back(date(2020, 3, 2), 1) == date(2020, 2, 28)
    assert shift_back(date(2020, 3, 2), 5) == date(2020, 2, 24)
    _, start, end = score_members(
        [1], D2, strategy(lookback=5, skip=1), FakePrices({1: path("100")})
    )
    assert (start, end) == (date(2020, 2, 20), date(2020, 2, 27))  # skip 1 day, then 5 back


def test_the_best_momentum_is_held_and_ties_break_by_security_id() -> None:
    prices = {
        1: path("100", "1.001"),
        2: path("100", "1.002"),
        3: path("100", "1.003"),
        4: path("100", "1.003"),
    }
    scores, _, _ = score_members([1, 2, 3, 4], D1, strategy(), FakePrices(prices))
    assert rank(scores) == [3, 4, 2, 1]  # 3 and 4 tie: lower id first
    result = run(prices, {D1: [1, 2, 3, 4]}, decisions=(D1,))
    bought = [t.security_id for t in result.trades]  # type: ignore[attr-defined]
    assert bought == [3, 4]


def test_money_is_split_equally_and_a_flat_market_leaves_the_value_unchanged() -> None:
    prices = {1: path("100", "1.0"), 2: path("50", "1.0")}
    result = run(prices, {D1: [1, 2]}, decisions=(D1,), last=D2)
    assert [t.notional for t in result.trades] == [Decimal("50000.00")] * 2  # type: ignore[attr-defined]
    assert {v for _, v in result.nav} == {START_NAV}  # type: ignore[attr-defined]


def test_a_holding_that_doubles_lifts_the_portfolio_by_half_when_two_are_held() -> None:
    prices = {1: path("100", "1.0"), 2: path("100", "1.0")}
    for day in trading_days(date(2020, 2, 10), date(2020, 5, 29)):
        prices[2][day] = (Decimal(200), Decimal(200))
    result = run(prices, {D1: [1, 2]}, decisions=(D1,), last=date(2020, 2, 28))
    assert result.nav[-1][1] == Decimal(150_000)  # type: ignore[attr-defined]


def test_costs_are_slippage_plus_commission_and_reduce_the_value() -> None:
    prices = {1: path("100", "1.0"), 2: path("100", "1.0")}
    result = run(prices, {D1: [1, 2]}, costs=REAL, slippage="10", decisions=(D1,), last=D1)
    # two orders of 50,000: slippage 10 bps = 50 each; commission 500 shares x 0.005 = 2.50 each
    assert [t.cost for t in result.trades] == [Decimal("52.5000")] * 2  # type: ignore[attr-defined]
    assert result.rebalances[0].costs == Decimal("105")  # type: ignore[attr-defined]
    assert result.nav[0][1] == START_NAV - Decimal("105")  # type: ignore[attr-defined]


def test_commission_has_a_minimum_per_order_and_a_cap_as_a_fraction_of_the_value() -> None:
    cost = order_cost(Decimal("100"), Decimal("50"), REAL, Decimal(0))
    assert cost == Decimal("1.00")  # 2 shares x 0.005 = 0.01 -> the $1 minimum
    cap = order_cost(Decimal("1000"), Decimal("0.10"), REAL, Decimal(0))
    assert cap == Decimal("10.00")  # 10,000 shares x 0.005 = 50 -> capped at 1% of 1,000
    assert order_cost(Decimal("1000"), Decimal("50"), REAL, Decimal(25)) == Decimal("3.5")


def test_a_stock_that_leaves_the_top_is_sold_with_its_rank_and_a_kept_one_is_rebalanced() -> None:
    flat = {1: path("100", "1.003"), 2: path("100", "1.002"), 3: path("100", "1.001")}
    # from 10 February on, stock 3 grows faster than the others, so it overtakes stock 2
    price = flat[3][date(2020, 2, 7)][0]
    for day in trading_days(date(2020, 2, 10), date(2020, 5, 29)):
        price *= Decimal("1.01")
        flat[3][day] = (price, price)
    result = run(flat, {D1: [1, 2, 3], D2: [1, 2, 3]}, decisions=(D1, D2), last=D2)
    second_month = [t for t in result.trades if t.trade_date == D2]  # type: ignore[attr-defined]
    by_id = {t.security_id: t for t in second_month}
    assert by_id[2].side == "SELL" and by_id[2].kind == "close"
    assert "fell out of the top 2" in by_id[2].reason and "rank 3 of 3" in by_id[2].reason
    assert by_id[3].side == "BUY" and by_id[3].kind == "open" and "ranked 1 of 3" in by_id[3].reason
    assert by_id[1].kind == "rebalance" and "equal weight" in by_id[1].reason


def test_a_stock_that_leaves_the_universe_is_sold_and_the_reason_says_why() -> None:
    prices = {1: path("100", "1.003"), 2: path("100", "1.002")}
    result = run(prices, {D1: [1, 2], D2: [1]}, decisions=(D1, D2), last=D2)
    sold = [t for t in result.trades if t.trade_date == D2 and t.side == "SELL"]  # type: ignore[attr-defined]
    assert [t.security_id for t in sold] == [2]
    assert "no longer eligible on 2020-03-02" in sold[0].reason
    assert "NON_HALAL (made-up reason for 2)" in sold[0].reason


def test_a_top_ranked_stock_with_no_price_today_cannot_be_bought_and_the_next_one_is() -> None:
    prices = {1: path("100", "1.003"), 2: path("100", "1.002"), 3: path("100", "1.001")}
    prices[1] = {d: p for d, p in prices[1].items() if d != D1}  # stock 1 has no price on D1
    result = run(prices, {D1: [1, 2, 3]}, decisions=(D1,), last=D1)
    assert sorted(t.security_id for t in result.trades) == [2, 3]  # type: ignore[attr-defined]


def test_a_delisted_holding_is_carried_at_its_last_price_and_closed_at_the_next_rebalance() -> None:
    prices = {1: path("100", "1.0"), 2: path("100", "1.0", stop=date(2020, 2, 14))}
    prices[2] = {
        d: (Decimal(40), Decimal(40)) if d > date(2020, 2, 10) else p for d, p in prices[2].items()
    }
    result = run(prices, {D1: [1, 2], D2: [1]}, decisions=(D1, D2), last=D3)
    values = dict(result.nav)  # type: ignore[attr-defined]
    # stock 2 fell 60% by its last price (14 Feb) and is carried at that price afterwards
    assert values[date(2020, 2, 28)] == Decimal(50_000) + Decimal(50_000) * Decimal("0.4")
    sold = [t for t in result.trades if t.security_id == 2 and t.side == "SELL"]  # type: ignore[attr-defined]
    assert len(sold) == 1 and sold[0].trade_date == D2
    assert sold[0].price == Decimal(40)  # closed at the last available price: no invented recovery


def test_with_no_eligible_stocks_everything_is_cash_and_with_fewer_than_n_the_rest_are_equal() -> (
    None
):
    prices = {1: path("100", "1.001"), 2: path("100", "1.002")}
    empty = run(prices, {}, decisions=(D1,), last=D2)
    assert not empty.trades and {v for _, v in empty.nav} == {START_NAV}  # type: ignore[attr-defined]
    one = run(prices, {D1: [1]}, top_n=5, decisions=(D1,), last=D1)
    assert [t.notional for t in one.trades] == [Decimal("100000.00")]  # type: ignore[attr-defined]


def test_a_stock_with_too_little_history_gets_no_score_and_is_not_bought() -> None:
    prices = {1: path("100", "1.001"), 2: path("100", "1.002", first=date(2020, 1, 30))}
    result = run(prices, {D1: [1, 2]}, decisions=(D1,), last=D1)
    assert [t.security_id for t in result.trades] == [1]  # type: ignore[attr-defined]
    assert result.rebalances[0].scored == 1  # type: ignore[attr-defined]


def test_the_purification_drag_reduces_the_value_a_little_each_day() -> None:
    prices = {1: path("100", "1.0")}
    result = run(prices, {D1: [1]}, drag="0.0252", decisions=(D1,), last=date(2020, 2, 7))
    days = len(result.nav) - 1  # type: ignore[attr-defined]
    expected = START_NAV * (1 - Decimal("0.0252") / 252) ** days
    assert abs(result.nav[-1][1] - expected) < Decimal("0.0001")  # type: ignore[attr-defined]


def test_the_same_inputs_give_the_same_result_whatever_the_order_of_the_members() -> None:
    prices = {i: path("100", f"1.00{i}") for i in range(1, 6)}
    first = run(
        prices,
        {D1: [1, 2, 3, 4, 5], D2: [5, 4, 3, 2, 1]},
        costs=REAL,
        slippage="10",
        decisions=(D1, D2),
    )
    again = run(
        prices,
        {D1: [5, 3, 1, 4, 2], D2: [1, 2, 3, 4, 5]},
        costs=REAL,
        slippage="10",
        decisions=(D1, D2),
    )
    assert result_hash(first) == result_hash(again)  # type: ignore[arg-type]
    different = run(
        prices,
        {D1: [1, 2, 3, 4, 5], D2: [5, 4, 3, 2, 1]},
        costs=REAL,
        slippage="25",
        decisions=(D1, D2),
    )
    assert result_hash(different) != result_hash(first)  # type: ignore[arg-type]


def test_no_decision_dates_gives_an_empty_result() -> None:
    assert not run({}, {}, decisions=()).nav  # type: ignore[attr-defined]


def test_the_nav_has_one_point_per_trading_day_and_the_rebalance_records_turnover() -> None:
    prices = {1: path("100", "1.001"), 2: path("100", "1.002")}
    result = run(prices, {D1: [1, 2], D2: [1, 2]}, decisions=(D1, D2), last=D2)
    days = [d for d, _ in result.nav]  # type: ignore[attr-defined]
    assert days == list(trading_days(D1, D2)) and len(days) == len(set(days))
    first, second = result.rebalances  # type: ignore[attr-defined]
    assert first.turnover == Decimal("0.5")  # buying 100% of the portfolio is half a turn
    assert second.turnover < Decimal("0.01")  # only a small trim to equal weight
