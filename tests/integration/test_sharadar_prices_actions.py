"""Task 10: prices and corporate actions are imported once, audited, version-stamped and add-only.

Made-up securities and rows, in a rolled-back transaction.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, select
from sqlalchemy.exc import ProgrammingError

from halal_quant.audit import audit_event_table
from halal_quant.core.logging import correlation_scope
from halal_quant.core.settings import DbRole
from halal_quant.data.market_data import corporate_action_table, daily_price_table
from halal_quant.data.security_master import SecurityInfo, create_security
from halal_quant.data.sharadar.actions import ActionResult, import_actions
from halal_quant.data.sharadar.client import Download
from halal_quant.data.sharadar.prices import PriceResult, import_prices


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
    info = SecurityInfo(source="test", source_id=uuid.uuid4().hex, company_name="Import Corp")
    return create_security(conn, info, ticker, date(2020, 1, 2), actor="test", reason="test")


def price_row(ticker: str, day: str = "2024-03-04", **kw: str) -> dict[str, str]:
    base = {
        "ticker": ticker,
        "date": day,
        "open": "10",
        "high": "11",
        "low": "9.5",
        "close": "10.5",
        "volume": "1000",
        "closeadj": "10.4",
        "closeunadj": "21",
        "lastupdated": "2026-08-10",
    }
    return {**base, **kw}


def action_row(
    ticker: str, action: str = "dividend", value: str = "0.5", **kw: str
) -> dict[str, str]:
    base = {
        "date": "2024-03-04",
        "action": action,
        "ticker": ticker,
        "name": "Import Corp",
        "value": value,
        "contraticker": "N/A",
        "contraname": "N/A",
    }
    return {**base, **kw}


def download(endpoint: str, rows: list[dict[str, str]], sha: str = "a" * 64) -> Download:
    return Download(endpoint, rows, sha, datetime(2026, 9, 30, 8, 0, tzinfo=UTC))


def run_prices(conn: Connection, rows: list[dict[str, str]], sha: str = "a" * 64) -> PriceResult:
    return import_prices(conn, download("stocks", rows, sha), "2024-03")


def run_actions(conn: Connection, rows: list[dict[str, str]], sha: str = "a" * 64) -> ActionResult:
    return import_actions(conn, download("actions", rows, sha))


def stored_prices(conn: Connection, security_id: int) -> list[Any]:
    p = daily_price_table.c
    return list(
        conn.execute(
            select(daily_price_table).where(p.security_id == security_id).order_by(p.price_date)
        ).mappings()
    )


def test_prices_are_stored_with_all_three_closes_and_their_lineage(
    conn: Connection, ticker: str, security_id: int
) -> None:
    result = run_prices(conn, [price_row(ticker), price_row(ticker, "2024-03-05")])
    assert (result.inserted, result.already_present, result.needs_review) == (2, 0, [])
    assert (result.first_date, result.last_date) == (date(2024, 3, 4), date(2024, 3, 5))
    first = stored_prices(conn, security_id)[0]
    assert (first["close"], first["adjusted_close"], first["close_unadjusted"]) == (
        Decimal("10.5"),
        Decimal("10.4"),
        Decimal("21"),
    )
    assert first["volume"] == 1000 and first["source"] == "sharadar"
    assert first["data_version"] == result.data_version


def test_running_the_same_prices_again_adds_nothing(
    conn: Connection, ticker: str, security_id: int
) -> None:
    rows = [price_row(ticker), price_row(ticker, "2024-03-05")]
    run_prices(conn, rows)
    before = stored_prices(conn, security_id)
    result = run_prices(conn, rows)
    assert (result.inserted, result.already_present, result.restated) == (0, 2, 0)
    assert stored_prices(conn, security_id) == before


def test_a_restated_price_is_reported_and_the_stored_one_is_kept(
    conn: Connection, ticker: str, security_id: int
) -> None:
    run_prices(conn, [price_row(ticker)])
    result = run_prices(conn, [price_row(ticker, close="5.25", closeadj="5.2")], sha="b" * 64)
    assert (result.inserted, result.already_present, result.restated) == (0, 1, 1)
    assert result.needs_review == [
        f"{ticker} 2024-03-04: Sharadar's copy now differs from the stored row"
    ]
    assert stored_prices(conn, security_id)[0]["close"] == Decimal("10.5")


def test_bad_unmatched_and_repeated_rows_are_skipped_and_counted(
    conn: Connection, ticker: str, security_id: int
) -> None:
    rows = [
        price_row(ticker),
        price_row(ticker),
        price_row(ticker, "2024-03-06", low="0"),
        price_row("ZQNOBODY"),
        price_row(ticker, "2019-01-02"),  # before this security's first day
    ]
    result = run_prices(conn, rows)
    assert result.inserted == 1
    assert dict(result.skipped) == {
        "duplicate_in_download": 1,
        "impossible_price_or_volume": 1,
        "no_security_for_ticker_on_date": 2,
    }
    assert any("2 rows had no security" in note for note in result.needs_review)


def test_the_price_run_is_audited_and_the_version_named_by_month(
    conn: Connection, ticker: str, security_id: int
) -> None:
    with correlation_scope() as cid:
        result = run_prices(conn, [price_row(ticker)])
    [event] = conn.execute(
        select(audit_event_table).where(audit_event_table.c.correlation_id == cid)
    ).all()
    assert event.action == "import.completed" and event.entity_type == "import:sharadar.stocks"
    assert result.data_version == f"sharadar.stocks.2024-03@{'a' * 12}"
    assert event.details["window"] == "2024-03" and event.details["inserted"] == 1
    assert (event.details["first_date"], event.details["last_date"]) == ("2024-03-04",) * 2


def test_an_empty_month_still_records_a_version_and_an_audit_event(conn: Connection) -> None:
    with correlation_scope() as cid:
        result = run_prices(conn, [])
    assert (result.inserted, result.first_date) == (0, None)
    events = conn.execute(
        select(audit_event_table).where(audit_event_table.c.correlation_id == cid)
    ).all()
    assert len(events) == 1


def test_actions_are_stored_with_type_value_and_details(
    conn: Connection, ticker: str, security_id: int
) -> None:
    rows = [
        action_row(ticker),
        action_row(ticker, "split", "0.5", date="2024-03-05"),
        action_row(ticker, "acquisitionof", "6111.1", date="2024-03-06", contraticker=ticker),
        action_row(ticker, "spinoff", "1", date="2024-03-07"),
    ]
    result = run_actions(conn, rows)
    assert (result.inserted, result.already_present) == (3, 0)
    assert dict(result.skipped) == {"unmapped_action:spinoff": 1}
    a = corporate_action_table.c
    stored = {
        r.action_type: r
        for r in conn.execute(select(corporate_action_table).where(a.security_id == security_id))
    }
    assert stored["DIVIDEND"].value == Decimal("0.5")
    assert stored["SPLIT"].value == Decimal("0.5") and stored["SPLIT"].effective_date == date(
        2024, 3, 5
    )
    merger = stored["MERGER"]
    assert merger.value is None and merger.related_security_id == security_id
    assert merger.details["sharadar_value"] == "6111.1"
    assert merger.data_version == result.data_version == f"sharadar.actions@{'a' * 12}"


def test_running_the_same_actions_again_adds_nothing(
    conn: Connection, ticker: str, security_id: int
) -> None:
    rows = [action_row(ticker), action_row(ticker, "split", "2")]
    run_actions(conn, rows)
    result = run_actions(conn, rows)
    assert (result.inserted, result.already_present) == (0, 2)


def test_the_same_event_twice_in_one_download_is_stored_once(
    conn: Connection, ticker: str, security_id: int
) -> None:
    result = run_actions(conn, [action_row(ticker), action_row(ticker)])
    assert result.inserted == 1 and dict(result.skipped) == {"same_event_twice_in_download": 1}


def test_actions_for_unknown_tickers_are_reported(conn: Connection) -> None:
    result = run_actions(conn, [action_row("ZQNOBODY")])
    assert result.inserted == 0
    assert any("1 actions had no security" in note for note in result.needs_review)


def test_the_action_run_is_audited(conn: Connection, ticker: str, security_id: int) -> None:
    with correlation_scope() as cid:
        run_actions(conn, [action_row(ticker), action_row(ticker, "split", "2")])
    [event] = conn.execute(
        select(audit_event_table).where(audit_event_table.c.correlation_id == cid)
    ).all()
    assert event.entity_type == "import:sharadar.actions"
    assert event.details["by_type"] == {"DIVIDEND": 1, "SPLIT": 1}


def test_the_app_role_cannot_change_or_delete_stored_market_data(
    conn: Connection, ticker: str, security_id: int
) -> None:
    run_prices(conn, [price_row(ticker)])
    run_actions(conn, [action_row(ticker)])
    for table in (daily_price_table, corporate_action_table):
        for statement in (table.update().values(source="x"), table.delete()):
            with pytest.raises(ProgrammingError, match="permission denied"), conn.begin_nested():
                conn.execute(statement)
