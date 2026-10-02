"""Download one consistent price vintage: every stock and the benchmark funds (P2-2, P2-3).

    uv run python -m halal_quant.data.sharadar.vintage --vintage v1 [--start 1998-01] [--finish]
        [--no-stocks] [--benchmark SPUS --benchmark HLAL ...]

A backtest must read one vintage only (ADR-003): adjusted prices shift whenever a split or
dividend happens, so downloads made at different times do not agree (G8 OI-16). This command
downloads every month of stock prices and the benchmark funds into a named vintage in one run. It
is resumable (`--start` the month that was running; rows already stored are left alone) and
`--finish` marks the vintage complete, after which nothing can be added to it.

Stock rows are matched to securities exactly as in the main price import (the same parsing, the
same skip reasons); fund rows are keyed by their symbol.
"""

import argparse
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP
from itertools import batched

from sqlalchemy import Connection, select

from halal_quant.audit import AuditEvent, record_event
from halal_quant.core.logging import configure_logging, correlation_scope
from halal_quant.core.settings import DbRole, get_settings
from halal_quant.data.security_master import TickerDirectory
from halal_quant.data.sharadar.cli import run_import
from halal_quant.data.sharadar.client import Download
from halal_quant.data.sharadar.prices import (
    FIRST_MONTH,
    REQUIRED_COLUMNS,
    month_windows,
    parse_price,
)
from halal_quant.data.sharadar.values import blank_to_none, parse_date, parse_decimal
from halal_quant.data.versions import DataVersion, register_data_version, version_label
from halal_quant.data.vintage import (
    finish_vintage,
    require_building,
    start_vintage,
    vintage_benchmark_price_table,
    vintage_price_table,
)
from halal_quant.db.engine import make_engine

SOURCE = "sharadar"
ACTOR = "system:sharadar-vintage"
FUND_COLUMNS = ("ticker", "date", "volume", "closeadj", "closeunadj")
DEFAULT_BENCHMARKS = ("SPUS", "HLAL", "SPY", "IVV")
INSERT_BATCH = 5000


@dataclass
class VintageResult:
    data_version: str
    inserted: int = 0
    already_present: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    first_date: date | None = None
    last_date: date | None = None
    needs_review: list[str] = field(default_factory=list)

    def summary(self) -> str:
        skipped = ", ".join(f"{k}: {v}" for k, v in sorted(self.skipped.items())) or "none"
        return (
            f"Data version {self.data_version}\n"
            f"  inserted {self.inserted}, already present {self.already_present}\n"
            f"  dates {self.first_date} to {self.last_date}\n"
            f"  skipped rows ({sum(self.skipped.values())}): {skipped}"
        )


def _register(conn: Connection, dataset: str, download: Download) -> str:
    version = version_label(dataset, download.sha256)
    register_data_version(
        conn,
        DataVersion(
            version=version,
            source=SOURCE,
            dataset=dataset,
            sha256=download.sha256,
            row_count=len(download.rows),
            downloaded_at=download.downloaded_at,
        ),
    )
    return version


def _audit(
    conn: Connection, actor: str, action: str, vintage_id: str, version: str, d: Download, r: object
) -> None:
    record_event(
        conn,
        AuditEvent(
            actor=actor,
            action=action,
            entity_type=f"vintage:{vintage_id}",
            entity_id=version,
            reason="Price vintage download",
            source=__name__,
            details={
                "vintage_id": vintage_id,
                "data_version": version,
                "sha256": d.sha256,
                "downloaded_at": d.downloaded_at.isoformat(),
                "rows_downloaded": len(d.rows),
                "result": str(r).replace("\n", "; ")[:600],
            },
        ),
    )


def import_vintage_month(
    conn: Connection,
    download: Download,
    vintage_id: str,
    window: str,
    directory: TickerDirectory | None = None,
    actor: str = ACTOR,
) -> VintageResult:
    """Add one month of stock prices to a vintage that is being built."""
    require_building(conn, vintage_id)
    version = _register(conn, f"sharadar.stocks.vintage.{vintage_id}.{window}", download)
    result = VintageResult(data_version=version)
    directory = directory or TickerDirectory.load(conn)
    fresh: dict[tuple[int, date], dict[str, object]] = {}
    for row in download.rows:
        parsed, reason = parse_price(row, directory, version)
        if parsed is None:
            result.skipped[reason or "unusable_row"] += 1
            continue
        price, _ = parsed
        key = (price.security_id, price.price_date)
        if key in fresh:
            result.skipped["duplicate_in_download"] += 1
            continue
        fresh[key] = {
            "vintage_id": vintage_id,
            "security_id": price.security_id,
            "price_date": price.price_date,
            "close_unadjusted": price.close_unadjusted,
            "adjusted_close": price.adjusted_close,
            "volume": price.volume,
        }
    if fresh:
        dates = [key[1] for key in fresh]
        result.first_date, result.last_date = min(dates), max(dates)
        p = vintage_price_table.c
        stored = {
            (r.security_id, r.price_date)
            for r in conn.execute(
                select(p.security_id, p.price_date).where(
                    p.vintage_id == vintage_id,
                    p.price_date >= result.first_date,
                    p.price_date <= result.last_date,
                )
            )
        }
        new_rows = [row for key, row in fresh.items() if key not in stored]
        result.already_present = len(fresh) - len(new_rows)
        for batch in batched(new_rows, INSERT_BATCH):
            conn.execute(vintage_price_table.insert(), list(batch))
        result.inserted = len(new_rows)
    _audit(conn, actor, "vintage.stocks_imported", vintage_id, version, download, result.summary())
    return result


def import_vintage_benchmark(
    conn: Connection, download: Download, vintage_id: str, symbol: str, actor: str = ACTOR
) -> VintageResult:
    """Add one benchmark fund's full price history to a vintage that is being built."""
    require_building(conn, vintage_id)
    version = _register(conn, f"sharadar.funds.vintage.{vintage_id}.{symbol}", download)
    result = VintageResult(data_version=version)
    fresh: dict[date, dict[str, object]] = {}
    for row in download.rows:
        if (blank_to_none(row.get("ticker")) or "").upper() != symbol.upper():
            result.skipped["other_ticker"] += 1
            continue
        try:
            day = parse_date(row.get("date"))
            adjusted = parse_decimal(row.get("closeadj"))
            unadjusted = parse_decimal(row.get("closeunadj"))
            volume = parse_decimal(row.get("volume"))
        except ValueError:
            result.skipped["bad_value"] += 1
            continue
        if day is None or adjusted is None or unadjusted is None or volume is None:
            result.skipped["missing_value"] += 1
            continue
        if adjusted <= 0 or unadjusted <= 0 or volume < 0:
            result.skipped["impossible_price_or_volume"] += 1
            continue
        if day in fresh:
            result.skipped["duplicate_in_download"] += 1
            continue
        fresh[day] = {
            "vintage_id": vintage_id,
            "symbol": symbol.upper(),
            "price_date": day,
            "close_unadjusted": unadjusted,
            "adjusted_close": adjusted,
            "volume": int(volume.to_integral_value(ROUND_HALF_UP)),
        }
    b = vintage_benchmark_price_table.c
    stored = {
        r.price_date
        for r in conn.execute(
            select(b.price_date).where(b.vintage_id == vintage_id, b.symbol == symbol.upper())
        )
    }
    new_rows = [row for day, row in fresh.items() if day not in stored]
    result.already_present = len(fresh) - len(new_rows)
    for batch in batched(new_rows, INSERT_BATCH):
        conn.execute(vintage_benchmark_price_table.insert(), list(batch))
    result.inserted = len(new_rows)
    if fresh:
        result.first_date, result.last_date = min(fresh), max(fresh)
    _audit(
        conn, actor, "vintage.benchmark_imported", vintage_id, version, download, result.summary()
    )
    return result


def _month(text: str) -> date:
    try:
        return date.fromisoformat(f"{text}-01")
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a month like 2019-03") from None


def _stock_importer(
    vintage_id: str, window: str
) -> Callable[[Connection, Download], VintageResult]:
    return lambda conn, download: import_vintage_month(conn, download, vintage_id, window)


def _fund_importer(vintage_id: str, symbol: str) -> Callable[[Connection, Download], VintageResult]:
    return lambda conn, download: import_vintage_benchmark(conn, download, vintage_id, symbol)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download one consistent price vintage.")
    parser.add_argument("--vintage", required=True, help="vintage id, for example v1")
    parser.add_argument("--description", default="Single-run Sharadar price vintage")
    parser.add_argument("--start", type=_month, default=FIRST_MONTH, help="first month, YYYY-MM")
    parser.add_argument("--end", type=_month, default=date.today(), help="last month, YYYY-MM")
    parser.add_argument("--benchmark", action="append", default=None, help="fund symbol")
    parser.add_argument("--no-stocks", action="store_true", help="only the benchmark funds")
    parser.add_argument(
        "--finish", action="store_true", help="mark the vintage complete at the end"
    )
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings, level=settings.log_level)
    engine = make_engine(settings, DbRole.APP)
    with correlation_scope():
        with engine.begin() as conn:
            created = start_vintage(conn, args.vintage, args.description)
        print(f"Vintage {args.vintage}: {'started' if created else 'resuming'}")
        if not args.no_stocks:
            for label, first, last in month_windows(args.start, args.end):
                print(f"== {label}")
                code = run_import(
                    "stocks",
                    REQUIRED_COLUMNS,
                    _stock_importer(args.vintage, label),
                    **{"date.gte": first.isoformat(), "date.lte": last.isoformat()},
                )
                if code != 0:
                    return code
        for symbol in args.benchmark or DEFAULT_BENCHMARKS:
            print(f"== fund {symbol}")
            code = run_import(
                "funds", FUND_COLUMNS, _fund_importer(args.vintage, symbol), ticker=symbol
            )
            if code != 0:
                return code
        if args.finish:
            with engine.begin() as conn:
                finish_vintage(conn, args.vintage)
            print(f"Vintage {args.vintage}: complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
