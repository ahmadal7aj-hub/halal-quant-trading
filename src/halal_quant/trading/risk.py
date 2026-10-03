"""The independent risk engine and pre-trade validation (Phase 4; PRD §12, §13, §37 tests 1-6).

This module knows nothing about strategies. It is given a proposed order, the facts about the
security, and the state of the portfolio, and it answers APPROVED, REJECTED or
REQUIRES_MANUAL_REVIEW with a machine-readable reason code and a plain-English explanation.
Checks run in the order of the PRD chain and the FIRST failure decides, so a critical failure
always prevents the order (PRD §13). Everything is `Decimal` and pure, so every rule is tested
on made-up data.

Selling is never blocked for Sharia or liquidity reasons (getting out of a holding must stay
possible); the kill switch, stale prices, a disconnected broker and the order-size and turnover
limits apply to every order.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from halal_quant.core.config import LoadedConfig, Status, _Strict, load_config

APPROVED = "APPROVED"
REJECTED = "REJECTED"
REQUIRES_MANUAL_REVIEW = "REQUIRES_MANUAL_REVIEW"

# Reason codes (machine readable).
OK = "OK"
KILL_SWITCH = "KILL_SWITCH_ENGAGED"
UNKNOWN_SECURITY = "UNKNOWN_SECURITY"
NOT_HALAL = "SHARIA_NON_HALAL"
SHARIA_UNKNOWN = "SHARIA_UNKNOWN"
FUND_NOT_APPROVED = "FUND_NOT_APPROVED"
STALE_PRICE = "STALE_PRICE"
ILLIQUID = "ILLIQUID"
DRAWDOWN_BREAKER = "PORTFOLIO_DRAWDOWN_LIMIT"
DAILY_LOSS_BREAKER = "DAILY_LOSS_LIMIT"
POSITION_LIMIT = "POSITION_LIMIT"
SECTOR_LIMIT = "SECTOR_LIMIT"
EXPOSURE_LIMIT = "EXPOSURE_LIMIT"
CASH_INSUFFICIENT = "CASH_INSUFFICIENT"
ORDER_TOO_LARGE = "ORDER_TOO_LARGE"
BAD_ORDER = "BAD_ORDER"
TURNOVER_LIMIT = "DAILY_TURNOVER_LIMIT"
BROKER_DISCONNECTED = "BROKER_DISCONNECTED"
MANUAL_REVIEW_SIZE = "MANUAL_REVIEW_ORDER_SIZE"

HUNDRED = Decimal(100)


class RiskLimits(_Strict):
    """Owner-set limits (doc 03 O1). Every value is configuration, none is hard-coded."""

    config_type: Literal["risk_limits"]
    version: str = Field(min_length=1)
    status: Status
    approved_by: str | None = None
    approved_on: date | None = None
    max_position_pct: Decimal = Field(gt=0, le=100)  # one stock, of the portfolio value
    max_fund_position_pct: Decimal = Field(gt=0, le=100)  # one approved fund
    max_sector_pct: Decimal = Field(gt=0, le=100)  # all stocks of one sector
    max_total_exposure_pct: Decimal = Field(gt=0, le=100)  # invested share; the rest stays cash
    max_daily_turnover_pct: Decimal = Field(gt=0, le=200)  # buys plus sells in a day
    max_order_value: Decimal = Field(gt=0)  # one order, in USD
    manual_review_order_value: Decimal = Field(gt=0)  # larger orders need the owner's approval
    max_portfolio_drawdown_pct: Decimal = Field(gt=0, le=100)  # buying stops beyond this fall
    max_daily_loss_pct: Decimal = Field(gt=0, le=100)  # buying stops beyond this day's loss
    min_average_daily_volume_usd: Decimal = Field(ge=0)
    max_price_age_days: int = Field(ge=0)
    approved_funds: list[str]

    @model_validator(mode="after")
    def _consistent(self) -> "RiskLimits":
        if self.manual_review_order_value > self.max_order_value:
            raise ValueError("manual_review_order_value cannot exceed max_order_value")
        if len({f.upper() for f in self.approved_funds}) != len(self.approved_funds):
            raise ValueError("approved_funds must not repeat a symbol")
        return self


def load_risk_limits(path: Path) -> LoadedConfig[RiskLimits]:
    return load_config(path, RiskLimits)


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: Decimal
    price: Decimal  # the reference price used to size the order
    reason: str = ""

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.price


@dataclass(frozen=True)
class OrderFacts:
    """What the data layer knows about the security right now (read from the database)."""

    exists: bool
    instrument: Literal["stock", "fund"] = "stock"
    sharia_status: str | None = None  # HALAL, NON_HALAL, UNKNOWN, or None when not classified
    price_age_days: int | None = None
    avg_daily_dollar_volume: Decimal | None = None
    sector: str | None = None


@dataclass(frozen=True)
class Position:
    market_value: Decimal
    instrument: Literal["stock", "fund"] = "stock"
    sector: str | None = None


@dataclass(frozen=True)
class PortfolioState:
    nav: Decimal
    cash: Decimal
    positions: Mapping[str, Position] = field(default_factory=dict)
    day_start_nav: Decimal | None = None
    peak_nav: Decimal | None = None
    traded_today: Decimal = Decimal(0)  # buys plus sells already approved today
    broker_connected: bool = True
    kill_switch_engaged: bool = False


@dataclass(frozen=True)
class RiskDecision:
    result: str
    reason_code: str
    explanation: str

    @property
    def allowed(self) -> bool:
        return self.result in (APPROVED, REQUIRES_MANUAL_REVIEW)


def _reject(code: str, text: str) -> RiskDecision:
    return RiskDecision(REJECTED, code, text)


def _pct(part: Decimal, whole: Decimal) -> Decimal:
    return part / whole * HUNDRED if whole > 0 else Decimal(0)


def check_order(
    order: OrderRequest, facts: OrderFacts, state: PortfolioState, limits: RiskLimits
) -> RiskDecision:
    """Validate one order. The first failed check decides; no order passes by default."""
    buy = order.side == "BUY"
    notional = order.notional
    if state.kill_switch_engaged:
        return _reject(KILL_SWITCH, "The kill switch is engaged: no orders may be sent.")
    if order.quantity <= 0 or order.price <= 0:
        return _reject(BAD_ORDER, "The order has no positive quantity and price.")
    if not facts.exists:
        return _reject(UNKNOWN_SECURITY, f"{order.symbol} is not in the security master.")
    if buy:
        decision = _sharia_check(order, facts, limits)
        if decision:
            return decision
    if facts.price_age_days is None or facts.price_age_days > limits.max_price_age_days:
        age = "missing" if facts.price_age_days is None else f"{facts.price_age_days} days old"
        return _reject(
            STALE_PRICE,
            f"The latest price for {order.symbol} is {age} "
            f"(limit {limits.max_price_age_days} days).",
        )
    if buy:
        decision = _buy_checks(order, facts, state, limits)
        if decision:
            return decision
    if notional > limits.max_order_value:
        return _reject(
            ORDER_TOO_LARGE,
            f"The order is ${notional:,.0f}, above the ${limits.max_order_value:,.0f} limit.",
        )
    turnover = _pct(state.traded_today + notional, state.nav)
    if turnover > limits.max_daily_turnover_pct:
        return _reject(
            TURNOVER_LIMIT,
            f"Today's trading would reach {turnover:.1f}% of the portfolio "
            f"(limit {limits.max_daily_turnover_pct}%).",
        )
    if not state.broker_connected:
        return _reject(BROKER_DISCONNECTED, "The broker is not connected.")
    if notional > limits.manual_review_order_value:
        return RiskDecision(
            REQUIRES_MANUAL_REVIEW,
            MANUAL_REVIEW_SIZE,
            f"The order is ${notional:,.0f}, above ${limits.manual_review_order_value:,.0f}: "
            "the owner must approve it.",
        )
    return RiskDecision(APPROVED, OK, "All risk checks passed.")


def _sharia_check(
    order: OrderRequest, facts: OrderFacts, limits: RiskLimits
) -> RiskDecision | None:
    """Tests 1 and 2: a non-halal or unclassified stock, or an unlisted fund, cannot be bought."""
    if facts.instrument == "fund":
        approved = {f.upper() for f in limits.approved_funds}
        if order.symbol.upper() not in approved:
            return _reject(
                FUND_NOT_APPROVED, f"{order.symbol} is not on the approved halal funds list."
            )
        return None
    if facts.sharia_status == "HALAL":
        return None
    if facts.sharia_status == "NON_HALAL":
        return _reject(NOT_HALAL, f"{order.symbol} is classified NON_HALAL and cannot be bought.")
    return _reject(
        SHARIA_UNKNOWN,
        f"{order.symbol} has no HALAL classification ({facts.sharia_status or 'none'}): "
        "unknown is never eligible.",
    )


def _buy_checks(
    order: OrderRequest, facts: OrderFacts, state: PortfolioState, limits: RiskLimits
) -> RiskDecision | None:
    notional = order.notional
    volume = facts.avg_daily_dollar_volume
    if volume is None or volume < limits.min_average_daily_volume_usd:
        return _reject(
            ILLIQUID,
            f"{order.symbol} trades too little (average daily value {volume or 0:,.0f} USD, "
            f"minimum {limits.min_average_daily_volume_usd:,.0f}).",
        )
    if state.peak_nav and (
        _pct(state.peak_nav - state.nav, state.peak_nav) >= limits.max_portfolio_drawdown_pct
    ):
        return _reject(DRAWDOWN_BREAKER, "The portfolio fell past its drawdown limit: no buying.")
    if state.day_start_nav and (
        _pct(state.day_start_nav - state.nav, state.day_start_nav) >= limits.max_daily_loss_pct
    ):
        return _reject(DAILY_LOSS_BREAKER, "Today's loss passed the daily limit: no buying.")
    current = state.positions.get(order.symbol)
    held = current.market_value if current else Decimal(0)
    position_pct = _pct(held + notional, state.nav)
    cap = limits.max_fund_position_pct if facts.instrument == "fund" else limits.max_position_pct
    if position_pct > cap:
        return _reject(
            POSITION_LIMIT,
            f"{order.symbol} would be {position_pct:.1f}% of the portfolio (limit {cap}%).",
        )
    if facts.instrument == "stock" and facts.sector:
        in_sector = sum(
            (
                p.market_value
                for p in state.positions.values()
                if p.instrument == "stock" and p.sector == facts.sector
            ),
            Decimal(0),
        )
        sector_pct = _pct(in_sector + notional, state.nav)
        if sector_pct > limits.max_sector_pct:
            return _reject(
                SECTOR_LIMIT,
                f"The {facts.sector} sector would be {sector_pct:.1f}% of the portfolio "
                f"(limit {limits.max_sector_pct}%).",
            )
    invested = sum((p.market_value for p in state.positions.values()), Decimal(0))
    exposure_pct = _pct(invested + notional, state.nav)
    if exposure_pct > limits.max_total_exposure_pct:
        return _reject(
            EXPOSURE_LIMIT,
            f"Total exposure would be {exposure_pct:.1f}% "
            f"(limit {limits.max_total_exposure_pct}%).",
        )
    if notional > state.cash:
        return _reject(
            CASH_INSUFFICIENT,
            f"The order needs ${notional:,.0f} but only ${state.cash:,.0f} is cash.",
        )
    return None


def apply_order(state: PortfolioState, order: OrderRequest, facts: OrderFacts) -> PortfolioState:
    """The portfolio state after `order` is assumed filled (used to check a batch in sequence)."""
    sign = Decimal(1) if order.side == "BUY" else Decimal(-1)
    positions = dict(state.positions)
    previous = positions.get(order.symbol)
    value = (previous.market_value if previous else Decimal(0)) + sign * order.notional
    if value > Decimal("0.005"):
        positions[order.symbol] = Position(value, facts.instrument, facts.sector)
    else:
        positions.pop(order.symbol, None)
    return replace(
        state,
        cash=state.cash - sign * order.notional,
        positions=positions,
        traded_today=state.traded_today + order.notional,
    )


def check_batch(
    orders: Sequence[OrderRequest],
    facts_for: Mapping[str, OrderFacts],
    state: PortfolioState,
    limits: RiskLimits,
) -> list[tuple[OrderRequest, RiskDecision]]:
    """Check a rebalance's orders in sequence, sells first, each seeing the earlier approvals.

    A rejected order changes nothing; an approved (or manual-review) order is assumed filled when
    the next one is checked, so the limits see the portfolio the batch would create.
    """
    ordered = sorted(orders, key=lambda o: (o.side != "SELL", o.symbol))
    results: list[tuple[OrderRequest, RiskDecision]] = []
    current = state
    for order in ordered:
        facts = facts_for.get(order.symbol, OrderFacts(exists=False))
        decision = check_order(order, facts, current, limits)
        results.append((order, decision))
        if decision.allowed:
            current = apply_order(current, order, facts)
    return results
