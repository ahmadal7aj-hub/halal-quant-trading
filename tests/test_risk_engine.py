"""The risk engine and equal-weight construction (PRD §12, §13; tests 1-6), made-up data."""

from decimal import Decimal
from pathlib import Path

import pytest

from halal_quant.core.config import ConfigError
from halal_quant.trading import risk
from halal_quant.trading.portfolio import Target, equal_weight_orders
from halal_quant.trading.risk import (
    OrderFacts,
    OrderRequest,
    PortfolioState,
    Position,
    check_batch,
    check_order,
    load_risk_limits,
)

D = Decimal
LIMITS = load_risk_limits(Path("config/risk/risk_v1.yaml")).config
GOOD = OrderFacts(
    exists=True,
    instrument="stock",
    sharia_status="HALAL",
    price_age_days=1,
    avg_daily_dollar_volume=D(5_000_000),
    sector="Technology",
)
FUND = OrderFacts(
    exists=True,
    instrument="fund",
    price_age_days=1,
    avg_daily_dollar_volume=D(50_000_000),
)


def state(**kw: object) -> PortfolioState:
    base: dict[str, object] = {"nav": D(100_000), "cash": D(100_000)}
    base.update(kw)
    return PortfolioState(**base)  # type: ignore[arg-type]


def buy(symbol: str = "AAA", shares: int = 50, price: int = 100) -> OrderRequest:
    return OrderRequest(symbol, "BUY", D(shares), D(price))


def sell(symbol: str = "AAA", shares: int = 50, price: int = 100) -> OrderRequest:
    return OrderRequest(symbol, "SELL", D(shares), D(price))


def test_the_shipped_limits_load_and_carry_the_owners_approval() -> None:
    assert LIMITS.status == "approved" and LIMITS.approved_by == "owner"
    assert LIMITS.max_position_pct == D(10) and LIMITS.approved_funds == ["SPUS", "HLAL"]


def test_inconsistent_limits_are_refused(tmp_path: Path) -> None:
    text = Path("config/risk/risk_v1.yaml").read_text(encoding="utf-8")
    bad = tmp_path / "r.yaml"
    bad.write_text(
        text.replace('manual_review_order_value: "20000"', 'manual_review_order_value: "90000"')
    )
    with pytest.raises(ConfigError, match="cannot exceed"):
        load_risk_limits(bad)


def test_a_clean_order_is_approved_with_a_reason() -> None:
    decision = check_order(buy(), GOOD, state(), LIMITS)
    assert decision.result == risk.APPROVED and decision.reason_code == risk.OK
    assert decision.explanation


def test_test1_a_non_halal_stock_cannot_be_bought_but_can_be_sold() -> None:
    bad = OrderFacts(**{**GOOD.__dict__, "sharia_status": "NON_HALAL"})
    held = state(positions={"AAA": Position(D(5000), "stock", "Technology")})
    rejected = check_order(buy(), bad, held, LIMITS)
    assert (rejected.result, rejected.reason_code) == (risk.REJECTED, risk.NOT_HALAL)
    assert (
        check_order(sell(), bad, held, LIMITS).result == risk.APPROVED
    )  # getting out stays possible


@pytest.mark.parametrize("status", ["UNKNOWN", None])
def test_test2_an_unknown_classification_cannot_be_bought(status: str | None) -> None:
    unknown = OrderFacts(**{**GOOD.__dict__, "sharia_status": status})
    decision = check_order(buy(), unknown, state(), LIMITS)
    assert (decision.result, decision.reason_code) == (risk.REJECTED, risk.SHARIA_UNKNOWN)


def test_test3_stale_or_missing_prices_block_trading_both_ways() -> None:
    for age in (LIMITS.max_price_age_days + 1, None):
        facts = OrderFacts(**{**GOOD.__dict__, "price_age_days": age})
        for order in (buy(), sell()):
            decision = check_order(order, facts, state(), LIMITS)
            assert decision.reason_code == risk.STALE_PRICE and decision.result == risk.REJECTED


def test_test4_a_position_over_the_maximum_size_is_rejected() -> None:
    held = state(positions={"AAA": Position(D(8000), "stock", "Technology")})
    decision = check_order(buy(shares=30), GOOD, held, LIMITS)  # 8,000 + 3,000 = 11% > 10%
    assert decision.reason_code == risk.POSITION_LIMIT and decision.result == risk.REJECTED
    assert check_order(buy(shares=20), GOOD, held, LIMITS).result == risk.APPROVED  # exactly 10%


def test_a_fund_has_its_own_larger_position_limit_and_must_be_on_the_approved_list() -> None:
    assert (
        check_order(buy("SPUS", 300, 100), FUND, state(), LIMITS).result
        == risk.REQUIRES_MANUAL_REVIEW
    )
    over = check_order(buy("SPUS", 700, 100), FUND, state(), LIMITS)
    assert over.reason_code in (risk.POSITION_LIMIT, risk.ORDER_TOO_LARGE) and not over.allowed
    other = check_order(buy("QQQ", 10, 100), FUND, state(), LIMITS)
    assert other.reason_code == risk.FUND_NOT_APPROVED


def test_test5_sector_exposure_over_the_maximum_is_rejected() -> None:
    positions = {
        "S1": Position(D(10_000), "stock", "Technology"),
        "S2": Position(D(10_000), "stock", "Technology"),
        "S3": Position(D(9_000), "stock", "Technology"),
    }
    held = state(positions=positions, cash=D(71_000))
    decision = check_order(buy("S4", 20, 100), GOOD, held, LIMITS)  # 29,000 + 2,000 = 31% > 30%
    assert decision.reason_code == risk.SECTOR_LIMIT and decision.result == risk.REJECTED
    other_sector = OrderFacts(**{**GOOD.__dict__, "sector": "Health Care"})
    assert check_order(buy("S4", 20, 100), other_sector, held, LIMITS).result == risk.APPROVED


def test_test6_the_kill_switch_blocks_every_order() -> None:
    engaged = state(kill_switch_engaged=True)
    for order in (buy(), sell()):
        decision = check_order(order, GOOD, engaged, LIMITS)
        assert (decision.result, decision.reason_code) == (risk.REJECTED, risk.KILL_SWITCH)


def test_unknown_security_and_illiquid_stocks_are_rejected() -> None:
    assert (
        check_order(buy(), OrderFacts(exists=False), state(), LIMITS).reason_code
        == risk.UNKNOWN_SECURITY
    )
    thin = OrderFacts(**{**GOOD.__dict__, "avg_daily_dollar_volume": D(10_000)})
    assert check_order(buy(), thin, state(), LIMITS).reason_code == risk.ILLIQUID
    none = OrderFacts(**{**GOOD.__dict__, "avg_daily_dollar_volume": None})
    assert check_order(buy(), none, state(), LIMITS).reason_code == risk.ILLIQUID


def test_large_orders_need_the_owner_and_huge_orders_are_rejected() -> None:
    wanted = check_order(
        buy("SPUS", 250, 100), FUND, state(nav=D(500_000), cash=D(500_000)), LIMITS
    )
    assert (
        wanted.result == risk.REQUIRES_MANUAL_REVIEW
        and wanted.reason_code == risk.MANUAL_REVIEW_SIZE
    )
    huge = check_order(buy("SPUS", 600, 100), FUND, state(nav=D(500_000), cash=D(500_000)), LIMITS)
    assert huge.reason_code == risk.ORDER_TOO_LARGE  # 60,000 > 50,000 maximum order


def test_daily_turnover_and_a_disconnected_broker_stop_orders() -> None:
    busy = state(traded_today=D(59_000))
    assert check_order(buy(shares=20), GOOD, busy, LIMITS).reason_code == risk.TURNOVER_LIMIT
    assert (
        check_order(buy(), GOOD, state(broker_connected=False), LIMITS).reason_code
        == risk.BROKER_DISCONNECTED
    )


def test_the_drawdown_and_daily_loss_breakers_stop_buying_but_not_selling() -> None:
    down = state(nav=D(70_000), cash=D(70_000), peak_nav=D(100_000))  # -30% from the peak
    assert check_order(buy(), GOOD, down, LIMITS).reason_code == risk.DRAWDOWN_BREAKER
    assert check_order(sell(), GOOD, down, LIMITS).result == risk.APPROVED
    bad_day = state(nav=D(94_000), cash=D(94_000), day_start_nav=D(100_000))  # -6% today
    assert check_order(buy(), GOOD, bad_day, LIMITS).reason_code == risk.DAILY_LOSS_BREAKER


def test_total_exposure_and_cash_limits() -> None:
    capped = LIMITS.model_copy(update={"max_total_exposure_pct": D(50)})
    held = state(positions={"X": Position(D(49_000), "fund")}, cash=D(51_000))
    assert check_order(buy(shares=20), GOOD, held, capped).reason_code == risk.EXPOSURE_LIMIT
    poor = state(cash=D(1_000))
    assert check_order(buy(shares=20), GOOD, poor, LIMITS).reason_code == risk.CASH_INSUFFICIENT


def test_a_zero_or_negative_order_is_rejected() -> None:
    assert check_order(buy(shares=0), GOOD, state(), LIMITS).reason_code == risk.BAD_ORDER


def test_a_batch_is_checked_sells_first_and_each_order_sees_the_earlier_ones() -> None:
    held = state(positions={"OLD": Position(D(9_000), "stock", "Technology")}, cash=D(91_000))
    facts = {s: GOOD for s in ("OLD", "N1", "N2")}
    orders = [buy("N1", 90, 100), buy("N2", 90, 100), sell("OLD", 90, 100)]
    results = check_batch(orders, facts, held, LIMITS)
    assert [o.symbol for o, _ in results] == ["OLD", "N1", "N2"]  # the sell first
    assert all(d.result == risk.APPROVED for _, d in results)
    crowded = check_batch(
        [buy(f"B{i}", 90, 100) for i in range(4)],
        {f"B{i}": OrderFacts(**{**GOOD.__dict__, "sector": "Technology"}) for i in range(4)},
        state(),
        LIMITS,
    )
    assert [d.reason_code for _, d in crowded] == [risk.OK, risk.OK, risk.OK, risk.SECTOR_LIMIT]


def test_equal_weight_orders_round_down_skip_tiny_trades_and_exit_dropped_names() -> None:
    targets = [Target("A", D(100)), Target("B", D(30)), Target("C", D(250))]
    orders = equal_weight_orders(targets, {"OLD": D(40)}, {"OLD": D(20)}, D(30_000))
    by = {o.symbol: o for o in orders}
    assert by["A"].quantity == D(100) and by["A"].side == "BUY"  # 10,000 / 100
    assert by["B"].quantity == D(333) and by["C"].quantity == D(40)  # rounded down
    assert (
        by["OLD"].side == "SELL" and by["OLD"].quantity == D(40) and "no longer" in by["OLD"].reason
    )
    assert all(o.notional <= D(10_000) for o in orders if o.side == "BUY")
    # already at target: nothing to do; a drift below the minimum trade is skipped
    assert equal_weight_orders(targets[:1], {"A": D(300)}, {}, D(30_000)) == []
    flat = equal_weight_orders([Target("A", D(100))], {"A": D(100)}, {}, D(10_000))
    assert flat == []
    nearly = equal_weight_orders(
        [Target("A", D(100))], {"A": D(99)}, {}, D(10_000), min_trade_value=D(500)
    )
    assert nearly == []  # a $100 top-up is below the $500 minimum


def test_a_partly_invested_target_keeps_cash_and_bad_prices_are_refused() -> None:
    half = equal_weight_orders([Target("A", D(100))], {}, {}, D(10_000), invested_fraction=D("0.5"))
    assert half[0].quantity == D(50)
    with pytest.raises(ValueError, match="invested_fraction"):
        equal_weight_orders([Target("A", D(100))], {}, {}, D(10_000), invested_fraction=D(2))
    with pytest.raises(ValueError, match="no usable price"):
        equal_weight_orders([Target("A", D(0))], {}, {}, D(10_000))
    with pytest.raises(ValueError, match="held but has no usable price"):
        equal_weight_orders([], {"Z": D(5)}, {}, D(10_000))
