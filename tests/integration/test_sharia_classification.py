"""Task 13: classification records are point-in-time, add-only and explain themselves (PRD Test 10).

Made-up securities and rows in a rolled-back transaction. Screening is always restricted to the
test's own securities, because the database holds real data too.
"""

import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, select
from sqlalchemy.exc import ProgrammingError

from halal_quant.audit import audit_event_table
from halal_quant.core.config import ShariaConfig, load_config
from halal_quant.core.settings import DbRole
from halal_quant.data.fundamentals import daily_market_cap_table, fundamental_table
from halal_quant.data.security_master import SecurityInfo, create_security, mark_delisted
from halal_quant.sharia import classification
from halal_quant.sharia.classification import (
    COMMON_STOCK_CATEGORIES,
    classification_on,
    classification_table,
    classify_date,
    effective_period,
    load_manual_list,
    screening_dates,
)
from halal_quant.sharia.screening import ManualEntry, Status

ROOT = Path(__file__).parents[2]
CONFIG = load_config(ROOT / "config" / "sharia" / "aaoifi_v1.yaml", ShariaConfig).config
D = date(2020, 1, 31)  # a Friday and the last trading day of January 2020
METHOD = CONFIG.version


@pytest.fixture
def conn(engines: dict[DbRole, Engine]) -> Iterator[Connection]:
    with engines[DbRole.APP].connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


def make_security(
    conn: Connection,
    industry: str = "Software - Application",
    category: str = "Domestic Common Stock",
    sic: str | None = "7372",
) -> tuple[int, str]:
    source_id = uuid.uuid4().hex
    info = SecurityInfo(
        source="test",
        source_id=source_id,
        company_name="Screen Corp",
        industry=industry,
        category=category,
        sic_code=sic,
    )
    ticker = "ZQ" + uuid.uuid4().hex[:6].upper()
    return create_security(conn, info, ticker, date(2015, 1, 2), "test", "test"), source_id


def fundamental(security_id: int, dimension: str, filed: date, period: date, **kw: object) -> dict:
    base: dict[str, object] = {
        "security_id": security_id,
        "dimension": dimension,
        "calendar_date": period,
        "filing_date": filed,
        "report_period": period,
        "source": "test",
        "data_version": "v",
        "debt": None,
        "cash_and_equivalents": None,
        "investments": None,
        "revenue": None,
        "ebit": None,
        "operating_income": None,
    }
    return {**base, **kw}


def give_data(
    conn: Connection, security_id: int, debt: int = 2000, market_value: int | None = 10000
) -> None:
    """The S1 worked example: quarterly balance sheet and trailing-twelve-month income."""
    filed, period = date(2019, 11, 1), date(2019, 9, 30)
    conn.execute(
        fundamental_table.insert(),
        [
            fundamental(
                security_id,
                "ARQ",
                filed,
                period,
                debt=Decimal(debt),
                cash_and_equivalents=Decimal(1500),
                investments=Decimal(500),
            ),
            fundamental(
                security_id,
                "ART",
                filed,
                period,
                revenue=Decimal(8000),
                ebit=Decimal(2300),
                operating_income=Decimal(2200),
            ),
        ],
    )
    if market_value is not None:
        conn.execute(
            daily_market_cap_table.insert().values(
                security_id=security_id,
                cap_date=D,
                market_cap_usd=Decimal(market_value),
                source="test",
                data_version="v",
            )
        )


def run(conn: Connection, ids: list[int], **kw: object) -> classification.ClassificationSummary:
    return classify_date(conn, D, CONFIG, security_ids=ids, **kw)  # type: ignore[arg-type]


def stored(conn: Connection, security_id: int) -> dict:
    c = classification_table.c
    return dict(
        conn.execute(select(classification_table).where(c.security_id == security_id))
        .mappings()
        .one()
    )


def test_a_halal_company_is_stored_with_its_ratios_period_and_sources(conn: Connection) -> None:
    sid, source_id = make_security(conn)
    give_data(conn, sid)
    summary = run(conn, [sid])
    record = stored(conn, sid)
    assert (summary.inserted, dict(summary.by_status)) == (1, {"HALAL": 1})
    assert record["status"] == "HALAL" and record["methodology"] == "AAOIFI-v1"
    assert record["provider"] == "internal_screen" and record["screening_date"] == D
    assert (record["effective_from"], record["effective_to"]) == (
        date(2020, 2, 3),
        date(2020, 2, 28),
    )
    assert "debt 20.00% of market value" in record["reason"]
    sources = record["source_reference"]
    assert sources["source_id"] and [f["dimension"] for f in sources["filings"]] == ["ARQ", "ART"]
    assert record["details"]["debt_to_market_value"] == "0.2"


def test_failed_missing_and_prohibited_companies_get_their_own_statuses(conn: Connection) -> None:
    high_debt, _ = make_security(conn)
    give_data(conn, high_debt, debt=3001)
    no_data, _ = make_security(conn)
    brewer, _ = make_security(conn, industry="Beverages - Brewers", sic=None)
    bank_by_sic, _ = make_security(conn, industry="Savings & Cooperative Banks", sic="6035")
    ids = [high_debt, no_data, brewer, bank_by_sic]
    run(conn, ids)
    assert stored(conn, high_debt)["status"] == "NON_HALAL"
    assert stored(conn, no_data)["status"] == "UNKNOWN"
    assert "Missing input" in stored(conn, no_data)["reason"]
    assert (
        stored(conn, brewer)["status"] == "NON_HALAL"
        and "alcohol" in stored(conn, brewer)["reason"]
    )
    assert stored(conn, bank_by_sic)["status"] == "NON_HALAL"


def test_test_10_filings_made_on_or_after_the_screening_date_are_never_used(
    conn: Connection,
) -> None:
    sid, _ = make_security(conn)
    give_data(conn, sid)  # the usable report: debt 2,000 (20%)
    conn.execute(
        fundamental_table.insert(),
        [
            # filed on the screening date itself: not yet usable (S1 E8)
            fundamental(
                sid,
                "ARQ",
                D,
                date(2019, 12, 31),
                debt=Decimal(9999),
                cash_and_equivalents=Decimal(0),
                investments=Decimal(0),
            ),
            # filed after the screening date: the future
            fundamental(
                sid,
                "ARQ",
                date(2020, 2, 15),
                date(2019, 12, 31),
                debt=Decimal(9999),
                cash_and_equivalents=Decimal(0),
                investments=Decimal(0),
            ),
        ],
    )
    run(conn, [sid])
    record = stored(conn, sid)
    assert record["status"] == "HALAL" and record["details"]["debt_to_market_value"] == "0.2"
    assert record["source_reference"]["filings"][0]["filing_date"] == "2019-11-01"


def test_test_10_a_result_applies_only_from_the_next_trading_day(conn: Connection) -> None:
    sid, _ = make_security(conn)
    give_data(conn, sid)
    run(conn, [sid])
    assert classification_on(conn, sid, D, METHOD) is None  # the screening date itself
    assert classification_on(conn, sid, date(2020, 2, 1), METHOD) is None  # a Saturday, not yet
    first = classification_on(conn, sid, date(2020, 2, 3), METHOD)
    assert first is not None and first["status"] == "HALAL"
    assert classification_on(conn, sid, date(2020, 2, 28), METHOD) is not None
    assert classification_on(conn, sid, date(2020, 3, 2), METHOD) is None  # nothing newer yet
    assert classification_on(conn, sid, date(2020, 2, 3), "AAOIFI-v2") is None  # other version
    assert classification_on(conn, sid, date(2020, 2, 3), METHOD, provider="zoya") is None


def test_the_next_screening_takes_over_and_old_records_stay(conn: Connection) -> None:
    sid, _ = make_security(conn)
    give_data(conn, sid)
    run(conn, [sid])
    conn.execute(
        daily_market_cap_table.insert().values(
            security_id=sid,
            cap_date=date(2020, 2, 28),
            market_cap_usd=Decimal(5000),
            source="test",
            data_version="v",
        )
    )  # the same debt is now 40% of a smaller market value
    classify_date(conn, date(2020, 2, 28), CONFIG, security_ids=[sid])
    assert classification_on(conn, sid, date(2020, 2, 10), METHOD)["status"] == "HALAL"  # type: ignore[index]
    assert classification_on(conn, sid, date(2020, 3, 2), METHOD)["status"] == "NON_HALAL"  # type: ignore[index]
    c = classification_table.c
    count = conn.execute(select(c.classification_id).where(c.security_id == sid)).all()
    assert len(count) == 2


def test_running_the_same_date_again_adds_nothing(conn: Connection) -> None:
    sid, _ = make_security(conn)
    give_data(conn, sid)
    run(conn, [sid])
    again = run(conn, [sid])
    assert (again.inserted, again.already_present) == (0, 1)


def test_only_the_wanted_categories_and_securities_trading_on_the_date_are_screened(
    conn: Connection,
) -> None:
    common, _ = make_security(conn)
    preferred, _ = make_security(conn, category="Domestic Preferred Stock")
    adr, _ = make_security(conn, category="ADR Common Stock")
    delisted, _ = make_security(conn)
    mark_delisted(conn, delisted, date(2019, 12, 31), "test", "test")
    summary = classify_date(
        conn,
        D,
        CONFIG,
        categories=COMMON_STOCK_CATEGORIES,
        security_ids=[common, preferred, adr, delisted],
    )
    assert summary.inserted == 1 and stored(conn, common)["status"] == "UNKNOWN"
    everything = classify_date(conn, D, CONFIG, categories=None, security_ids=[preferred, adr])
    assert everything.inserted == 2


def test_a_manual_list_entry_decides_before_the_ratios(conn: Connection) -> None:
    sid, source_id = make_security(conn)
    give_data(conn, sid)  # would be HALAL
    manual = {source_id: ManualEntry(Status.PENDING_REVIEW, "Reviewer wants to check this")}
    run(conn, [sid], manual=manual)
    record = stored(conn, sid)
    assert record["status"] == "PENDING_REVIEW" and record["reason"].startswith("Manual list:")


def test_the_run_is_audited(conn: Connection) -> None:
    sid, _ = make_security(conn)
    run(conn, [sid])
    a = audit_event_table.c
    event = (
        conn.execute(
            select(audit_event_table)
            .where(a.action == "classification.run", a.entity_id == f"{METHOD}:{D}")
            .order_by(a.sequence.desc())
        )
        .mappings()
        .first()
    )
    assert event is not None and event["details"]["inserted"] == 1


def test_the_database_rejects_impossible_records_and_the_app_role_cannot_change_them(
    conn: Connection,
) -> None:
    sid, _ = make_security(conn)
    run(conn, [sid])
    for statement in (
        classification_table.update().values(status="HALAL"),
        classification_table.delete(),
    ):
        with pytest.raises(ProgrammingError, match="permission denied"), conn.begin_nested():
            conn.execute(statement)
    record = stored(conn, sid)
    bad = {k: v for k, v in record.items() if k != "classification_id"}
    for changes in (
        {"status": "MAYBE"},
        {"effective_from": date(2019, 12, 31)},
        {"effective_to": date(2020, 1, 1)},
    ):
        with pytest.raises(Exception, match="ck_classification"), conn.begin_nested():
            conn.execute(
                classification_table.insert(),
                {**bad, **changes, "screening_date": date(2019, 12, 31)},
            )


def test_screening_dates_are_month_end_trading_days() -> None:
    assert list(screening_dates(date(2020, 1, 1), date(2020, 3, 15))) == [
        date(2020, 1, 31),
        date(2020, 2, 28),
        date(2020, 3, 31),
    ]
    assert list(screening_dates(date(2019, 6, 1), date(2019, 6, 30))) == [date(2019, 6, 28)]
    assert list(screening_dates(date(2019, 12, 1), date(2020, 1, 1)))[0] == date(2019, 12, 31)


def test_the_effective_period_runs_to_the_next_screening_date() -> None:
    assert effective_period(date(2020, 1, 31)) == (date(2020, 2, 3), date(2020, 2, 28))
    assert effective_period(date(2019, 12, 31)) == (date(2020, 1, 2), date(2020, 1, 31))
    assert effective_period(date(2019, 6, 28)) == (date(2019, 7, 1), date(2019, 7, 31))


def test_the_repo_manual_list_is_empty_and_a_filled_one_becomes_entries(tmp_path: Path) -> None:
    entries, version = load_manual_list(ROOT / "config" / "sharia" / "manual_list.yaml")
    assert entries == {} and version == "manual-list-v1"
    filled = tmp_path / "manual.yaml"
    filled.write_text(
        "config_type: sharia_manual_list\nversion: manual-list-v2\nstatus: approved\nentries:\n"
        "  - source_id: '123'\n    company_name: Example Pork Co\n    status: NON_HALAL\n"
        "    reason: Pork producer\n    approved_by: owner\n    approved_on: 2026-10-01\n",
        encoding="utf-8",
    )
    entries, version = load_manual_list(filled)
    assert version == "manual-list-v2" and entries["123"].status is Status.NON_HALAL
    assert "Pork producer" in entries["123"].reason and "owner" in entries["123"].reason
