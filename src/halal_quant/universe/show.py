"""Show the eligible universe for any date, and prove it can be rebuilt (Phase 1 demo; PRD §44).

    uv run python -m halal_quant.universe.show --date 2020-01-02 [--top 25] [--config path]

Answers the Phase 1 question: *which securities were eligible to be bought on this date under
the selected Sharia methodology, and why were the others not?* It rebuilds the universe from the
stored data in a transaction that is rolled back, prints the funnel of exclusions and the
largest members, and says whether the identical universe (same content hash) was already stored:
that is the reproducibility check. Nothing is written.
"""

import argparse
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Connection, select

from halal_quant.core.config import UniverseConfig, load_config
from halal_quant.core.logging import configure_logging, correlation_scope
from halal_quant.core.settings import DbRole, get_settings
from halal_quant.data.fundamentals import daily_market_cap_table
from halal_quant.data.security_master import security_table, security_ticker_table
from halal_quant.db.engine import make_engine
from halal_quant.sharia.classification import PROVIDER, classification_table
from halal_quant.universe.builder import UniverseResult, build_universe

TopRow = tuple[str, str, Decimal | None, str]  # ticker, company, market value, Sharia reason

REASON_TEXT = {
    "sharia_non_halal": "Sharia screen says NON_HALAL",
    "sharia_unknown": "Sharia screen could not decide (UNKNOWN)",
    "sharia_pending_review": "waiting for a manual review",
    "sharia_no_record": "no Sharia classification in effect on the date",
    "stale_price": "latest price too old or missing",
    "below_min_price": "price below the minimum",
    "below_min_liquidity": "median daily dollar volume below the minimum",
    "data_quality_finding": "a data-quality finding in the last 20 trading days",
    "duplicate_share_class": "another share class of the same company is more liquid",
}


def largest_members(
    conn: Connection, result: UniverseResult, methodology: str, limit: int
) -> list[TopRow]:
    """(ticker, company, market value the day before, Sharia reason) of the biggest members."""
    s, t, m, c = (
        security_table.c,
        security_ticker_table.c,
        daily_market_cap_table.c,
        classification_table.c,
    )
    rows: list[TopRow] = []
    for security_id in result.members:
        info = conn.execute(
            select(s.company_name, t.ticker)
            .join_from(security_table, security_ticker_table, t.security_id == s.security_id)
            .where(
                s.security_id == security_id,
                t.valid_from <= result.as_of,
                (t.valid_to.is_(None)) | (t.valid_to > result.as_of),
            )
        ).first()
        cap = conn.execute(
            select(m.market_cap_usd)
            .where(m.security_id == security_id, m.cap_date < result.as_of)
            .order_by(m.cap_date.desc())
            .limit(1)
        ).scalar_one_or_none()
        why = conn.execute(
            select(c.reason)
            .where(
                c.security_id == security_id,
                c.provider == PROVIDER,
                c.methodology == methodology,
                c.effective_from <= result.as_of,
                c.effective_to >= result.as_of,
            )
            .order_by(c.screening_date.desc())
            .limit(1)
        ).scalar_one_or_none()
        if info:
            rows.append((info.ticker, info.company_name, cap, why or ""))
    rows.sort(key=lambda r: r[2] or Decimal(0), reverse=True)
    return rows[:limit]


def render(
    result: UniverseResult, universe_version: str, methodology: str, top: list[TopRow]
) -> str:
    lines = [
        f"Eligible universe on {result.as_of}",
        f"  methodology {methodology}, filters {universe_version}",
        f"  members: {len(result.members)}",
        f"  content hash: {result.content_sha256}",
        "  reproducible: "
        + (
            "YES, an identical universe was already stored (same hash)"
            if result.reused
            else "this exact universe was not stored before"
        ),
        "",
        "Why securities are not in it (first failed rule, counted once each):",
    ]
    for reason, count in result.exclusions.most_common():
        lines.append(f"  {count:6d}  {REASON_TEXT.get(reason, reason)}")
    lines += ["", f"Largest {len(top)} members by market value:"]
    for ticker, name, cap, why in top:
        billions = "n/a" if cap is None else f"{cap / Decimal(10**9):,.1f}bn"
        lines.append(f"  {ticker:7s} {name[:34]:34s} {billions:>10s}  {why[:78]}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show the eligible universe for a date.")
    parser.add_argument("--date", type=date.fromisoformat, required=True)
    parser.add_argument("--top", type=int, default=25)
    parser.add_argument("--config", type=Path, default=Path("config/universe/default.yaml"))
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings, level=settings.log_level)
    universe = load_config(args.config, UniverseConfig)
    with correlation_scope(), make_engine(settings, DbRole.APP).connect() as conn:
        transaction = conn.begin()
        try:
            result = build_universe(conn, args.date, universe)
            top = largest_members(conn, result, universe.config.sharia_methodology, args.top)
        finally:
            transaction.rollback()  # a demonstration: nothing is stored
    print(render(result, universe.ref.version, universe.config.sharia_methodology, top))
    return 0


if __name__ == "__main__":
    sys.exit(main())
