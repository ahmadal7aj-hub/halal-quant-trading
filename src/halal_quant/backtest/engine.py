"""The backtest engine: Momentum V1 on the date-correct Halal universe (P2-5, P2-6; ADR-003).

Pure logic over two small interfaces, so every rule can be tested on made-up data:
`UniverseSource` (who is eligible on a decision date) and `PriceSource` (one price vintage).

How a month works (decision day D = the first trading day of the month):
1. The eligible universe for D is read (it was built from information before D only).
2. Each member is scored by its adjusted-close return over `lookback_trading_days`, ending
   `skip_trading_days` trading days before the last close before D. Nothing dated on or after D is
   read for the score (PRD Test 10 spirit): the window ends strictly before D.
3. The best `top_n` that can actually be traded on D (they have a price dated D) are held in
   equal dollar weights. Ties break by security id, so the result never depends on dict order.
4. Trades happen at the close of D. Costs per order: slippage (basis points of the traded value)
   plus commission per share (with a minimum per order and a cap as a fraction of the value).
5. Until the next decision day the holdings earn their adjusted-close returns (dividends and
   splits included) less a small daily purification drag. A holding with no price for a while is
   carried at its last price (a delisted stock is closed at its last available price: no
   survivorship bias, but also no invented recovery).

All money arithmetic is `Decimal`, so the same inputs always give the identical result hash.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Protocol

from halal_quant.backtest.config import Costs, MomentumConfig
from halal_quant.data.calendar import previous_trading_day, trading_days

START_NAV = Decimal(100_000)
MAX_PRICE_AGE_DAYS = 7  # how stale a price may be for a score
TRADING_DAYS_PER_YEAR = Decimal(252)
BPS = Decimal(10_000)
CENT = Decimal("0.01")


@dataclass(frozen=True)
class PricePoint:
    day: date
    adjusted: Decimal
    unadjusted: Decimal


class UniverseSource(Protocol):
    def members(self, as_of: date) -> list[int]:
        """The security ids eligible on `as_of`."""

    def describe(self, security_id: int, as_of: date) -> str:
        """Plain words on why a security is, or is not, eligible on `as_of`."""


class PriceSource(Protocol):
    def on_or_before(
        self, security_ids: Sequence[int], day: date, max_age_days: int
    ) -> dict[int, PricePoint]:
        """The latest price on or before `day`, no older than `max_age_days` calendar days."""

    def series(
        self, security_ids: Sequence[int], first: date, last: date
    ) -> dict[int, dict[date, tuple[Decimal, Decimal]]]:
        """(adjusted, unadjusted) closes by day, from `first` to `last` inclusive."""


@dataclass(frozen=True)
class Trade:
    trade_date: date
    security_id: int
    side: str  # BUY or SELL
    shares: Decimal
    price: Decimal  # the price paid, before slippage
    notional: Decimal
    cost: Decimal  # slippage plus commission
    reason: str
    kind: str  # open, close, rebalance


@dataclass(frozen=True)
class Rebalance:
    decision_date: date
    eligible: int
    scored: int
    selected: int
    turnover: Decimal  # one-way traded value as a fraction of the portfolio value
    costs: Decimal
    nav_before: Decimal
    nav_after: Decimal


@dataclass
class BacktestResult:
    nav: list[tuple[date, Decimal]] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    rebalances: list[Rebalance] = field(default_factory=list)


def shift_back(day: date, n: int) -> date:
    """The trading day `n` trading days before `day` (`n` = 0 is `day` itself)."""
    current = day
    for _ in range(n):
        current = previous_trading_day(current)
    return current


def score_members(
    members: Sequence[int], decision: date, strategy: MomentumConfig, prices: PriceSource
) -> tuple[dict[int, Decimal], date, date]:
    """Momentum scores for `members`, and the window used. Uses only closes before `decision`."""
    window_end = shift_back(previous_trading_day(decision), strategy.skip_trading_days)
    window_start = shift_back(window_end, strategy.lookback_trading_days)
    ends = prices.on_or_before(members, window_end, MAX_PRICE_AGE_DAYS)
    starts = prices.on_or_before(members, window_start, MAX_PRICE_AGE_DAYS)
    scores = {
        sid: ends[sid].adjusted / starts[sid].adjusted - 1
        for sid in members
        if sid in ends and sid in starts and starts[sid].adjusted > 0
    }
    return scores, window_start, window_end


def rank(scores: dict[int, Decimal]) -> list[int]:
    """Best score first; ties by security id so the order never depends on insertion order."""
    return sorted(scores, key=lambda sid: (-scores[sid], sid))


def order_cost(
    notional: Decimal, unadjusted: Decimal, costs: Costs, slippage_bps: Decimal
) -> Decimal:
    """Slippage plus commission for one order."""
    shares = notional / unadjusted
    commission = shares * costs.commission_per_share
    commission = max(commission, costs.commission_min_per_order)
    commission = min(commission, notional * costs.commission_max_fraction)
    return notional * slippage_bps / BPS + commission


def run_backtest(
    decision_dates: Sequence[date],
    last_day: date,
    universe: UniverseSource,
    prices: PriceSource,
    strategy: MomentumConfig,
    costs: Costs,
    slippage_bps: Decimal,
    purification_drag: Decimal,
) -> BacktestResult:
    """Run the monthly rotation from the first decision date to `last_day`."""
    result = BacktestResult()
    if not decision_dates:
        return result
    daily_drag = 1 - purification_drag / TRADING_DAYS_PER_YEAR
    holdings: dict[int, Decimal] = {}  # dollars held after the last trade, at base prices
    base_adj: dict[int, Decimal] = {}  # adjusted close each holding was set up at
    last_adj: dict[int, PricePoint] = {}  # latest known price of every holding (carried if stale)
    cash = START_NAV
    drag_factor = Decimal(1)

    def value_now() -> Decimal:
        total = cash
        for sid, dollars in holdings.items():
            total += dollars * drag_factor * last_adj[sid].adjusted / base_adj[sid]
        return total

    for index, decision in enumerate(decision_dates):
        next_decision = decision_dates[index + 1] if index + 1 < len(decision_dates) else None
        window_last = next_decision if next_decision else last_day
        # --- valuation at the close of the decision day, before trading ---
        members = universe.members(decision)
        today = prices.on_or_before(
            sorted(set(members) | set(holdings)), decision, MAX_PRICE_AGE_DAYS
        )
        for sid, point in today.items():
            if sid in holdings:
                last_adj[sid] = point
        current = {
            sid: dollars * drag_factor * last_adj[sid].adjusted / base_adj[sid]
            for sid, dollars in holdings.items()
        }
        nav_before = cash + sum(current.values(), Decimal(0))

        # --- choose the holdings: top N by momentum that can be traded today ---
        scores, window_start, window_end = score_members(members, decision, strategy, prices)
        ranking = rank(scores)
        position = {sid: i + 1 for i, sid in enumerate(ranking)}
        tradable = [sid for sid in ranking if sid in today and today[sid].day == decision]
        selected = tradable[: strategy.top_n]
        target = {sid: nav_before / len(selected) for sid in selected} if selected else {}

        # --- trades at the close of the decision day ---
        trades: list[Trade] = []
        costs_total = Decimal(0)
        traded = Decimal(0)
        for sid in sorted(set(current) | set(target)):
            delta = target.get(sid, Decimal(0)) - current.get(sid, Decimal(0))
            notional = abs(delta)
            if notional < CENT:
                continue
            point = today.get(sid) or last_adj[sid]
            cost = order_cost(notional, point.unadjusted, costs, slippage_bps)
            costs_total += cost
            traded += notional
            held, wanted = sid in current, sid in target
            if not held:
                kind, reason = (
                    "open",
                    (
                        f"bought: ranked {position[sid]} of {len(ranking)} eligible by "
                        f"{strategy.lookback_trading_days}-day momentum (score {scores[sid]:+.1%}, "
                        f"measured {window_start} to {window_end})"
                    ),
                )
            elif not wanted:
                kind = "close"
                if sid not in members:
                    why_not = universe.describe(sid, decision)
                    reason = f"sold: no longer eligible on {decision} ({why_not})"
                elif sid in position:
                    reason = (
                        f"sold: fell out of the top {strategy.top_n} "
                        f"(rank {position[sid]} of {len(ranking)})"
                    )
                else:
                    reason = "sold: no momentum score available (missing price history)"
            else:
                kind = "rebalance"
                reason = (
                    f"kept (rank {position[sid]}); {'added to' if delta > 0 else 'trimmed to'} "
                    "equal weight"
                )
            result.trades.append(
                Trade(
                    decision,
                    sid,
                    "BUY" if delta > 0 else "SELL",
                    (notional / point.unadjusted).quantize(Decimal("0.0001")),
                    point.unadjusted,
                    notional.quantize(CENT),
                    cost.quantize(Decimal("0.0001")),
                    reason,
                    kind,
                )
            )
            trades.append(result.trades[-1])

        nav_after = nav_before - costs_total
        if selected:
            weight = nav_after / len(selected)
            holdings = {sid: weight for sid in selected}
            base_adj = {sid: today[sid].adjusted for sid in selected}
            last_adj = {sid: today[sid] for sid in selected}
            cash = Decimal(0)
        else:
            holdings, base_adj, last_adj, cash = {}, {}, {}, nav_after
        drag_factor = Decimal(1)
        result.rebalances.append(
            Rebalance(
                decision,
                len(members),
                len(scores),
                len(selected),
                (traded / 2 / nav_before) if nav_before else Decimal(0),
                costs_total,
                nav_before,
                nav_after,
            )
        )
        result.nav.append((decision, nav_after))

        # --- daily path until the next decision day (or the end) ---
        days = [d for d in trading_days(decision, window_last) if d > decision]
        if not days:
            continue
        series = prices.series(sorted(holdings), decision, window_last) if holdings else {}
        for day in days:
            drag_factor *= daily_drag
            for sid in holdings:
                closes = series.get(sid, {}).get(day)
                if closes is not None:
                    last_adj[sid] = PricePoint(day, closes[0], closes[1])
            if day == next_decision:
                break  # the next iteration revalues at this close, before its trades
            result.nav.append((day, value_now()))
    return result
