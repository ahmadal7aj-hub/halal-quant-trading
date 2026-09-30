"""Import Sharadar as-reported fundamentals (task 11; PRD §19, BRD §9, S1 §3).

    uv run python -m halal_quant.data.sharadar.fundamentals [--start 1998] [--end 2026]

Only the as-reported dimensions ARQ, ARY and ART are requested: the "most recent" dimensions
include later restatements that were not public at the time (look-ahead). The table is
downloaded one dimension and one filing year at a time; each slice is its own download, data
version, transaction and audit event, and can be re-run safely.

Sharadar names the filing date `date` (G8 OI-1). Rows carry a ticker only, so each row is matched
to a security through the dated ticker history on its filing date; a row with no match is skipped
and counted, never guessed (G8 OI-13). Rows are only ever added; a stored filing that Sharadar
now shows differently is reported for review, not overwritten.
"""

import argparse
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from itertools import batched

from sqlalchemy import Connection, select

from halal_quant.audit import AuditEvent, record_event
from halal_quant.data.fundamentals import AMOUNT_COLUMNS, DIMENSIONS, fundamental_table
from halal_quant.data.security_master import TickerDirectory, normalize_ticker
from halal_quant.data.sharadar.cli import run_import
from halal_quant.data.sharadar.client import Download
from halal_quant.data.sharadar.values import blank_to_none, parse_date, parse_decimal
from halal_quant.data.versions import DataVersion, register_data_version, version_label

SOURCE = "sharadar"
DATASET = "sharadar.fundamentals"
ACTOR = "system:sharadar-import"
REQUIRED_COLUMNS = (
    "ticker",
    "dimension",
    "calendardate",
    "date",
    "reportperiod",
    "fiscalperiod",
    *AMOUNT_COLUMNS.values(),
)
FIRST_YEAR = 1998
INSERT_BATCH = 5000
MAX_REVIEW_ITEMS = 20
MAX_REVIEW_ITEMS_IN_AUDIT = 50

Key = tuple[int, str, date, date]  # (security_id, dimension, calendar_date, filing_date)
Parsed = tuple[Key, dict[str, object]]


@dataclass
class FundamentalsResult:
    data_version: str
    inserted: int = 0
    already_present: int = 0
    restated: int = 0  # stored filings whose values differ from this download
    skipped: Counter[str] = field(default_factory=Counter)
    unmatched_tickers: Counter[str] = field(default_factory=Counter)
    first_filing: date | None = None
    last_filing: date | None = None
    needs_review: list[str] = field(default_factory=list)

    def summary(self) -> str:
        skipped = ", ".join(f"{k}: {v}" for k, v in sorted(self.skipped.items())) or "none"
        return (
            f"Data version {self.data_version}\n"
            f"  inserted {self.inserted}, already present {self.already_present} "
            f"(of which restated by Sharadar: {self.restated})\n"
            f"  filings from {self.first_filing} to {self.last_filing}\n"
            f"  skipped rows ({sum(self.skipped.values())}): {skipped}\n"
            f"  needs review: {len(self.needs_review)}"
        )


def parse_fundamental(
    row: dict[str, str], directory: TickerDirectory
) -> tuple[Parsed | None, str | None]:
    """Turn one source row into (key, stored values), or say why it cannot be used."""
    ticker = blank_to_none(row.get("ticker"))
    if not ticker:
        return None, "missing_ticker"
    dimension = (blank_to_none(row.get("dimension")) or "").upper()
    if dimension not in DIMENSIONS:
        return None, "not_an_as_reported_dimension"
    try:
        calendar_date = parse_date(row.get("calendardate"))
        filing_date = parse_date(row.get("date"))
        report_period = parse_date(row.get("reportperiod"))
        amounts: dict[str, Decimal | None] = {
            name: parse_decimal(row.get(column)) for name, column in AMOUNT_COLUMNS.items()
        }
    except ValueError:
        return None, "bad_value"
    if calendar_date is None or filing_date is None:
        return None, "missing_date"
    security_id = directory.resolve(normalize_ticker(ticker), filing_date)
    if security_id is None:
        return None, "no_security_for_ticker_on_date"
    values: dict[str, object] = {
        "report_period": report_period,
        "fiscal_period": blank_to_none(row.get("fiscalperiod")),
        **amounts,
    }
    return ((security_id, dimension, calendar_date, filing_date), values), None


def _stored(conn: Connection, first: date, last: date) -> dict[Key, dict[str, object]]:
    f = fundamental_table.c
    rows = conn.execute(
        select(fundamental_table).where(f.filing_date >= first, f.filing_date <= last)
    ).mappings()
    return {
        (r["security_id"], r["dimension"], r["calendar_date"], r["filing_date"]): {
            name: r[name] for name in ("report_period", "fiscal_period", *AMOUNT_COLUMNS)
        }
        for r in rows
    }


def import_fundamentals(
    conn: Connection,
    download: Download,
    window: str,
    directory: TickerDirectory | None = None,
    actor: str = ACTOR,
) -> FundamentalsResult:
    """Add the download's filings that are not stored yet, and audit the run.

    `window` names the slice downloaded (e.g. "ARQ.2019") and is part of the data version.
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
    result = FundamentalsResult(data_version=version)
    directory = directory or TickerDirectory.load(conn)

    fresh: dict[Key, tuple[dict[str, object], str]] = {}
    for source_row in download.rows:
        parsed, reason = parse_fundamental(source_row, directory)
        if parsed is None:
            result.skipped[reason or "unusable_row"] += 1
            if reason == "no_security_for_ticker_on_date":
                result.unmatched_tickers[normalize_ticker(source_row["ticker"])] += 1
            continue
        key, values = parsed
        if key in fresh:
            result.skipped["duplicate_in_download"] += 1
            continue
        fresh[key] = (values, normalize_ticker(source_row["ticker"]))
    if fresh:
        filings = [key[3] for key in fresh]
        result.first_filing, result.last_filing = min(filings), max(filings)

    stored = (
        _stored(conn, result.first_filing, result.last_filing)
        if result.first_filing and result.last_filing
        else {}
    )
    new_rows: list[dict[str, object]] = []
    for key, (values, ticker) in fresh.items():
        if key in stored:
            result.already_present += 1
            if stored[key] != values:
                result.restated += 1
                if len(result.needs_review) < MAX_REVIEW_ITEMS:
                    result.needs_review.append(
                        f"{ticker} {key[1]} {key[2]} filed {key[3]}: "
                        "Sharadar's copy now differs from the stored row"
                    )
        else:
            new_rows.append(
                {
                    "security_id": key[0],
                    "dimension": key[1],
                    "calendar_date": key[2],
                    "filing_date": key[3],
                    **values,
                    "source": SOURCE,
                    "data_version": version,
                }
            )
    for batch in batched(new_rows, INSERT_BATCH):
        conn.execute(fundamental_table.insert(), list(batch))
    result.inserted = len(new_rows)
    if result.unmatched_tickers:
        common = ", ".join(t for t, _ in result.unmatched_tickers.most_common(5))
        result.needs_review.append(
            f"{sum(result.unmatched_tickers.values())} rows had no security for their ticker on "
            f"their filing date (most common: {common})"
        )

    record_event(
        conn,
        AuditEvent(
            actor=actor,
            action="import.completed",
            entity_type="import:sharadar.fundamentals",
            entity_id=version,
            reason=f"Sharadar fundamentals import, {window}",
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
                "first_filing": result.first_filing,
                "last_filing": result.last_filing,
                "needs_review_count": len(result.needs_review),
                "needs_review": result.needs_review[:MAX_REVIEW_ITEMS_IN_AUDIT],
            },
        ),
    )
    return result


def year_windows(first_year: int, last_year: int) -> Iterator[tuple[str, date, date]]:
    """(dimension.year label, first day, last day) of every filing year and dimension."""
    for year in range(first_year, last_year + 1):
        for dimension in DIMENSIONS:
            yield f"{dimension}.{year}", date(year, 1, 1), date(year, 12, 31)


def _importer(window: str) -> Callable[[Connection, Download], FundamentalsResult]:
    return lambda conn, download: import_fundamentals(conn, download, window)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import Sharadar as-reported fundamentals.")
    parser.add_argument("--start", type=int, default=FIRST_YEAR, help="first filing year")
    parser.add_argument("--end", type=int, default=date.today().year, help="last filing year")
    args = parser.parse_args(argv)
    for label, first, last in year_windows(args.start, args.end):
        print(f"== {label}")
        code = run_import(
            "fundamentals",
            REQUIRED_COLUMNS,
            _importer(label),
            dimension=label.split(".")[0],
            **{"date.gte": first.isoformat(), "date.lte": last.isoformat()},
        )
        if code != 0:
            return code
    return 0


if __name__ == "__main__":
    sys.exit(main())
