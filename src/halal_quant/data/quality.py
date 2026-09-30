"""Data-quality checks and report (task 12; BRD §18, §25; private doc 03 R3).

    uv run python -m halal_quant.data.quality [--start 1998] [--end 2026] [--as-of 2026-09-30]
                                               [--report path.md]

Every check looks for one kind of problem and returns findings: which security, which date, what
is wrong. Nothing is fixed, hidden or dropped: findings are stored (add-only) against a run, and a
later step (the universe builder, task 14) treats a security with a finding on a date as unusable
on that date (fail closed). The report lists the count of every check, including checks that found
nothing, so silence never means "not looked at".

Some problems cannot be stored at all, because the database refuses them: duplicate prices and
non-positive prices. They are listed in the report as enforced by the database.

Thresholds are parameters of the run and are stored with it, so a finding can be reproduced.
"""

import argparse
import sys
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import batched
from pathlib import Path
from typing import Any

from sqlalchemy import (
    BigInteger,
    Column,
    Connection,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Table,
    Text,
    exists,
    func,
    or_,
    select,
    text,
    type_coerce,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql.expression import ColumnElement, Subquery

from halal_quant.audit import AuditEvent, record_event
from halal_quant.core.logging import configure_logging, correlation_scope
from halal_quant.core.settings import DbRole, get_settings
from halal_quant.data.calendar import (
    MAX_YEAR,
    MIN_YEAR,
    is_trading_day,
    previous_trading_day,
    trading_days_between,
)
from halal_quant.data.market_data import corporate_action_table, daily_price_table
from halal_quant.db.engine import make_engine, metadata

ACTOR = "system:data-quality"
INSERT_BATCH = 5000
EXAMPLES_PER_CHECK = 5
LOOKBACK_DAYS = 30  # how far before `first` a window check looks for the previous price

ENFORCED_BY_DATABASE = {
    "duplicate_price": "primary key (security_id, price_date)",
    "non_positive_price": "check constraints on every price column",
    "negative_volume": "check constraint on volume",
    "duplicate_fundamental": "unique constraint on (security, dimension, period, filing date)",
    "overlapping_ticker_history": "exclusion constraints on security_ticker",
}

data_quality_run_table = Table(
    "data_quality_run",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("first_date", Date, nullable=False),
    Column("last_date", Date, nullable=False),
    Column("as_of", Date),
    Column("thresholds", JSONB, nullable=False),
    Column("checked_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

data_quality_finding_table = Table(
    "data_quality_finding",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column(
        "run_id",
        BigInteger,
        ForeignKey("data_quality_run.id", name="fk_data_quality_finding_run"),
        nullable=False,
    ),
    Column(
        "security_id",
        BigInteger,
        ForeignKey("security.security_id", name="fk_data_quality_finding_security"),
        nullable=False,
    ),
    Column("check_name", Text, nullable=False),
    Column("data_date", Date, nullable=False),
    Column("detail", Text, nullable=False),
    Index("ix_data_quality_finding_lookup", "security_id", "data_date"),
    Index("ix_data_quality_finding_run", "run_id", "check_name"),
)


@dataclass(frozen=True)
class Thresholds:
    max_daily_move: Decimal = Decimal("0.5")  # |close / previous close - 1| above this is a jump
    max_gap_trading_days: int = 5  # more missing trading days between two prices is a gap
    split_ratio: Decimal = Decimal(
        "1.8"
    )  # unadjusted close moving by this factor looks like a split
    split_window_days: int = 3  # a SPLIT action within this many days explains such a move
    max_price_age_trading_days: int = 5  # the same as the approved universe-v1 staleness limit

    def as_json(self) -> dict[str, object]:
        return {k: str(v) for k, v in asdict(self).items()}


@dataclass(frozen=True)
class Finding:
    security_id: int
    check_name: str
    data_date: date
    detail: str


@dataclass
class QualityResult:
    run_id: int | None = None
    checks_run: list[str] = field(default_factory=list)
    counts: Counter[str] = field(default_factory=Counter)
    examples: dict[str, list[Finding]] = field(default_factory=dict)

    def add(self, finding: Finding) -> None:
        self.counts[finding.check_name] += 1
        bucket = self.examples.setdefault(finding.check_name, [])
        if len(bucket) < EXAMPLES_PER_CHECK:
            bucket.append(finding)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


Check = Callable[[Connection, date, date, Thresholds], Iterable[Finding]]


def _with_previous(first: date, last: date) -> Subquery:
    """Each price row with the previous row of the same security (looking back before `first`)."""
    p = daily_price_table.c

    def previous(column: ColumnElement[Any]) -> ColumnElement[Any]:
        return func.lag(column).over(partition_by=p.security_id, order_by=p.price_date)

    return (
        select(
            p.security_id,
            p.price_date,
            p.close,
            p.close_unadjusted,
            previous(p.close).label("prev_close"),
            previous(p.close_unadjusted).label("prev_unadjusted"),
            previous(p.price_date).label("prev_date"),
        )
        .where(p.price_date.between(first - timedelta(days=LOOKBACK_DAYS), last))
        .subquery()
    )


def check_ohlc_inconsistent(
    conn: Connection, first: date, last: date, _: Thresholds
) -> Iterable[Finding]:
    """High below low, or open / close outside the day's range."""
    rows = conn.execute(
        text(
            """
            SELECT security_id, price_date, open, high, low, close
            FROM hq.daily_price
            WHERE price_date BETWEEN :first AND :last
              AND (high < low OR open > high OR open < low OR close > high OR close < low)
            """
        ),
        {"first": first, "last": last},
    )
    for r in rows:
        yield Finding(
            r.security_id,
            "ohlc_inconsistent",
            r.price_date,
            f"open {r.open}, high {r.high}, low {r.low}, close {r.close}",
        )


def check_bad_dates(conn: Connection, first: date, last: date, _: Thresholds) -> Iterable[Finding]:
    """Prices dated on a day the exchange was closed, or in the future."""
    today = datetime.now(UTC).date()
    dates = [
        r.price_date
        for r in conn.execute(
            text(
                "SELECT DISTINCT price_date FROM hq.daily_price "
                "WHERE price_date BETWEEN :first AND :last"
            ),
            {"first": first, "last": last},
        )
    ]
    bad: dict[date, str] = {}
    for day in dates:
        if day > today:
            bad[day] = "dated in the future"
        elif MIN_YEAR <= day.year <= MAX_YEAR and not is_trading_day(day):
            bad[day] = "dated on a day the NYSE was closed"
    if not bad:
        return
    rows = conn.execute(
        text(
            "SELECT security_id, price_date FROM hq.daily_price WHERE price_date = ANY(:days)"
        ).bindparams(days=list(bad))
    )
    for r in rows:
        yield Finding(r.security_id, "bad_price_date", r.price_date, bad[r.price_date])


def check_abnormal_jumps(
    conn: Connection, first: date, last: date, t: Thresholds
) -> Iterable[Finding]:
    """A split-adjusted close that moved by more than the threshold in one day."""
    w = _with_previous(first, last)
    rows = conn.execute(
        select(w).where(
            w.c.price_date >= first,
            w.c.prev_close > 0,
            func.abs(w.c.close / w.c.prev_close - 1) > t.max_daily_move,
        )
    )
    for r in rows:
        move = (r.close / r.prev_close - 1) * 100
        yield Finding(
            r.security_id,
            "abnormal_jump",
            r.price_date,
            f"close {r.prev_close} -> {r.close} ({move:+.1f}%) since {r.prev_date}",
        )


def check_price_gaps(conn: Connection, first: date, last: date, t: Thresholds) -> Iterable[Finding]:
    """More trading days missing between two consecutive prices than the threshold allows."""
    w = _with_previous(first, last)
    calendar_days = type_coerce(w.c.price_date - w.c.prev_date, Integer)
    rows = conn.execute(
        select(w.c.security_id, w.c.price_date, w.c.prev_date).where(
            w.c.price_date >= first,
            w.c.prev_date.is_not(None),
            calendar_days > t.max_gap_trading_days + 2,
        )
    )
    for r in rows:
        if not (
            MIN_YEAR <= r.prev_date.year <= MAX_YEAR and MIN_YEAR <= r.price_date.year <= MAX_YEAR
        ):
            continue
        missing = trading_days_between(r.prev_date, r.price_date) - 1
        if missing > t.max_gap_trading_days:
            yield Finding(
                r.security_id,
                "price_gap",
                r.price_date,
                f"{missing} trading days with no price since {r.prev_date}",
            )


def check_possible_missing_splits(
    conn: Connection, first: date, last: date, t: Thresholds
) -> Iterable[Finding]:
    """The price actually paid jumped like a split, with no SPLIT action nearby."""
    w = _with_previous(first, last)
    a = corporate_action_table.c
    near = timedelta(days=t.split_window_days)
    explained = exists().where(
        a.security_id == w.c.security_id,
        a.action_type == "SPLIT",
        a.effective_date.between(w.c.price_date - near, w.c.price_date + near),
    )
    rows = conn.execute(
        select(w).where(
            w.c.price_date >= first,
            w.c.prev_unadjusted > 0,
            w.c.close_unadjusted > 0,
            or_(
                w.c.prev_unadjusted / w.c.close_unadjusted >= t.split_ratio,
                w.c.close_unadjusted / w.c.prev_unadjusted >= t.split_ratio,
            ),
            ~explained,
        )
    )
    for r in rows:
        yield Finding(
            r.security_id,
            "possible_missing_split",
            r.price_date,
            f"price paid {r.prev_unadjusted} -> {r.close_unadjusted} with no split action near",
        )


def check_price_outside_ticker_history(
    conn: Connection, first: date, last: date, _: Thresholds
) -> Iterable[Finding]:
    """A price on a day when the security had no ticker, or after its last trading day."""
    rows = conn.execute(
        text(
            """
            SELECT p.security_id, p.price_date, s.end_date,
                   EXISTS (SELECT 1 FROM hq.security_ticker t
                           WHERE t.security_id = p.security_id
                             AND t.valid_from <= p.price_date
                             AND (t.valid_to IS NULL OR p.price_date < t.valid_to)) AS has_ticker
            FROM hq.daily_price p JOIN hq.security s USING (security_id)
            WHERE p.price_date BETWEEN :first AND :last
              AND (NOT EXISTS (SELECT 1 FROM hq.security_ticker t
                               WHERE t.security_id = p.security_id
                                 AND t.valid_from <= p.price_date
                                 AND (t.valid_to IS NULL OR p.price_date < t.valid_to))
                   OR p.price_date > s.end_date)
            """
        ),
        {"first": first, "last": last},
    )
    for r in rows:
        reason = (
            f"after the security's last trading day {r.end_date}"
            if r.has_ticker
            else "no ticker was assigned to the security on this date"
        )
        yield Finding(r.security_id, "price_outside_ticker_history", r.price_date, reason)


def check_fundamentals_timing(
    conn: Connection, first: date, last: date, _: Thresholds
) -> Iterable[Finding]:
    """A report filed before the period it reports on had ended."""
    rows = conn.execute(
        text(
            """
            SELECT security_id, filing_date, dimension,
                   coalesce(report_period, calendar_date) AS period_end
            FROM hq.fundamental
            WHERE filing_date BETWEEN :first AND :last
              AND filing_date < coalesce(report_period, calendar_date)
            """
        ),
        {"first": first, "last": last},
    )
    for r in rows:
        yield Finding(
            r.security_id,
            "filing_before_period_end",
            r.filing_date,
            f"{r.dimension} filed {r.filing_date}, before its period ended {r.period_end}",
        )


def check_negative_balance_sheet(
    conn: Connection, first: date, last: date, _: Thresholds
) -> Iterable[Finding]:
    """Negative debt, cash or investments: bad data, and UNKNOWN in the Sharia screen (S1)."""
    rows = conn.execute(
        text(
            """
            SELECT security_id, filing_date, dimension
            FROM hq.fundamental
            WHERE filing_date BETWEEN :first AND :last
              AND (debt < 0 OR cash_and_equivalents < 0 OR investments < 0
                   OR investments_current < 0 OR investments_noncurrent < 0)
            """
        ),
        {"first": first, "last": last},
    )
    for r in rows:
        yield Finding(
            r.security_id,
            "negative_balance_sheet_value",
            r.filing_date,
            f"{r.dimension} filing has a negative debt, cash or investments value",
        )


def check_non_positive_market_value(
    conn: Connection, first: date, last: date, _: Thresholds
) -> Iterable[Finding]:
    """Market value of zero or less: UNKNOWN in the Sharia screen (S1)."""
    rows = conn.execute(
        text(
            "SELECT security_id, cap_date, market_cap_usd FROM hq.daily_market_cap "
            "WHERE cap_date BETWEEN :first AND :last AND market_cap_usd <= 0"
        ),
        {"first": first, "last": last},
    )
    for r in rows:
        yield Finding(
            r.security_id,
            "non_positive_market_value",
            r.cap_date,
            f"market value {r.market_cap_usd}",
        )


CHECKS: dict[str, Check] = {
    "ohlc_inconsistent": check_ohlc_inconsistent,
    "bad_price_date": check_bad_dates,
    "abnormal_jump": check_abnormal_jumps,
    "price_gap": check_price_gaps,
    "possible_missing_split": check_possible_missing_splits,
    "price_outside_ticker_history": check_price_outside_ticker_history,
    "filing_before_period_end": check_fundamentals_timing,
    "negative_balance_sheet_value": check_negative_balance_sheet,
    "non_positive_market_value": check_non_positive_market_value,
}


def check_stale_prices(conn: Connection, as_of: date, t: Thresholds) -> list[Finding]:
    """Active securities whose latest price is older than the staleness limit on `as_of`."""
    found: list[Finding] = []
    rows = conn.execute(
        text(
            """
            SELECT s.security_id,
                   (SELECT max(p.price_date) FROM hq.daily_price p
                    WHERE p.security_id = s.security_id AND p.price_date <= :as_of) AS last_price
            FROM hq.security s
            WHERE s.active_flag AND (s.start_date IS NULL OR s.start_date <= :as_of)
            """
        ),
        {"as_of": as_of},
    )
    cutoff = as_of
    for _ in range(t.max_price_age_trading_days):
        cutoff = previous_trading_day(cutoff)
    for r in rows:
        if r.last_price is None:
            found.append(Finding(r.security_id, "stale_price", as_of, "no price at all"))
        elif r.last_price < cutoff:  # older than the limit allows
            found.append(
                Finding(
                    r.security_id,
                    "stale_price",
                    as_of,
                    f"latest price {r.last_price} is older than "
                    f"{t.max_price_age_trading_days} trading days before {as_of}",
                )
            )
    return found


def year_ranges(first_year: int, last_year: int) -> list[tuple[date, date]]:
    return [(date(y, 1, 1), date(y, 12, 31)) for y in range(first_year, last_year + 1)]


def run_checks(
    conn: Connection,
    first: date,
    last: date,
    as_of: date | None = None,
    thresholds: Thresholds | None = None,
    actor: str = ACTOR,
) -> QualityResult:
    """Run every check over [first, last], store the findings and audit the run.

    Runs on the caller's connection: the caller commits, or rolls everything back. Long ranges
    are checked one calendar year at a time to keep memory bounded.
    """
    t = thresholds or Thresholds()
    result = QualityResult(checks_run=[*CHECKS, *(["stale_price"] if as_of else [])])
    run_id: int = conn.execute(
        data_quality_run_table.insert()
        .values(first_date=first, last_date=last, as_of=as_of, thresholds=t.as_json())
        .returning(data_quality_run_table.c.id)
    ).scalar_one()
    result.run_id = run_id

    def store(findings: Iterable[Finding]) -> None:
        for batch in batched(findings, INSERT_BATCH):
            for finding in batch:
                result.add(finding)
            conn.execute(
                data_quality_finding_table.insert(),
                [{"run_id": run_id, **asdict(f)} for f in batch],
            )

    for year_first, year_last in year_ranges(first.year, last.year):
        lo, hi = max(first, year_first), min(last, year_last)
        if lo > hi:
            continue
        for check in CHECKS.values():
            store(check(conn, lo, hi, t))
    if as_of:
        store(check_stale_prices(conn, as_of, t))

    record_event(
        conn,
        AuditEvent(
            actor=actor,
            action="data_quality.run",
            entity_type="data_quality_run",
            entity_id=str(run_id),
            reason="Data-quality checks (BRD §18)",
            source=__name__,
            details={
                "first_date": first,
                "last_date": last,
                "as_of": as_of,
                "thresholds": t.as_json(),
                "findings_by_check": dict(result.counts),
                "findings_total": result.total,
            },
        ),
    )
    return result


def render_report(result: QualityResult, first: date, last: date) -> str:
    """The R3 data-quality report: every check, its count and examples; nothing left out."""
    lines = [
        f"# Data-quality report, run {result.run_id}",
        "",
        f"Range {first} to {last}. Findings: **{result.total}**.",
        "",
        "| Check | Findings |",
        "|---|---|",
    ]
    lines += [f"| {name} | {result.counts.get(name, 0)} |" for name in result.checks_run]
    lines += ["", "## Examples", ""]
    for name in result.checks_run:
        for f in result.examples.get(name, []):
            lines.append(f"- `{name}` security {f.security_id} on {f.data_date}: {f.detail}")
    lines += ["", "## Enforced by the database (cannot be stored, so not checked here)", ""]
    lines += [f"- {name}: {how}" for name, how in ENFORCED_BY_DATABASE.items()]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the data-quality checks and print the report."
    )
    parser.add_argument("--start", type=int, default=MIN_YEAR, help="first year")
    parser.add_argument("--end", type=int, default=date.today().year, help="last year")
    parser.add_argument("--as-of", type=date.fromisoformat, default=None, help="staleness date")
    parser.add_argument("--report", type=Path, default=None, help="also write the report here")
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings, level=settings.log_level)
    first, last = date(args.start, 1, 1), date(args.end, 12, 31)
    with correlation_scope(), make_engine(settings, DbRole.APP).begin() as conn:
        result = run_checks(conn, first, last, args.as_of)
    report = render_report(result, first, last)
    print(report)
    if args.report:
        args.report.write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
