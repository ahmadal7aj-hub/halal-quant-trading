"""Phase 2 protocol guard, performance analytics and the result hash (made-up data)."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from halal_quant.backtest.analytics import (
    cagr,
    daily_returns,
    max_drawdown,
    monthly_returns,
    sharpe,
    sortino,
    summarize,
    total_return,
    volatility,
)
from halal_quant.backtest.config import (
    ProtocolError,
    guard_period,
    load_momentum,
    load_protocol,
    period_name,
)
from halal_quant.backtest.engine import BacktestResult, Rebalance, Trade
from halal_quant.backtest.records import RunInputs, result_hash, run_key
from halal_quant.core.config import ConfigError

PROTOCOL = load_protocol(Path("config/research/protocol_v1.yaml")).config


def nav(*values: str, start: date = date(2020, 1, 2)) -> list[tuple[date, Decimal]]:
    return [(date.fromordinal(start.toordinal() + i), Decimal(v)) for i, v in enumerate(values)]


def test_the_shipped_protocol_and_strategy_load_with_the_approved_values() -> None:
    assert PROTOCOL.periods.in_sample.first == date(2004, 1, 1)
    assert PROTOCOL.periods.out_of_sample.last == date(2026, 9, 30)
    assert PROTOCOL.costs.slippage_bps == {
        "optimistic": Decimal(5),
        "base": Decimal(10),
        "stress": Decimal(25),
    }
    momentum = load_momentum(Path("config/strategy/momentum_v1.yaml")).config
    assert (momentum.lookback_trading_days, momentum.skip_trading_days, momentum.top_n) == (
        126,
        21,
        20,
    )


def test_periods_that_overlap_are_refused(tmp_path: Path) -> None:
    text = Path("config/research/protocol_v1.yaml").read_text(encoding="utf-8")
    broken = tmp_path / "p.yaml"
    broken.write_text(text.replace("first: 2014-01-01", "first: 2013-06-01"), encoding="utf-8")
    with pytest.raises(ConfigError, match="without overlap"):
        load_protocol(broken)


def test_each_period_is_named_and_a_straddling_run_is_custom() -> None:
    assert period_name(PROTOCOL, date(2004, 1, 1), date(2013, 12, 31)) == "in_sample"
    assert period_name(PROTOCOL, date(2005, 1, 3), date(2006, 1, 3)) == "in_sample"
    assert period_name(PROTOCOL, date(2012, 1, 3), date(2015, 1, 2)) == "custom"


def test_out_of_sample_is_refused_without_the_final_test_flag_even_for_a_short_overlap() -> None:
    with pytest.raises(ProtocolError, match="out-of-sample"):
        guard_period(PROTOCOL, date(2018, 6, 1), date(2019, 1, 2), final_test=False)
    with pytest.raises(ProtocolError, match="out-of-sample"):
        guard_period(PROTOCOL, date(2019, 1, 1), date(2026, 9, 30), final_test=False)


def test_the_final_test_flag_is_only_for_a_run_that_covers_out_of_sample() -> None:
    with pytest.raises(ProtocolError, match="only for"):
        guard_period(PROTOCOL, date(2004, 1, 1), date(2013, 12, 31), final_test=True)
    name = guard_period(PROTOCOL, date(2019, 1, 1), date(2026, 9, 30), final_test=True)
    assert name == "out_of_sample"


def test_a_backwards_run_or_one_before_the_data_starts_is_refused() -> None:
    with pytest.raises(ProtocolError, match="ends before"):
        guard_period(PROTOCOL, date(2010, 1, 2), date(2010, 1, 1), final_test=False)
    with pytest.raises(ProtocolError, match="before the first date"):
        guard_period(PROTOCOL, date(1990, 1, 2), date(2000, 1, 1), final_test=False)


def test_allowed_periods_pass_the_guard() -> None:
    assert guard_period(PROTOCOL, date(2004, 1, 1), date(2013, 12, 31), False) == "in_sample"
    assert guard_period(PROTOCOL, date(2014, 1, 1), date(2018, 12, 31), False) == "validation"


def test_returns_total_return_and_cagr_on_a_simple_series() -> None:
    series = nav("100", "110", "99")
    assert daily_returns(series) == pytest.approx([0.10, -0.10])
    assert total_return(series) == pytest.approx(-0.01)
    one_year = [(date(2020, 1, 1), Decimal(100)), (date(2021, 1, 1), Decimal(121))]
    assert cagr(one_year) == pytest.approx(0.21, abs=0.002)
    assert total_return(nav("100")) is None and cagr(nav("100")) is None


def test_volatility_sharpe_and_sortino_have_the_expected_signs() -> None:
    steady_gain = [0.01, 0.012, 0.008, 0.011]
    assert volatility(steady_gain) is not None
    assert (sharpe(steady_gain) or 0) > 0
    assert sortino(steady_gain) is None  # no downside moves at all
    mixed = [0.02, -0.01, 0.015, -0.005]
    assert (sortino(mixed) or 0) > 0
    assert volatility([0.01]) is None and sharpe([0.01, 0.01]) is None  # no spread, no ratio


def test_max_drawdown_reports_the_worst_peak_to_trough_fall_and_its_dates() -> None:
    series = nav("100", "120", "90", "130", "117")
    fall, peak, trough = max_drawdown(series)
    assert fall == pytest.approx(-0.25)
    assert (peak, trough) == (series[1][0], series[2][0])
    assert max_drawdown(nav("100", "101", "102"))[0] == 0.0


def test_monthly_returns_are_measured_from_the_previous_month_end() -> None:
    series = [
        (date(2020, 1, 2), Decimal(100)),
        (date(2020, 1, 31), Decimal(110)),
        (date(2020, 2, 3), Decimal(111)),
        (date(2020, 2, 28), Decimal(99)),
    ]
    months = dict(monthly_returns(series))
    assert months["2020-01"] == pytest.approx(0.10)
    assert months["2020-02"] == pytest.approx(99 / 110 - 1)


def test_summary_has_the_headline_figures_and_survives_an_empty_series() -> None:
    series = nav("100", "101", "99", "103")
    figures = summarize(series, traded_value=Decimal(200), costs=Decimal(5))
    assert figures["trading_days"] == 4
    assert figures["total_return"] == pytest.approx(0.03)
    assert figures["costs_fraction_of_start_nav"] == pytest.approx(0.05)
    empty = summarize([])
    assert empty["trading_days"] == 0 and empty["cagr"] is None


def make_result(cost: str = "1.0000") -> BacktestResult:
    day = date(2010, 2, 1)
    trade = Trade(
        trade_date=day,
        security_id=7,
        side="BUY",
        kind="open",
        shares=Decimal("10.0000"),
        price=Decimal("5.000000"),
        notional=Decimal("50.00"),
        cost=Decimal(cost),
        reason="ranked 1 of 3",
    )
    rebalance = Rebalance(
        decision_date=day,
        eligible=3,
        scored=3,
        selected=1,
        turnover=Decimal("0.5"),
        costs=Decimal(cost),
        nav_before=Decimal(100),
        nav_after=Decimal(99),
    )
    return BacktestResult(nav=[(day, Decimal(99))], trades=[trade], rebalances=[rebalance])


def test_the_result_hash_is_stable_and_changes_when_anything_in_the_result_changes() -> None:
    assert result_hash(make_result()) == result_hash(make_result())
    assert result_hash(make_result()) != result_hash(make_result(cost="1.0001"))


def inputs(**changes: object) -> RunInputs:
    base = RunInputs(
        vintage_id="v1",
        strategy_version="s",
        strategy_params={},
        strategy_sha256="a",
        protocol_version="p",
        protocol_sha256="b",
        universe_version="u",
        universe_sha256="c",
        methodology="AAOIFI-v1",
        slippage_case="base",
        slippage_bps=Decimal(10),
        period_name="in_sample",
        first_day=date(2004, 1, 1),
        last_day=date(2013, 12, 31),
        final_test=False,
    )
    return RunInputs(**{**base.__dict__, **changes})


def test_the_run_key_depends_on_every_input_that_decides_the_result() -> None:
    base = run_key(inputs())
    assert base == run_key(inputs())
    for change in (
        {"vintage_id": "v2"},
        {"strategy_sha256": "z"},
        {"protocol_sha256": "z"},
        {"universe_sha256": "z"},
        {"first_day": date(2005, 1, 3)},
        {"last_day": date(2012, 12, 31)},
        {"slippage_bps": Decimal(25)},
    ):
        assert run_key(inputs(**change)) != base
