"""Security master (PRD §5, BRD §10 and §18 "inconsistent ticker mappings").

A ticker is never a security's identity: companies change tickers, and old tickers get reused
by other companies. Every security gets a permanent `security_id`, and `security_ticker`
records which ticker it used over which dates (`valid_from` inclusive, `valid_to` exclusive).
The database guarantees a ticker points to at most one security on any date.

The PRD's "current ticker" is not stored twice: it is the ticker whose period is still open.
Lookups that find nothing return None, which callers treat as "not eligible" (rulebook B1).

Every change writes an audit event on the caller's connection, so both commit together.
"""

from collections import defaultdict
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Connection,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Table,
    Text,
    UniqueConstraint,
    and_,
    column,
    false,
    func,
    or_,
    select,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint

from halal_quant.audit import AuditEvent, record_event
from halal_quant.db.engine import metadata

SOURCE = "halal_quant.data.security_master"

security_table = Table(
    "security",
    metadata,
    Column("security_id", BigInteger, Identity(always=True), primary_key=True),
    Column("source", Text, nullable=False),  # data provider, e.g. "sharadar"
    Column("source_id", Text, nullable=False),  # provider's permanent ID, e.g. permaticker
    Column("company_name", Text, nullable=False),
    Column("exchange", Text),
    Column("currency", Text),
    Column("country", Text),
    Column("sector", Text),
    Column("industry", Text),
    Column("category", Text),  # provider's security type, e.g. "Domestic Common Stock"
    Column("sic_code", Text),  # SEC industry code (current only: G8 OI-2)
    # Space-separated tickers of the company's other securities (share classes, units): NULL until
    # imported, "" when the provider lists none (G8 OI-19).
    Column("related_tickers", Text),
    Column("isin", Text),
    Column("cusip", Text),
    Column("start_date", Date),
    Column("end_date", Date),  # last trading day, once delisted
    Column("delisted_flag", Boolean, nullable=False, server_default=false()),
    Column("active_flag", Boolean, nullable=False, server_default=true()),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("source", "source_id", name="uq_security_source_id"),
    CheckConstraint("source <> '' AND source_id <> ''", name="ck_security_source"),
    CheckConstraint("company_name <> ''", name="ck_security_company_name"),
    CheckConstraint("end_date >= start_date", name="ck_security_lifecycle_dates"),
    CheckConstraint("NOT (delisted_flag AND active_flag)", name="ck_security_delisted_not_active"),
    CheckConstraint(
        "NOT delisted_flag OR end_date IS NOT NULL", name="ck_security_delisted_has_end"
    ),
)

security_ticker_table = Table(
    "security_ticker",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column(
        "security_id",
        BigInteger,
        ForeignKey("security.security_id", name="fk_security_ticker_security"),
        nullable=False,
    ),
    Column("ticker", Text, nullable=False),
    Column("valid_from", Date, nullable=False),
    Column("valid_to", Date),  # exclusive; NULL = still current
    CheckConstraint(
        "ticker <> '' AND ticker = upper(btrim(ticker))", name="ck_security_ticker_ticker"
    ),
    CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="ck_security_ticker_period"),
    ExcludeConstraint(
        (column("ticker"), "="),
        (text("daterange(valid_from, valid_to)"), "&&"),
        using="gist",
        name="ex_security_ticker_one_security_per_ticker",
    ),
    ExcludeConstraint(
        (column("security_id"), "="),
        (text("daterange(valid_from, valid_to)"), "&&"),
        using="gist",
        name="ex_security_ticker_one_ticker_per_security",
    ),
)


class SecurityMasterError(Exception):
    """A security master change was refused; nothing was written."""


class SecurityInfo(BaseModel):
    """Descriptive fields for a new security. ISIN/CUSIP only when licensed (PRD §5)."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    source: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    company_name: str = Field(min_length=1)
    exchange: str | None = None
    currency: str | None = None
    country: str | None = None
    sector: str | None = None
    industry: str | None = None
    category: str | None = None
    sic_code: str | None = None
    related_tickers: str | None = None
    isin: str | None = None
    cusip: str | None = None


def normalize_ticker(ticker: str) -> str:
    """Tickers are stored upper-case without surrounding spaces."""
    normalized = ticker.strip().upper()
    if not normalized:
        raise SecurityMasterError("A ticker cannot be empty.")
    return normalized


def _active_on(on: date) -> Any:
    t = security_ticker_table.c
    return and_(t.valid_from <= on, or_(t.valid_to.is_(None), t.valid_to > on))


def resolve_ticker(conn: Connection, ticker: str, on: date) -> int | None:
    """The security that traded as `ticker` on date `on`, or None if no security did."""
    t = security_ticker_table.c
    return conn.execute(
        select(t.security_id).where(t.ticker == normalize_ticker(ticker), _active_on(on))
    ).scalar_one_or_none()


def ticker_on(conn: Connection, security_id: int, on: date) -> str | None:
    """The ticker a security traded under on date `on`, or None (not listed then)."""
    t = security_ticker_table.c
    return conn.execute(
        select(t.ticker).where(t.security_id == security_id, _active_on(on))
    ).scalar_one_or_none()


class TickerDirectory:
    """Every ticker period held in memory, for resolving millions of provider rows quickly.

    Answers the same question as `resolve_ticker`, without a database query per row. It is a
    snapshot: build a fresh one after the security master changes.
    """

    def __init__(self, periods: Iterable[tuple[str, int, date, date | None]]) -> None:
        self._periods: dict[str, list[tuple[date, date | None, int]]] = defaultdict(list)
        for ticker, security_id, valid_from, valid_to in periods:
            self._periods[ticker].append((valid_from, valid_to, security_id))

    @classmethod
    def load(cls, conn: Connection) -> "TickerDirectory":
        t = security_ticker_table.c
        rows = conn.execute(select(t.ticker, t.security_id, t.valid_from, t.valid_to))
        return cls((r.ticker, r.security_id, r.valid_from, r.valid_to) for r in rows)

    def resolve(self, ticker: str, on: date) -> int | None:
        for valid_from, valid_to, security_id in self._periods.get(normalize_ticker(ticker), ()):
            if valid_from <= on and (valid_to is None or on < valid_to):
                return security_id
        return None


def _ensure_ticker_free(conn: Connection, ticker: str, from_date: date) -> None:
    """Refuse a ticker that another security holds on or after `from_date`.

    The exclusion constraint is the real guarantee; this check gives a plain-English error.
    """
    t = security_ticker_table.c
    holder = conn.execute(
        select(t.security_id)
        .where(t.ticker == ticker, or_(t.valid_to.is_(None), t.valid_to > from_date))
        .limit(1)
    ).scalar_one_or_none()
    if holder is not None:
        raise SecurityMasterError(
            f"Ticker {ticker} is already used by security {holder} on or after {from_date}."
        )


def create_security(
    conn: Connection, info: SecurityInfo, ticker: str, listed_on: date, actor: str, reason: str
) -> int:
    """Add a security with its first ticker, starting on `listed_on`. Returns its security_id."""
    ticker = normalize_ticker(ticker)
    _ensure_ticker_free(conn, ticker, listed_on)
    security_id: int = conn.execute(
        security_table.insert()
        .values(**info.model_dump(), start_date=listed_on)
        .returning(security_table.c.security_id)
    ).scalar_one()
    conn.execute(
        security_ticker_table.insert().values(
            security_id=security_id, ticker=ticker, valid_from=listed_on
        )
    )
    record_event(
        conn,
        AuditEvent(
            actor=actor,
            action="security.created",
            entity_type="security",
            entity_id=str(security_id),
            new_value={**info.model_dump(), "ticker": ticker, "start_date": listed_on},
            reason=reason,
            source=SOURCE,
        ),
    )
    return security_id


def _open_ticker(conn: Connection, security_id: int) -> Any:
    t = security_ticker_table.c
    row = conn.execute(
        select(t.id, t.ticker, t.valid_from)
        .where(t.security_id == security_id, t.valid_to.is_(None))
        .with_for_update()
    ).one_or_none()
    if row is None:
        raise SecurityMasterError(
            f"Security {security_id} has no current ticker (unknown or delisted)."
        )
    return row


def change_ticker(
    conn: Connection, security_id: int, new_ticker: str, effective: date, actor: str, reason: str
) -> None:
    """From `effective` on, the security trades as `new_ticker`; its security_id stays the same."""
    new_ticker = normalize_ticker(new_ticker)
    current = _open_ticker(conn, security_id)
    if new_ticker == current.ticker:
        raise SecurityMasterError(f"Security {security_id} already trades as {new_ticker}.")
    if effective <= current.valid_from:
        raise SecurityMasterError(
            f"A ticker change on {effective} must come after the current ticker's start "
            f"({current.valid_from})."
        )
    _ensure_ticker_free(conn, new_ticker, effective)
    t = security_ticker_table
    conn.execute(t.update().where(t.c.id == current.id).values(valid_to=effective))
    conn.execute(
        t.insert().values(security_id=security_id, ticker=new_ticker, valid_from=effective)
    )
    record_event(
        conn,
        AuditEvent(
            actor=actor,
            action="security.ticker_changed",
            entity_type="security",
            entity_id=str(security_id),
            old_value={"ticker": current.ticker},
            new_value={"ticker": new_ticker, "effective": effective},
            reason=reason,
            source=SOURCE,
        ),
    )


def mark_delisted(
    conn: Connection, security_id: int, last_trading_day: date, actor: str, reason: str
) -> None:
    """Record that the security stopped trading after `last_trading_day`.

    Its ticker is released from the next day, so another company may reuse it later.
    """
    current = _open_ticker(conn, security_id)
    if last_trading_day < current.valid_from:
        raise SecurityMasterError(
            f"Last trading day {last_trading_day} is before the current ticker's start "
            f"({current.valid_from})."
        )
    t = security_ticker_table
    conn.execute(
        t.update().where(t.c.id == current.id).values(valid_to=last_trading_day + timedelta(1))
    )
    s = security_table
    conn.execute(
        s.update()
        .where(s.c.security_id == security_id)
        .values(
            end_date=last_trading_day,
            delisted_flag=True,
            active_flag=False,
            updated_at=func.now(),
        )
    )
    record_event(
        conn,
        AuditEvent(
            actor=actor,
            action="security.delisted",
            entity_type="security",
            entity_id=str(security_id),
            old_value={"active_flag": True, "ticker": current.ticker},
            new_value={"active_flag": False, "end_date": last_trading_day},
            reason=reason,
            source=SOURCE,
        ),
    )
