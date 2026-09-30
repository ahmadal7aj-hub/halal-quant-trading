"""Task 14: the eligible universe (PRD critical Tests 1, 2, 3 and 10 at universe level).

Made-up securities, prices, classifications and quality findings in a rolled-back transaction.
Every build is restricted to the test's own securities because the database holds real data too.
"""

import uuid
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, select
from sqlalchemy.exc import ProgrammingError

from halal_quant.audit import audit_event_table
from halal_quant.core.config import UniverseConfig, load_config
from halal_quant.core.settings import DbRole
from halal_quant.data.market_data import DailyPrice, add_prices
from halal_quant.data.quality import data_quality_finding_table, data_quality_run_table
from halal_quant.data.security_master import SecurityInfo, create_security, mark_delisted
from halal_quant.sharia.classification import PROVIDER, classification_table
from halal_quant.universe import builder
from halal_quant.universe.builder import (
    UniverseError,
    build_universe,
    content_hash,
    liquidity_window,
    staleness_cutoff,
    stored_members,
    universe_build_table,
    universe_member_table,
)

ROOT = Path(__file__).parents[2]
UNIVERSE = load_config(ROOT / "config" / "universe" / "default.yaml", UniverseConfig)
AS_OF = date(2020, 3, 2)  # a Monday
WINDOW = liquidity_window(AS_OF, 20)
METHOD = UNIVERSE.config.sharia_methodology


@pytest.fixture
def conn(engines: dict[DbRole, Engine]) -> Iterator[Connection]:
    with engines[DbRole.APP].connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


@pytest.fixture
def run_id(conn: Connection) -> int:
    """A data-quality run covering the liquidity window."""
    return int(
        conn.execute(
            data_quality_run_table.insert()
            .values(first_date=WINDOW[0], last_date=WINDOW[-1], thresholds={})
            .returning(data_quality_run_table.c.id)
        ).scalar_one()
    )


def make_security(
    conn: Connection, name: str | None = None, category: str = "Domestic Common Stock"
) -> int:
    info = SecurityInfo(
        source="test",
        source_id=uuid.uuid4().hex,
        company_name=name or f"Company {uuid.uuid4().hex[:8]}",
        category=category,
    )
    ticker = "ZQ" + uuid.uuid4().hex[:6].upper()
    return create_security(conn, info, ticker, date(2015, 1, 2), "test", "test")


def classify(
    conn: Connection,
    security_id: int,
    status: str = "HALAL",
    effective_from: date = date(2020, 3, 2),  # from the 28 Feb screening, for AS_OF = 2 Mar
    effective_to: date = date(2020, 3, 31),
) -> None:
    conn.execute(
        classification_table.insert().values(
            security_id=security_id,
            provider=PROVIDER,
            methodology=METHOD,
            status=status,
            effective_from=effective_from,
            effective_to=effective_to,
            screening_date=effective_from - timedelta(days=3),
            reason="test",
            details={},
            source_reference={},
        )
    )


def trade(
    conn: Connection,
    security_id: int,
    days: list[date] | None = None,
    close: str = "10",
    unadjusted: str | None = None,
    volume: int = 200_000,
) -> None:
    """Prices for each of `days` (default: the whole liquidity window)."""
    rows = [
        DailyPrice.model_validate(
            {
                "security_id": security_id,
                "price_date": day,
                "open": Decimal(close),
                "high": Decimal(close),
                "low": Decimal(close),
                "close": Decimal(close),
                "adjusted_close": Decimal(close),
                "close_unadjusted": Decimal(unadjusted or close),
                "volume": volume,
                "source": "test",
                "data_version": "v",
            }
        )
        for day in (days if days is not None else WINDOW)
    ]
    add_prices(conn, rows)


def eligible(conn: Connection, *security_ids: int) -> builder.UniverseResult:
    return build_universe(conn, AS_OF, UNIVERSE, security_ids=list(security_ids))


def test_a_halal_liquid_fresh_security_is_in_the_universe_and_stored(
    conn: Connection, run_id: int
) -> None:
    sid = make_security(conn)
    classify(conn, sid)
    trade(conn, sid)
    result = eligible(conn, sid)
    assert result.members == [sid] and not result.reused
    assert len(result.content_sha256) == 64 and stored_members(conn, result.build_id) == [sid]
    b = universe_build_table.c
    build = (
        conn.execute(select(universe_build_table).where(b.id == result.build_id)).mappings().one()
    )
    assert build["as_of"] == AS_OF and build["methodology"] == "AAOIFI-v1"
    assert build["universe_version"] == "universe-v1" and build["member_count"] == 1
    assert build["data_quality_run_id"] == run_id and build["config_sha256"] == UNIVERSE.ref.sha256


def test_test_1_a_non_halal_security_is_excluded(conn: Connection, run_id: int) -> None:
    sid = make_security(conn)
    classify(conn, sid, "NON_HALAL")
    trade(conn, sid)
    result = eligible(conn, sid)
    assert result.members == [] and result.exclusions["sharia_non_halal"] == 1


def test_test_2_unknown_pending_and_unclassified_securities_are_excluded(
    conn: Connection, run_id: int
) -> None:
    unknown, pending, none = make_security(conn), make_security(conn), make_security(conn)
    classify(conn, unknown, "UNKNOWN")
    classify(conn, pending, "PENDING_REVIEW")
    for sid in (unknown, pending, none):
        trade(conn, sid)
    result = eligible(conn, unknown, pending, none)
    assert result.members == []
    assert dict(result.exclusions) == {
        "sharia_unknown": 1,
        "sharia_pending_review": 1,
        "sharia_no_record": 1,
    }


def test_test_3_a_stale_price_is_excluded_and_one_at_the_cutoff_is_not(
    conn: Connection, run_id: int
) -> None:
    cutoff = staleness_cutoff(AS_OF, 5)
    assert cutoff == date(2020, 2, 24)
    stale, fresh = make_security(conn), make_security(conn)
    for sid in (stale, fresh):
        classify(conn, sid)
    trade(conn, stale, [d for d in WINDOW if d < cutoff])  # last price the day before the cutoff
    trade(conn, fresh, [d for d in WINDOW if d <= cutoff])  # last price exactly on the cutoff
    result = eligible(conn, stale, fresh)
    assert result.members == [fresh]
    assert dict(result.exclusions) == {"stale_price": 1}


def test_test_10_nothing_from_on_or_after_the_decision_date_is_used(
    conn: Connection, run_id: int
) -> None:
    future_record, expired_record, future_price = (make_security(conn) for _ in range(3))
    classify(conn, future_record, effective_from=date(2020, 3, 3), effective_to=date(2020, 4, 1))
    classify(conn, expired_record, effective_from=date(2020, 1, 2), effective_to=date(2020, 1, 31))
    classify(conn, future_price)
    for sid in (future_record, expired_record):
        trade(conn, sid)
    trade(conn, future_price, [AS_OF, AS_OF + timedelta(days=1)])  # only prices on/after as_of
    result = eligible(conn, future_record, expired_record, future_price)
    assert result.members == []
    assert result.exclusions["sharia_no_record"] == 2 and result.exclusions["stale_price"] == 1


def test_the_minimum_price_uses_the_price_actually_paid_and_is_inclusive(
    conn: Connection, run_id: int
) -> None:
    cheap, at_limit = make_security(conn), make_security(conn)
    for sid in (cheap, at_limit):
        classify(conn, sid)
    # split-adjusted close is high but the price paid was under $5
    trade(conn, cheap, close="50", unadjusted="4.99", volume=100_000)
    trade(conn, at_limit, close="5", unadjusted="5.00", volume=1_000_000)
    result = eligible(conn, cheap, at_limit)
    assert result.members == [at_limit] and result.exclusions["below_min_price"] == 1


def test_liquidity_is_the_median_daily_dollar_volume_and_a_missing_day_counts_as_zero(
    conn: Connection, run_id: int
) -> None:
    exactly, below, gappy = make_security(conn), make_security(conn), make_security(conn)
    for sid in (exactly, below, gappy):
        classify(conn, sid)
    trade(conn, exactly, volume=100_000)  # 10 x 100,000 = exactly 1,000,000 a day
    trade(conn, below, volume=99_999)
    trade(conn, gappy, WINDOW[-9:], volume=1_000_000)  # 9 traded days, 11 missing: median 0
    result = eligible(conn, exactly, below, gappy)
    assert result.members == [exactly]
    assert result.exclusions["below_min_liquidity"] == 2


def test_a_data_quality_finding_in_the_window_excludes_but_one_outside_does_not(
    conn: Connection, run_id: int
) -> None:
    flagged, old_finding = make_security(conn), make_security(conn)
    for sid in (flagged, old_finding):
        classify(conn, sid)
        trade(conn, sid)
    for sid, day in ((flagged, WINDOW[5]), (old_finding, WINDOW[0] - timedelta(days=10))):
        conn.execute(
            data_quality_finding_table.insert().values(
                run_id=run_id,
                security_id=sid,
                check_name="abnormal_jump",
                data_date=day,
                detail="x",
            )
        )
    result = eligible(conn, flagged, old_finding)
    assert result.members == [old_finding] and result.exclusions["data_quality_finding"] == 1


def test_no_quality_run_covering_the_window_means_no_universe(conn: Connection) -> None:
    sid = make_security(conn)
    classify(conn, sid)
    trade(conn, sid)
    with pytest.raises(UniverseError, match="No data-quality run covers"):
        eligible(conn, sid)


def test_only_the_most_liquid_share_class_of_a_company_is_kept(
    conn: Connection, run_id: int
) -> None:
    name = f"Dual Class {uuid.uuid4().hex[:6]}"
    a, b, c = (make_security(conn, name if i < 3 else None) for i in range(3))
    for sid in (a, b, c):
        classify(conn, sid)
    trade(conn, a, volume=200_000)
    trade(conn, b, volume=400_000)  # the most liquid class
    trade(conn, c, volume=400_000)  # ties with b: the lower security_id wins
    result = eligible(conn, a, b, c)
    assert result.members == [b] and result.exclusions["duplicate_share_class"] == 2


def test_only_allowed_categories_and_securities_still_trading_are_considered(
    conn: Connection, run_id: int
) -> None:
    common, preferred, delisted = (
        make_security(conn),
        make_security(conn, category="Domestic Preferred Stock"),
        make_security(conn),
    )
    for sid in (common, preferred, delisted):
        classify(conn, sid)
        trade(conn, sid)
    mark_delisted(conn, delisted, date(2020, 2, 14), "test", "test")
    result = eligible(conn, common, preferred, delisted)
    assert result.members == [common]
    assert sum(result.exclusions.values()) == 0  # never candidates, so not counted as exclusions


def test_the_same_inputs_give_the_same_stored_universe(conn: Connection, run_id: int) -> None:
    sid = make_security(conn)
    classify(conn, sid)
    trade(conn, sid)
    first = eligible(conn, sid)
    second = eligible(conn, sid)
    assert second.reused and second.build_id == first.build_id
    assert second.content_sha256 == first.content_sha256 and second.members == first.members
    count = conn.execute(select(universe_build_table.c.id)).all()
    assert len([r for r in count if r.id == first.build_id]) == 1


def test_a_different_configuration_or_quality_run_gives_a_different_hash(
    conn: Connection, run_id: int
) -> None:
    base = content_hash(AS_OF, "AAOIFI-v1", "c1", 1, [1, 2], ["A"])
    assert base == content_hash(AS_OF, "AAOIFI-v1", "c1", 1, [2, 1], ["A"])  # order-independent
    assert base != content_hash(AS_OF, "AAOIFI-v1", "c2", 1, [1, 2], ["A"])
    assert base != content_hash(AS_OF, "AAOIFI-v1", "c1", 2, [1, 2], ["A"])
    assert base != content_hash(AS_OF, "AAOIFI-v1", "c1", 1, [1], ["A"])
    assert base != content_hash(date(2020, 3, 3), "AAOIFI-v1", "c1", 1, [1, 2], ["A"])
    assert base != content_hash(AS_OF, "AAOIFI-v2", "c1", 1, [1, 2], ["A"])
    assert base != content_hash(AS_OF, "AAOIFI-v1", "c1", 1, [1, 2], ["B"])


def test_the_build_is_audited_and_the_app_role_cannot_change_it(
    conn: Connection, run_id: int
) -> None:
    sid = make_security(conn)
    classify(conn, sid)
    trade(conn, sid)
    result = eligible(conn, sid)
    a = audit_event_table.c
    event = (
        conn.execute(
            select(audit_event_table).where(
                a.action == "universe.built", a.entity_id == str(result.build_id)
            )
        )
        .mappings()
        .one()
    )
    assert event["details"]["member_count"] == 1
    assert event["details"]["content_sha256"] == result.content_sha256
    for table in (universe_build_table, universe_member_table):
        for statement in (table.delete(),):
            with pytest.raises(ProgrammingError, match="permission denied"), conn.begin_nested():
                conn.execute(statement)
    with pytest.raises(ProgrammingError, match="permission denied"), conn.begin_nested():
        conn.execute(universe_build_table.update().values(member_count=99))


def test_the_liquidity_window_is_the_trading_days_before_the_date() -> None:
    assert len(WINDOW) == 20 and sorted(WINDOW) == WINDOW
    assert WINDOW[-1] == date(2020, 2, 28) and AS_OF not in WINDOW
    assert date(2020, 2, 17) not in WINDOW  # Presidents Day: the NYSE was closed
