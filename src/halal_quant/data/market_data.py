"""Market data model: daily prices and corporate actions (PRD §7, BRD §10).

Prices are keyed by (security_id, trading date). Trading dates are US Eastern dates, stored as
dates; `imported_at` is a UTC timestamp. Every row records its `source` and the `data_version`
it came from, so a result can be traced back to the exact data (BRD §6.4).

`open`, `high`, `low`, `close` and `volume` are as delivered by the provider: Sharadar adjusts them
for splits (as of the latest split it knows of), so they are comparable across time.
`close_unadjusted` is the price actually paid on the day and `adjusted_close` is also adjusted for
dividends. Stored rows are never changed by the app role; see migration 0008.

The database rejects duplicate rows and non-positive prices or negative volume. Prices that are
merely suspicious (high below low, big jumps, gaps) are stored and flagged by the data-quality
checks (task 12), so bad source data is reported rather than silently refused or hidden.

Corporate actions are separate from prices (PRD §7). `value` means:
- SPLIT: new shares per old share (2 = a 2-for-1 split, 0.1 = a 1-for-10 reverse split);
- DIVIDEND: cash per share in the security's currency;
- MERGER: cash per share paid, if any (NULL for a share-for-share deal);
- SYMBOL_CHANGE and DELISTING: NULL, with the details (old and new ticker, delisting reason,
  acquirer) in `details`. `related_security_id` names the acquirer or successor if known.
"""

from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
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
from sqlalchemy.dialects.postgresql import JSONB

from halal_quant.db.engine import metadata

# Wide on purpose: split-adjusted history of a company that reverse-split many times reaches ~1e14.
PRICE = Numeric(30, 6)
ACTION_VALUE = Numeric(19, 8)
ACTION_TYPES = ("SPLIT", "DIVIDEND", "MERGER", "SYMBOL_CHANGE", "DELISTING")

daily_price_table = Table(
    "daily_price",
    metadata,
    Column(
        "security_id",
        BigInteger,
        ForeignKey("security.security_id", name="fk_daily_price_security"),
        nullable=False,
    ),
    Column("price_date", Date, nullable=False),
    Column("open", PRICE, nullable=False),
    Column("high", PRICE, nullable=False),
    Column("low", PRICE, nullable=False),
    Column("close", PRICE, nullable=False),  # split-adjusted, like open/high/low
    Column("adjusted_close", PRICE, nullable=False),  # adjusted for splits and dividends
    Column("close_unadjusted", PRICE, nullable=False),  # the price actually paid on the day
    Column("volume", BigInteger, nullable=False),
    Column("source", Text, nullable=False),
    Column("data_version", Text, nullable=False),
    Column("imported_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("security_id", "price_date", name="pk_daily_price"),
    CheckConstraint("open > 0 AND high > 0 AND low > 0 AND close > 0", name="ck_daily_price_ohlc"),
    CheckConstraint("adjusted_close > 0", name="ck_daily_price_adjusted_close"),
    CheckConstraint("close_unadjusted > 0", name="ck_daily_price_close_unadjusted"),
    CheckConstraint("volume >= 0", name="ck_daily_price_volume"),
    CheckConstraint("source <> '' AND data_version <> ''", name="ck_daily_price_lineage"),
    Index("ix_daily_price_price_date", "price_date"),  # "everything traded on this date"
)

corporate_action_table = Table(
    "corporate_action",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column(
        "security_id",
        BigInteger,
        ForeignKey("security.security_id", name="fk_corporate_action_security"),
        nullable=False,
    ),
    Column("action_type", Text, nullable=False),
    Column("effective_date", Date, nullable=False),
    Column("value", ACTION_VALUE),
    Column(
        "related_security_id",
        BigInteger,
        ForeignKey("security.security_id", name="fk_corporate_action_related_security"),
    ),
    Column("details", JSONB),
    Column("source", Text, nullable=False),
    Column("data_version", Text, nullable=False),
    Column("imported_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "action_type IN ('SPLIT', 'DIVIDEND', 'MERGER', 'SYMBOL_CHANGE', 'DELISTING')",
        name="ck_corporate_action_type",
    ),
    CheckConstraint(
        "action_type NOT IN ('SPLIT', 'DIVIDEND') OR (value IS NOT NULL AND value > 0)",
        name="ck_corporate_action_value",
    ),
    CheckConstraint("value IS NULL OR value >= 0", name="ck_corporate_action_value_sign"),
    CheckConstraint("source <> '' AND data_version <> ''", name="ck_corporate_action_lineage"),
    # The same action, on the same day, with the same value, is one action (NULLs compare equal).
    UniqueConstraint(
        "security_id",
        "action_type",
        "effective_date",
        "value",
        name="uq_corporate_action_event",
        postgresql_nulls_not_distinct=True,
    ),
    Index("ix_corporate_action_security_date", "security_id", "effective_date"),
)

ActionType = Literal["SPLIT", "DIVIDEND", "MERGER", "SYMBOL_CHANGE", "DELISTING"]


class DailyPrice(BaseModel):
    """One security's prices for one trading day."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    security_id: int
    price_date: date
    open: Decimal = Field(gt=0, allow_inf_nan=False)
    high: Decimal = Field(gt=0, allow_inf_nan=False)
    low: Decimal = Field(gt=0, allow_inf_nan=False)
    close: Decimal = Field(gt=0, allow_inf_nan=False)
    adjusted_close: Decimal = Field(gt=0, allow_inf_nan=False)
    close_unadjusted: Decimal = Field(gt=0, allow_inf_nan=False)
    volume: int = Field(ge=0)
    source: str = Field(min_length=1)
    data_version: str = Field(min_length=1)


class CorporateAction(BaseModel):
    """One corporate action. See the module docstring for what `value` means per type."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    security_id: int
    action_type: ActionType
    effective_date: date
    value: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    related_security_id: int | None = None
    details: dict[str, Any] | None = None
    source: str = Field(min_length=1)
    data_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def _split_and_dividend_have_a_positive_value(self) -> "CorporateAction":
        if self.action_type in ("SPLIT", "DIVIDEND") and not (self.value and self.value > 0):
            raise ValueError(f"A {self.action_type} needs a value above zero.")
        return self


def add_prices(conn: Connection, prices: list[DailyPrice]) -> None:
    """Insert prices. A duplicate (security, date) raises IntegrityError and writes nothing."""
    if prices:
        conn.execute(daily_price_table.insert(), [p.model_dump() for p in prices])


def add_corporate_actions(conn: Connection, actions: list[CorporateAction]) -> None:
    """Insert corporate actions. An exact duplicate raises IntegrityError and writes nothing."""
    if actions:
        conn.execute(corporate_action_table.insert(), [a.model_dump() for a in actions])


def price_history(conn: Connection, security_id: int, start: date, end: date) -> list[DailyPrice]:
    """The stored prices for one security from `start` to `end` inclusive, oldest first."""
    p = daily_price_table.c
    rows = conn.execute(
        select(daily_price_table)
        .where(p.security_id == security_id, p.price_date >= start, p.price_date <= end)
        .order_by(p.price_date)
    ).mappings()
    return [
        DailyPrice.model_validate({k: v for k, v in row.items() if k != "imported_at"})
        for row in rows
    ]
