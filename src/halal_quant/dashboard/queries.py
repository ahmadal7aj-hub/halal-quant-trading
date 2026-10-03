"""What the dashboard shows, as plain functions over the database (Phase 6; PRD §26-§28).

Everything here only READS (the dashboard reads through the read-only role); the two actions the
owner can take, deciding a proposal and the kill switch, live in `trading.records` and are called
by the pages with the application role. Keeping the logic here, away from the screen code, means
every number on a page is tested without a browser.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, text

from halal_quant.trading.records import AWAITING_APPROVAL, kill_switch_engaged


@dataclass(frozen=True)
class Holding:
    symbol: str
    quantity: Decimal
    price: Decimal | None
    value: Decimal | None
    weight_pct: Decimal | None
    kind: str  # "approved fund" or "stock"


def _rows(conn: Connection, sql: str, **params: Any) -> list[dict[str, Any]]:
    return [dict(r._mapping) for r in conn.execute(text(sql), params)]


def _scalar(conn: Connection, sql: str, **params: Any) -> Any:
    return conn.execute(text(sql), params).scalar_one_or_none()


def _count_orders_in(conn: Connection, states: tuple[str, ...]) -> int:
    """How many broker orders are currently in one of `states`."""
    sql = "SELECT count(*) FROM hq.order_current_state WHERE state = ANY(:states)"
    return int(_scalar(conn, sql, states=list(states)) or 0)


def latest_fund_prices(conn: Connection) -> dict[str, tuple[date, Decimal]]:
    """The last known price of each benchmark/fund symbol in the newest complete price vintage."""
    rows = _rows(
        conn,
        """
        SELECT DISTINCT ON (symbol) symbol, price_date, adjusted_close
        FROM hq.vintage_benchmark_price
        WHERE vintage_id = (SELECT vintage_id FROM hq.price_vintage WHERE status = 'complete'
                            ORDER BY finished_at DESC NULLS LAST LIMIT 1)
        ORDER BY symbol, price_date DESC
        """,
    )
    return {r["symbol"]: (r["price_date"], r["adjusted_close"]) for r in rows}


def holdings(conn: Connection, approved_funds: list[str]) -> list[Holding]:
    """What the order records say we hold (fills), valued at the latest known fund price."""
    rows = _rows(
        conn,
        """
        SELECT symbol, SUM(signed_quantity) AS quantity FROM hq.fill_signed
        GROUP BY symbol HAVING SUM(signed_quantity) <> 0 ORDER BY symbol
        """,
    )
    prices = latest_fund_prices(conn)
    funds = {f.upper() for f in approved_funds}
    values: dict[str, Decimal | None] = {}
    for r in rows:
        price = prices.get(r["symbol"])
        values[r["symbol"]] = r["quantity"] * price[1] if price else None
    total = sum((v for v in values.values() if v is not None), Decimal(0))
    result = []
    for r in rows:
        symbol, value = r["symbol"], values[r["symbol"]]
        result.append(
            Holding(
                symbol,
                r["quantity"],
                prices[symbol][1] if symbol in prices else None,
                value,
                (value / total * 100) if value is not None and total else None,
                "approved fund" if symbol.upper() in funds else "stock",
            )
        )
    return result


def pending_approvals(conn: Connection) -> list[dict[str, Any]]:
    """Proposals waiting for the owner, with everything the approval screen needs (PRD §27)."""
    return _rows(
        conn,
        """
        SELECT p.id, p.symbol, p.side, p.quantity, p.ref_price, p.notional, p.strategy_reason,
               p.risk_result, p.reason_code, p.explanation, p.created_at
        FROM hq.trade_proposal p JOIN hq.proposal_current_status s ON s.proposal_id = p.id
        WHERE s.status = :waiting
        ORDER BY p.id
        """,
        waiting=AWAITING_APPROVAL,
    )


def recent_proposals(conn: Connection, limit: int = 50) -> list[dict[str, Any]]:
    return _rows(
        conn,
        """
        SELECT p.id, p.created_at, p.symbol, p.side, p.quantity, p.notional, p.risk_result,
               p.reason_code, p.explanation, p.strategy_reason, s.status
        FROM hq.trade_proposal p LEFT JOIN hq.proposal_current_status s ON s.proposal_id = p.id
        ORDER BY p.id DESC LIMIT :n
        """,
        n=limit,
    )


def orders(conn: Connection, limit: int = 50) -> list[dict[str, Any]]:
    """Every broker order with its latest state and how much has filled (PRD §26 Orders)."""
    return _rows(
        conn,
        """
        SELECT o.id, o.created_at, o.symbol, o.side, o.quantity, o.limit_price, o.account_id,
               s.state,
               COALESCE((SELECT SUM(f.quantity) FROM hq.order_fill f WHERE f.order_id = o.id), 0)
                   AS filled
        FROM hq.broker_order o LEFT JOIN hq.order_current_state s ON s.order_id = o.id
        ORDER BY o.id DESC LIMIT :n
        """,
        n=limit,
    )


def order_history(conn: Connection, order_id: int) -> list[dict[str, Any]]:
    return _rows(
        conn,
        "SELECT created_at, state, actor, cause, broker_ref FROM hq.order_state_event "
        "WHERE order_id = :i ORDER BY id",
        i=order_id,
    )


def sharia_summary(conn: Connection, methodology: str = "AAOIFI-v1") -> dict[str, Any]:
    """The latest screening date, how many securities fell in each class, and the universe size."""
    latest = _scalar(
        conn,
        "SELECT max(screening_date) FROM hq.classification WHERE methodology = :m",
        m=methodology,
    )
    counts: dict[str, int] = {}
    if latest:
        rows = _rows(
            conn,
            "SELECT status, count(*) AS n FROM hq.classification "
            "WHERE methodology = :m AND screening_date = :d GROUP BY status ORDER BY status",
            m=methodology,
            d=latest,
        )
        counts = {r["status"]: int(r["n"]) for r in rows}
    universe = _rows(
        conn,
        "SELECT as_of, member_count, universe_version FROM hq.universe_build "
        "ORDER BY as_of DESC, id DESC LIMIT 1",
    )
    return {
        "methodology": methodology,
        "provider": "internal screen (not reviewed by a qualified scholar)",
        "latest_screening_date": latest,
        "counts": counts,
        "unknown_count": counts.get("UNKNOWN", 0),
        "latest_universe": universe[0] if universe else None,
    }


def risk_status(conn: Connection) -> dict[str, Any]:
    """The kill switch, and which rules have fired recently (rejections by reason code)."""
    fired = _rows(
        conn,
        "SELECT reason_code, count(*) AS n FROM hq.trade_proposal WHERE risk_result <> 'APPROVED' "
        "GROUP BY reason_code ORDER BY n DESC, reason_code",
    )
    last = _rows(
        conn,
        "SELECT created_at, engaged, actor, reason FROM hq.kill_switch_event "
        "ORDER BY id DESC LIMIT 5",
    )
    return {
        "kill_switch_engaged": kill_switch_engaged(conn),
        "recent_kill_switch_events": last,
        "rules_triggered": fired,
        "orders_in_doubt": _count_orders_in(conn, ("SUBMISSION_UNKNOWN",)),
    }


def backtests(conn: Connection, limit: int = 100) -> list[dict[str, Any]]:
    """Stored backtest runs with their headline numbers (PRD §26 Backtests)."""
    return _rows(
        conn,
        """
        SELECT id, created_at, strategy_version, period_name, slippage_case, first_day, last_day,
               (metrics->>'cagr')::float AS cagr, (metrics->>'volatility')::float AS volatility,
               (metrics->>'sharpe')::float AS sharpe,
               (metrics->>'max_drawdown')::float AS max_drawdown,
               left(result_sha256, 12) AS result_hash
        FROM hq.backtest_run ORDER BY id DESC LIMIT :n
        """,
        n=limit,
    )


def system_health(conn: Connection) -> dict[str, Any]:
    """Is the database up, what version, how recent is the data, and are there problems."""
    vintages = _rows(
        conn,
        "SELECT vintage_id, status, finished_at FROM hq.price_vintage ORDER BY created_at DESC",
    )
    return {
        "database": "connected",
        "schema_revision": _scalar(conn, "SELECT version_num FROM hq.alembic_version"),
        "price_vintages": vintages,
        "latest_daily_price": _scalar(conn, "SELECT max(price_date) FROM hq.daily_price"),
        "audit_events_last_24h": int(
            _scalar(
                conn,
                "SELECT count(*) FROM hq.audit_event "
                "WHERE occurred_at > now() - interval '24 hours'",
            )
            or 0
        ),
        "orders_failed_or_in_doubt": _count_orders_in(conn, ("FAILED", "SUBMISSION_UNKNOWN")),
        "broker": "not connected (IBKR paper adapter not built yet)",
        "backups": "no backup monitoring yet (Phase 7)",
    }


def audit_trail(conn: Connection, limit: int = 100) -> list[dict[str, Any]]:
    return _rows(
        conn,
        "SELECT occurred_at, actor, action, entity_type, entity_id, reason "
        "FROM hq.audit_event ORDER BY sequence DESC LIMIT :n",
        n=limit,
    )
