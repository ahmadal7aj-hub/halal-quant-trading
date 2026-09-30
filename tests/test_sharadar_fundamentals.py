"""Parsing Sharadar fundamentals and daily market value rows (made-up data)."""

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from halal_quant.data.security_master import TickerDirectory
from halal_quant.data.sharadar import fundamentals, market_cap
from halal_quant.data.sharadar.fundamentals import (
    FundamentalsResult,
    parse_fundamental,
    year_windows,
)
from halal_quant.data.sharadar.market_cap import MarketCapResult, parse_market_cap

DIRECTORY = TickerDirectory(
    [
        ("ZQA", 1, date(2015, 1, 1), None),
        ("ZQB", 2, date(2015, 1, 1), date(2020, 1, 1)),
    ]
)


def fundamental_row(**overrides: str) -> dict[str, str]:
    base = {
        "ticker": "zqa",
        "dimension": "ARQ",
        "calendardate": "2019-09-30",
        "date": "2019-11-01",
        "reportperiod": "2019-09-28",
        "fiscalperiod": "2019-Q4",
        "debt": "108047000000",
        "debtc": "N/A",
        "cashneq": "48844000000",
        "investments": "157054000000",
        "revenue": "64040000000",
        "ebit": "16937000000",
        "opinc": "15625000000",
        "marketcap": "1105306601400",
    }
    return {**base, **overrides}


def test_a_fundamentals_row_is_keyed_by_filing_date_and_security() -> None:
    parsed, reason = parse_fundamental(fundamental_row(), DIRECTORY)
    assert reason is None and parsed is not None
    key, values = parsed
    assert key == (1, "ARQ", date(2019, 9, 30), date(2019, 11, 1))
    assert values["debt"] == Decimal("108047000000")
    assert values["debt_current"] is None  # "N/A" means no value
    assert values["report_period"] == date(2019, 9, 28) and values["fiscal_period"] == "2019-Q4"


@pytest.mark.parametrize("dimension", ["MRQ", "MRY", "MRT", ""])
def test_restated_most_recent_dimensions_are_never_used(dimension: str) -> None:
    row = fundamental_row(dimension=dimension)
    assert parse_fundamental(row, DIRECTORY) == (None, "not_an_as_reported_dimension")


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"ticker": ""}, "missing_ticker"),
        ({"date": ""}, "missing_date"),
        ({"calendardate": ""}, "missing_date"),
        ({"date": "01/11/2019"}, "bad_value"),
        ({"debt": "lots"}, "bad_value"),
        ({"ticker": "ZQZ"}, "no_security_for_ticker_on_date"),
        (
            {"ticker": "ZQB", "date": "2020-02-03"},
            "no_security_for_ticker_on_date",
        ),  # after delisting
    ],
)
def test_unusable_fundamentals_rows_say_why(overrides: dict[str, str], reason: str) -> None:
    assert parse_fundamental(fundamental_row(**overrides), DIRECTORY) == (None, reason)


def test_the_ticker_is_matched_on_the_filing_date() -> None:
    row = fundamental_row(ticker="ZQB", date="2019-11-01")
    parsed, _ = parse_fundamental(row, DIRECTORY)
    assert parsed is not None and parsed[0][0] == 2


def test_year_windows_cover_every_dimension_of_every_year() -> None:
    windows = list(year_windows(2018, 2019))
    assert [label for label, _, _ in windows] == [
        "ARQ.2018",
        "ARY.2018",
        "ART.2018",
        "ARQ.2019",
        "ARY.2019",
        "ART.2019",
    ]
    assert windows[0][1:] == (date(2018, 1, 1), date(2018, 12, 31))


def test_market_value_is_converted_from_millions_to_dollars() -> None:
    parsed, reason = parse_market_cap(
        {"ticker": "zqa", "date": "2019-11-05", "marketcap": "1142487.8"}, DIRECTORY
    )
    assert reason is None and parsed == ((1, date(2019, 11, 5)), Decimal("1142487800000.0"))


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"ticker": ""}, "missing_ticker"),
        ({"date": ""}, "missing_date"),
        ({"date": "x"}, "bad_value"),
        ({"marketcap": "N/A"}, "missing_value"),
        ({"marketcap": "abc"}, "bad_value"),
        ({"ticker": "ZQZ"}, "no_security_for_ticker_on_date"),
    ],
)
def test_unusable_market_value_rows_say_why(overrides: dict[str, str], reason: str) -> None:
    row = {"ticker": "zqa", "date": "2019-11-05", "marketcap": "10", **overrides}
    assert parse_market_cap(row, DIRECTORY) == (None, reason)


def test_summaries_name_the_data_version_and_the_skips() -> None:
    fundamentals = FundamentalsResult(data_version="v1")
    fundamentals.skipped["bad_value"] += 2
    market_cap = MarketCapResult(data_version="v2")
    assert "v1" in fundamentals.summary() and "bad_value: 2" in fundamentals.summary()
    assert "v2" in market_cap.summary() and "none" in market_cap.summary()


def test_fundamentals_main_runs_one_download_per_dimension_and_year(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, str]] = []

    def fake_run_import(endpoint: str, required: Any, apply: Any, **params: str) -> int:
        assert endpoint == "fundamentals" and "debt" in tuple(required)
        calls.append(params)
        return 1 if len(calls) == 2 else 0

    monkeypatch.setattr(fundamentals, "run_import", fake_run_import)
    assert fundamentals.main(["--start", "2019", "--end", "2019"]) == 1
    assert calls == [
        {"dimension": "ARQ", "date.gte": "2019-01-01", "date.lte": "2019-12-31"},
        {"dimension": "ARY", "date.gte": "2019-01-01", "date.lte": "2019-12-31"},
    ]
    seen: list[Any] = []
    monkeypatch.setattr(
        fundamentals, "run_import", lambda e, r, apply, **k: seen.append(apply) or 0
    )
    monkeypatch.setattr(
        fundamentals, "import_fundamentals", lambda conn, dl, w: FundamentalsResult("v")
    )
    assert fundamentals.main(["--start", "2019", "--end", "2019"]) == 0
    assert seen[0]("conn", object()).data_version == "v"


def test_market_cap_main_runs_one_download_per_month(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, str]] = []

    def fake_run_import(endpoint: str, required: Any, apply: Any, **params: str) -> int:
        assert endpoint == "daily" and "marketcap" in tuple(required)
        calls.append(params)
        return 1 if len(calls) == 2 else 0

    monkeypatch.setattr(market_cap, "run_import", fake_run_import)
    assert market_cap.main(["--start", "2024-01", "--end", "2024-03"]) == 1
    assert calls == [
        {"date.gte": "2024-01-01", "date.lte": "2024-01-31"},
        {"date.gte": "2024-02-01", "date.lte": "2024-02-29"},
    ]
    seen: list[Any] = []
    monkeypatch.setattr(market_cap, "run_import", lambda e, r, apply, **k: seen.append(apply) or 0)
    monkeypatch.setattr(market_cap, "import_market_cap", lambda conn, dl, w: MarketCapResult("v"))
    assert market_cap.main(["--start", "2024-01", "--end", "2024-01"]) == 0
    assert seen[0]("conn", object()).data_version == "v"


def test_market_cap_main_refuses_a_bad_month() -> None:
    with pytest.raises(SystemExit):
        market_cap.main(["--start", "January"])
