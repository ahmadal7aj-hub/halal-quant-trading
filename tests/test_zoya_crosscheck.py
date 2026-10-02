"""The Zoya client and the cross-check, with made-up data and a fake transport (no network)."""

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from halal_quant.sharia import crosscheck as cc
from halal_quant.sharia import zoya
from halal_quant.sharia.zoya import ZoyaClient, ZoyaError, ZoyaReport, authorization_value

KEY = "live-FAKE-KEY-FOR-TESTS-0123456789"


def item(symbol: str, status: str = "COMPLIANT", **kw: Any) -> dict[str, Any]:
    base = {
        "symbol": symbol,
        "name": f"{symbol} Inc",
        "exchange": "NASDAQ",
        "status": status,
        "purificationRatio": 0.02,
        "reportDate": "2026-08-06T00:00:00Z",
    }
    return {**base, **kw}


def pages(*page_items: list[dict[str, Any]], field: str = "reports") -> Any:
    calls: list[dict[str, Any]] = []

    def transport(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        calls.append(variables)
        index = len(calls) - 1
        token = f"t{index + 1}" if index + 1 < len(page_items) else None
        return {
            "data": {"basicCompliance": {field: {"items": page_items[index], "nextToken": token}}}
        }

    transport.calls = calls  # type: ignore[attr-defined]
    return transport


def client(transport: Any) -> ZoyaClient:
    return ZoyaClient(SecretStr(KEY), transport=transport, sleep=lambda _: None)


def test_the_key_is_used_as_shown_and_a_bare_key_is_treated_as_live() -> None:
    assert authorization_value(" live-abc ") == "live-abc"
    assert authorization_value("sandbox-abc") == "sandbox-abc"
    assert authorization_value("abc") == "live-abc"


def test_every_page_is_followed_until_the_token_runs_out() -> None:
    transport = pages([item("AAPL"), item("MSFT", "QUESTIONABLE")], [item("JPM", "NON_COMPLIANT")])
    reports = client(transport).fetch_us_reports()
    assert [r.symbol for r in reports] == ["AAPL", "MSFT", "JPM"]
    assert transport.calls == [
        {"input": {"limit": 1000}},
        {"input": {"limit": 1000, "nextToken": "t1"}},
    ]
    assert reports[0].purification_ratio == Decimal("0.02") and reports[0].report_date == date(
        2026, 8, 6
    )


def test_funds_use_their_own_query_and_page_size() -> None:
    transport = pages([item("SPUS")], field="funds")
    assert [r.symbol for r in client(transport).fetch_funds()] == ["SPUS"]
    assert transport.calls == [{"input": {"limit": 2000}}]


def test_one_stock_report_and_a_missing_one() -> None:
    found = client(lambda q, v: {"data": {"basicCompliance": {"report": item("AAPL")}}}).report(
        "AAPL"
    )
    assert found is not None and found.status == "COMPLIANT"
    assert (
        client(lambda q, v: {"data": {"basicCompliance": {"report": None}}}).report("ZZZZ") is None
    )


def test_symbols_are_upper_cased_and_nullable_fields_are_accepted() -> None:
    parsed = zoya.parse_report(
        item(" brk.b ", "UNRATED", purificationRatio=None, reportDate=None, exchange=None)
    )
    assert (
        parsed.symbol == "BRK.B"
        and parsed.purification_ratio is None
        and parsed.report_date is None
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"symbol": "X"},
        item("X", "MAYBE"),
        item("X", reportDate="not a date"),
        item("X", purificationRatio="abc"),
    ],
)
def test_malformed_reports_are_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ZoyaError):
        zoya.parse_report(bad)


def test_graphql_errors_become_zoya_errors_without_the_key() -> None:
    boom = client(lambda q, v: {"errors": [{"message": "Not authorised"}], "data": None})
    with pytest.raises(ZoyaError, match="Not authorised") as caught:
        boom.fetch_us_reports()
    assert KEY not in str(caught.value)


def test_an_unexpected_response_shape_is_refused() -> None:
    with pytest.raises(ZoyaError, match="unexpected shape"):
        client(lambda q, v: {"data": {"somethingElse": {}}}).fetch_us_reports()


def test_rate_limiting_is_retried_with_a_pause_then_gives_up() -> None:
    sleeps: list[float] = []
    attempts = {"n": 0}

    def flaky(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ZoyaError("Zoya answered HTTP 429: slow down")
        return {
            "data": {"basicCompliance": {"reports": {"items": [item("AAPL")], "nextToken": None}}}
        }

    assert [
        r.symbol
        for r in ZoyaClient(SecretStr(KEY), transport=flaky, sleep=sleeps.append).fetch_us_reports()
    ] == ["AAPL"]
    assert sleeps == [2.0, 4.0]

    def always(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        raise ZoyaError("Zoya answered HTTP 429: slow down")

    with pytest.raises(ZoyaError, match="429"):
        client(always).fetch_us_reports()
    with pytest.raises(ZoyaError, match="HTTP 500"):
        client(
            lambda q, v: (_ for _ in ()).throw(ZoyaError("Zoya answered HTTP 500: x"))
        ).fetch_us_reports()


def ours(ticker: str, status: str, reason: str = "r") -> cc.OurRecord:
    return cc.OurRecord(1, ticker, f"{ticker} Co", status, reason)


@pytest.mark.parametrize(
    ("mine", "theirs", "group"),
    [
        ("HALAL", "COMPLIANT", cc.AGREE_HALAL),
        ("HALAL", "NON_COMPLIANT", cc.WE_HALAL_ZOYA_NOT),
        ("HALAL", "QUESTIONABLE", cc.WE_HALAL_ZOYA_QUESTIONABLE),
        ("HALAL", "UNRATED", cc.ZOYA_UNRATED),
        ("NON_HALAL", "NON_COMPLIANT", cc.AGREE_NOT_HALAL),
        ("NON_HALAL", "COMPLIANT", cc.WE_NOT_ZOYA_COMPLIANT),
        ("NON_HALAL", "QUESTIONABLE", cc.WE_NOT_ZOYA_QUESTIONABLE),
        ("NON_HALAL", "UNRATED", cc.ZOYA_UNRATED),
        ("UNKNOWN", "COMPLIANT", cc.WE_UNKNOWN_ZOYA_DECIDED),
        ("PENDING_REVIEW", "NON_COMPLIANT", cc.WE_UNKNOWN_ZOYA_DECIDED),
        ("UNKNOWN", "UNRATED", cc.BOTH_UNDECIDED),
    ],
)
def test_every_pair_of_statuses_lands_in_one_group(mine: str, theirs: str, group: str) -> None:
    assert cc.classify_pair(mine, theirs) == group


def report(symbol: str, status: str) -> ZoyaReport:
    return ZoyaReport(symbol, f"{symbol} Inc", "NASDAQ", status, None, date(2026, 8, 6))


def test_the_headline_numbers_only_count_firm_pairs() -> None:
    mine = {
        "A": ours("A", "HALAL"), "B": ours("B", "HALAL"), "C": ours("C", "NON_HALAL"),
        "D": ours("D", "NON_HALAL"), "E": ours("E", "NON_HALAL"), "F": ours("F", "UNKNOWN"),
        "G": ours("G", "HALAL"), "NOZ": ours("NOZ", "HALAL"),
    }  # fmt: skip
    theirs = [
        report("A", "COMPLIANT"), report("B", "NON_COMPLIANT"), report("C", "NON_COMPLIANT"),
        report("D", "NON_COMPLIANT"), report("E", "COMPLIANT"), report("F", "COMPLIANT"),
        report("G", "QUESTIONABLE"), report("ONLYZ", "COMPLIANT"),
    ]  # fmt: skip
    check = cc.compare(mine, theirs, date(2026, 10, 2))
    assert (check.matched, check.only_ours, check.only_zoya) == (7, 1, 1)
    assert check.firm_pairs == 5 and check.agreement_rate == Decimal(3) / 5
    assert check.halal_precision == Decimal(1) / 2  # A agrees, B does not
    assert check.halal_recall == Decimal(1) / 2  # A agrees, E missed
    assert [r.ticker for r in check.ours_not_in_zoya] == ["NOZ"]


def test_empty_comparisons_have_no_rates() -> None:
    check = cc.compare({}, [], date(2026, 10, 2))
    assert (
        check.agreement_rate is None
        and check.halal_precision is None
        and check.halal_recall is None
    )


def test_symbols_are_compared_on_one_spelling() -> None:
    assert cc.normalise_symbol(" brk-b ") == "BRK.B" == cc.normalise_symbol("BRK/B")
    check = cc.compare(
        {"BRK.B": ours("BRK.B", "HALAL")}, [report("BRK-B", "COMPLIANT")], date(2026, 10, 2)
    )
    assert check.matched == 1


def test_examples_are_capped_and_the_report_lists_counts_not_everything() -> None:
    mine = {f"T{i}": ours(f"T{i}", "HALAL") for i in range(30)}
    theirs = [report(f"T{i}", "NON_COMPLIANT") for i in range(30)]
    check = cc.compare(mine, theirs, date(2026, 10, 2))
    assert check.groups[cc.WE_HALAL_ZOYA_NOT] == 30
    assert len(check.examples[cc.WE_HALAL_ZOYA_NOT]) == cc.EXAMPLES_PER_GROUP
    text = cc.render_markdown(check)
    assert "| we_halal_zoya_non_compliant | 30 |" in text
    assert sum(1 for line in text.splitlines() if line.startswith("- T")) == cc.EXAMPLES_PER_GROUP
    assert "Agreement on firm pairs" in text and "0.0%" in text


def test_snapshots_round_trip(tmp_path: Path) -> None:
    original = [
        report("AAPL", "COMPLIANT"),
        ZoyaReport("X", "X", None, "UNRATED", Decimal("0.5"), None),
    ]
    path = tmp_path / "deep" / "snapshot.json"
    cc.save_snapshot(path, original)
    assert cc.load_snapshot(path) == original


def test_main_without_a_key_stops_and_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class NoKey:
        zoya_api_key = None
        log_level = "INFO"

    monkeypatch.setattr(cc, "get_settings", lambda: NoKey())
    monkeypatch.setattr(cc, "configure_logging", lambda *a, **k: None)
    assert cc.main([]) == 1
    assert "HQ_ZOYA_API_KEY is not set" in capsys.readouterr().out
