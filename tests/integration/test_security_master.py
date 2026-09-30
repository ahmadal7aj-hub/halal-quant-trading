"""Task 7 acceptance: permanent security IDs and date-correct ticker lookups.

All data is made up (the repo is public; real data is licensed). Every test runs in a rolled-back
transaction because the audit events it writes can never be deleted.
"""

import uuid
from collections.abc import Iterator
from datetime import date

import pytest
from sqlalchemy import Connection, Engine, select
from sqlalchemy.exc import IntegrityError, ProgrammingError

from halal_quant.audit import audit_event_table
from halal_quant.core.logging import correlation_scope
from halal_quant.core.settings import DbRole
from halal_quant.data.security_master import (
    SecurityInfo,
    SecurityMasterError,
    change_ticker,
    create_security,
    mark_delisted,
    resolve_ticker,
    security_table,
    security_ticker_table,
    ticker_on,
)

WHO = {"actor": "system:integration-test", "reason": "test"}


@pytest.fixture
def conn(engines: dict[DbRole, Engine]) -> Iterator[Connection]:
    with engines[DbRole.APP].connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


def add(conn: Connection, ticker: str, listed_on: date, name: str = "Example Corp") -> int:
    info = SecurityInfo(source="test", source_id=uuid.uuid4().hex, company_name=name)
    return create_security(conn, info, ticker, listed_on, **WHO)


def test_ticker_resolves_to_its_security_only_while_listed(conn: Connection) -> None:
    sid = add(conn, "zqa", date(2020, 1, 2))
    assert resolve_ticker(conn, "ZQA", date(2020, 1, 2)) == sid
    assert resolve_ticker(conn, " zqa ", date(2025, 6, 30)) == sid
    assert resolve_ticker(conn, "ZQA", date(2020, 1, 1)) is None  # before listing
    assert resolve_ticker(conn, "NOPE", date(2020, 1, 2)) is None


def test_ticker_change_keeps_the_same_security_id(conn: Connection) -> None:
    sid = add(conn, "ZQOLD", date(2012, 5, 18))
    change_ticker(conn, sid, "ZQNEW", date(2022, 6, 9), **WHO)

    assert resolve_ticker(conn, "ZQOLD", date(2022, 6, 8)) == sid
    assert resolve_ticker(conn, "ZQNEW", date(2022, 6, 9)) == sid
    assert resolve_ticker(conn, "ZQOLD", date(2022, 6, 9)) is None
    assert resolve_ticker(conn, "ZQNEW", date(2022, 6, 8)) is None
    assert ticker_on(conn, sid, date(2022, 6, 8)) == "ZQOLD"
    assert ticker_on(conn, sid, date(2022, 6, 9)) == "ZQNEW"


def test_reused_ticker_resolves_to_the_right_company_on_each_date(conn: Connection) -> None:
    first = add(conn, "ZQR", date(2001, 3, 1), name="First Holder Inc")
    mark_delisted(conn, first, date(2015, 3, 31), **WHO)
    second = add(conn, "ZQR", date(2018, 1, 2), name="Second Holder Inc")

    assert first != second
    assert resolve_ticker(conn, "ZQR", date(2015, 3, 31)) == first  # last trading day
    assert resolve_ticker(conn, "ZQR", date(2016, 7, 1)) is None  # nobody in between
    assert resolve_ticker(conn, "ZQR", date(2018, 1, 2)) == second
    assert ticker_on(conn, first, date(2016, 7, 1)) is None


def test_delisting_sets_lifecycle_fields(conn: Connection) -> None:
    sid = add(conn, "ZQD", date(2010, 1, 4))
    mark_delisted(conn, sid, date(2019, 12, 31), **WHO)
    row = conn.execute(select(security_table).where(security_table.c.security_id == sid)).one()
    assert (row.start_date, row.end_date) == (date(2010, 1, 4), date(2019, 12, 31))
    assert row.delisted_flag and not row.active_flag
    with pytest.raises(SecurityMasterError, match="no current ticker"):
        mark_delisted(conn, sid, date(2020, 1, 31), **WHO)
    with pytest.raises(SecurityMasterError, match="no current ticker"):
        change_ticker(conn, sid, "ZQD2", date(2020, 1, 31), **WHO)


def test_ticker_in_use_by_another_security_is_refused(conn: Connection) -> None:
    add(conn, "ZQU", date(2020, 1, 2))
    other = add(conn, "ZQV", date(2020, 1, 2))
    with pytest.raises(SecurityMasterError, match="already used"):
        add(conn, "ZQU", date(2024, 1, 2))
    with pytest.raises(SecurityMasterError, match="already used"):
        change_ticker(conn, other, "ZQU", date(2024, 1, 2), **WHO)


@pytest.mark.parametrize(
    ("new_ticker", "effective", "message"),
    [("ZQS", date(2021, 1, 4), "already trades"), ("ZQT", date(2020, 1, 2), "must come after")],
)
def test_invalid_ticker_changes_are_refused(
    conn: Connection, new_ticker: str, effective: date, message: str
) -> None:
    sid = add(conn, "ZQS", date(2020, 1, 2))
    with pytest.raises(SecurityMasterError, match=message):
        change_ticker(conn, sid, new_ticker, effective, **WHO)


def test_delisting_before_the_current_ticker_started_is_refused(conn: Connection) -> None:
    sid = add(conn, "ZQE", date(2020, 1, 2))
    with pytest.raises(SecurityMasterError, match="before the current ticker"):
        mark_delisted(conn, sid, date(2019, 12, 31), **WHO)


def test_every_change_is_audited(conn: Connection) -> None:
    with correlation_scope() as cid:
        sid = add(conn, "ZQA1", date(2020, 1, 2))
        change_ticker(conn, sid, "ZQA2", date(2021, 1, 4), **WHO)
        mark_delisted(conn, sid, date(2022, 1, 3), **WHO)
    rows = conn.execute(
        select(audit_event_table)
        .where(audit_event_table.c.correlation_id == cid)
        .order_by(audit_event_table.c.sequence)
    ).all()
    assert [r.action for r in rows] == [
        "security.created",
        "security.ticker_changed",
        "security.delisted",
    ]
    assert {r.entity_id for r in rows} == {str(sid)}
    assert rows[1].old_value == {"ticker": "ZQA1"}
    assert rows[1].new_value == {"ticker": "ZQA2", "effective": "2021-01-04"}


# The database itself enforces the rules, even for code that bypasses the module.


def test_database_rejects_one_ticker_for_two_securities_at_once(conn: Connection) -> None:
    add(conn, "ZQX", date(2020, 1, 2))
    other = add(conn, "ZQY", date(2020, 1, 2))
    with pytest.raises(IntegrityError, match="one_security_per_ticker"), conn.begin_nested():
        conn.execute(
            security_ticker_table.insert().values(
                security_id=other, ticker="ZQX", valid_from=date(2023, 1, 3), valid_to=None
            )
        )


def test_database_rejects_two_tickers_for_one_security_at_once(conn: Connection) -> None:
    sid = add(conn, "ZQZ", date(2020, 1, 2))
    with pytest.raises(IntegrityError, match="one_ticker_per_security"), conn.begin_nested():
        conn.execute(
            security_ticker_table.insert().values(
                security_id=sid, ticker="ZQZ2", valid_from=date(2023, 1, 3)
            )
        )


def test_database_rejects_duplicate_provider_id(conn: Connection) -> None:
    info = SecurityInfo(source="test", source_id=uuid.uuid4().hex, company_name="Dup Corp")
    create_security(conn, info, "ZQP1", date(2020, 1, 2), **WHO)
    with pytest.raises(IntegrityError, match="uq_security_source_id"), conn.begin_nested():
        create_security(conn, info, "ZQP2", date(2020, 1, 2), **WHO)


def test_database_rejects_active_and_delisted_together(conn: Connection) -> None:
    sid = add(conn, "ZQF", date(2020, 1, 2))
    s = security_table
    with pytest.raises(IntegrityError, match="delisted_not_active"), conn.begin_nested():
        conn.execute(
            s.update()
            .where(s.c.security_id == sid)
            .values(delisted_flag=True, end_date=date(2021, 1, 4))
        )


def test_app_role_cannot_rewrite_or_delete_history(conn: Connection) -> None:
    sid = add(conn, "ZQH", date(2020, 1, 2))
    t = security_ticker_table
    statements = [
        t.update().where(t.c.security_id == sid).values(ticker="ZQHX"),
        t.update().where(t.c.security_id == sid).values(valid_from=date(2019, 1, 2)),
        t.delete().where(t.c.security_id == sid),
        security_table.delete().where(security_table.c.security_id == sid),
    ]
    for statement in statements:
        with pytest.raises(ProgrammingError, match="permission denied"), conn.begin_nested():
            conn.execute(statement)
