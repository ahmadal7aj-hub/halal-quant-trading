"""Parsing Sharadar price and action rows, month windows and the ticker lookup (made-up data)."""

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from halal_quant.data.security_master import TickerDirectory
from halal_quant.data.sharadar import actions, prices
from halal_quant.data.sharadar.actions import ActionResult, parse_action
from halal_quant.data.sharadar.prices import PriceResult, month_windows, parse_price
from halal_quant.data.sharadar.values import parse_date, parse_decimal

DIRECTORY = TickerDirectory(
    [
        ("ZQA", 1, date(2020, 1, 1), None),
        ("ZQB", 2, date(2020, 1, 1), date(2022, 1, 1)),  # delisted: last day 2021-12-31
        ("ZQB", 3, date(2023, 1, 1), None),  # the ticker was reused by another company
    ]
)


def price_row(**overrides: str) -> dict[str, str]:
    base = {
        "ticker": "zqa",
        "date": "2024-03-04",
        "open": "126.013",
        "high": "126.442",
        "low": "124.578",
        "close": "124.808",
        "volume": "187630000",
        "closeadj": "120.965",
        "closeunadj": "499.23",
        "lastupdated": "2026-08-10",
    }
    return {**base, **overrides}


def action_row(**overrides: str) -> dict[str, str]:
    base = {
        "date": "2024-03-04",
        "action": "dividend",
        "ticker": "ZQA",
        "name": "Example Corp",
        "value": "0.91",
        "contraticker": "N/A",
        "contraname": "N/A",
    }
    return {**base, **overrides}


def test_ticker_directory_follows_dated_periods_and_reuse() -> None:
    assert DIRECTORY.resolve(" zqa ", date(2030, 1, 1)) == 1
    assert DIRECTORY.resolve("ZQA", date(2019, 12, 31)) is None
    assert DIRECTORY.resolve("ZQB", date(2021, 12, 31)) == 2
    assert DIRECTORY.resolve("ZQB", date(2022, 6, 1)) is None
    assert DIRECTORY.resolve("ZQB", date(2023, 1, 1)) == 3
    assert DIRECTORY.resolve("NOPE", date(2024, 1, 1)) is None


def test_values_treat_blank_and_na_as_missing_and_refuse_junk() -> None:
    assert parse_decimal("N/A") is None and parse_date(" ") is None
    assert parse_decimal("1.5") == Decimal("1.5")
    for junk in ("abc", "NaN", "Infinity"):
        with pytest.raises(ValueError):
            parse_decimal(junk)


def test_a_price_row_becomes_a_price_with_all_three_closes() -> None:
    parsed, reason = parse_price(price_row(), DIRECTORY, "v1")
    assert reason is None and parsed is not None
    price, ticker = parsed
    assert ticker == "ZQA" and price.security_id == 1
    assert (price.close, price.adjusted_close, price.close_unadjusted) == (
        Decimal("124.808"),
        Decimal("120.965"),
        Decimal("499.23"),
    )
    assert price.volume == 187630000 and price.data_version == "v1" and price.source == "sharadar"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"ticker": ""}, "missing_ticker"),
        ({"date": ""}, "missing_date"),
        ({"date": "04/03/2024"}, "bad_value"),
        ({"open": "x"}, "bad_value"),
        ({"close": ""}, "missing_value"),
        ({"volume": "-1"}, "impossible_price_or_volume"),
        ({"low": "0"}, "impossible_price_or_volume"),
        ({"ticker": "ZQZ"}, "no_security_for_ticker_on_date"),
        ({"date": "2019-01-02"}, "no_security_for_ticker_on_date"),
    ],
)
def test_unusable_price_rows_say_why(overrides: dict[str, str], reason: str) -> None:
    assert parse_price(price_row(**overrides), DIRECTORY, "v1") == (None, reason)


def test_suspicious_but_possible_prices_are_kept_for_the_quality_checks() -> None:
    parsed, _ = parse_price(price_row(high="1", low="2"), DIRECTORY, "v1")
    assert parsed is not None


def test_a_volume_written_with_a_decimal_point_is_accepted() -> None:
    parsed, _ = parse_price(price_row(volume="1000.0"), DIRECTORY, "v1")
    assert parsed is not None and parsed[0].volume == 1000


@pytest.mark.parametrize(
    ("delivered", "stored"), [("6239.16", 6239), ("283000.5", 283001), ("0.4", 0), ("10.5", 11)]
)
def test_a_fractional_split_adjusted_volume_is_rounded_to_whole_shares(
    delivered: str, stored: int
) -> None:
    parsed, _ = parse_price(price_row(volume=delivered), DIRECTORY, "v1")
    assert parsed is not None and parsed[0].volume == stored


def test_month_windows_cover_every_day_once() -> None:
    windows = list(month_windows(date(2023, 12, 15), date(2024, 3, 2)))
    assert windows == [
        ("2023-12", date(2023, 12, 1), date(2023, 12, 31)),
        ("2024-01", date(2024, 1, 1), date(2024, 1, 31)),
        ("2024-02", date(2024, 2, 1), date(2024, 2, 29)),
        ("2024-03", date(2024, 3, 1), date(2024, 3, 31)),
    ]


def test_dividend_and_split_keep_their_value() -> None:
    dividend, _ = parse_action(action_row(), DIRECTORY, "v1")
    split, _ = parse_action(action_row(action="split", value="0.5"), DIRECTORY, "v1")
    assert dividend is not None and (dividend.action_type, dividend.value) == (
        "DIVIDEND",
        Decimal("0.91"),
    )
    assert split is not None and (split.action_type, split.value) == ("SPLIT", Decimal("0.5"))


def test_an_acquisition_keeps_its_deal_size_in_details_and_links_the_other_company() -> None:
    parsed, _ = parse_action(
        action_row(
            action="acquisitionof",
            value="6111.1",
            contraticker="zqb",
            contraname="Target Corp",
            date="2021-06-01",
        ),
        DIRECTORY,
        "v1",
    )
    assert parsed is not None
    assert parsed.action_type == "MERGER" and parsed.value is None
    assert parsed.related_security_id == 2
    assert parsed.details == {
        "sharadar_action": "acquisitionof",
        "contra_ticker": "ZQB",
        "contra_name": "Target Corp",
        "sharadar_value": "6111.1",
    }


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"action": "spinoff"}, "unmapped_action:spinoff"),
        ({"action": "relation"}, "unmapped_action:relation"),
        ({"action": ""}, "missing_key_field"),
        ({"ticker": ""}, "missing_key_field"),
        ({"date": ""}, "missing_date"),
        ({"date": "x"}, "bad_value"),
        ({"value": "abc"}, "bad_value"),
        ({"value": ""}, "bad_value"),  # a dividend needs an amount
        ({"action": "split", "value": "0"}, "bad_value"),
        ({"ticker": "ZQZ"}, "no_security_for_ticker_on_date"),
    ],
)
def test_unusable_action_rows_say_why(overrides: dict[str, str], reason: str) -> None:
    assert parse_action(action_row(**overrides), DIRECTORY, "v1") == (None, reason)


def test_summaries_are_readable() -> None:
    p = PriceResult(data_version="v", inserted=3, already_present=2, restated=1)
    p.first_date, p.last_date = date(2024, 1, 2), date(2024, 1, 31)
    assert "inserted 3, already present 2 (of which restated by Sharadar: 1)" in p.summary()
    a = ActionResult(data_version="v", inserted=1)
    a.by_type["SPLIT"] += 1
    assert "SPLIT: 1" in a.summary()


def test_price_main_runs_one_import_per_month_and_stops_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, str]] = []

    def fake_run_import(endpoint: str, required: Any, apply: Any, **params: str) -> int:
        assert endpoint == "stocks" and "closeunadj" in tuple(required)
        calls.append(params)
        return 1 if len(calls) == 2 else 0

    monkeypatch.setattr(prices, "run_import", fake_run_import)
    assert prices.main(["--start", "2024-01", "--end", "2024-03"]) == 1
    assert calls == [
        {"date.gte": "2024-01-01", "date.lte": "2024-01-31"},
        {"date.gte": "2024-02-01", "date.lte": "2024-02-29"},
    ]
    monkeypatch.setattr(prices, "run_import", lambda *a, **k: 0)
    assert prices.main(["--start", "2024-01", "--end", "2024-01"]) == 0


def test_price_main_refuses_a_bad_month() -> None:
    with pytest.raises(SystemExit):
        prices.main(["--start", "January"])


def test_the_import_shells_use_the_shared_command(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    def fake_run_import(endpoint: str, required: Any, apply: Any, **params: str) -> int:
        seen.append((endpoint, apply))
        return 0

    monkeypatch.setattr(actions, "run_import", fake_run_import)
    monkeypatch.setattr(actions, "import_actions", lambda conn, download: ActionResult("v"))
    assert actions.main() == 0 and seen[0][0] == "actions"
    assert seen[0][1]("conn", object()).data_version == "v"
    monkeypatch.setattr(prices, "run_import", fake_run_import)
    monkeypatch.setattr(prices, "import_prices", lambda conn, download, w: PriceResult("v"))
    assert prices.main(["--start", "2024-01", "--end", "2024-01"]) == 0
    assert seen[1][0] == "stocks" and seen[1][1]("conn", object()).data_version == "v"
