"""Run a backtest and store it (P2-6; ADR-003).

    uv run python -m halal_quant.backtest.run --period in_sample [--slippage base|all]
    uv run python -m halal_quant.backtest.run --first 2010-01-01 --last 2012-12-31 --slippage base

Checks before anything runs: the price vintage must be complete (one consistent download); the
period must respect the research protocol (the out-of-sample period only with --final-test); the
strategy and protocol versions are registered (audited). Running the same inputs again recomputes
the result and must reproduce the stored hash, otherwise the run fails (PRD §18, §20).
"""

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import Connection

from halal_quant.audit import AuditEvent, record_event
from halal_quant.backtest.analytics import summarize
from halal_quant.backtest.config import (
    MomentumConfig,
    OverlayConfig,
    ProtocolError,
    ResearchProtocol,
    guard_period,
    load_protocol,
    load_strategy,
)
from halal_quant.backtest.data import SqlPriceSource, SqlUniverseSource, trend_signal
from halal_quant.backtest.engine import (
    BacktestResult,
    PriceSource,
    UniverseSource,
    run_backtest,
)
from halal_quant.backtest.records import (
    ReproducibilityError,
    RunInputs,
    code_version,
    result_hash,
    run_key,
    save_run,
    stored_result_hash,
)
from halal_quant.core.config import (
    LoadedConfig,
    UniverseConfig,
    load_config,
    register_config,
)
from halal_quant.core.logging import configure_logging, correlation_scope
from halal_quant.core.settings import DbRole, get_settings
from halal_quant.data.calendar import is_trading_day, next_trading_day
from halal_quant.data.vintage import COMPLETE, VintageError, vintage_status
from halal_quant.db.engine import make_engine

ACTOR = "system:backtest"
SLIPPAGE_CHOICES = ("optimistic", "base", "stress", "all")


@dataclass(frozen=True)
class RunOutcome:
    run_id: int
    result_sha256: str
    metrics: dict[str, Any]
    reused: bool  # True when the same inputs were already stored (and reproduced identically)
    period_name: str
    slippage_case: str


def decision_dates(first: date, last: date) -> list[date]:
    """The first trading day of every month whose first trading day lies in [first, last]."""
    dates = []
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        day = date(year, month, 1)
        day = day if is_trading_day(day) else next_trading_day(day)
        if first <= day <= last:
            dates.append(day)
        year, month = year + month // 12, month % 12 + 1
    return dates


def execute_run(
    conn: Connection,
    vintage_id: str,
    strategy: LoadedConfig[MomentumConfig] | LoadedConfig[OverlayConfig],
    protocol: LoadedConfig[ResearchProtocol],
    universe: LoadedConfig[UniverseConfig],
    first: date,
    last: date,
    slippage_case: str,
    final_test: bool = False,
    prices: PriceSource | None = None,
    universe_source: UniverseSource | None = None,
    actor: str = ACTOR,
    risk_on: Callable[[date], bool] | None = None,
) -> RunOutcome:
    """Run one backtest, verify reproducibility, store it if new, and return its outcome."""
    status = vintage_status(conn, vintage_id)
    if status != COMPLETE:
        raise VintageError(
            f"Price vintage {vintage_id!r} is {status!r}: a backtest needs a complete vintage."
        )
    period = guard_period(protocol.config, first, last, final_test)
    bps = protocol.config.costs.slippage_bps[slippage_case]
    methodology = universe.config.sharia_methodology
    inputs = RunInputs(
        vintage_id=vintage_id,
        strategy_version=strategy.ref.version,
        strategy_params=strategy.config.model_dump(mode="json"),
        strategy_sha256=strategy.ref.sha256,
        protocol_version=protocol.ref.version,
        protocol_sha256=protocol.ref.sha256,
        universe_version=universe.ref.version,
        universe_sha256=universe.ref.sha256,
        methodology=methodology,
        slippage_case=slippage_case,
        slippage_bps=bps,
        period_name=period,
        first_day=first,
        last_day=last,
        final_test=final_test,
    )
    days = strategy.config.trend_filter_days
    if risk_on is None and days is not None:
        risk_on = trend_signal(conn, vintage_id, strategy.config.trend_symbol, days)
    result: BacktestResult = run_backtest(
        decision_dates(first, last),
        last,
        universe_source or SqlUniverseSource(conn, universe, methodology),
        prices or SqlPriceSource(conn, vintage_id),
        strategy.config,
        protocol.config.costs,
        bps,
        protocol.config.purification_annual_drag,
        risk_on,
    )
    digest = result_hash(result)
    stored = stored_result_hash(conn, run_key(inputs))
    if stored is not None:
        run_id, previous = stored
        if previous != digest:
            raise ReproducibilityError(
                f"The same inputs (run {run_id}) gave a different result: stored {previous[:12]}, "
                f"now {digest[:12]}. The code, the data or the universe changed."
            )
        return RunOutcome(run_id, digest, _metrics(result), True, period, slippage_case)
    metrics = _metrics(result)
    run_id, digest = save_run(conn, inputs, result, metrics, code_version())
    record_event(
        conn,
        AuditEvent(
            actor=actor,
            action="backtest.run",
            entity_type="backtest_run",
            entity_id=str(run_id),
            reason=f"Backtest {strategy.ref.version} {period} {slippage_case}",
            source=__name__,
            details={
                "vintage_id": vintage_id,
                "period": period,
                "first": first,
                "last": last,
                "slippage_case": slippage_case,
                "final_test": final_test,
                "result_sha256": digest,
                "run_key_sha256": run_key(inputs),
            },
        ),
    )
    return RunOutcome(run_id, digest, metrics, False, period, slippage_case)


def _metrics(result: BacktestResult) -> dict[str, Any]:
    traded = sum((t.notional for t in result.trades), Decimal(0))
    costs = sum((t.cost for t in result.trades), Decimal(0))
    figures = summarize(result.nav, traded, costs)
    figures["rebalances"] = len(result.rebalances)
    figures["trades"] = len(result.trades)
    figures["average_holdings"] = (
        float(sum(r.selected for r in result.rebalances)) / len(result.rebalances)
        if result.rebalances
        else 0.0
    )
    return figures


def _percent(value: object) -> str:
    return "n/a" if not isinstance(value, float) else f"{value * 100:.1f}%"


def describe(outcome: RunOutcome) -> str:
    """One plain line about a run."""
    m = outcome.metrics
    sharpe = m.get("sharpe")
    sharpe_text = f"{sharpe:.2f}" if isinstance(sharpe, float) else "n/a"
    state = "REPRODUCED the stored result" if outcome.reused else "stored"
    return (
        f"run {outcome.run_id} [{outcome.period_name}, slippage {outcome.slippage_case}] {state}; "
        f"hash {outcome.result_sha256[:12]}; CAGR {_percent(m.get('cagr'))}, "
        f"volatility {_percent(m.get('volatility'))}, Sharpe {sharpe_text}, "
        f"max drawdown {_percent(m.get('max_drawdown'))}, "
        f"turnover {_percent(m.get('annual_turnover'))} a year"
    )


def _date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a date like 2010-01-01") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run and store a backtest.")
    parser.add_argument("--vintage", default="v1")
    parser.add_argument("--strategy", type=Path, default=Path("config/strategy/momentum_v1.yaml"))
    parser.add_argument("--protocol", type=Path, default=Path("config/research/protocol_v1.yaml"))
    parser.add_argument("--universe", type=Path, default=Path("config/universe/default.yaml"))
    parser.add_argument("--period", choices=["burn_in", "in_sample", "validation", "out_of_sample"])
    parser.add_argument("--first", type=_date)
    parser.add_argument("--last", type=_date)
    parser.add_argument("--slippage", choices=SLIPPAGE_CHOICES, default="base")
    parser.add_argument("--final-test", action="store_true", help="the one use of out-of-sample")
    args = parser.parse_args(argv)
    protocol = load_protocol(args.protocol)
    if args.period:
        window = getattr(protocol.config.periods, args.period)
        first, last = window.first, window.last
    elif args.first and args.last:
        first, last = args.first, args.last
    else:
        parser.error("give --period, or both --first and --last")
    cases = list(protocol.config.costs.slippage_bps) if args.slippage == "all" else [args.slippage]
    strategy = load_strategy(args.strategy)
    universe = load_config(args.universe, UniverseConfig)
    settings = get_settings()
    configure_logging(settings, level=settings.log_level)
    engine = make_engine(settings, DbRole.APP)
    with correlation_scope():
        with engine.begin() as conn:
            register_config(conn, strategy, ACTOR, "Strategy used by a backtest run")
            register_config(conn, protocol, ACTOR, "Research protocol used by a backtest run")
        for case in cases:
            try:
                with engine.begin() as conn:
                    outcome = execute_run(
                        conn,
                        args.vintage,
                        strategy,
                        protocol,
                        universe,
                        first,
                        last,
                        case,
                        args.final_test,
                    )
            except (ProtocolError, VintageError, ReproducibilityError) as exc:
                print(f"NOT RUN: {exc}")
                return 1
            print(describe(outcome))
    return 0


if __name__ == "__main__":
    sys.exit(main())
