"""The research report for one period: the strategy against its benchmarks (P2-7, P2-9; ADR-003).

    uv run python -m halal_quant.backtest.report --period in_sample --vintage v1

Compared over the same days and from the same starting value:
- the strategy, under every slippage case that was run (stored runs, read back from the database);
- the equal-weight Halal universe (the stored run of `equal_weight_universe_v1`: every eligible
  stock with enough price history, same monthly rotation and same costs);
- buy-and-hold SPY, SPUS and HLAL from the same price vintage (no trading costs, a generous
  yardstick). A fund that did not exist yet in the period is reported as having no data.

The report only reads: it never runs a backtest and never touches a period the protocol guards.
"""

import argparse
import sys
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Connection, select

from halal_quant.backtest.analytics import summarize
from halal_quant.backtest.config import load_protocol
from halal_quant.backtest.engine import START_NAV
from halal_quant.backtest.records import backtest_nav_table, backtest_run_table
from halal_quant.core.settings import DbRole, get_settings
from halal_quant.data.vintage import vintage_benchmark_price_table
from halal_quant.db.engine import make_engine

Nav = list[tuple[date, Decimal]]
BENCHMARKS = ("SPY", "SPUS", "HLAL")
STRATEGY_PREFIX = "momentum"
UNIVERSE_PREFIX = "equal_weight_universe"


def rebase(series: Sequence[tuple[date, Decimal]], first: date, last: date) -> Nav:
    """The part of a price series inside [first, last], scaled to start at START_NAV."""
    inside = [(d, v) for d, v in series if first <= d <= last]
    if not inside or inside[0][1] <= 0:
        return []
    base = inside[0][1]
    return [(d, START_NAV * v / base) for d, v in inside]


def common_window(series: dict[str, Nav]) -> dict[str, Nav]:
    """Cut every series to the days they all have, restarting each at START_NAV."""
    present = {name: nav for name, nav in series.items() if nav}
    if not present:
        return {}
    days = set.intersection(*(set(d for d, _ in nav) for nav in present.values()))
    return {
        name: rebase([(d, v) for d, v in nav if d in days], min(days), max(days))
        for name, nav in present.items()
        if days
    }


def yearly_returns(nav: Sequence[tuple[date, Decimal]]) -> dict[int, float]:
    """Calendar-year returns, each measured from the last NAV of the year before."""
    last: dict[int, Decimal] = {}
    for day, value in nav:
        last[day.year] = value
    out: dict[int, float] = {}
    previous = nav[0][1] if nav else None
    for year in sorted(last):
        if previous and previous > 0:
            out[year] = float(last[year] / previous) - 1
        previous = last[year]
    return out


def _pct(value: object) -> str:
    return "n/a" if not isinstance(value, float) else f"{value * 100:.1f}%"


def _num(value: object) -> str:
    return "n/a" if not isinstance(value, float) else f"{value:.2f}"


def render_report(
    title: str,
    window_note: str,
    series: dict[str, Nav],
    missing: Sequence[str],
    run_notes: Sequence[str],
) -> str:
    """The report as Markdown. `series` are already cut to the same days."""
    figures = {name: summarize(nav) for name, nav in series.items()}
    lines = [f"# {title}", "", window_note, ""]
    lines += [
        "| Series | Total return | CAGR | Volatility | Sharpe | Sortino | Max drawdown |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, f in figures.items():
        lines.append(
            f"| {name} | {_pct(f['total_return'])} | {_pct(f['cagr'])} | {_pct(f['volatility'])} "
            f"| {_num(f['sharpe'])} | {_num(f['sortino'])} | {_pct(f['max_drawdown'])} |"
        )
    years = sorted({y for nav in series.values() for y in yearly_returns(nav)})
    by_series = {name: yearly_returns(nav) for name, nav in series.items()}
    if years:
        lines += ["", "## Calendar-year returns", ""]
        lines.append("| Year | " + " | ".join(series) + " |")
        lines.append("|---|" + "---|" * len(series))
        for year in years:
            cells = [_pct(by_series[name].get(year)) for name in series]
            lines.append(f"| {year} | " + " | ".join(cells) + " |")
    if missing:
        lines += ["", "## No data in this period", ""]
        lines += [f"- {name}" for name in missing]
    lines += ["", "## Stored runs behind these figures", ""]
    lines += [f"- {note}" for note in run_notes] or ["- none"]
    lines += [
        "",
        "Notes: risk-free rate taken as zero; strategy and equal-weight universe pay commission,",
        "slippage and the purification drag; benchmark funds are buy-and-hold, no costs.",
        "The Sharia screen (AAOIFI-v1) is our own reading of the standard and has not",
        "been reviewed by a qualified scholar.",
        "These are research results on past data, not a forecast and not advice.",
    ]
    return "\n".join(lines) + "\n"


def _stored_runs(
    conn: Connection, vintage: str, protocol_sha: str, period: str, prefix: str
) -> list[tuple[int, str, str, str]]:
    """(run id, strategy version, slippage case, result hash) of the stored runs for a period."""
    r = backtest_run_table.c
    rows = conn.execute(
        select(r.id, r.strategy_version, r.slippage_case, r.result_sha256)
        .where(
            r.vintage_id == vintage,
            r.protocol_sha256 == protocol_sha,
            r.period_name == period,
            r.strategy_version.like(prefix + "%"),
        )
        .order_by(r.slippage_case, r.id)
    )
    return [(x.id, x.strategy_version, x.slippage_case, x.result_sha256) for x in rows]


def _run_nav(conn: Connection, run_id: int) -> Nav:
    n = backtest_nav_table.c
    rows = conn.execute(select(n.nav_date, n.nav).where(n.run_id == run_id).order_by(n.nav_date))
    return [(x.nav_date, x.nav) for x in rows]


def _benchmark_prices(conn: Connection, vintage: str, symbol: str, first: date, last: date) -> Nav:
    b = vintage_benchmark_price_table.c
    rows = conn.execute(
        select(b.price_date, b.adjusted_close)
        .where(
            b.vintage_id == vintage,
            b.symbol == symbol,
            b.price_date >= first,
            b.price_date <= last,
        )
        .order_by(b.price_date)
    )
    return [(x.price_date, x.adjusted_close) for x in rows]


def build_report(conn: Connection, vintage: str, period: str, protocol_path: Path) -> str:
    protocol = load_protocol(protocol_path)
    window = getattr(protocol.config.periods, period)
    first, last = window.first, window.last
    series: dict[str, Nav] = {}
    notes: list[str] = []
    for prefix, label in (
        (STRATEGY_PREFIX, "Momentum V1"),
        (UNIVERSE_PREFIX, "Equal-weight Halal"),
    ):
        for run_id, version, case, digest in _stored_runs(
            conn, vintage, protocol.ref.sha256, period, prefix
        ):
            series[f"{label} ({case} slippage)"] = _run_nav(conn, run_id)
            notes.append(f"run {run_id}: {version}, slippage {case}, result hash {digest[:12]}")
    missing: list[str] = []
    for symbol in BENCHMARKS:
        prices = _benchmark_prices(conn, vintage, symbol, first, last)
        if prices:
            series[f"{symbol} (buy and hold)"] = rebase(prices, first, last)
        else:
            missing.append(f"{symbol}: no price history in {first} to {last}")
    if not any(name.startswith("Momentum V1") for name in series):
        raise SystemExit(f"No stored Momentum V1 run for {period} in vintage {vintage}.")
    cut = common_window(series)
    days = sorted({d for nav in cut.values() for d, _ in nav})
    note = (
        f"Period {period} ({first} to {last}); series compared over {days[0]} to {days[-1]} "
        f"({len(days)} trading days), each starting at {START_NAV:,.0f}; price vintage {vintage}."
        if days
        else f"Period {period}: the series have no days in common."
    )
    return render_report(f"Backtest report: {period}", note, cut, missing, notes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the research report for one period.")
    parser.add_argument("--period", required=True, choices=["in_sample", "validation"])
    parser.add_argument("--vintage", default="v1")
    parser.add_argument("--protocol", type=Path, default=Path("config/research/protocol_v1.yaml"))
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    engine = make_engine(get_settings(), DbRole.READONLY)
    with engine.connect() as conn:
        text = build_report(conn, args.vintage, args.period, args.protocol)
    out = args.out or Path("private/reports") / f"backtest_{args.period}_{date.today()}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"Report written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
