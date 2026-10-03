"""Equal-weight portfolio construction (Phase 4; PRD §11).

Turns a list of chosen assets into the orders that move the current holdings to equal weights, in
whole shares (rounded down, so the portfolio never needs more cash than it has). Pure functions:
the risk engine, not this module, decides whether an order may go out.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from halal_quant.trading.risk import OrderRequest

ZERO = Decimal(0)


@dataclass(frozen=True)
class Target:
    symbol: str
    price: Decimal  # the latest price, used to size the order


def equal_weight_orders(
    targets: Sequence[Target],
    holdings: Mapping[str, Decimal],
    prices: Mapping[str, Decimal],
    nav: Decimal,
    invested_fraction: Decimal = Decimal(1),
    min_trade_value: Decimal = Decimal(50),
) -> list[OrderRequest]:
    """Orders to reach equal weights over `targets`, holding `invested_fraction` of `nav`.

    `holdings` is symbol -> shares held; `prices` must cover every held symbol. Anything held that
    is not a target is sold in full. A top-up or trim smaller than `min_trade_value` is skipped
    (no churn); a full exit is never skipped.
    """
    if not (ZERO <= invested_fraction <= 1):
        raise ValueError("invested_fraction must be between 0 and 1")
    target_price = {t.symbol: t.price for t in targets}
    wanted: dict[str, Decimal] = {}
    if targets:
        each = nav * invested_fraction / len(targets)
        for t in targets:
            if t.price <= 0:
                raise ValueError(f"{t.symbol} has no usable price")
            wanted[t.symbol] = (each / t.price).quantize(Decimal(1), rounding=ROUND_DOWN)
    orders: list[OrderRequest] = []
    for symbol in sorted(set(wanted) | set(holdings)):
        price = target_price.get(symbol) or prices.get(symbol)
        if price is None or price <= 0:
            raise ValueError(f"{symbol} is held but has no usable price")
        delta = wanted.get(symbol, ZERO) - holdings.get(symbol, ZERO)
        if delta == 0:
            continue
        if symbol in wanted and abs(delta) * price < min_trade_value:
            continue
        if symbol not in wanted:
            reason = "sell: no longer a target"
        else:
            reason = "equal-weight " + ("top-up" if delta > 0 else "trim")
        orders.append(
            OrderRequest(symbol, "BUY" if delta > 0 else "SELL", abs(delta), price, reason)
        )
    return orders
