"""P2-2 / P2-3: a price vintage is one consistent, add-only, closable download (made-up data)."""

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import Connection, Engine, select
from sqlalchemy.exc import ProgrammingError

from halal_quant.core.settings import DbRole
from halal_quant.data.security_master import SecurityInfo, create_security
from halal_quant.data.sharadar.client import Download
from halal_quant.data.sharadar.vintage import import_vintage_benchmark, import_vintage_month
from halal_quant.data.vintage import (
    COMPLETE,
    VintageError,
    adjusted_close_on,
    finish_vintage,
    price_vintage_table,
    require_building,
    start_vintage,
    vintage_benchmark_price_table,
    vintage_price_table,
    vintage_status,
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
def vintage(conn: Connection) -> str:
    vintage_id = "t" + uuid.uuid4().hex[:10]
    assert start_vintage(conn, vintage_id, "test vintage") is True
    return vintage_id


@pytest.fixture
def ticker() -> str:
    return "ZQ" + uuid.uuid4().hex[:6].upper()


@pytest.fixture
def security_id(conn: Connection, ticker: str) -> int:
    info = SecurityInfo(source="test", source_id=uuid.uuid4().hex, company_name="Vintage Corp")
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
        "lastupdated": "2026-10-02",
    }
    return {**base, **kw}


def download(endpoint: str, rows: list[dict[str, str]], sha: str = "a" * 64) -> Download:
    return Download(endpoint, rows, sha, datetime(2026, 10, 2, 8, 0, tzinfo=UTC))


def test_a_month_of_stock_prices_is_stored_in_the_vintage_and_a_rerun_adds_nothing(
    conn: Connection, vintage: str, ticker: str, security_id: int
) -> None:
    rows = [price_row(ticker), price_row(ticker, "2024-03-05", closeadj="10.6", volume="10.5")]
    first = import_vintage_month(conn, download("stocks", rows), vintage, "2024-03")
    assert (first.inserted, first.already_present) == (2, 0)
    assert (first.first_date, first.last_date) == (date(2024, 3, 4), date(2024, 3, 5))
    p = vintage_price_table.c
    stored = conn.execute(
        select(vintage_price_table).where(p.vintage_id == vintage).order_by(p.price_date)
    ).mappings().all()  # fmt: skip
    assert [(r["close_unadjusted"], r["adjusted_close"], r["volume"]) for r in stored] == [
        (Decimal("21"), Decimal("10.4"), 1000),
        (Decimal("21"), Decimal("10.6"), 11),  # fractional split-adjusted volume is rounded
    ]
    again = import_vintage_month(conn, download("stocks", rows), vintage, "2024-03")
    assert (again.inserted, again.already_present) == (0, 2)
    assert adjusted_close_on(conn, vintage, security_id, date(2024, 3, 4)) == Decimal("10.4")
    assert adjusted_close_on(conn, vintage, security_id, date(2024, 3, 6)) is None


def test_unmatched_and_duplicate_rows_are_skipped_and_counted(
    conn: Connection, vintage: str, ticker: str, security_id: int
) -> None:
    rows = [price_row(ticker), price_row(ticker), price_row("ZQNOBODY"), price_row(ticker, "bad")]
    result = import_vintage_month(conn, download("stocks", rows), vintage, "2024-03")
    assert result.inserted == 1
    assert result.skipped["duplicate_in_download"] == 1
    assert result.skipped["no_security_for_ticker_on_date"] == 1
    assert result.skipped["bad_value"] == 1


def test_two_vintages_do_not_share_rows(
    conn: Connection, vintage: str, ticker: str, security_id: int
) -> None:
    other = "t" + uuid.uuid4().hex[:10]
    start_vintage(conn, other, "another")
    rows = [price_row(ticker)]
    import_vintage_month(conn, download("stocks", rows), vintage, "2024-03")
    second = import_vintage_month(conn, download("stocks", rows, "b" * 64), other, "2024-03")
    assert second.inserted == 1  # the same day is stored once per vintage
    assert adjusted_close_on(conn, other, security_id, date(2024, 3, 4)) == Decimal("10.4")


def fund_row(symbol: str, day: str, **kw: str) -> dict[str, str]:
    base = {"ticker": symbol, "date": day, "volume": "5000", "closeadj": "55.5", "closeunadj": "60"}
    return {**base, **kw}


def test_benchmark_funds_are_stored_and_bad_rows_skipped(conn: Connection, vintage: str) -> None:
    rows = [
        fund_row("SPUS", "2026-10-01"),
        fund_row("SPUS", "2026-09-30", volume="12.5"),
        fund_row("SPUS", "2026-09-30"),  # a duplicate day
        fund_row("OTHR", "2026-09-29"),
        fund_row("SPUS", "2026-09-26", closeadj="0"),
        fund_row("SPUS", "2026-09-25", closeadj="N/A"),
        fund_row("SPUS", "not a date"),
    ]
    result = import_vintage_benchmark(conn, download("funds", rows), vintage, "spus")
    assert result.inserted == 2
    assert dict(result.skipped) == {
        "duplicate_in_download": 1,
        "other_ticker": 1,
        "impossible_price_or_volume": 1,
        "missing_value": 1,
        "bad_value": 1,
    }
    assert (result.first_date, result.last_date) == (date(2026, 9, 30), date(2026, 10, 1))
    b = vintage_benchmark_price_table.c
    volumes = {
        r.price_date: (r.symbol, r.volume)
        for r in conn.execute(
            select(b.price_date, b.symbol, b.volume).where(b.vintage_id == vintage)
        )
    }
    assert volumes == {date(2026, 10, 1): ("SPUS", 5000), date(2026, 9, 30): ("SPUS", 13)}
    again = import_vintage_benchmark(conn, download("funds", rows), vintage, "SPUS")
    assert (again.inserted, again.already_present) == (0, 2)


def test_a_vintage_is_registered_resumed_finished_and_then_closed(
    conn: Connection, ticker: str, security_id: int
) -> None:
    vid = "t" + uuid.uuid4().hex[:10]
    assert vintage_status(conn, vid) is None
    assert start_vintage(conn, vid, "d") is True
    assert start_vintage(conn, vid, "d") is False  # resuming a vintage that is still being built
    import_vintage_month(conn, download("stocks", [price_row(ticker)]), vid, "2024-03")
    import_vintage_benchmark(conn, download("funds", [fund_row("SPY", "2026-10-01")]), vid, "SPY")
    finish_vintage(conn, vid)
    v = price_vintage_table.c
    row = conn.execute(select(price_vintage_table).where(v.vintage_id == vid)).mappings().one()
    assert (row["status"], row["stock_rows"], row["benchmark_rows"]) == (COMPLETE, 1, 1)
    assert row["finished_at"] is not None
    with pytest.raises(VintageError, match="complete"):
        start_vintage(conn, vid, "d")
    with pytest.raises(VintageError, match="not being built"):
        import_vintage_month(
            conn, download("stocks", [price_row(ticker)], "c" * 64), vid, "2024-03"
        )
    with pytest.raises(VintageError, match="not being built"):
        finish_vintage(conn, vid)
    with pytest.raises(VintageError):
        require_building(conn, "never-registered")


def test_the_app_role_can_only_close_a_vintage_never_rewrite_it(
    conn: Connection, vintage: str, ticker: str, security_id: int
) -> None:
    import_vintage_month(conn, download("stocks", [price_row(ticker)]), vintage, "2024-03")
    import_vintage_benchmark(
        conn, download("funds", [fund_row("SPY", "2026-10-01")]), vintage, "SPY"
    )
    for table in (vintage_price_table, vintage_benchmark_price_table, price_vintage_table):
        for statement in (table.delete(),):
            with pytest.raises(ProgrammingError, match="permission denied"), conn.begin_nested():
                conn.execute(statement)
    with pytest.raises(ProgrammingError, match="permission denied"), conn.begin_nested():
        conn.execute(vintage_price_table.update().values(adjusted_close=Decimal("1")))
    with pytest.raises(ProgrammingError, match="permission denied"), conn.begin_nested():
        conn.execute(price_vintage_table.update().values(description="rewritten"))
    # the completion columns may be updated (that is how finish_vintage works)
    conn.execute(
        price_vintage_table.update()
        .where(price_vintage_table.c.vintage_id == vintage)
        .values(status=COMPLETE)
    )
