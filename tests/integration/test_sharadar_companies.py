"""Task 9 acceptance: the companies import is idempotent, audited and version-stamped.

Made-up data under a throwaway source name, in a rolled-back transaction, so it never mixes with
a real local import and the audit events it writes leave nothing behind.
"""

import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, func, select

from halal_quant.audit import audit_event_table
from halal_quant.core.logging import correlation_scope
from halal_quant.core.settings import DbRole
from halal_quant.data.security_master import (
    resolve_ticker,
    security_table,
    security_ticker_table,
)
from halal_quant.data.sharadar.client import Download
from halal_quant.data.sharadar.companies import DATASET, ImportResult, import_companies
from halal_quant.data.versions import data_version_table


@pytest.fixture
def conn(engines: dict[DbRole, Engine]) -> Iterator[Connection]:
    with engines[DbRole.APP].connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


@pytest.fixture
def source() -> str:
    return f"test-{uuid.uuid4().hex[:8]}"


def row(permaticker: str, ticker: str, **overrides: str) -> dict[str, str]:
    base = {
        "permaticker": permaticker,
        "ticker": ticker,
        "name": f"Company {permaticker}",
        "exchange": "NYSE",
        "isdelisted": "N",
        "category": "Domestic Common Stock",
        "siccode": "3571",
        "sector": "Technology",
        "industry": "Computer Hardware",
        "currency": "USD",
        "location": "Nowhere; U.S.A",
        "firstpricedate": "2010-01-04",
        "lastpricedate": "2026-09-29",
    }
    return {**base, **overrides}


def download(rows: list[dict[str, str]], sha256: str = "a" * 64) -> Download:
    return Download("tickers", rows, sha256, datetime(2026, 9, 30, 8, 0, tzinfo=UTC))


def run(conn: Connection, source: str, rows: list[dict[str, str]], **kw: Any) -> ImportResult:
    return import_companies(conn, download(rows, **kw), source=source)


def stored(conn: Connection, source: str) -> dict[str, Any]:
    rows = conn.execute(select(security_table).where(security_table.c.source == source)).mappings()
    return {r["source_id"]: r for r in rows}


ROWS = [
    row("900001", "ZQI1"),
    row("900002", "ZQI2", isdelisted="Y", lastpricedate="2015-03-31", firstpricedate="2001-03-01"),
    row("900003", "ZQI3", sector="", industry=""),
]


def test_first_run_creates_active_and_delisted_securities(conn: Connection, source: str) -> None:
    result = run(conn, source, ROWS)
    assert (result.created, result.updated, result.unchanged) == (3, 0, 0)
    securities = stored(conn, source)
    active, delisted = securities["900001"], securities["900002"]
    assert active["active_flag"] and not active["delisted_flag"] and active["end_date"] is None
    assert delisted["delisted_flag"] and not delisted["active_flag"]
    assert delisted["end_date"] == date(2015, 3, 31)
    assert active["category"] == "Domestic Common Stock" and active["sic_code"] == "3571"
    # Ticker lookups are date-correct, including a delisted company's last trading day.
    assert resolve_ticker(conn, "ZQI1", date(2010, 1, 4)) == active["security_id"]
    assert resolve_ticker(conn, "ZQI1", date(2009, 12, 31)) is None
    assert resolve_ticker(conn, "ZQI2", date(2015, 3, 31)) == delisted["security_id"]
    assert resolve_ticker(conn, "ZQI2", date(2015, 4, 1)) is None


def test_running_again_on_the_same_data_changes_nothing(conn: Connection, source: str) -> None:
    run(conn, source, ROWS)
    before = stored(conn, source)
    result = run(conn, source, ROWS)
    assert (result.created, result.updated, result.unchanged) == (0, 0, 3)
    assert result.needs_review == [] and not result.skipped
    after = stored(conn, source)
    assert {k: dict(v) for k, v in before.items()} == {k: dict(v) for k, v in after.items()}
    count = conn.execute(
        select(func.count())
        .select_from(security_ticker_table)
        .where(security_ticker_table.c.security_id.in_([v["security_id"] for v in after.values()]))
    ).scalar_one()
    assert count == 3  # no duplicate ticker periods


def test_descriptive_changes_are_applied_to_the_same_security(
    conn: Connection, source: str
) -> None:
    run(conn, source, ROWS)
    security_id = stored(conn, source)["900003"]["security_id"]
    changed = [*ROWS[:2], row("900003", "ZQI3", sector="Energy", industry="Oil & Gas")]
    result = run(conn, source, changed, sha256="b" * 64)
    assert (result.updated, result.unchanged) == (1, 2)
    now = stored(conn, source)["900003"]
    assert now["security_id"] == security_id
    assert (now["sector"], now["industry"]) == ("Energy", "Oil & Gas")


def test_a_newly_delisted_company_is_closed(conn: Connection, source: str) -> None:
    run(conn, source, ROWS)
    delisting = [row("900001", "ZQI1", isdelisted="Y", lastpricedate="2026-06-30"), *ROWS[1:]]
    result = run(conn, source, delisting, sha256="c" * 64)
    assert (result.newly_delisted, result.unchanged) == (1, 2)
    company = stored(conn, source)["900001"]
    assert company["delisted_flag"] and company["end_date"] == date(2026, 6, 30)
    assert resolve_ticker(conn, "ZQI1", date(2026, 6, 30)) == company["security_id"]
    assert resolve_ticker(conn, "ZQI1", date(2026, 7, 1)) is None


def test_disagreements_about_known_securities_are_reported_not_applied(
    conn: Connection, source: str
) -> None:
    run(conn, source, ROWS)
    changed = [
        row("900001", "ZQNEW"),  # ticker differs
        row("900002", "ZQI2", isdelisted="N"),  # delisted company "reappears"
        row("900003", "ZQI3", sector="", industry="", firstpricedate="2009-01-02"),  # start moved
    ]
    result = run(conn, source, changed, sha256="d" * 64)
    text = "\n".join(result.needs_review)
    assert "ZQNEW, stored as ZQI1" in text
    assert "900002 (ZQI2): stored as delisted but Sharadar lists it as active again" in text
    assert "first price date is now 2009-01-02" in text
    securities = stored(conn, source)
    assert securities["900002"]["delisted_flag"]  # not changed
    assert resolve_ticker(conn, "ZQI1", date(2020, 1, 2)) == securities["900001"]["security_id"]
    assert resolve_ticker(conn, "ZQNEW", date(2020, 1, 2)) is None


def test_a_stored_company_missing_from_the_download_is_reported(
    conn: Connection, source: str
) -> None:
    run(conn, source, ROWS)
    result = run(conn, source, ROWS[:2], sha256="e" * 64)
    assert result.needs_review == ["900003: stored, but not in this Sharadar download"]


def test_unusable_rows_and_ticker_conflicts_are_skipped_and_counted(
    conn: Connection, source: str
) -> None:
    rows = [
        row("900001", "ZQC1"),
        row("900004", "ZQC1"),  # same ticker, overlapping dates: the database refuses it
        row("900005", "ZQC5", firstpricedate=""),
        row("", "ZQC6"),
    ]
    result = run(conn, source, rows)
    assert result.created == 1
    assert result.skipped["no_first_price_date"] == 1
    assert result.skipped["missing_identity"] == 1
    assert result.skipped["rejected_by_ex_security_ticker_one_security_per_ticker"] == 1
    assert any("900004 (ZQC1): rejected by" in n for n in result.needs_review)
    assert set(stored(conn, source)) == {"900001"}  # the rejected row left nothing half-written


def test_the_run_is_audited_with_counts_and_data_version(conn: Connection, source: str) -> None:
    with correlation_scope() as cid:
        result = run(conn, source, [*ROWS, row("900005", "ZQC5", firstpricedate="")])
    [event] = conn.execute(
        select(audit_event_table).where(audit_event_table.c.correlation_id == cid)
    ).all()
    assert event.action == "import.completed" and event.actor == "system:sharadar-import"
    details = event.details
    assert details["data_version"] == result.data_version == f"{DATASET}@{'a' * 12}"
    assert details["sha256"] == "a" * 64
    assert (details["rows_downloaded"], details["created"]) == (4, 3)
    assert details["skipped"] == {"no_first_price_date": 1}
    assert details["downloaded_at"].startswith("2026-09-30T08:00:00")


def test_the_data_version_is_recorded_once_per_distinct_download(
    conn: Connection, source: str
) -> None:
    run(conn, source, ROWS, sha256="f" * 64)
    run(conn, source, ROWS, sha256="f" * 64)  # identical download again
    run(conn, source, ROWS, sha256="9" * 64)  # different fingerprint
    mine = conn.execute(
        select(data_version_table).where(data_version_table.c.sha256.in_(["f" * 64, "9" * 64]))
    ).mappings()
    versions = list(mine)
    assert sorted(v["sha256"] for v in versions) == ["9" * 64, "f" * 64]
    assert all(v["row_count"] == 3 and v["source"] == "sharadar" for v in versions)


def test_the_run_logs_at_info_level_without_clashing_with_log_record_fields(
    conn: Connection, source: str, caplog: pytest.LogCaptureFixture
) -> None:
    # Regression: an `extra` key named like a LogRecord attribute ("created") raises KeyError,
    # but only when INFO logging is on, which the tests did not do by default.
    with caplog.at_level(logging.INFO, logger="halal_quant.data.sharadar.companies"):
        run(conn, source, ROWS)
    [record] = [r for r in caplog.records if r.name == "halal_quant.data.sharadar.companies"]
    assert record.getMessage() == "sharadar companies import finished"
    assert record.securities_created == 3  # type: ignore[attr-defined]
