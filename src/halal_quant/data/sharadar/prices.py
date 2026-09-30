"""Import Sharadar daily prices (task 10; PRD §7, BRD §10, R1 "DS-1 API findings").

    uv run python -m halal_quant.data.sharadar.prices [--start 1998-01] [--end 2026-09]

The full price history is tens of millions of rows, so it is downloaded one calendar month at a
time. Each month is its own download, data version, transaction and audit event, and can be
re-run safely: rows already stored are left alone, so an interrupted import simply continues.

Sharadar identifies a price row by ticker and date only, so each row is matched to a security
through the dated ticker history in the security master (`TickerDirectory`). A row whose ticker
belonged to no security on that day is skipped and counted, never guessed.

Sharadar's open, high, low, close and volume are split-adjusted as of the download, `closeadj` is
also dividend-adjusted, and `closeunadj` is the price actually paid. After a later split or
dividend the provider restates old rows. Stored rows are never overwritten (the app role cannot):
a row whose stored values differ from a new download is reported for review (G8 OI-16).
"""

import argparse
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from itertools import batched

from pydantic import ValidationError
from sqlalchemy import Connection, select

from halal_quant.audit import AuditEvent, record_event
from halal_quant.data.market_data import DailyPrice, daily_price_table
from halal_quant.data.security_master import TickerDirectory, normalize_ticker
from halal_quant.data.sharadar.cli import run_import
from halal_quant.data.sharadar.client import Download
from halal_quant.data.sharadar.values import blank_to_none, parse_date, parse_decimal
from halal_quant.data.versions import DataVersion, register_data_version, version_label

SOURCE = "sharadar"
DATASET = "sharadar.stocks"
ACTOR = "system:sharadar-import"
REQUIRED_COLUMNS = (
    "ticker",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "closeadj",
    "closeunadj",
)
FIRST_MONTH = date(1998, 1, 1)
INSERT_BATCH = 5000
MAX_REVIEW_ITEMS = 20
MAX_REVIEW_ITEMS_IN_AUDIT = 50
COMPARED = ("open", "high", "low", "close", "adjusted_close", "close_unadjusted", "volume")

Key = tuple[int, date]  # (security_id, price_date)


@dataclass
class PriceResult:
    data_version: str
    inserted: int = 0
    already_present: int = 0
    restated: int = 0  # stored rows whose values differ from this download
    skipped: Counter[str] = field(default_factory=Counter)
    unmatched_tickers: Counter[str] = field(default_factory=Counter)
    first_date: date | None = None
    last_date: date | None = None
    needs_review: list[str] = field(default_factory=list)

    def summary(self) -> str:
        skipped = ", ".join(f"{k}: {v}" for k, v in sorted(self.skipped.items())) or "none"
        return (
            f"Data version {self.data_version}\n"
            f"  inserted {self.inserted}, already present {self.already_present} "
            f"(of which restated by Sharadar: {self.restated})\n"
            f"  dates {self.first_date} to {self.last_date}\n"
            f"  skipped rows ({sum(self.skipped.values())}): {skipped}\n"
            f"  needs review: {len(self.needs_review)}"
        )


def parse_price(
    row: dict[str, str], directory: TickerDirectory, data_version: str
) -> tuple[tuple[DailyPrice, str] | None, str | None]:
    """Turn one source row into (price, ticker), or say why it cannot be used."""
    ticker = blank_to_none(row.get("ticker"))
    if not ticker:
        return None, "missing_ticker"
    try:
        price_date = parse_date(row.get("date"))
        numbers = {
            name: parse_decimal(row.get(column))
            for name, column in (
                ("open", "open"),
                ("high", "high"),
                ("low", "low"),
                ("close", "close"),
                ("adjusted_close", "closeadj"),
                ("close_unadjusted", "closeunadj"),
                ("volume", "volume"),
            )
        }
    except ValueError:
        return None, "bad_value"
    if price_date is None:
        return None, "missing_date"
    if any(value is None for value in numbers.values()):
        return None, "missing_value"
    volume = numbers.pop("volume")
    if volume is None or volume != volume.to_integral_value():
        return None, "bad_volume"
    ticker = normalize_ticker(ticker)
    security_id = directory.resolve(ticker, price_date)
    if security_id is None:
        return None, "no_security_for_ticker_on_date"
    try:
        price = DailyPrice.model_validate(
            {
                "security_id": security_id,
                "price_date": price_date,
                "volume": int(volume),
                "source": SOURCE,
                "data_version": data_version,
                **numbers,
            }
        )
    except ValidationError:
        return None, "impossible_price_or_volume"
    return (price, ticker), None


def _stored(conn: Connection, first: date, last: date) -> dict[Key, tuple[Decimal | int, ...]]:
    p = daily_price_table.c
    rows = conn.execute(
        select(
            p.security_id,
            p.price_date,
            p.open,
            p.high,
            p.low,
            p.close,
            p.adjusted_close,
            p.close_unadjusted,
            p.volume,
        ).where(p.price_date >= first, p.price_date <= last)
    )
    return {(r.security_id, r.price_date): tuple(r[2:]) for r in rows}


def import_prices(
    conn: Connection,
    download: Download,
    window: str,
    directory: TickerDirectory | None = None,
    actor: str = ACTOR,
) -> PriceResult:
    """Add the download's prices that are not stored yet, and audit the run.

    `window` names the slice downloaded (e.g. "2019-03") and is part of the data version.
    Runs on the caller's connection: the caller commits, or rolls everything back.
    """
    dataset = f"{DATASET}.{window}"
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
    result = PriceResult(data_version=version)
    directory = directory or TickerDirectory.load(conn)

    fresh: dict[Key, tuple[DailyPrice, str]] = {}
    for source_row in download.rows:
        parsed, reason = parse_price(source_row, directory, version)
        if parsed is None:
            result.skipped[reason or "unusable_row"] += 1
            if reason == "no_security_for_ticker_on_date":
                result.unmatched_tickers[normalize_ticker(source_row["ticker"])] += 1
            continue
        price, ticker = parsed
        key = (price.security_id, price.price_date)
        if key in fresh:
            result.skipped["duplicate_in_download"] += 1
            continue
        fresh[key] = (price, ticker)
    if fresh:
        dates = [key[1] for key in fresh]
        result.first_date, result.last_date = min(dates), max(dates)

    stored = (
        _stored(conn, result.first_date, result.last_date)
        if result.first_date and result.last_date
        else {}
    )
    new_rows: list[dict[str, object]] = []
    for key, (price, ticker) in fresh.items():
        if key in stored:
            result.already_present += 1
            if stored[key] != tuple(getattr(price, name) for name in COMPARED):
                result.restated += 1
                if len(result.needs_review) < MAX_REVIEW_ITEMS:
                    result.needs_review.append(
                        f"{ticker} {key[1]}: Sharadar's copy now differs from the stored row"
                    )
        else:
            new_rows.append(price.model_dump())
    for batch in batched(new_rows, INSERT_BATCH):
        conn.execute(daily_price_table.insert(), list(batch))
    result.inserted = len(new_rows)
    if result.unmatched_tickers:
        common = ", ".join(t for t, _ in result.unmatched_tickers.most_common(5))
        result.needs_review.append(
            f"{sum(result.unmatched_tickers.values())} rows had no security for their ticker on "
            f"their date (most common: {common})"
        )

    record_event(
        conn,
        AuditEvent(
            actor=actor,
            action="import.completed",
            entity_type="import:sharadar.stocks",
            entity_id=version,
            reason=f"Sharadar daily prices import, {window}",
            source=__name__,
            details={
                "data_version": version,
                "window": window,
                "sha256": download.sha256,
                "downloaded_at": download.downloaded_at.isoformat(),
                "rows_downloaded": len(download.rows),
                "inserted": result.inserted,
                "already_present": result.already_present,
                "restated": result.restated,
                "skipped": dict(result.skipped),
                "first_date": result.first_date,
                "last_date": result.last_date,
                "needs_review_count": len(result.needs_review),
                "needs_review": result.needs_review[:MAX_REVIEW_ITEMS_IN_AUDIT],
            },
        ),
    )
    return result


def month_windows(start: date, end: date) -> Iterator[tuple[str, date, date]]:
    """(label, first day, last day) of every calendar month from `start`'s to `end`'s."""
    current = start.replace(day=1)
    while current <= end:
        following = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
        yield f"{current:%Y-%m}", current, following - timedelta(days=1)
        current = following


def _month(text: str) -> date:
    try:
        return date.fromisoformat(f"{text}-01")
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a month like 2019-03") from None


def _importer(window: str) -> Callable[[Connection, Download], PriceResult]:
    return lambda conn, download: import_prices(conn, download, window)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import Sharadar daily prices, month by month.")
    parser.add_argument("--start", type=_month, default=FIRST_MONTH, help="first month, YYYY-MM")
    parser.add_argument("--end", type=_month, default=date.today(), help="last month, YYYY-MM")
    args = parser.parse_args(argv)
    for label, first, last in month_windows(args.start, args.end):
        print(f"== {label}")
        code = run_import(
            "stocks",
            REQUIRED_COLUMNS,
            _importer(label),
            **{"date.gte": first.isoformat(), "date.lte": last.isoformat()},
        )
        if code != 0:
            return code
    return 0


if __name__ == "__main__":
    sys.exit(main())
