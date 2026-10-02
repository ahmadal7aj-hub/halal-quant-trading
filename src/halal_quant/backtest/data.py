"""Reading the price vintage and the stored universes for the backtest engine (P2-4).

`SqlPriceSource` reads one price vintage and nothing else: a backtest can never mix vintages.
`SqlUniverseSource` gets a date's universe through `build_universe`, which reuses the stored
build when the inputs are identical (same content hash), so a rerun sees the same universe.
"""

from collections import defaultdict
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import Connection, select
from sqlalchemy.dialects.postgresql import distinct_on

from halal_quant.backtest.engine import PricePoint
from halal_quant.core.config import LoadedConfig, UniverseConfig
from halal_quant.data.vintage import vintage_price_table
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
