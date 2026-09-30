"""Import Sharadar's company list into the security master (task 9; PRD §5, R1 "Tickers").

    uv run python -m halal_quant.data.sharadar.companies

Source rows are the `stocks` rows of Sharadar's Tickers table: one row per security, with its
permanent ID (`permaticker`), its *current* ticker, descriptive fields, and first and last price
dates. Delisted companies are included, which is what prevents survivorship bias.

What a run does, per security, keyed by (`sharadar`, permaticker):
- new: created, its ticker valid from the first price date, and closed the day after the last
  price date if delisted;
- known: descriptive fields (name, exchange, sector, industry, ...) are refreshed; a company that
  has newly delisted is closed. Running again on the same data changes nothing.

Sharadar's table holds only today's ticker, so the importer never rewrites ticker history or
start dates of a known security. If Sharadar now disagrees (ticker changed, start date moved,
delisted company reappears, company missing) it is reported for review, not changed: the dated
ticker changes come from the corporate-actions import (task 10).

Rows that cannot be dated or identified are skipped and counted, never guessed at. One audit
event per run records the counts and the data version (download time + fingerprint).
"""

import logging
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Connection, and_, func, select, update
from sqlalchemy.exc import IntegrityError

from halal_quant.audit import AuditEvent, record_event
from halal_quant.data.security_master import (
    SecurityInfo,
    normalize_ticker,
    security_table,
    security_ticker_table,
)
from halal_quant.data.sharadar.cli import run_import
from halal_quant.data.sharadar.client import Download
from halal_quant.data.versions import DataVersion, register_data_version, version_label

log = logging.getLogger(__name__)

SOURCE = "sharadar"
DATASET = "sharadar.tickers.stocks"
ACTOR = "system:sharadar-import"
REQUIRED_COLUMNS = (
    "permaticker",
    "ticker",
    "name",
    "exchange",
    "isdelisted",
    "category",
    "siccode",
    "sector",
    "industry",
    "currency",
    "location",
    "firstpricedate",
    "lastpricedate",
)
DESCRIPTIVE_FIELDS = (
    "company_name",
    "exchange",
    "currency",
    "country",
    "sector",
    "industry",
    "category",
    "sic_code",
)
MAX_REVIEW_ITEMS_IN_AUDIT = 50


@dataclass(frozen=True)
class CompanyRecord:
    info: SecurityInfo
    ticker: str
    first_price_date: date
    last_price_date: date | None  # set only for delisted securities


@dataclass
class ImportResult:
    data_version: str
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    newly_delisted: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    needs_review: list[str] = field(default_factory=list)

    def summary(self) -> str:
        skipped = ", ".join(f"{k}: {v}" for k, v in sorted(self.skipped.items())) or "none"
        return (
            f"Data version {self.data_version}\n"
            f"  created {self.created}, updated {self.updated}, unchanged {self.unchanged}, "
            f"newly delisted {self.newly_delisted}\n"
            f"  skipped rows ({sum(self.skipped.values())}): {skipped}\n"
            f"  needs review: {len(self.needs_review)}"
        )


def _clean(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def parse_company(row: dict[str, str]) -> tuple[CompanyRecord | None, str | None]:
    """Turn one source row into a record, or say why it cannot be used."""
    permaticker, ticker, name = (_clean(row.get(k)) for k in ("permaticker", "ticker", "name"))
    if not (permaticker and ticker and name):
        return None, "missing_identity"
    flag = (row.get("isdelisted") or "").strip().upper()
    if flag not in ("Y", "N"):
        return None, "bad_isdelisted_flag"
    try:
        first = _clean(row.get("firstpricedate"))
        last = _clean(row.get("lastpricedate"))
        first_date = date.fromisoformat(first) if first else None
        last_date = date.fromisoformat(last) if last else None
    except ValueError:
        return None, "bad_date"
    if first_date is None:
        return None, "no_first_price_date"
    delisted = flag == "Y"
    if delisted and last_date is None:
        return None, "delisted_without_last_price_date"
    if delisted and last_date is not None and last_date < first_date:
        return None, "last_price_before_first"
    info = SecurityInfo(
        source=SOURCE,
        source_id=permaticker,
        company_name=name,
        exchange=_clean(row.get("exchange")),
        currency=_clean(row.get("currency")),
        country=_clean(row.get("location")),
        sector=_clean(row.get("sector")),
        industry=_clean(row.get("industry")),
        category=_clean(row.get("category")),
        sic_code=_clean(row.get("siccode")),
    )
    return (
        CompanyRecord(
            info=info,
            ticker=normalize_ticker(ticker),
            first_price_date=first_date,
            last_price_date=last_date if delisted else None,
        ),
        None,
    )


def _existing_securities(conn: Connection, source: str) -> dict[str, Any]:
    """Every stored security of this source, with its currently open ticker, by source_id."""
    t = security_ticker_table.c
    open_ticker = and_(t.security_id == security_table.c.security_id, t.valid_to.is_(None))
    rows = conn.execute(
        select(security_table, t.ticker.label("open_ticker"), t.id.label("open_ticker_id"))
        .select_from(security_table.outerjoin(security_ticker_table, open_ticker))
        .where(security_table.c.source == source)
    ).mappings()
    return {row["source_id"]: row for row in rows}


def _create(conn: Connection, record: CompanyRecord) -> None:
    delisted = record.last_price_date is not None
    security_id: int = conn.execute(
        security_table.insert()
        .values(
            **record.info.model_dump(),
            start_date=record.first_price_date,
            end_date=record.last_price_date,
            delisted_flag=delisted,
            active_flag=not delisted,
        )
        .returning(security_table.c.security_id)
    ).scalar_one()
    valid_to = record.last_price_date + timedelta(days=1) if record.last_price_date else None
    conn.execute(
        security_ticker_table.insert().values(
            security_id=security_id,
            ticker=record.ticker,
            valid_from=record.first_price_date,
            valid_to=valid_to,
        )
    )


def _close(conn: Connection, existing: Any, last_price_date: date) -> None:
    """A known security has delisted: end its open ticker the day after its last price."""
    if existing["open_ticker_id"] is not None:
        conn.execute(
            update(security_ticker_table)
            .where(security_ticker_table.c.id == existing["open_ticker_id"])
            .values(valid_to=last_price_date + timedelta(days=1))
        )
    conn.execute(
        update(security_table)
        .where(security_table.c.security_id == existing["security_id"])
        .values(
            end_date=last_price_date,
            delisted_flag=True,
            active_flag=False,
            updated_at=func.now(),
        )
    )


def _review_notes(record: CompanyRecord, existing: Any) -> list[str]:
    """Disagreements the importer reports but does not change."""
    who = f"{record.info.source_id} ({record.ticker})"
    notes = []
    if existing["open_ticker"] is not None and existing["open_ticker"] != record.ticker:
        notes.append(f"{who}: ticker is now {record.ticker}, stored as {existing['open_ticker']}")
    if existing["start_date"] != record.first_price_date:
        notes.append(
            f"{who}: first price date is now {record.first_price_date}, "
            f"stored as {existing['start_date']}"
        )
    if existing["delisted_flag"] and record.last_price_date is None:
        notes.append(f"{who}: stored as delisted but Sharadar lists it as active again")
    return notes


def _apply_known(
    conn: Connection, record: CompanyRecord, existing: Any, result: ImportResult
) -> None:
    result.needs_review += _review_notes(record, existing)
    changes = {
        name: getattr(record.info, name)
        for name in DESCRIPTIVE_FIELDS
        if existing[name] != getattr(record.info, name)
    }
    if changes:
        conn.execute(
            update(security_table)
            .where(security_table.c.security_id == existing["security_id"])
            .values(**changes, updated_at=func.now())
        )
        result.updated += 1
    if record.last_price_date is not None and not existing["delisted_flag"]:
        _close(conn, existing, record.last_price_date)
        result.newly_delisted += 1
    elif not changes:
        result.unchanged += 1


def import_companies(
    conn: Connection, download: Download, source: str = SOURCE, actor: str = ACTOR
) -> ImportResult:
    """Apply a tickers download to the security master and audit the run.

    Runs on the caller's connection: the caller commits, or rolls everything back.
    """
    version = version_label(DATASET, download.sha256)
    register_data_version(
        conn,
        DataVersion(
            version=version,
            source=SOURCE,
            dataset=DATASET,
            sha256=download.sha256,
            row_count=len(download.rows),
            downloaded_at=download.downloaded_at,
        ),
    )
    result = ImportResult(data_version=version)
    known = _existing_securities(conn, source)
    seen: set[str] = set()

    for row in download.rows:
        seen.add((row.get("permaticker") or "").strip())
        parsed, reason = parse_company(row)
        if parsed is None:
            result.skipped[reason or "unusable_row"] += 1
            continue
        record = CompanyRecord(
            parsed.info.model_copy(update={"source": source}),
            parsed.ticker,
            parsed.first_price_date,
            parsed.last_price_date,
        )
        existing = known.get(record.info.source_id)
        try:
            with conn.begin_nested():
                if existing is None:
                    _create(conn, record)
                    result.created += 1
                else:
                    _apply_known(conn, record, existing, result)
        except IntegrityError as exc:
            diag = getattr(exc.orig, "diag", None)
            name = getattr(diag, "constraint_name", None) or "database_constraint"
            result.skipped[f"rejected_by_{name}"] += 1
            result.needs_review.append(
                f"{record.info.source_id} ({record.ticker}): rejected by {name}"
            )

    missing = sorted(set(known) - seen)
    result.needs_review += [f"{sid}: stored, but not in this Sharadar download" for sid in missing]

    record_event(
        conn,
        AuditEvent(
            actor=actor,
            action="import.completed",
            entity_type="import:sharadar.tickers",
            entity_id=version,
            reason="Sharadar companies import",
            source=__name__,
            details={
                "data_version": version,
                "sha256": download.sha256,
                "downloaded_at": download.downloaded_at.isoformat(),
                "rows_downloaded": len(download.rows),
                "created": result.created,
                "updated": result.updated,
                "unchanged": result.unchanged,
                "newly_delisted": result.newly_delisted,
                "skipped": dict(result.skipped),
                "needs_review_count": len(result.needs_review),
                "needs_review": result.needs_review[:MAX_REVIEW_ITEMS_IN_AUDIT],
            },
        ),
    )
    log.info(
        "sharadar companies import finished",
        extra={
            "data_version": version,
            "securities_created": result.created,
            "securities_updated": result.updated,
            "needs_review": len(result.needs_review),
        },
    )
    return result


def main() -> int:
    return run_import(
        "tickers",
        REQUIRED_COLUMNS,
        lambda conn, download: import_companies(conn, download),
        table="stocks",
    )


if __name__ == "__main__":
    sys.exit(main())
