"""Task 11: fundamentals are point-in-time, add-only, audited and version-stamped (made-up data)."""

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import Connection, Engine, select
from sqlalchemy.exc import ProgrammingError

from halal_quant.audit import audit_event_table
from halal_quant.core.logging import correlation_scope
from halal_quant.core.settings import DbRole
from halal_quant.data.fundamentals import (
    daily_market_cap_table,
    fundamental_table,
    latest_filing_before,
    market_cap_on,
)
from halal_quant.data.security_master import SecurityInfo, create_security
from halal_quant.data.sharadar.client import Download
from halal_quant.data.sharadar.fundamentals import import_fundamentals
from halal_quant.data.sharadar.market_cap import import_market_cap


@pytest.fixture
def conn(engines: dict[DbRole, Engine]) -> Iterator[Connection]:
    with engines[DbRole.APP].connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


@pytest.fixture
def ticker() -> str:
    return "ZQ" + uuid.uuid4().hex[:6].upper()


@pytest.fixture
def security_id(conn: Connection, ticker: str) -> int:
    info = SecurityInfo(source="test", source_id=uuid.uuid4().hex, company_name="Filing Corp")
    return create_security(conn, info, ticker, date(2015, 1, 2), actor="test", reason="test")


def row(ticker: str, filed: str, period: str, dimension: str = "ARQ", **kw: str) -> dict[str, str]:
    base = {
        "ticker": ticker,
        "dimension": dimension,
        "calendardate": period,
        "date": filed,
        "reportperiod": period,
        "fiscalperiod": "2019-Q4",
        "debt": "100",
        "cashneq": "50",
        "revenue": "1000",
    }
    return {**base, **kw}


def download(endpoint: str, rows: list[dict[str, str]], sha: str = "a" * 64) -> Download:
    return Download(endpoint, rows, sha, datetime(2026, 9, 30, 8, 0, tzinfo=UTC))


def load(conn: Connection, rows: list[dict[str, str]], sha: str = "a" * 64) -> object:
    return import_fundamentals(conn, download("fundamentals", rows, sha), "t")


def test_a_filing_is_invisible_until_the_day_after_it_was_filed(
    conn: Connection, ticker: str, security_id: int
) -> None:
    filed = date(2019, 11, 1)
    load(conn, [row(ticker, "2019-11-01", "2019-09-30")])
    assert latest_filing_before(conn, security_id, "ARQ", date(2019, 10, 31)) is None
    assert latest_filing_before(conn, security_id, "ARQ", filed) is None  # not on the filing day
    seen = latest_filing_before(conn, security_id, "ARQ", date(2019, 11, 2))
    assert seen is not None and seen["filing_date"] == filed and seen["debt"] == Decimal("100")


def test_the_latest_filing_before_the_date_wins_and_later_ones_stay_hidden(
    conn: Connection, ticker: str, security_id: int
) -> None:
    load(
        conn,
        [
            row(ticker, "2019-05-01", "2019-03-31", debt="1"),
            row(ticker, "2019-08-01", "2019-06-30", debt="2"),
            row(ticker, "2019-11-01", "2019-09-30", debt="3"),
        ],
    )
    on = date(2019, 9, 1)
    found = latest_filing_before(conn, security_id, "ARQ", on)
    assert found is not None and found["debt"] == Decimal("2")
    assert latest_filing_before(conn, security_id, "ARY", on) is None  # other dimension: nothing


def test_running_the_same_fundamentals_again_adds_nothing(
    conn: Connection, ticker: str, security_id: int
) -> None:
    rows = [row(ticker, "2019-11-01", "2019-09-30")]
    first = import_fundamentals(conn, download("fundamentals", rows), "t")
    second = import_fundamentals(conn, download("fundamentals", rows), "t")
    assert (first.inserted, second.inserted, second.already_present) == (1, 0, 1)
    assert second.restated == 0 and second.needs_review == []


def test_a_changed_filing_is_reported_and_the_stored_row_is_kept(
    conn: Connection, ticker: str, security_id: int
) -> None:
    load(conn, [row(ticker, "2019-11-01", "2019-09-30")])
    changed = [row(ticker, "2019-11-01", "2019-09-30", debt="999")]
    result = import_fundamentals(conn, download("fundamentals", changed, "b" * 64), "t")
    assert (result.inserted, result.restated, len(result.needs_review)) == (0, 1, 1)
    kept = latest_filing_before(conn, security_id, "ARQ", date(2019, 11, 2))
    assert kept is not None and kept["debt"] == Decimal("100")


def test_restated_dimensions_and_unmatched_tickers_are_skipped_and_counted(
    conn: Connection, ticker: str, security_id: int
) -> None:
    rows = [
        row(ticker, "2019-11-01", "2019-09-30", dimension="MRQ"),
        row("ZQNOBODY", "2019-11-01", "2019-09-30"),
        row(ticker, "2019-11-01", "2019-09-30"),
    ]
    result = import_fundamentals(conn, download("fundamentals", rows), "t")
    assert result.inserted == 1
    assert result.skipped["not_an_as_reported_dimension"] == 1
    assert result.skipped["no_security_for_ticker_on_date"] == 1
    assert any("ZQNOBODY" in line for line in result.needs_review)


def test_market_value_is_stored_in_dollars_and_read_on_its_own_day(
    conn: Connection, ticker: str, security_id: int
) -> None:
    rows = [{"ticker": ticker, "date": "2019-11-05", "marketcap": "1142487.8"}]
    result = import_market_cap(conn, download("daily", rows), "t")
    assert result.inserted == 1
    assert market_cap_on(conn, security_id, date(2019, 11, 5)) == Decimal("1142487800000.0000")
    assert market_cap_on(conn, security_id, date(2019, 11, 6)) is None


def test_market_value_reruns_add_nothing_and_changes_are_reported(
    conn: Connection, ticker: str, security_id: int
) -> None:
    rows = [{"ticker": ticker, "date": "2019-11-05", "marketcap": "10"}]
    import_market_cap(conn, download("daily", rows), "t")
    again = import_market_cap(conn, download("daily", rows), "t")
    assert (again.inserted, again.already_present, again.restated) == (0, 1, 0)
    changed = [{"ticker": ticker, "date": "2019-11-05", "marketcap": "11"}]
    result = import_market_cap(conn, download("daily", changed, "b" * 64), "t")
    assert (result.inserted, result.restated) == (0, 1)


def test_every_run_is_audited_with_its_data_version(
    conn: Connection, ticker: str, security_id: int
) -> None:
    rows = [row(ticker, "2019-11-01", "2019-09-30")]
    with correlation_scope():
        result = import_fundamentals(conn, download("fundamentals", rows), "ARQ.2019")
    a = audit_event_table.c
    event = (
        conn.execute(
            select(audit_event_table).where(
                a.action == "import.completed", a.entity_id == result.data_version
            )
        )
        .mappings()
        .one()
    )
    assert event["entity_type"] == "import:sharadar.fundamentals"
    assert event["details"]["inserted"] == 1


def test_the_app_role_cannot_change_or_delete_stored_fundamentals(
    conn: Connection, ticker: str, security_id: int
) -> None:
    load(conn, [row(ticker, "2019-11-01", "2019-09-30")])
    cap = [{"ticker": ticker, "date": "2019-11-05", "marketcap": "1"}]
    import_market_cap(conn, download("daily", cap), "t")
    for table in (fundamental_table, daily_market_cap_table):
        for statement in (table.update().values(source="x"), table.delete()):
            with pytest.raises(ProgrammingError, match="permission denied"), conn.begin_nested():
                conn.execute(statement)


def test_market_value_skips_unmatched_tickers_and_duplicates_and_says_so(
    conn: Connection, ticker: str, security_id: int
) -> None:
    rows = [
        {"ticker": ticker, "date": "2019-11-05", "marketcap": "10"},
        {"ticker": ticker, "date": "2019-11-05", "marketcap": "10"},
        {"ticker": "ZQNOBODY", "date": "2019-11-05", "marketcap": "10"},
    ]
    result = import_market_cap(conn, download("daily", rows), "t")
    assert result.inserted == 1
    assert result.skipped["duplicate_in_download"] == 1
    assert result.skipped["no_security_for_ticker_on_date"] == 1
    assert any("ZQNOBODY" in line for line in result.needs_review)


def test_fundamentals_duplicates_in_one_download_are_counted_once(
    conn: Connection, ticker: str, security_id: int
) -> None:
    same = row(ticker, "2019-11-01", "2019-09-30")
    result = import_fundamentals(conn, download("fundamentals", [same, same]), "t")
    assert (result.inserted, result.skipped["duplicate_in_download"]) == (1, 1)


def test_review_lists_are_capped_when_many_filings_changed(
    conn: Connection, ticker: str, security_id: int
) -> None:
    originals = [
        row(ticker, f"{year}-{month + 1:02d}-15", f"{year}-{month:02d}-28")
        for year in range(2016, 2024)
        for month in (3, 6, 9)
    ]
    load(conn, originals)
    changed = [dict(r, debt="999") for r in originals]
    result = import_fundamentals(conn, download("fundamentals", changed, "b" * 64), "t")
    assert result.restated == len(originals)
    assert len(result.needs_review) == 20
