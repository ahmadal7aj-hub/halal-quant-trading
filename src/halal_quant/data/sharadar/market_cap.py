"""Import Sharadar's daily market value (task 11; S1 §3 "Market value").

    uv run python -m halal_quant.data.sharadar.market_cap [--start 1998-12] [--end 2026-09]

Sharadar's `daily` table delivers `marketcap` in USD millions (G8 OI-6); it is converted to USD
here so it is directly comparable with the SF1 amounts. Like prices, it is downloaded one calendar
month at a time, matched to securities by dated ticker, add-only, and safe to re-run.
"""

import argparse
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from itertools import batched

from sqlalchemy import Connection, select

from halal_quant.audit import AuditEvent, record_event
from halal_quant.data.fundamentals import daily_market_cap_table
from halal_quant.data.security_master import TickerDirectory, normalize_ticker
from halal_quant.data.sharadar.cli import run_import
from halal_quant.data.sharadar.client import Download
from halal_quant.data.sharadar.prices import month_windows
from halal_quant.data.sharadar.values import blank_to_none, parse_date, parse_decimal
from halal_quant.data.versions import DataVersion, register_data_version, version_label

SOURCE = "sharadar"
DATASET = "sharadar.daily"
ACTOR = "system:sharadar-import"
REQUIRED_COLUMNS = ("ticker", "date", "marketcap")
FIRST_MONTH = date(1998, 12, 1)  # the daily table starts in December 1998
USD_PER_MILLION = Decimal(1_000_000)
INSERT_BATCH = 5000
MAX_REVIEW_ITEMS = 20
MAX_REVIEW_ITEMS_IN_AUDIT = 50

Key = tuple[int, date]  # (security_id, cap_date)


@dataclass
class MarketCapResult:
    data_version: str
    inserted: int = 0
    already_present: int = 0
    restated: int = 0
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


def parse_market_cap(
    row: dict[str, str], directory: TickerDirectory
) -> tuple[tuple[Key, Decimal] | None, str | None]:
    """Turn one source row into (key, market value in USD), or say why it cannot be used."""
    ticker = blank_to_none(row.get("ticker"))
    if not ticker:
        return None, "missing_ticker"
    try:
        cap_date = parse_date(row.get("date"))
        millions = parse_decimal(row.get("marketcap"))
    except ValueError:
        return None, "bad_value"
    if cap_date is None:
        return None, "missing_date"
    if millions is None:
        return None, "missing_value"
    security_id = directory.resolve(normalize_ticker(ticker), cap_date)
    if security_id is None:
        return None, "no_security_for_ticker_on_date"
    # A non-positive value is stored as delivered; S1 treats it as UNKNOWN when screening.
    return ((security_id, cap_date), millions * USD_PER_MILLION), None


def _stored(conn: Connection, first: date, last: date) -> dict[Key, Decimal]:
    m = daily_market_cap_table.c
    rows = conn.execute(
        select(m.security_id, m.cap_date, m.market_cap_usd).where(
            m.cap_date >= first, m.cap_date <= last
        )
    )
    return {(r.security_id, r.cap_date): r.market_cap_usd for r in rows}


def import_market_cap(
    conn: Connection,
    download: Download,
    window: str,
    directory: TickerDirectory | None = None,
    actor: str = ACTOR,
) -> MarketCapResult:
    """Add the download's market values that are not stored yet, and audit the run.

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
    result = MarketCapResult(data_version=version)
    directory = directory or TickerDirectory.load(conn)

    fresh: dict[Key, tuple[Decimal, str]] = {}
    for source_row in download.rows:
        parsed, reason = parse_market_cap(source_row, directory)
        if parsed is None:
            result.skipped[reason or "unusable_row"] += 1
            if reason == "no_security_for_ticker_on_date":
                result.unmatched_tickers[normalize_ticker(source_row["ticker"])] += 1
            continue
        key, value = parsed
        if key in fresh:
            result.skipped["duplicate_in_download"] += 1
            continue
        fresh[key] = (value, normalize_ticker(source_row["ticker"]))
    if fresh:
        dates = [key[1] for key in fresh]
        result.first_date, result.last_date = min(dates), max(dates)

    stored = (
        _stored(conn, result.first_date, result.last_date)
        if result.first_date and result.last_date
        else {}
    )
    new_rows: list[dict[str, object]] = []
    for key, (value, ticker) in fresh.items():
        if key in stored:
            result.already_present += 1
            if stored[key] != value:
                result.restated += 1
                if len(result.needs_review) < MAX_REVIEW_ITEMS:
                    result.needs_review.append(
                        f"{ticker} {key[1]}: Sharadar's copy now differs from the stored row"
                    )
        else:
            new_rows.append(
                {
                    "security_id": key[0],
                    "cap_date": key[1],
                    "market_cap_usd": value,
                    "source": SOURCE,
                    "data_version": version,
                }
            )
    for batch in batched(new_rows, INSERT_BATCH):
        conn.execute(daily_market_cap_table.insert(), list(batch))
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
            entity_type="import:sharadar.daily",
            entity_id=version,
            reason=f"Sharadar daily market value import, {window}",
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


def _month(text: str) -> date:
    try:
        return date.fromisoformat(f"{text}-01")
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a month like 2019-03") from None


def _importer(window: str) -> Callable[[Connection, Download], MarketCapResult]:
    return lambda conn, download: import_market_cap(conn, download, window)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import Sharadar daily market value by month.")
    parser.add_argument("--start", type=_month, default=FIRST_MONTH, help="first month, YYYY-MM")
    parser.add_argument("--end", type=_month, default=date.today(), help="last month, YYYY-MM")
    args = parser.parse_args(argv)
    for label, first, last in month_windows(args.start, args.end):
        print(f"== {label}")
        code = run_import(
            "daily",
            REQUIRED_COLUMNS,
            _importer(label),
            **{"date.gte": first.isoformat(), "date.lte": last.isoformat()},
        )
        if code != 0:
            return code
    return 0


if __name__ == "__main__":
    sys.exit(main())
