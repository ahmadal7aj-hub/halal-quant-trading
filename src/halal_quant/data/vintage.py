"""A price vintage: one consistent download of every price series (Phase 2; ADR-003; G8 OI-16).

Sharadar adjusts old prices for every later split and dividend, so two downloads made at different
times differ slightly (about 1% of stock-months a few hours apart). A backtest must therefore read
**one** vintage, downloaded in one run, and never mix vintages. A vintage is registered when its
download starts (status `building`) and marked `complete` when it ends; after that no row can be
added to it. Rows are add-only for the application role.

`vintage_price` holds what returns and costs need for every stock (adjusted close for returns,
the price actually paid for share counts, volume for liquidity); `vintage_benchmark_price` holds
the same for the benchmark funds (SPUS, HLAL, SPY, IVV).
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Connection,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    PrimaryKeyConstraint,
    Table,
    Text,
    func,
    select,
    update,
)

from halal_quant.db.engine import metadata

PRICE = Numeric(30, 6)
BUILDING, COMPLETE = "building", "complete"

price_vintage_table = Table(
    "price_vintage",
    metadata,
    Column("vintage_id", Text, primary_key=True),
    Column("description", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("finished_at", DateTime(timezone=True)),
    Column("stock_rows", BigInteger),
    Column("benchmark_rows", BigInteger),
    CheckConstraint("status IN ('building', 'complete')", name="ck_price_vintage_status"),
    CheckConstraint("vintage_id <> ''", name="ck_price_vintage_id"),
)

vintage_price_table = Table(
    "vintage_price",
    metadata,
    Column(
        "vintage_id",
        Text,
        ForeignKey("price_vintage.vintage_id", name="fk_vintage_price_vintage"),
        nullable=False,
    ),
    Column(
        "security_id",
        BigInteger,
        ForeignKey("security.security_id", name="fk_vintage_price_security"),
        nullable=False,
    ),
    Column("price_date", Date, nullable=False),
    Column("close_unadjusted", PRICE, nullable=False),  # the price actually paid on the day
    Column("adjusted_close", PRICE, nullable=False),  # adjusted for splits and dividends
    Column("volume", BigInteger, nullable=False),
    PrimaryKeyConstraint("vintage_id", "security_id", "price_date", name="pk_vintage_price"),
    CheckConstraint(
        "close_unadjusted > 0 AND adjusted_close > 0", name="ck_vintage_price_positive"
    ),
    CheckConstraint("volume >= 0", name="ck_vintage_price_volume"),
    Index("ix_vintage_price_date", "vintage_id", "price_date"),
)

vintage_benchmark_price_table = Table(
    "vintage_benchmark_price",
    metadata,
    Column(
        "vintage_id",
        Text,
        ForeignKey("price_vintage.vintage_id", name="fk_vintage_benchmark_vintage"),
        nullable=False,
    ),
    Column("symbol", Text, nullable=False),
    Column("price_date", Date, nullable=False),
    Column("close_unadjusted", PRICE, nullable=False),
    Column("adjusted_close", PRICE, nullable=False),
    Column("volume", BigInteger, nullable=False),
    PrimaryKeyConstraint("vintage_id", "symbol", "price_date", name="pk_vintage_benchmark_price"),
    CheckConstraint(
        "close_unadjusted > 0 AND adjusted_close > 0", name="ck_vintage_benchmark_positive"
    ),
)


class VintageError(Exception):
    """A vintage cannot be used as asked (unknown, or already complete)."""


def start_vintage(conn: Connection, vintage_id: str, description: str) -> bool:
    """Register a vintage, or accept a resume of one still being built. True if it is new.

    A vintage that is already complete is never reopened.
    """
    v = price_vintage_table.c
    status = conn.execute(select(v.status).where(v.vintage_id == vintage_id)).scalar_one_or_none()
    if status == COMPLETE:
        raise VintageError(f"Vintage {vintage_id!r} is complete: no row can be added to it.")
    if status is None:
        conn.execute(
            price_vintage_table.insert().values(
                vintage_id=vintage_id, description=description, status=BUILDING
            )
        )
        return True
    return False


def require_building(conn: Connection, vintage_id: str) -> None:
    v = price_vintage_table.c
    status = conn.execute(select(v.status).where(v.vintage_id == vintage_id)).scalar_one_or_none()
    if status != BUILDING:
        raise VintageError(f"Vintage {vintage_id!r} is not being built (status {status!r}).")


def finish_vintage(conn: Connection, vintage_id: str, finished_at: datetime | None = None) -> None:
    """Close a vintage: count its rows and mark it complete (only once)."""
    require_building(conn, vintage_id)
    vp, vb = vintage_price_table.c, vintage_benchmark_price_table.c
    stocks = conn.execute(select(func.count()).where(vp.vintage_id == vintage_id)).scalar_one()
    funds = conn.execute(select(func.count()).where(vb.vintage_id == vintage_id)).scalar_one()
    v = price_vintage_table.c
    conn.execute(
        update(price_vintage_table)
        .where(v.vintage_id == vintage_id)
        .values(
            status=COMPLETE,
            finished_at=finished_at or func.now(),
            stock_rows=stocks,
            benchmark_rows=funds,
        )
    )


def vintage_status(conn: Connection, vintage_id: str) -> str | None:
    v = price_vintage_table.c
    return conn.execute(select(v.status).where(v.vintage_id == vintage_id)).scalar_one_or_none()


def adjusted_close_on(
    conn: Connection, vintage_id: str, security_id: int, on: date
) -> Decimal | None:
    """The adjusted close of one security on one day in one vintage, or None."""
    p = vintage_price_table.c
    return conn.execute(
        select(p.adjusted_close).where(
            p.vintage_id == vintage_id, p.security_id == security_id, p.price_date == on
        )
    ).scalar_one_or_none()
