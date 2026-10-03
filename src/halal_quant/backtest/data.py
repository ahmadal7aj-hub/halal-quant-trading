"""Reading the price vintage and the stored universes for the backtest engine (P2-4).

`SqlPriceSource` reads one price vintage and nothing else: a backtest can never mix vintages.
`SqlUniverseSource` gets a date's universe through `build_universe`, which reuses the stored
build when the inputs are identical (same content hash), so a rerun sees the same universe.
"""

from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import Connection, select
from sqlalchemy.dialects.postgresql import distinct_on

from halal_quant.backtest.engine import PricePoint
from halal_quant.core.config import LoadedConfig, UniverseConfig
from halal_quant.data.calendar import previous_trading_day
from halal_quant.data.fundamentals import daily_market_cap_table
from halal_quant.data.vintage import vintage_benchmark_price_table, vintage_price_table
from halal_quant.sharia.classification import classification_on
from halal_quant.universe.builder import build_universe


class SqlPriceSource:
    def __init__(self, conn: Connection, vintage_id: str) -> None:
        self._conn = conn
        self._vintage = vintage_id

    def on_or_before(
        self, security_ids: Sequence[int], day: date, max_age_days: int
    ) -> dict[int, PricePoint]:
        if not security_ids:
            return {}
        p = vintage_price_table.c
        rows = self._conn.execute(
            select(p.security_id, p.price_date, p.adjusted_close, p.close_unadjusted)
            .ext(distinct_on(p.security_id))
            .where(
                p.vintage_id == self._vintage,
                p.security_id.in_(list(security_ids)),
                p.price_date <= day,
                p.price_date > day - timedelta(days=max_age_days),
            )
            .order_by(p.security_id, p.price_date.desc())
        )
        return {
            r.security_id: PricePoint(r.price_date, r.adjusted_close, r.close_unadjusted)
            for r in rows
        }

    def series(
        self, security_ids: Sequence[int], first: date, last: date
    ) -> dict[int, dict[date, tuple[Decimal, Decimal]]]:
        if not security_ids:
            return {}
        p = vintage_price_table.c
        out: dict[int, dict[date, tuple[Decimal, Decimal]]] = defaultdict(dict)
        for r in self._conn.execute(
            select(p.security_id, p.price_date, p.adjusted_close, p.close_unadjusted).where(
                p.vintage_id == self._vintage,
                p.security_id.in_(list(security_ids)),
                p.price_date >= first,
                p.price_date <= last,
            )
        ):
            out[r.security_id][r.price_date] = (r.adjusted_close, r.close_unadjusted)
        return dict(out)


class SqlUniverseSource:
    def __init__(
        self, conn: Connection, universe: LoadedConfig[UniverseConfig], methodology: str
    ) -> None:
        self._conn = conn
        self._universe = universe
        self._methodology = methodology

    def members(self, as_of: date) -> list[int]:
        return list(build_universe(self._conn, as_of, self._universe).members)

    def describe(self, security_id: int, as_of: date) -> str:
        record = classification_on(self._conn, security_id, as_of, self._methodology)
        if record is None:
            return "no Sharia classification in effect"
        return f"Sharia screen: {record['status']}: {str(record['reason'])[:90]}"


def above_average(closes_newest_first: Sequence[Decimal]) -> bool:
    """True when the newest close is above the simple average of all the closes given."""
    return closes_newest_first[0] > sum(closes_newest_first, Decimal(0)) / len(closes_newest_first)


def trend_signal(
    conn: Connection, vintage_id: str, symbol: str, days: int
) -> Callable[[date], bool]:
    """The trend filter: is `symbol` above its `days`-day average as of the day before a decision?

    Reads the one price vintage and only closes before the decision day. If the market series is
    too short or stale, it raises: a silent default would change a result without anyone seeing it.
    """
    b = vintage_benchmark_price_table.c

    def risk_on(decision: date) -> bool:
        last_day = previous_trading_day(decision)
        rows = conn.execute(
            select(b.price_date, b.adjusted_close)
            .where(b.vintage_id == vintage_id, b.symbol == symbol, b.price_date <= last_day)
            .order_by(b.price_date.desc())
            .limit(days)
        ).all()
        if len(rows) < days or rows[0].price_date < last_day - timedelta(days=7):
            raise ValueError(
                f"The {symbol} series in vintage {vintage_id!r} cannot give a {days}-day average "
                f"for {decision} ({len(rows)} closes found)."
            )
        return above_average([r.adjusted_close for r in rows])

    return risk_on


def market_cap_lookup(conn: Connection) -> Callable[[Sequence[int], date], dict[int, Decimal]]:
    """Market value in USD on or before a day (no older than a week), for the market_cap ranking."""
    m = daily_market_cap_table.c

    def lookup(security_ids: Sequence[int], day: date) -> dict[int, Decimal]:
        if not security_ids:
            return {}
        rows = conn.execute(
            select(m.security_id, m.market_cap_usd)
            .ext(distinct_on(m.security_id))
            .where(
                m.security_id.in_(list(security_ids)),
                m.cap_date <= day,
                m.cap_date > day - timedelta(days=7),
            )
            .order_by(m.security_id, m.cap_date.desc())
        )
        return {r.security_id: r.market_cap_usd for r in rows}

    return lookup
