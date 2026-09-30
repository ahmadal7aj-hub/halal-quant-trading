"""Task 9 (S&P 500 part): rows are stored as delivered, added once, audited and version-stamped.

Made-up data under a throwaway index name, in a rolled-back transaction.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import Connection, Engine, select
from sqlalchemy.exc import ProgrammingError

from halal_quant.audit import audit_event_table
from halal_quant.core.logging import correlation_scope
from halal_quant.core.settings import DbRole
from halal_quant.data.sharadar.client import Download
from halal_quant.data.sharadar.index_membership import (
    DATASET,
    MembershipResult,
    import_index_membership,
    index_membership_table,
)


@pytest.fixture
def conn(engines: dict[DbRole, Engine]) -> Iterator[Connection]:
    with engines[DbRole.APP].connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


@pytest.fixture
def index() -> str:
    return f"test-{uuid.uuid4().hex[:8]}"


def row(
    ticker: str, action: str = "historical", day: str = "2026-06-30", **kw: str
) -> dict[str, str]:
    base = {
        "date": day,
        "action": action,
        "ticker": ticker,
        "name": f"{ticker} Corp",
        "contraticker": "N/A",
        "contraname": "N/A",
        "note": "",
    }
    return {**base, **kw}


ROWS = [
    row("ZQA"),
    row("ZQB"),
    row("ZQA", action="current", day="2026-09-29"),
    row("ZQC", action="added", day="2026-08-01", contraticker="ZQB", contraname="ZQB Corp"),
]


def run(
    conn: Connection, index: str, rows: list[dict[str, str]], sha: str = "a" * 64
) -> MembershipResult:
    download = Download("sp500", rows, sha, datetime(2026, 9, 30, 8, 0, tzinfo=UTC))
    return import_index_membership(conn, download, index_name=index)


def stored(conn: Connection, index: str) -> list[Any]:
    t = index_membership_table.c
    return list(
        conn.execute(
            select(index_membership_table)
            .where(t.index_name == index)
            .order_by(t.record_date, t.action, t.ticker)
        ).mappings()
    )


def test_rows_are_stored_as_delivered(conn: Connection, index: str) -> None:
    result = run(conn, index, ROWS)
    assert (result.inserted, result.already_present, result.needs_review) == (4, 0, [])
    assert dict(result.actions) == {"historical": 2, "current": 1, "added": 1}
    assert (result.first_date, result.last_date) == (date(2026, 6, 30), date(2026, 9, 29))
    added = next(r for r in stored(conn, index) if r["action"] == "added")
    assert (added["ticker"], added["contra_ticker"], added["contra_name"]) == (
        "ZQC",
        "ZQB",
        "ZQB Corp",
    )
    assert added["source"] == "sharadar" and added["data_version"] == result.data_version
    plain = next(r for r in stored(conn, index) if r["action"] == "current")
    assert plain["contra_ticker"] is None and plain["note"] is None


def test_running_again_adds_nothing(conn: Connection, index: str) -> None:
    run(conn, index, ROWS)
    before = stored(conn, index)
    result = run(conn, index, ROWS)
    assert (result.inserted, result.already_present, result.needs_review) == (0, 4, [])
    assert stored(conn, index) == before


def test_only_new_rows_are_added_when_the_table_grows(conn: Connection, index: str) -> None:
    run(conn, index, ROWS[:2])
    result = run(conn, index, ROWS, sha="b" * 64)
    assert (result.inserted, result.already_present) == (2, 2)
    assert len(stored(conn, index)) == 4


def test_a_changed_copy_of_a_stored_row_is_reported_not_overwritten(
    conn: Connection, index: str
) -> None:
    run(conn, index, ROWS)
    corrected = [row("ZQA", name="Renamed Corp"), *ROWS[1:]]
    result = run(conn, index, corrected, sha="c" * 64)
    assert result.inserted == 0 and result.already_present == 4
    assert result.needs_review == [
        "ZQA historical 2026-06-30: Sharadar's copy now differs from the stored row"
    ]
    original = next(
        r for r in stored(conn, index) if r["ticker"] == "ZQA" and r["action"] == "historical"
    )
    assert original["company_name"] == "ZQA Corp"


def test_unusable_and_repeated_rows_are_skipped_and_counted(conn: Connection, index: str) -> None:
    rows = [row("ZQA"), row("ZQA"), row("ZQD", day=""), row("ZQE", day="not-a-date")]
    result = run(conn, index, rows)
    assert result.inserted == 1
    assert dict(result.skipped) == {
        "duplicate_in_download": 1,
        "missing_key_field": 1,
        "bad_date": 1,
    }


def test_the_run_is_audited_and_the_version_recorded(conn: Connection, index: str) -> None:
    with correlation_scope() as cid:
        result = run(conn, index, ROWS)
    [event] = conn.execute(
        select(audit_event_table).where(audit_event_table.c.correlation_id == cid)
    ).all()
    assert event.action == "import.completed" and event.entity_type == "import:sharadar.sp500"
    details = event.details
    assert details["data_version"] == result.data_version == f"{DATASET}@{'a' * 12}"
    assert (details["rows_downloaded"], details["inserted"]) == (4, 4)
    assert details["actions"] == {"historical": 2, "current": 1, "added": 1}
    assert (details["first_date"], details["last_date"]) == ("2026-06-30", "2026-09-29")


def test_the_app_role_cannot_change_or_delete_membership_rows(conn: Connection, index: str) -> None:
    run(conn, index, ROWS)
    t = index_membership_table
    for statement in (t.update().values(ticker="X"), t.delete()):
        with pytest.raises(ProgrammingError, match="permission denied"), conn.begin_nested():
            conn.execute(statement)
