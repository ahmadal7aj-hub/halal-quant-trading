"""Import Sharadar corporate actions (task 10; PRD §7, BRD §10, R1 "DS-1 API findings").

    uv run python -m halal_quant.data.sharadar.actions

Sharadar's action codes are mapped onto the five types the market data model knows (see
`market_data.py` for what `value` means for each). Codes with no matching type (spin-offs,
share-class relations, exchange moves, ...) are not stored: they are counted by code in the
report, so the gap is visible (G8 OI-17).

Split values were checked against real prices (Honeywell, June 2026): `value` is new shares per
old share, so 0.5 is a 1-for-2 reverse split and prices before it were multiplied by 2.

Ticker changes are stored as SYMBOL_CHANGE actions but NOT yet applied to the ticker history of
the security master (G8 OI-13): which ticker Sharadar's price rows carry before a change cannot
be told from the free sample, so that step waits for the full data.

Actions are only ever added; a repeat of a stored action is left alone.
"""

import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import ValidationError
from sqlalchemy import Connection, select

from halal_quant.audit import AuditEvent, record_event
from halal_quant.data.market_data import ActionType, CorporateAction, corporate_action_table
from halal_quant.data.security_master import TickerDirectory, normalize_ticker
from halal_quant.data.sharadar.cli import run_import
from halal_quant.data.sharadar.client import Download
from halal_quant.data.sharadar.values import blank_to_none, parse_date, parse_decimal
from halal_quant.data.versions import DataVersion, register_data_version, version_label

SOURCE = "sharadar"
DATASET = "sharadar.actions"
ACTOR = "system:sharadar-import"
REQUIRED_COLUMNS = ("date", "action", "ticker", "name", "value", "contraticker", "contraname")
MAX_REVIEW_ITEMS_IN_AUDIT = 50

ACTION_TYPE_BY_CODE: dict[str, ActionType] = {
    "split": "SPLIT",
    "dividend": "DIVIDEND",
    "acquisitionof": "MERGER",
    "acquisitionby": "MERGER",
    "mergerfrom": "MERGER",
    "mergerto": "MERGER",
    "tickerchangefrom": "SYMBOL_CHANGE",
    "tickerchangeto": "SYMBOL_CHANGE",
    "delisted": "DELISTING",
    "regulatorydelisting": "DELISTING",
    "voluntarydelisting": "DELISTING",
    "bankruptcyliquidation": "DELISTING",
}
# Only these carry a per-share value in the model. For the others Sharadar's `value` is something
# else (an acquisition's deal size, say), so it is kept in `details`, not in `value`.
VALUED_TYPES = ("SPLIT", "DIVIDEND")

Key = tuple[int, str, date, Decimal | None]


@dataclass
class ActionResult:
    data_version: str
    inserted: int = 0
    already_present: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    unmatched_tickers: Counter[str] = field(default_factory=Counter)
    by_type: Counter[str] = field(default_factory=Counter)
    needs_review: list[str] = field(default_factory=list)

    def summary(self) -> str:
        skipped = ", ".join(f"{k}: {v}" for k, v in sorted(self.skipped.items())) or "none"
        types = ", ".join(f"{k}: {v}" for k, v in sorted(self.by_type.items())) or "none"
        return (
            f"Data version {self.data_version}\n"
            f"  inserted {self.inserted}, already present {self.already_present}\n"
            f"  new and existing actions by type: {types}\n"
            f"  skipped rows ({sum(self.skipped.values())}): {skipped}\n"
            f"  needs review: {len(self.needs_review)}"
        )


def parse_action(
    row: dict[str, str], directory: TickerDirectory, data_version: str
) -> tuple[CorporateAction | None, str | None]:
    """Turn one source row into a corporate action, or say why it cannot be used."""
    code = (blank_to_none(row.get("action")) or "").lower()
    ticker = blank_to_none(row.get("ticker"))
    if not (code and ticker):
        return None, "missing_key_field"
    action_type = ACTION_TYPE_BY_CODE.get(code)
    if action_type is None:
        return None, f"unmapped_action:{code}"
    try:
        effective = parse_date(row.get("date"))
        raw_value = parse_decimal(row.get("value"))
    except ValueError:
        return None, "bad_value"
    if effective is None:
        return None, "missing_date"
    security_id = directory.resolve(ticker, effective)
    if security_id is None:
        return None, "no_security_for_ticker_on_date"
    contra_ticker = blank_to_none(row.get("contraticker"))
    details: dict[str, Any] = {"sharadar_action": code}
    if contra_ticker:
        details["contra_ticker"] = normalize_ticker(contra_ticker)
    if contra_name := blank_to_none(row.get("contraname")):
        details["contra_name"] = contra_name
    if action_type not in VALUED_TYPES and raw_value is not None:
        details["sharadar_value"] = str(raw_value)
    try:
        return (
            CorporateAction(
                security_id=security_id,
                action_type=action_type,
                effective_date=effective,
                value=raw_value if action_type in VALUED_TYPES else None,
                related_security_id=(
                    directory.resolve(contra_ticker, effective) if contra_ticker else None
                ),
                details=details,
                source=SOURCE,
                data_version=data_version,
            ),
            None,
        )
    except ValidationError:
        return None, "bad_value"


def import_actions(
    conn: Connection,
    download: Download,
    directory: TickerDirectory | None = None,
    actor: str = ACTOR,
) -> ActionResult:
    """Add the download's actions that are not stored yet, and audit the run.

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
    result = ActionResult(data_version=version)
    directory = directory or TickerDirectory.load(conn)
    a = corporate_action_table.c
    stored: set[Key] = {
        (r.security_id, r.action_type, r.effective_date, r.value)
        for r in conn.execute(
            select(a.security_id, a.action_type, a.effective_date, a.value).where(
                a.source == SOURCE
            )
        )
    }

    fresh: dict[Key, CorporateAction] = {}
    for source_row in download.rows:
        action, reason = parse_action(source_row, directory, version)
        if action is None:
            result.skipped[reason or "unusable_row"] += 1
            if reason == "no_security_for_ticker_on_date":
                result.unmatched_tickers[normalize_ticker(source_row["ticker"])] += 1
            continue
        key = (action.security_id, action.action_type, action.effective_date, action.value)
        result.by_type[action.action_type] += 1
        if key in stored:
            result.already_present += 1
        elif key in fresh:
            result.skipped["same_event_twice_in_download"] += 1
        else:
            fresh[key] = action
    if fresh:
        conn.execute(corporate_action_table.insert(), [x.model_dump() for x in fresh.values()])
    result.inserted = len(fresh)
    if result.unmatched_tickers:
        common = ", ".join(t for t, _ in result.unmatched_tickers.most_common(5))
        result.needs_review.append(
            f"{sum(result.unmatched_tickers.values())} actions had no security for their ticker "
            f"on their date (most common: {common})"
        )

    record_event(
        conn,
        AuditEvent(
            actor=actor,
            action="import.completed",
            entity_type="import:sharadar.actions",
            entity_id=version,
            reason="Sharadar corporate actions import",
            source=__name__,
            details={
                "data_version": version,
                "sha256": download.sha256,
                "downloaded_at": download.downloaded_at.isoformat(),
                "rows_downloaded": len(download.rows),
                "inserted": result.inserted,
                "already_present": result.already_present,
                "skipped": dict(result.skipped),
                "by_type": dict(result.by_type),
                "needs_review_count": len(result.needs_review),
                "needs_review": result.needs_review[:MAX_REVIEW_ITEMS_IN_AUDIT],
            },
        ),
    )
    return result


def main() -> int:
    return run_import(
        "actions",
        REQUIRED_COLUMNS,
        lambda conn, download: import_actions(conn, download),
    )


if __name__ == "__main__":
    sys.exit(main())
