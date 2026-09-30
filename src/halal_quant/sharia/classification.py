"""Classification records and the batch that creates them (task 13; PRD §6, §19; S1 §7).

    uv run python -m halal_quant.sharia.classification [--start 1998-12] [--end 2026-09]

For each screening date (the last trading day of a month) every security that was trading on it
is screened using only what was public before that date: the latest as-reported filings filed
strictly before the date and the market value on the date. The result is stored once, as a
record that applies from the next trading day through the next screening date.

Records are only ever added (the app role cannot change or delete them). A new methodology
version adds records under its own name and rewrites nothing. The status for a date is looked
up with `classification_on`, which never returns a record that only takes effect later (PRD §19,
critical Test 10).
"""

import argparse
import sys
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from itertools import batched
from pathlib import Path
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Connection,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Table,
    Text,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB, distinct_on

from halal_quant.audit import AuditEvent, record_event
from halal_quant.core.config import (
    ManualListConfig,
    ShariaConfig,
    load_config,
    register_config,
)
from halal_quant.core.logging import configure_logging, correlation_scope
from halal_quant.core.settings import DbRole, get_settings
from halal_quant.data.calendar import next_trading_day, previous_trading_day
from halal_quant.data.fundamentals import daily_market_cap_table, fundamental_table
from halal_quant.data.security_master import security_table
from halal_quant.db.engine import make_engine, metadata
from halal_quant.sharia.screening import (
    Filing,
    ManualEntry,
    ScreeningInputs,
    Status,
    choose_balance_sheet,
    choose_income_report,
    describe_sources,
    screen,
)

PROVIDER = "internal_screen"
ACTOR = "system:sharia-screening"
INSERT_BATCH = 2000
# Only US common stock is traded in V1 (owner decision, G8 OI-14, 30 Sep 2026).
COMMON_STOCK_CATEGORIES = (
    "Domestic Common Stock",
    "Domestic Common Stock Primary Class",
    "Domestic Common Stock Secondary Class",
)
FILING_FIELDS = (
    "debt",
    "cash_and_equivalents",
    "investments",
    "investments_current",
    "investments_noncurrent",
    "revenue",
    "ebit",
    "operating_income",
)

classification_table = Table(
    "classification",
    metadata,
    Column("classification_id", BigInteger, Identity(always=True), primary_key=True),
    Column(
        "security_id",
        BigInteger,
        ForeignKey("security.security_id", name="fk_classification_security"),
        nullable=False,
    ),
    Column("provider", Text, nullable=False),  # "internal_screen", or an external provider later
    Column("methodology", Text, nullable=False),  # e.g. "AAOIFI-v1"
    Column("status", Text, nullable=False),
    Column("effective_from", Date, nullable=False),  # first trading day the result applies
    Column("effective_to", Date, nullable=False),  # last day it applies: the next screening date
    Column("screening_date", Date, nullable=False),
    Column("reason", Text, nullable=False),  # which rule decided it and with which numbers
    Column("details", JSONB, nullable=False),  # the ratios and problems, machine-readable
    Column("source_reference", JSONB, nullable=False),  # permaticker, filings used, config
    Column("imported_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint(
        "security_id", "provider", "methodology", "screening_date", name="uq_classification_screen"
    ),
    CheckConstraint(
        "status IN ('HALAL', 'NON_HALAL', 'UNKNOWN', 'PENDING_REVIEW')",
        name="ck_classification_status",
    ),
    CheckConstraint("effective_from > screening_date", name="ck_classification_after_screening"),
    CheckConstraint("effective_to >= effective_from", name="ck_classification_period"),
    Index("ix_classification_lookup", "security_id", "methodology", "effective_from"),
)


@dataclass
class ClassificationSummary:
    screening_date: date
    inserted: int = 0
    already_present: int = 0
    by_status: Counter[str] = field(default_factory=Counter)

    def line(self) -> str:
        statuses = ", ".join(f"{k}: {v}" for k, v in sorted(self.by_status.items())) or "none"
        return (
            f"{self.screening_date}: inserted {self.inserted}, already present "
            f"{self.already_present}; {statuses}"
        )


def last_trading_day_of_month(year: int, month: int) -> date:
    """The last day of the month on which the NYSE traded."""
    return previous_trading_day(date(year + month // 12, month % 12 + 1, 1))


def screening_dates(first: date, last: date) -> Iterator[date]:
    """The last trading day of every month from `first`'s month to `last`'s month."""
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        yield last_trading_day_of_month(year, month)
        year, month = year + month // 12, month % 12 + 1


def effective_period(screening_date: date) -> tuple[date, date]:
    """A result applies from the next trading day through the next screening date."""
    year, month = screening_date.year + screening_date.month // 12, screening_date.month % 12 + 1
    return next_trading_day(screening_date), last_trading_day_of_month(year, month)


def load_manual_list(path: Path) -> tuple[dict[str, ManualEntry], str]:
    """The manual list as {provider id: entry}, and its version."""
    loaded = load_config(path, ManualListConfig)
    entries = {
        e.source_id: ManualEntry(
            Status(e.status), f"{e.reason} (approved by {e.approved_by} on {e.approved_on})"
        )
        for e in loaded.config.entries
    }
    return entries, loaded.ref.version


def _filing(row: Any) -> Filing:
    return Filing(
        dimension=row["dimension"],
        filing_date=row["filing_date"],
        period_end=row["report_period"] or row["calendar_date"],
        **{name: row[name] for name in FILING_FIELDS},
    )


def _latest_filings(
    conn: Connection, dimension: str, on: date, security_ids: Sequence[int] | None
) -> dict[int, Filing]:
    """The latest `dimension` filing filed strictly before `on`, per security (no look-ahead)."""
    f = fundamental_table.c
    query = (
        select(fundamental_table)
        .ext(distinct_on(f.security_id))
        .where(f.dimension == dimension, f.filing_date < on)
        .order_by(f.security_id, f.filing_date.desc(), f.calendar_date.desc())
    )
    if security_ids is not None:
        query = query.where(f.security_id.in_(security_ids))
    return {r["security_id"]: _filing(r) for r in conn.execute(query).mappings()}


def _sic(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def classify_date(
    conn: Connection,
    screening_date: date,
    config: ShariaConfig,
    manual: dict[str, ManualEntry] | None = None,
    categories: Sequence[str] | None = COMMON_STOCK_CATEGORIES,
    security_ids: Sequence[int] | None = None,
    actor: str = ACTOR,
) -> ClassificationSummary:
    """Screen every security trading on `screening_date` and store the results not stored yet.

    Runs on the caller's connection: the caller commits, or rolls everything back.
    """
    manual = manual or {}
    summary = ClassificationSummary(screening_date)
    s = security_table.c
    wanted = select(s.security_id, s.source_id, s.industry, s.sic_code).where(
        (s.start_date.is_(None)) | (s.start_date <= screening_date),
        (s.end_date.is_(None)) | (s.end_date >= screening_date),
    )
    if categories is not None:
        wanted = wanted.where(s.category.in_(categories))
    if security_ids is not None:
        wanted = wanted.where(s.security_id.in_(security_ids))
    securities = conn.execute(wanted).all()

    ids = security_ids
    quarterly = _latest_filings(conn, "ARQ", screening_date, ids)
    annual = _latest_filings(conn, "ARY", screening_date, ids)
    trailing = _latest_filings(conn, "ART", screening_date, ids)
    m = daily_market_cap_table.c
    market_values: dict[int, Decimal] = {
        r.security_id: r.market_cap_usd
        for r in conn.execute(
            select(m.security_id, m.market_cap_usd).where(m.cap_date == screening_date)
        )
    }
    c = classification_table.c
    stored = {
        r.security_id
        for r in conn.execute(
            select(c.security_id).where(
                c.provider == PROVIDER,
                c.methodology == config.version,
                c.screening_date == screening_date,
            )
        )
    }
    effective_from, effective_to = effective_period(screening_date)
    new_rows: list[dict[str, object]] = []
    for security in securities:
        if security.security_id in stored:
            summary.already_present += 1
            continue
        balance = choose_balance_sheet(
            quarterly.get(security.security_id), annual.get(security.security_id)
        )
        income = choose_income_report(
            trailing.get(security.security_id), annual.get(security.security_id)
        )
        result = screen(
            ScreeningInputs(
                screening_date=screening_date,
                industry=security.industry,
                sic_code=_sic(security.sic_code),
                market_value=market_values.get(security.security_id),
                balance_sheet=balance,
                income=income,
                manual=manual.get(security.source_id),
            ),
            config,
        )
        summary.by_status[result.status.value] += 1
        new_rows.append(
            {
                "security_id": security.security_id,
                "provider": PROVIDER,
                "methodology": config.version,
                "status": result.status.value,
                "effective_from": effective_from,
                "effective_to": effective_to,
                "screening_date": screening_date,
                "reason": result.reason,
                "details": result.details,
                "source_reference": {
                    "source_id": security.source_id,
                    "filings": describe_sources([balance, income]),
                },
            }
        )
    for batch in batched(new_rows, INSERT_BATCH):
        conn.execute(classification_table.insert(), list(batch))
    summary.inserted = len(new_rows)

    record_event(
        conn,
        AuditEvent(
            actor=actor,
            action="classification.run",
            entity_type="classification",
            entity_id=f"{config.version}:{screening_date}",
            reason="Sharia screening run",
            source=__name__,
            details={
                "methodology": config.version,
                "screening_date": screening_date,
                "inserted": summary.inserted,
                "already_present": summary.already_present,
                "by_status": dict(summary.by_status),
            },
        ),
    )
    return summary


def classification_on(
    conn: Connection,
    security_id: int,
    on: date,
    methodology: str,
    provider: str = PROVIDER,
) -> dict[str, Any] | None:
    """The record that applies to `security_id` on `on`, or None.

    Only records already in effect on `on` count: a result computed on a screening date applies
    from the next trading day, so it is never used on or before the day it was computed.
    """
    c = classification_table.c
    row = (
        conn.execute(
            select(classification_table)
            .where(
                c.security_id == security_id,
                c.provider == provider,
                c.methodology == methodology,
                c.effective_from <= on,
                c.effective_to >= on,
            )
            .order_by(c.screening_date.desc())
            .limit(1)
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def _month(text: str) -> date:
    try:
        return date.fromisoformat(f"{text}-01")
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a month like 2019-03") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Screen securities month by month (AAOIFI-v1).")
    parser.add_argument("--start", type=_month, default=date(1998, 12, 1), help="first month")
    parser.add_argument("--end", type=_month, default=date.today(), help="last month")
    parser.add_argument("--config", type=Path, default=Path("config/sharia/aaoifi_v1.yaml"))
    parser.add_argument("--manual-list", type=Path, default=Path("config/sharia/manual_list.yaml"))
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings, level=settings.log_level)
    loaded = load_config(args.config, ShariaConfig)
    manual, manual_version = load_manual_list(args.manual_list)
    engine = make_engine(settings, DbRole.APP)
    with correlation_scope():
        with engine.begin() as conn:
            register_config(conn, loaded, ACTOR, "Sharia methodology used by the screening run")
        print(
            f"Methodology {loaded.ref.version}; manual list {manual_version}, {len(manual)} entries"
        )
        for day in screening_dates(args.start, args.end):
            with engine.begin() as conn:
                print(classify_date(conn, day, loaded.config, manual).line())
    return 0


if __name__ == "__main__":
    sys.exit(main())
