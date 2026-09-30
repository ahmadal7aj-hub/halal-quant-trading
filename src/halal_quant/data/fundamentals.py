"""Point-in-time fundamentals and daily market value (task 11; PRD §19, BRD §9, S1 §3).

Only the "as reported" Sharadar dimensions are stored: ARQ (quarterly), ARY (annual) and ART
(trailing twelve months). The "most recent" dimensions are never stored because they include
later restatements that were not public at the time (look-ahead).

A filing is keyed by the day it was filed (`filing_date`, Sharadar's `date` column, G8 OI-1) and
is invisible to any query dated on or before that day: `latest_filing_before` only ever returns
filings filed strictly before the screening date.

Amounts are in US dollars as delivered in SF1. The daily table delivers market value in USD
millions; it is converted to USD on import (G8 OI-6) so both tables use one unit.
"""

from datetime import date
from decimal import Decimal
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
    Numeric,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    func,
    select,
)

from halal_quant.db.engine import metadata

AMOUNT = Numeric(24, 4)
DIMENSIONS = ("ARQ", "ARY", "ART")

# Stored column -> Sharadar SF1 column. Order is the order of the table.
AMOUNT_COLUMNS: dict[str, str] = {
    "debt": "debt",
    "debt_current": "debtc",
    "debt_noncurrent": "debtnc",
    "cash_and_equivalents": "cashneq",
    "investments": "investments",
    "investments_current": "investmentsc",
    "investments_noncurrent": "investmentsnc",
    "receivables": "receivables",
    "assets": "assets",
    "revenue": "revenue",
    "interest_expense": "intexp",
    "ebit": "ebit",
    "ebt": "ebt",
    "operating_income": "opinc",
    "net_income": "netinc",
    "market_cap": "marketcap",
    "shares_basic": "sharesbas",
    "shares_weighted_avg": "shareswa",
    "price": "price",
}

fundamental_table = Table(
    "fundamental",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column(
        "security_id",
        BigInteger,
        ForeignKey("security.security_id", name="fk_fundamental_security"),
        nullable=False,
    ),
    Column("dimension", Text, nullable=False),
    Column("calendar_date", Date, nullable=False),  # Sharadar's normalised period end
    Column("filing_date", Date, nullable=False),  # the day the report was filed (Sharadar `date`)
    Column("report_period", Date),  # the company's own period end
    Column("fiscal_period", Text),
    *(Column(name, AMOUNT) for name in AMOUNT_COLUMNS),
    Column("source", Text, nullable=False),
    Column("data_version", Text, nullable=False),
    Column("imported_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint(
        "security_id", "dimension", "calendar_date", "filing_date", name="uq_fundamental_filing"
    ),
    CheckConstraint("dimension IN ('ARQ', 'ARY', 'ART')", name="ck_fundamental_dimension"),
    CheckConstraint("source <> '' AND data_version <> ''", name="ck_fundamental_lineage"),
    Index("ix_fundamental_lookup", "security_id", "dimension", "filing_date"),
)

daily_market_cap_table = Table(
    "daily_market_cap",
    metadata,
    Column(
        "security_id",
        BigInteger,
        ForeignKey("security.security_id", name="fk_daily_market_cap_security"),
        nullable=False,
    ),
    Column("cap_date", Date, nullable=False),
    Column("market_cap_usd", AMOUNT, nullable=False),
    Column("source", Text, nullable=False),
    Column("data_version", Text, nullable=False),
    Column("imported_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("security_id", "cap_date", name="pk_daily_market_cap"),
    CheckConstraint("source <> '' AND data_version <> ''", name="ck_daily_market_cap_lineage"),
    Index("ix_daily_market_cap_cap_date", "cap_date"),
)


def latest_filing_before(
    conn: Connection, security_id: int, dimension: str, screening_date: date
) -> dict[str, Any] | None:
    """The latest filing of `dimension` filed strictly before `screening_date`, or None.

    Filed on `screening_date` itself does not count: a report is never used on the day it is
    filed (S1 §3, PRD §19). When a period was filed more than once, the latest filing wins.
    """
    f = fundamental_table.c
    row = (
        conn.execute(
            select(fundamental_table)
            .where(
                f.security_id == security_id,
                f.dimension == dimension,
                f.filing_date < screening_date,
            )
            .order_by(f.filing_date.desc(), f.calendar_date.desc())
            .limit(1)
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def market_cap_on(conn: Connection, security_id: int, on: date) -> Decimal | None:
    """Market value in USD on exactly `on`, or None if there is no row for that day."""
    m = daily_market_cap_table.c
    return conn.execute(
        select(m.market_cap_usd).where(m.security_id == security_id, m.cap_date == on)
    ).scalar_one_or_none()
