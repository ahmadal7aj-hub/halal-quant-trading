"""Parsing Sharadar company rows: made-up data, one case per rule."""

from datetime import UTC, date, datetime
from typing import Any

import pytest
from pydantic import SecretStr

from halal_quant.core.settings import Settings
from halal_quant.data.sharadar import cli, companies
from halal_quant.data.sharadar.client import Download, SharadarError
from halal_quant.data.sharadar.companies import ImportResult, parse_company


def row(**overrides: Any) -> dict[str, str]:
    base = {
        "permaticker": "900001",
        "ticker": "zqa",
        "name": "Example Corp",
        "exchange": "NYSE",
        "isdelisted": "N",
        "category": "Domestic Common Stock",
        "siccode": "3571",
        "sector": "Technology",
        "industry": "Computer Hardware",
        "currency": "USD",
        "location": "California; U.S.A",
        "firstpricedate": "2010-01-04",
        "lastpricedate": "2026-09-29",
    }
    return {**base, **overrides}


def test_an_active_company_is_parsed_and_its_last_price_date_ignored() -> None:
    record, reason = parse_company(row())
    assert reason is None and record is not None
    assert record.ticker == "ZQA"
    assert record.first_price_date == date(2010, 1, 4)
    assert record.last_price_date is None  # still trading: the last price is just "latest"
    info = record.info
    assert (info.source, info.source_id, info.company_name) == (
        "sharadar",
        "900001",
        "Example Corp",
    )
    assert (info.category, info.sic_code, info.country) == (
        "Domestic Common Stock",
        "3571",
        "California; U.S.A",
    )


def test_a_delisted_company_keeps_its_last_price_date() -> None:
    record, _ = parse_company(row(isdelisted="Y", lastpricedate="2015-03-31"))
    assert record is not None and record.last_price_date == date(2015, 3, 31)


def test_blank_optional_fields_become_none() -> None:
    record, _ = parse_company(row(exchange=" ", sector="", industry="", siccode=""))
    assert record is not None
    assert (record.info.exchange, record.info.sector, record.info.sic_code) == (None, None, None)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"permaticker": ""}, "missing_identity"),
        ({"ticker": " "}, "missing_identity"),
        ({"name": ""}, "missing_identity"),
        ({"isdelisted": ""}, "bad_isdelisted_flag"),
        ({"isdelisted": "maybe"}, "bad_isdelisted_flag"),
        ({"firstpricedate": ""}, "no_first_price_date"),
        ({"firstpricedate": "04/01/2010"}, "bad_date"),
        ({"isdelisted": "Y", "lastpricedate": ""}, "delisted_without_last_price_date"),
        ({"isdelisted": "Y", "lastpricedate": "2009-12-31"}, "last_price_before_first"),
    ],
)
def test_rows_that_cannot_be_dated_or_identified_are_skipped_with_a_reason(
    overrides: dict[str, str], reason: str
) -> None:
    assert parse_company(row(**overrides)) == (None, reason)


def test_summary_is_readable() -> None:
    result = ImportResult(data_version="sharadar.tickers.stocks@abc", created=3)
    result.skipped["bad_date"] += 2
    text = result.summary()
    assert "created 3" in text and "bad_date: 2" in text and "needs review: 0" in text


class FakeEngine:
    """Stands in for the database: begin() gives a placeholder connection, nothing is stored."""

    disposed = False

    def begin(self) -> "FakeEngine":
        return self

    def __enter__(self) -> str:
        return "conn"

    def __exit__(self, *exc: object) -> None:
        return None

    def dispose(self) -> None:
        self.disposed = True


def settings_with_key(fake_settings: Settings, key: str | None) -> Settings:
    secret = SecretStr(key) if key else None
    return fake_settings.model_copy(update={"sharadar_api_key": secret})


def test_main_needs_the_api_key(
    monkeypatch: pytest.MonkeyPatch, fake_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: settings_with_key(fake_settings, None))
    assert companies.main() == 1
    assert "HQ_SHARADAR_API_KEY is not set" in capsys.readouterr().out


def test_main_reports_a_failed_download_without_the_key(
    monkeypatch: pytest.MonkeyPatch, fake_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    class Failing:
        def __init__(self, key: SecretStr) -> None:
            pass

        def fetch_csv(self, *args: Any, **kwargs: Any) -> None:
            raise SharadarError("Sharadar answered HTTP 401: bad key")

    monkeypatch.setattr(cli, "get_settings", lambda: settings_with_key(fake_settings, "k-1"))
    monkeypatch.setattr(cli, "SharadarClient", Failing)
    assert companies.main() == 1
    assert "download failed: Sharadar answered HTTP 401" in capsys.readouterr().out


def test_main_downloads_imports_and_prints_a_summary(
    monkeypatch: pytest.MonkeyPatch, fake_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    fetched: dict[str, Any] = {}

    class Working:
        def __init__(self, key: SecretStr) -> None:
            fetched["key"] = key

        def fetch_csv(self, endpoint: str, required: Any, **params: str) -> Download:
            fetched.update(endpoint=endpoint, params=params, required=required)
            return Download(endpoint, [{"permaticker": "1"}], "0" * 64, datetime.now(UTC))

    engine = FakeEngine()
    result = ImportResult(data_version="v1", created=1)
    result.needs_review.append("1 (ZQ): check me")
    monkeypatch.setattr(cli, "get_settings", lambda: settings_with_key(fake_settings, "k-1"))
    monkeypatch.setattr(cli, "SharadarClient", Working)
    monkeypatch.setattr(cli, "make_engine", lambda *a, **k: engine)
    monkeypatch.setattr(companies, "import_companies", lambda conn, download: result)
    assert companies.main() == 0
    out = capsys.readouterr().out
    assert "Downloaded 1 rows" in out and "created 1" in out and "review: 1 (ZQ): check me" in out
    assert fetched["endpoint"] == "tickers" and fetched["params"] == {"table": "stocks"}
    assert "k-1" not in out and engine.disposed


@pytest.mark.parametrize(
    ("delivered", "stored"),
    [
        ("GOOGN GOOGM GOOGL", "GOOGL GOOGM GOOGN"),  # sorted, so the order delivered never matters
        ("brk.a", "BRK.A"),  # upper-cased
        ("", ""),  # imported, none listed
        ("N/A", ""),
        ("  LAFA   LAFAU ", "LAFA LAFAU"),
    ],
)
def test_related_tickers_are_stored_sorted_upper_case_and_empty_when_none(
    delivered: str, stored: str
) -> None:
    record, reason = parse_company(row(relatedtickers=delivered))
    assert reason is None and record is not None
    assert record.info.related_tickers == stored


def test_a_download_without_the_related_tickers_column_is_refused() -> None:
    assert "relatedtickers" in companies.REQUIRED_COLUMNS
    assert "related_tickers" in companies.DESCRIPTIVE_FIELDS  # so a changed list updates the row
