"""Task 15: the Sharia change log (S4) and UNKNOWN report (S5), on made-up classifications.

A made-up methodology name keeps these rows apart from the real classifications.
"""

import uuid
from collections.abc import Iterator
from datetime import date

import pytest
from sqlalchemy import Connection, Engine

from halal_quant.core.settings import DbRole
from halal_quant.data.security_master import SecurityInfo, create_security
from halal_quant.sharia.classification import PROVIDER, classification_table
from halal_quant.sharia.reports import build_reports, render_markdown

JAN, FEB, MAR = date(2026, 1, 30), date(2026, 2, 27), date(2026, 3, 31)
FIRST, LAST = date(2026, 1, 1), date(2026, 4, 1)
SECONDARY_WHY = "secondary share class (provider carries the data under the primary class)"
NO_DATA = "Missing input: market value on the screening date; Missing input: no report"


@pytest.fixture
def conn(engines: dict[DbRole, Engine]) -> Iterator[Connection]:
    with engines[DbRole.APP].connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


@pytest.fixture
def methodology() -> str:
    return "TEST-" + uuid.uuid4().hex[:8]


def make(conn: Connection, name: str, category: str = "Domestic Common Stock") -> int:
    info = SecurityInfo(
        source="test", source_id=uuid.uuid4().hex, company_name=name, category=category
    )
    ticker = "ZQ" + uuid.uuid4().hex[:6].upper()
    return create_security(conn, info, ticker, date(2015, 1, 2), "test", "test")


def classify(conn: Connection, method: str, sid: int, day: date, status: str, why: str) -> None:
    conn.execute(
        classification_table.insert().values(
            security_id=sid,
            provider=PROVIDER,
            methodology=method,
            status=status,
            effective_from=date(2026, 6, 1),  # after every screening date used here
            effective_to=date(2026, 6, 30),
            screening_date=day,
            reason=why,
            details={},
            source_reference={},
        )
    )


def populate(conn: Connection, method: str) -> None:
    flipper = make(conn, "Flip Flop Corp")
    stuck = make(conn, "Stuck Unknown Corp")
    primary = make(conn, "Dual Class Corp")
    secondary = make(conn, "Dual Class Corp", "Domestic Common Stock Secondary Class")
    steady = make(conn, "Steady Corp")
    for day, status in ((JAN, "HALAL"), (FEB, "NON_HALAL"), (MAR, "HALAL")):
        classify(conn, method, flipper, day, status, f"debt 10% as of {day}")
    for day in (JAN, FEB, MAR):
        classify(conn, method, stuck, day, "UNKNOWN", "Missing input: revenue, ebit, opinc")
        classify(conn, method, secondary, day, "UNKNOWN", NO_DATA)
        classify(conn, method, steady, day, "HALAL", "debt 5%")
        classify(conn, method, primary, day, "HALAL", "debt 5%")


def test_the_change_log_counts_changes_by_year_and_lists_the_latest_ones(
    conn: Connection, methodology: str
) -> None:
    populate(conn, methodology)
    reports = build_reports(conn, FIRST, LAST, methodology)
    assert reports.latest_screening == MAR
    moves = {(t.previous, t.status): t.n for t in reports.transitions if t.year == 2026}
    assert moves == {("HALAL", "NON_HALAL"): 1, ("NON_HALAL", "HALAL"): 1}
    [year] = reports.by_year
    # 5 securities x 3 months. HALAL: flipper 2 + steady 3 + primary 3.
    # UNKNOWN: stuck 3 + secondary 3.
    assert (year.screenings, year.records, year.halal, year.unknown) == (3, 15, 8, 6)
    [latest] = reports.latest_changes
    assert latest.company_name == "Flip Flop Corp"
    assert (latest.previous, latest.status) == ("NON_HALAL", "HALAL")
    assert "debt 10%" in latest.why
    flips = [(f.company_name, f.changes) for f in reports.flip_floppers]
    assert flips == [("Flip Flop Corp", 2)]


def test_the_unknown_report_separates_secondary_classes_and_streaks(
    conn: Connection, methodology: str
) -> None:
    populate(conn, methodology)
    reports = build_reports(conn, FIRST, LAST, methodology)
    why = {r.why: r.n for r in reports.unknown_reasons}
    assert why == {SECONDARY_WHY: 1, "revenue or EBIT data missing": 1}
    streaks = [(r.company_name, r.months) for r in reports.longest_unknown]
    assert streaks == [("Stuck Unknown Corp", 3)]  # the secondary class is left out


def test_the_markdown_has_both_reports_and_every_year(conn: Connection, methodology: str) -> None:
    populate(conn, methodology)
    text = render_markdown(build_reports(conn, FIRST, LAST, methodology))
    for heading in (
        "## S4 Classification change log",
        "## S5 Missing-data (UNKNOWN) report",
        "Securities that changed status most often",
        "Stuck Unknown Corp: 3 months",
    ):
        assert heading in text
    assert "| 2026 | 3 | 15 | 8 | 2 | 1 | 1 |" in text
    assert "| 2026 | 15 | 6 | 40.0% |" in text
    assert "NON_HALAL to HALAL: 1" in text


def test_an_empty_methodology_gives_empty_but_valid_reports(conn: Connection) -> None:
    reports = build_reports(conn, FIRST, LAST, "TEST-NONE-" + uuid.uuid4().hex[:6])
    text = render_markdown(reports)
    assert reports.latest_screening is None and "No status changes." in text
    assert "None." in text
