"""Task 15: our classification as the cross-check sees it (made-up securities, rolled back)."""

import uuid
from collections.abc import Iterator
from datetime import date

import pytest
from sqlalchemy import Connection, Engine

from halal_quant.core.settings import DbRole
from halal_quant.data.security_master import SecurityInfo, create_security
from halal_quant.sharia.classification import PROVIDER, classification_table
from halal_quant.sharia.crosscheck import our_records

AS_OF = date(2026, 10, 2)


@pytest.fixture
def conn(engines: dict[DbRole, Engine]) -> Iterator[Connection]:
    with engines[DbRole.APP].connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


def make(conn: Connection, ticker: str, category: str = "Domestic Common Stock") -> int:
    info = SecurityInfo(
        source="test", source_id=uuid.uuid4().hex, company_name=f"{ticker} Co", category=category
    )
    return create_security(conn, info, ticker, date(2015, 1, 2), "test", "test")


def classify(
    conn: Connection,
    security_id: int,
    status: str,
    screened: date = date(2026, 9, 30),
    effective_from: date = date(2026, 10, 1),
    effective_to: date = date(2026, 10, 30),
    methodology: str = "AAOIFI-v1",
) -> None:
    conn.execute(
        classification_table.insert().values(
            security_id=security_id,
            provider=PROVIDER,
            methodology=methodology,
            status=status,
            effective_from=effective_from,
            effective_to=effective_to,
            screening_date=screened,
            reason=f"why {status}",
            details={},
            source_reference={},
        )
    )


def test_our_records_are_what_was_in_effect_on_the_date_keyed_by_normalised_ticker(
    conn: Connection,
) -> None:
    suffix = uuid.uuid4().hex[:5].upper()
    current = make(conn, f"ZQ-{suffix}")  # a dash: compared as ZQ.<suffix>
    old_then_new = make(conn, f"ZR{suffix}")
    not_yet = make(conn, f"ZS{suffix}")
    preferred = make(conn, f"ZT{suffix}", category="Domestic Preferred Stock")
    classify(conn, current, "HALAL")
    classify(conn, old_then_new, "NON_HALAL", screened=date(2026, 8, 31),
             effective_from=date(2026, 9, 1), effective_to=date(2026, 10, 30))  # fmt: skip
    classify(conn, old_then_new, "HALAL")  # the newer screening wins
    classify(
        conn, not_yet, "HALAL", effective_from=date(2026, 10, 5), effective_to=date(2026, 11, 3)
    )
    classify(conn, preferred, "HALAL")
    classify(conn, current, "NON_HALAL", methodology="AAOIFI-v2")  # another methodology

    records = our_records(conn, AS_OF, "AAOIFI-v1")
    assert (
        records[f"ZQ.{suffix}"].status == "HALAL"
        and records[f"ZQ.{suffix}"].ticker == f"ZQ-{suffix}"
    )
    assert records[f"ZR{suffix}"].status == "HALAL"  # not the older NON_HALAL screening
    assert f"ZS{suffix}" not in records  # takes effect only on 5 Oct
    assert f"ZT{suffix}" not in records  # not common stock
    assert records[f"ZQ.{suffix}"].reason == "why HALAL"
    assert f"ZT{suffix}" in our_records(
        conn, AS_OF, "AAOIFI-v1", categories=("Domestic Preferred Stock",)
    )
