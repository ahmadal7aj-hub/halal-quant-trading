"""Import Sharadar's S&P 500 constituents table, as delivered (task 9; BRD §10, R1 "S&P 500").

    uv run python -m halal_quant.data.sharadar.index_membership

The table mixes quarterly membership snapshots, index changes and the current list, tagged by
its `action` column. The importer stores every row faithfully (blank and "N/A" become NULL) and
does not interpret it: the free Sharadar tier only shows `current` and `historical` rows, so how
a past membership list is rebuilt from snapshots plus changes cannot be checked yet. That logic
comes after the full table has been examined.

Rows carry only a ticker, not a permanent ID, and tickers change (G8 OI-13). Matching a row to a
`security_id` is therefore left to whoever uses it, on the row's own date.

Rows are only ever added. A row already stored is left alone; if Sharadar's copy of it now
differs (say a corrected company name) that is reported for review, not overwritten.
"""

import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import (
    BigInteger,
    Column,
    Connection,
    Date,
    DateTime,
    Identity,
    Table,
    Text,
    UniqueConstraint,
    func,
    select,
)

from halal_quant.audit import AuditEvent, record_event
from halal_quant.data.security_master import normalize_ticker
from halal_quant.data.sharadar.cli import run_import
from halal_quant.data.sharadar.client import Download
from halal_quant.data.versions import DataVersion, register_data_version, version_label
from halal_quant.db.engine import metadata

INDEX = "sp500"
SOURCE = "sharadar"
DATASET = "sharadar.sp500"
ACTOR = "system:sharadar-import"
REQUIRED_COLUMNS = ("date", "action", "ticker", "name", "contraticker", "contraname", "note")
MAX_REVIEW_ITEMS_IN_AUDIT = 50

index_membership_table = Table(
    "index_membership",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("index_name", Text, nullable=False),
    Column("record_date", Date, nullable=False),
    Column("action", Text, nullable=False),  # as delivered: "current", "historical", ...
    Column("ticker", Text, nullable=False),
    Column("company_name", Text),
    Column("contra_ticker", Text),
    Column("contra_name", Text),
    Column("note", Text),
    Column("source", Text, nullable=False),
    Column("data_version", Text, nullable=False),
    Column("imported_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint(
        "index_name", "record_date", "action", "ticker", name="uq_index_membership_record"
    ),
)

Key = tuple[date, str, str]  # (record_date, action, ticker)
VALUE_FIELDS = ("company_name", "contra_ticker", "contra_name", "note")


@dataclass
class MembershipResult:
    data_version: str
    inserted: int = 0
    already_present: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    actions: Counter[str] = field(default_factory=Counter)  # actions seen in this download
    first_date: date | None = None
    last_date: date | None = None
    needs_review: list[str] = field(default_factory=list)

    def summary(self) -> str:
        skipped = ", ".join(f"{k}: {v}" for k, v in sorted(self.skipped.items())) or "none"
        actions = ", ".join(f"{k}: {v}" for k, v in sorted(self.actions.items())) or "none"
        return (
            f"Data version {self.data_version}\n"
            f"  inserted {self.inserted}, already present {self.already_present}\n"
            f"  rows by action: {actions}; dates {self.first_date} to {self.last_date}\n"
            f"  skipped rows ({sum(self.skipped.values())}): {skipped}\n"
            f"  needs review: {len(self.needs_review)}"
        )


def _blank_to_none(value: str | None) -> str | None:
    text = (value or "").strip()
    return None if text.upper() in ("", "N/A") else text


def parse_row(row: dict[str, str]) -> tuple[tuple[Key, dict[str, str | None]] | None, str | None]:
    """Turn one source row into (key, other fields), or say why it cannot be used."""
    action = _blank_to_none(row.get("action"))
    ticker = _blank_to_none(row.get("ticker"))
    raw_date = _blank_to_none(row.get("date"))
    if not (action and ticker and raw_date):
        return None, "missing_key_field"
    try:
        record_date = date.fromisoformat(raw_date)
    except ValueError:
        return None, "bad_date"
    values = {
        "company_name": _blank_to_none(row.get("name")),
        "contra_ticker": _blank_to_none(row.get("contraticker")),
        "contra_name": _blank_to_none(row.get("contraname")),
        "note": _blank_to_none(row.get("note")),
    }
    return ((record_date, action.lower(), normalize_ticker(ticker)), values), None


def import_index_membership(
    conn: Connection, download: Download, index_name: str = INDEX, actor: str = ACTOR
) -> MembershipResult:
    """Add the download's rows that are not stored yet, and audit the run.

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
    result = MembershipResult(data_version=version)
    t = index_membership_table.c
    stored: dict[Key, tuple[str | None, ...]] = {
        (r.record_date, r.action, r.ticker): (
            r.company_name,
            r.contra_ticker,
            r.contra_name,
            r.note,
        )
        for r in conn.execute(select(index_membership_table).where(t.index_name == index_name))
    }

    new_rows: dict[Key, dict[str, str | None]] = {}
    for source_row in download.rows:
        parsed, reason = parse_row(source_row)
        if parsed is None:
            result.skipped[reason or "unusable_row"] += 1
            continue
        key, values = parsed
        if key in new_rows:
            result.skipped["duplicate_in_download"] += 1
            continue
        result.actions[key[1]] += 1
        result.first_date = min(result.first_date or key[0], key[0])
        result.last_date = max(result.last_date or key[0], key[0])
        if key in stored:
            result.already_present += 1
            if stored[key] != tuple(values[f] for f in VALUE_FIELDS):
                result.needs_review.append(
                    f"{key[2]} {key[1]} {key[0]}: Sharadar's copy now differs from the stored row"
                )
            continue
        new_rows[key] = values

    if new_rows:
        conn.execute(
            index_membership_table.insert(),
            [
                {
                    "index_name": index_name,
                    "record_date": key[0],
                    "action": key[1],
                    "ticker": key[2],
                    **values,
                    "source": SOURCE,
                    "data_version": version,
                }
                for key, values in new_rows.items()
            ],
        )
    result.inserted = len(new_rows)

    record_event(
        conn,
        AuditEvent(
            actor=actor,
            action="import.completed",
            entity_type="import:sharadar.sp500",
            entity_id=version,
            reason="Sharadar S&P 500 membership import",
            source=__name__,
            details={
                "data_version": version,
                "sha256": download.sha256,
                "downloaded_at": download.downloaded_at.isoformat(),
                "rows_downloaded": len(download.rows),
                "inserted": result.inserted,
                "already_present": result.already_present,
                "skipped": dict(result.skipped),
                "actions": dict(result.actions),
                "first_date": result.first_date,
                "last_date": result.last_date,
                "needs_review_count": len(result.needs_review),
                "needs_review": result.needs_review[:MAX_REVIEW_ITEMS_IN_AUDIT],
            },
        ),
    )
    return result


def main() -> int:
    return run_import(
        "sp500",
        REQUIRED_COLUMNS,
        lambda conn, download: import_index_membership(conn, download),
    )


if __name__ == "__main__":
    sys.exit(main())
