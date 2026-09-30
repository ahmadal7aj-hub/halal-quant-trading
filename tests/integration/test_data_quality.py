"""Task 12: every data-quality check catches its deliberately broken fixture, and nothing is hidden.

Made-up securities and rows in a rolled-back transaction. Findings are filtered to the test's own
security because the checks look at the whole database in the date range.
"""

import uuid
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Connection, Engine, select
from sqlalchemy.exc import ProgrammingError

from halal_quant.audit import audit_event_table
from halal_quant.core.settings import DbRole
from halal_quant.data import quality
from halal_quant.data.fundamentals import daily_market_cap_table, fundamental_table
from halal_quant.data.market_data import (
    CorporateAction,
    DailyPrice,
    add_corporate_actions,
    add_prices,
)
from halal_quant.data.quality import (
    ENFORCED_BY_DATABASE,
    Finding,
    QualityResult,
    Thresholds,
    data_quality_finding_table,
    data_quality_run_table,
    render_report,
    run_checks,
)
from halal_quant.data.security_master import SecurityInfo, create_security, mark_delisted

START = date(2010, 1, 4)
FIRST, LAST = date(2011, 3, 1), date(2011, 3, 31)


@pytest.fixture
def conn(engines: dict[DbRole, Engine]) -> Iterator[Connection]:
    with engines[DbRole.APP].connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


@pytest.fixture
def security_id(conn: Connection) -> int:
    info = SecurityInfo(source="test", source_id=uuid.uuid4().hex, company_name="Quality Corp")
    ticker = "ZQ" + uuid.uuid4().hex[:6].upper()
    return create_security(conn, info, ticker, START, actor="test", reason="test")


def price(security_id: int, day: date, **kw: object) -> DailyPrice:
    base: dict[str, object] = {
        "security_id": security_id,
        "price_date": day,
        "open": Decimal("10"),
        "high": Decimal("11"),
        "low": Decimal("9"),
        "close": Decimal("10"),
        "adjusted_close": Decimal("10"),
        "close_unadjusted": Decimal("10"),
        "volume": 1000,
        "source": "test",
        "data_version": "v",
    }
    return DailyPrice.model_validate({**base, **kw})


def mine(findings: object, security_id: int) -> list[Finding]:
    return [f for f in findings if f.security_id == security_id]  # type: ignore[attr-defined]


def run_check(
    check: quality.Check, conn: Connection, security_id: int, first: date = FIRST, last: date = LAST
) -> list[Finding]:
    return mine(list(check(conn, first, last, Thresholds())), security_id)


def test_inconsistent_ohlc_is_found(conn: Connection, security_id: int) -> None:
    add_prices(
        conn,
        [
            price(security_id, date(2011, 3, 1)),  # fine
            price(security_id, date(2011, 3, 2), high=Decimal("8"), low=Decimal("9")),
            price(security_id, date(2011, 3, 3), close=Decimal("12")),  # above the high
        ],
    )
    found = run_check(quality.check_ohlc_inconsistent, conn, security_id)
    assert {f.data_date for f in found} == {date(2011, 3, 2), date(2011, 3, 3)}


def test_prices_on_closed_days_and_in_the_future_are_found(
    conn: Connection, security_id: int
) -> None:
    add_prices(
        conn,
        [
            price(security_id, date(2011, 3, 4)),  # a Friday: fine
            price(security_id, date(2011, 3, 5)),  # a Saturday
            price(security_id, date(2099, 1, 4)),  # the future
        ],
    )
    found = run_check(quality.check_bad_dates, conn, security_id, FIRST, date(2099, 12, 31))
    by_date = {f.data_date: f.detail for f in found}
    assert set(by_date) == {date(2011, 3, 5), date(2099, 1, 4)}
    assert "closed" in by_date[date(2011, 3, 5)] and "future" in by_date[date(2099, 1, 4)]


def test_an_abnormal_jump_is_found_and_a_normal_move_is_not(
    conn: Connection, security_id: int
) -> None:
    add_prices(
        conn,
        [
            price(security_id, date(2011, 3, 1), close=Decimal("10"), open=Decimal("10")),
            price(security_id, date(2011, 3, 2), close=Decimal("11"), high=Decimal("11")),
            price(security_id, date(2011, 3, 3), close=Decimal("22"), high=Decimal("22")),
        ],
    )
    found = run_check(quality.check_abnormal_jumps, conn, security_id)
    assert [f.data_date for f in found] == [date(2011, 3, 3)] and "+100.0%" in found[0].detail


def test_a_jump_across_the_start_of_the_range_is_still_seen(
    conn: Connection, security_id: int
) -> None:
    add_prices(
        conn,
        [
            price(security_id, date(2011, 2, 28), close=Decimal("10"), open=Decimal("10")),
            price(security_id, date(2011, 3, 1), close=Decimal("30"), high=Decimal("30")),
        ],
    )
    found = run_check(quality.check_abnormal_jumps, conn, security_id)
    assert [f.data_date for f in found] == [date(2011, 3, 1)]


def test_a_long_gap_between_prices_is_found_and_a_short_one_is_not(
    conn: Connection, security_id: int
) -> None:
    add_prices(
        conn,
        [
            price(security_id, date(2011, 3, 1)),
            price(security_id, date(2011, 3, 4)),  # a normal weekend-sized hole
            price(security_id, date(2011, 3, 22)),  # 11 trading days missing
        ],
    )
    found = run_check(quality.check_price_gaps, conn, security_id)
    assert [f.data_date for f in found] == [date(2011, 3, 22)]
    assert "11 trading days" in found[0].detail


def test_a_split_like_move_without_a_split_action_is_found(
    conn: Connection, security_id: int
) -> None:
    add_prices(
        conn,
        [
            price(security_id, date(2011, 3, 1), close_unadjusted=Decimal("100")),
            price(security_id, date(2011, 3, 2), close_unadjusted=Decimal("50")),
        ],
    )
    found = run_check(quality.check_possible_missing_splits, conn, security_id)
    assert [f.data_date for f in found] == [date(2011, 3, 2)]


def test_a_split_like_move_with_a_split_action_nearby_is_explained(
    conn: Connection, security_id: int
) -> None:
    add_prices(
        conn,
        [
            price(security_id, date(2011, 3, 1), close_unadjusted=Decimal("100")),
            price(security_id, date(2011, 3, 2), close_unadjusted=Decimal("50")),
        ],
    )
    split = CorporateAction(
        security_id=security_id,
        action_type="SPLIT",
        effective_date=date(2011, 3, 2),
        value=Decimal("2"),
        source="test",
        data_version="v",
    )
    add_corporate_actions(conn, [split])
    assert run_check(quality.check_possible_missing_splits, conn, security_id) == []


def test_a_price_before_the_ticker_existed_or_after_delisting_is_found(
    conn: Connection, security_id: int
) -> None:
    add_prices(
        conn,
        [
            price(security_id, date(2009, 12, 31)),  # before the security had a ticker
            price(security_id, date(2011, 3, 1)),  # fine
            price(security_id, date(2011, 3, 10)),  # after the delisting below
        ],
    )
    mark_delisted(conn, security_id, date(2011, 3, 4), actor="test", reason="test")
    found = run_check(
        quality.check_price_outside_ticker_history, conn, security_id, date(2009, 1, 1), LAST
    )
    assert {f.data_date for f in found} == {date(2009, 12, 31), date(2011, 3, 10)}


def fundamental_row(security_id: int, **kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "security_id": security_id,
        "dimension": "ARQ",
        "calendar_date": date(2011, 3, 31),
        "filing_date": date(2011, 5, 2),
        "report_period": date(2011, 3, 31),
        "source": "test",
        "data_version": "v",
        "debt": None,
        "cash_and_equivalents": None,
        "investments_noncurrent": None,
    }
    return {**base, **kw}


def test_a_report_filed_before_its_period_ended_is_found(
    conn: Connection, security_id: int
) -> None:
    conn.execute(
        fundamental_table.insert(),
        [
            fundamental_row(security_id),  # fine
            fundamental_row(
                security_id,
                calendar_date=date(2011, 6, 30),
                report_period=date(2011, 6, 30),
                filing_date=date(2011, 3, 15),
            ),
        ],
    )
    found = run_check(quality.check_fundamentals_timing, conn, security_id)
    assert [f.data_date for f in found] == [date(2011, 3, 15)]


def test_negative_debt_cash_or_investments_are_found(conn: Connection, security_id: int) -> None:
    conn.execute(
        fundamental_table.insert(),
        [
            fundamental_row(security_id, debt=Decimal("-1")),
            fundamental_row(
                security_id,
                dimension="ARY",
                filing_date=date(2011, 3, 10),
                cash_and_equivalents=Decimal("5"),
            ),
            fundamental_row(
                security_id,
                dimension="ART",
                filing_date=date(2011, 3, 11),
                investments_noncurrent=Decimal("-2"),
            ),
        ],
    )
    found = run_check(
        quality.check_negative_balance_sheet,
        conn,
        security_id,
        date(2011, 1, 1),
        date(2011, 12, 31),
    )
    assert {f.data_date for f in found} == {date(2011, 5, 2), date(2011, 3, 11)}


def test_a_zero_or_negative_market_value_is_found(conn: Connection, security_id: int) -> None:
    lineage = {"security_id": security_id, "source": "test", "data_version": "v"}
    conn.execute(
        daily_market_cap_table.insert(),
        [
            {**lineage, "cap_date": date(2011, 3, 1), "market_cap_usd": Decimal("100")},
            {**lineage, "cap_date": date(2011, 3, 2), "market_cap_usd": Decimal("0")},
            {**lineage, "cap_date": date(2011, 3, 3), "market_cap_usd": Decimal("-5")},
        ],
    )
    found = run_check(quality.check_non_positive_market_value, conn, security_id)
    assert {f.data_date for f in found} == {date(2011, 3, 2), date(2011, 3, 3)}


def test_a_security_whose_latest_price_is_old_is_stale_and_one_without_prices_too(
    conn: Connection, security_id: int
) -> None:
    add_prices(conn, [price(security_id, date(2011, 3, 1))])
    info = SecurityInfo(source="test", source_id=uuid.uuid4().hex, company_name="No Price Corp")
    no_prices = create_security(conn, info, "ZQ" + uuid.uuid4().hex[:6].upper(), START, "test", "t")
    found = quality.check_stale_prices(conn, date(2011, 3, 31), Thresholds())
    stale = mine(found, security_id)
    assert len(stale) == 1 and "older than 5 trading days" in stale[0].detail
    assert mine(found, no_prices)[0].detail == "no price at all"
    assert mine(quality.check_stale_prices(conn, date(2011, 3, 4), Thresholds()), security_id) == []


def test_a_run_stores_every_finding_with_its_thresholds_and_is_audited(
    conn: Connection, security_id: int
) -> None:
    add_prices(
        conn,
        [
            price(security_id, date(2011, 3, 1), close=Decimal("10")),
            price(security_id, date(2011, 3, 2), close=Decimal("30"), high=Decimal("30")),
        ],
    )
    result = run_checks(conn, FIRST, LAST, as_of=date(2011, 3, 31))
    f = data_quality_finding_table.c
    stored = conn.execute(
        select(f.check_name, f.data_date).where(
            f.run_id == result.run_id, f.security_id == security_id
        )
    ).all()
    assert ("abnormal_jump", date(2011, 3, 2)) in stored
    assert ("stale_price", date(2011, 3, 31)) in stored
    assert result.counts["abnormal_jump"] >= 1 and result.total == sum(result.counts.values())
    run = (
        conn.execute(
            select(data_quality_run_table).where(data_quality_run_table.c.id == result.run_id)
        )
        .mappings()
        .one()
    )
    assert run["thresholds"]["max_daily_move"] == "0.5" and run["as_of"] == date(2011, 3, 31)
    a = audit_event_table.c
    event = (
        conn.execute(
            select(audit_event_table).where(
                a.action == "data_quality.run", a.entity_id == str(result.run_id)
            )
        )
        .mappings()
        .one()
    )
    assert event["details"]["findings_total"] == result.total


def test_the_report_lists_every_check_even_with_no_findings_and_what_the_database_enforces() -> (
    None
):
    result = QualityResult(run_id=7, checks_run=list(quality.CHECKS) + ["stale_price"])
    result.add(Finding(1, "price_gap", date(2011, 3, 22), "11 trading days with no price"))
    report = render_report(result, FIRST, LAST)
    for name in result.checks_run:
        assert f"| {name} |" in report
    assert "| price_gap | 1 |" in report and "| abnormal_jump | 0 |" in report
    assert "security 1 on 2011-03-22" in report
    for name in ENFORCED_BY_DATABASE:
        assert name in report


def test_examples_are_capped_but_counts_are_not() -> None:
    result = QualityResult()
    for i in range(20):
        result.add(Finding(i, "price_gap", FIRST + timedelta(days=i), "x"))
    assert result.counts["price_gap"] == 20
    assert len(result.examples["price_gap"]) == quality.EXAMPLES_PER_CHECK


def test_the_app_role_cannot_change_or_delete_findings(conn: Connection, security_id: int) -> None:
    add_prices(conn, [price(security_id, date(2011, 3, 5))])  # a Saturday
    run_checks(conn, FIRST, LAST)
    for table in (data_quality_finding_table, data_quality_run_table):
        changes = {"detail": "x"} if table is data_quality_finding_table else {"as_of": None}
        for statement in (table.update().values(**changes), table.delete()):
            with pytest.raises(ProgrammingError, match="permission denied"), conn.begin_nested():
                conn.execute(statement)


def test_the_same_year_range_is_split_into_calendar_years() -> None:
    assert quality.year_ranges(2019, 2020) == [
        (date(2019, 1, 1), date(2019, 12, 31)),
        (date(2020, 1, 1), date(2020, 12, 31)),
    ]
