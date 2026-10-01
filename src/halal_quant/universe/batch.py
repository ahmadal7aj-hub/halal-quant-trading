"""Build the eligible universe for every month (task 14; PRD §8, §29).

    uv run python -m halal_quant.universe.batch [--start 1998-12] [--end 2026-09]

For each month-end screening date, the universe is built for the next trading day: the first day
the month's Sharia classification applies, and the day a monthly rebalance would decide. Each
month is its own transaction and its own audit event; rebuilding a month with the same inputs
reuses the stored build (same content hash), so the command is safe to re-run.

The data-quality checks must already have run over the range: the builder refuses to build a
universe whose 20-day window no run covers (fail closed).
"""

import argparse
import sys
from datetime import date
from pathlib import Path

from halal_quant.core.config import UniverseConfig, load_config, register_config
from halal_quant.core.logging import configure_logging, correlation_scope
from halal_quant.core.settings import DbRole, get_settings
from halal_quant.data.calendar import next_trading_day
from halal_quant.db.engine import make_engine
from halal_quant.sharia.classification import screening_dates
from halal_quant.universe.builder import ACTOR, UniverseError, UniverseResult, build_universe

TOP_REASONS = 3


def summary_line(result: UniverseResult) -> str:
    top = ", ".join(f"{k}: {v}" for k, v in result.exclusions.most_common(TOP_REASONS)) or "none"
    reused = " (reused)" if result.reused else ""
    return f"{result.as_of}: {len(result.members)} members{reused}; top exclusions: {top}"


def _month(text: str) -> date:
    try:
        return date.fromisoformat(f"{text}-01")
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a month like 2019-03") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the eligible universe month by month.")
    parser.add_argument("--start", type=_month, default=date(1998, 12, 1), help="first month")
    parser.add_argument("--end", type=_month, default=date.today(), help="last month")
    parser.add_argument("--config", type=Path, default=Path("config/universe/default.yaml"))
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings, level=settings.log_level)
    universe = load_config(args.config, UniverseConfig)
    engine = make_engine(settings, DbRole.APP)
    with correlation_scope():
        with engine.begin() as conn:
            register_config(conn, universe, ACTOR, "Universe configuration used by a batch build")
        print(f"Universe {universe.ref.version} (methodology {universe.config.sharia_methodology})")
        for screened in screening_dates(args.start, args.end):
            as_of = next_trading_day(screened)
            try:
                with engine.begin() as conn:
                    print(summary_line(build_universe(conn, as_of, universe)))
            except UniverseError as exc:
                print(f"{as_of}: NOT BUILT: {exc}")
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
