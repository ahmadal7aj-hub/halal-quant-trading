"""The broker interface, the paper-only guard and a simulated broker (Phase 5; PRD §15, doc 02 §4).

The platform talks to a broker only through the `Broker` protocol, so every safety rule (paper only,
no duplicate orders, safe disconnects, reconciliation) is built and tested here with a simulated
broker, before any real connection exists. The real IBKR adapter is added later behind the same
protocol and must pass `assert_paper_only` before it is used. Live trading is refused in code.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, Protocol

# IBKR paper accounts start with "DU"; live accounts do not. Paper API ports only.
PAPER_ACCOUNT_PREFIX = "DU"
PAPER_PORTS = frozenset({4002, 7497})  # IB Gateway paper, TWS paper
LIVE_PORTS = frozenset({4001, 7496})  # IB Gateway live, TWS live: always refused


class LiveTradingRefused(Exception):
    """Something other than an IBKR paper account or paper port was used. V1 never trades live."""


class BrokerNotConnected(Exception):
    """The broker is not connected: the order was definitely NOT sent."""


class AmbiguousSubmit(Exception):
    """The order may or may not have reached the broker (timeout, lost connection mid-send)."""


class BrokerRejected(Exception):
    """The broker received the order and refused it."""


def assert_paper_account(account_id: str) -> None:
    """Refuse an account that is not an IBKR paper account (paper IDs start with DU)."""
    if not account_id.startswith(PAPER_ACCOUNT_PREFIX):
        raise LiveTradingRefused(
            f"Account {account_id[:2]}... is not a paper account (paper accounts start with "
            f"{PAPER_ACCOUNT_PREFIX}); V1 is paper only."
        )


def assert_paper_only(account_id: str, port: int) -> None:
    """Refuse to go on unless this is a paper account on a paper port (doc 02 §4 hard guards)."""
    if port in LIVE_PORTS:
        raise LiveTradingRefused(f"Port {port} is a LIVE trading port; V1 is paper only.")
    if port not in PAPER_PORTS:
        raise LiveTradingRefused(f"Port {port} is not a known paper port {sorted(PAPER_PORTS)}.")
    assert_paper_account(account_id)


@dataclass(frozen=True)
class BrokerOrder:
    """What is sent: the client reference (our idempotency key) is how a lost order is found."""

    client_ref: str
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: Decimal
    limit_price: Decimal


@dataclass(frozen=True)
class BrokerFill:
    exec_id: str
    quantity: Decimal
    price: Decimal


@dataclass(frozen=True)
class BrokerOrderInfo:
    broker_ref: str
    client_ref: str
    symbol: str
    side: str
    quantity: Decimal
    status: Literal["ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED", "CANCELLED", "REJECTED"]
    fills: tuple[BrokerFill, ...] = ()


class Broker(Protocol):
    def is_connected(self) -> bool: ...

    def account_id(self) -> str: ...

    def positions(self) -> dict[str, Decimal]:
        """Shares held by symbol."""

    def cash(self) -> Decimal: ...

    def open_orders(self) -> list[BrokerOrderInfo]: ...

    def find_order(self, client_ref: str) -> BrokerOrderInfo | None:
        """The order sent with this client reference, or None if the broker never saw it."""

    def submit(self, order: BrokerOrder) -> BrokerOrderInfo:
        """Send an order; may raise BrokerNotConnected, BrokerRejected or AmbiguousSubmit."""


@dataclass
class SimulatedBroker:
    """An in-memory paper broker with switchable failure modes, for tests and dry runs.

    It deliberately does NOT de-duplicate on the client reference: a real broker would accept a
    second identical order, so the platform itself must make duplicates impossible.
    """

    account: str = "DU0000001"
    connected: bool = True
    starting_cash: Decimal = Decimal(100_000)
    mode: Literal["normal", "reject", "lose_ack", "partial"] = "normal"
    submissions: int = 0  # how many orders really reached the broker (for the tests)
    _orders: dict[str, BrokerOrderInfo] = field(default_factory=dict)
    _positions: dict[str, Decimal] = field(default_factory=dict)
    _cash: Decimal | None = None
    _counter: int = 0

    def is_connected(self) -> bool:
        return self.connected

    def account_id(self) -> str:
        return self.account

    def positions(self) -> dict[str, Decimal]:
        return {s: q for s, q in self._positions.items() if q != 0}

    def cash(self) -> Decimal:
        return self.starting_cash if self._cash is None else self._cash

    def open_orders(self) -> list[BrokerOrderInfo]:
        return [
            o for o in self._orders.values() if o.status in ("ACKNOWLEDGED", "PARTIALLY_FILLED")
        ]

    def find_order(self, client_ref: str) -> BrokerOrderInfo | None:
        if not self.connected:
            raise BrokerNotConnected("Cannot look up orders while disconnected.")
        matches = [o for o in self._orders.values() if o.client_ref == client_ref]
        return matches[-1] if matches else None

    def _apply_fill(self, order: BrokerOrder, quantity: Decimal) -> None:
        sign = Decimal(1) if order.side == "BUY" else Decimal(-1)
        self._positions[order.symbol] = (
            self._positions.get(order.symbol, Decimal(0)) + sign * quantity
        )
        self._cash = self.cash() - sign * quantity * order.limit_price

    def submit(self, order: BrokerOrder) -> BrokerOrderInfo:
        if not self.connected:
            raise BrokerNotConnected("The broker is not connected.")
        if self.mode == "reject":
            raise BrokerRejected("The broker rejected the order (simulated).")
        self.submissions += 1
        self._counter += 1
        ref = f"SIM-{self._counter}"
        if self.mode == "partial":
            half = (order.quantity / 2).quantize(Decimal(1))
            self._apply_fill(order, half)
            info = BrokerOrderInfo(
                ref, order.client_ref, order.symbol, order.side, order.quantity,
                "PARTIALLY_FILLED", (BrokerFill(f"{ref}-1", half, order.limit_price),),
            )  # fmt: skip
        else:
            self._apply_fill(order, order.quantity)
            info = BrokerOrderInfo(
                ref, order.client_ref, order.symbol, order.side, order.quantity,
                "FILLED", (BrokerFill(f"{ref}-1", order.quantity, order.limit_price),),
            )  # fmt: skip
        self._orders[ref] = info
        if self.mode == "lose_ack":
            raise AmbiguousSubmit("The connection dropped before the broker's reply (simulated).")
        return info

    # --- test helpers: things that happen at the broker without us asking ---
    def fill_rest(self, broker_ref: str) -> None:
        """Complete a partly filled order."""
        info = self._orders[broker_ref]
        done = sum((f.quantity for f in info.fills), Decimal(0))
        rest = info.quantity - done
        side: Literal["BUY", "SELL"] = "BUY" if info.side == "BUY" else "SELL"
        order = BrokerOrder(info.client_ref, info.symbol, side, rest, info.fills[0].price)
        self._apply_fill(order, rest)
        fills = (*info.fills, BrokerFill(f"{broker_ref}-2", rest, info.fills[0].price))
        self._orders[broker_ref] = BrokerOrderInfo(
            broker_ref, info.client_ref, info.symbol, info.side, info.quantity, "FILLED", fills
        )

    def tamper_position(self, symbol: str, quantity: Decimal) -> None:
        """Make the broker's position differ from our records (to test reconciliation)."""
        self._positions[symbol] = quantity
