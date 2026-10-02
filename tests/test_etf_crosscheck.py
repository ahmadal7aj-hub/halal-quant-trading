"""The SEC client and the ETF holdings cross-check, with made-up filings (no network)."""

import json
import urllib.error
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from defusedxml.common import EntitiesForbidden

from halal_quant.sharia import etf_crosscheck as ec
from halal_quant.sharia.crosscheck import OurRecord
from halal_quant.sharia.etf_crosscheck import (
    EdgarClient,
    EdgarError,
    Holding,
    compare_holdings,
    parse_holdings,
    render_markdown,
)

NS = "http://www.sec.gov/edgar/nport"


def holding_xml(name: str, ticker: str | None, value: str, category: str = "EC") -> str:
    identifiers = (
        f'<identifiers><isin value="US0000000001"/><ticker value="{ticker}"/></identifiers>'
    )
    return (
        f"<invstOrSec><name>{name}</name><title>{name}</title>"
        f"{identifiers if ticker else ''}<valUSD>{value}</valUSD><assetCat>{category}</assetCat>"
        "</invstOrSec>"
    )


def filing_xml(series: str, *holdings: str) -> bytes:
    body = "".join(holdings)
    return (
        f'<?xml version="1.0"?><edgarSubmission xmlns="{NS}"><headerData><filerInfo>'
        f"<seriesClassInfo><seriesId>{series}</seriesId></seriesClassInfo></filerInfo></headerData>"
        f"<formData><invstOrSecs>{body}</invstOrSecs></formData></edgarSubmission>"
    ).encode()


def test_holdings_are_parsed_with_ticker_value_and_category() -> None:
    xml = filing_xml(
        "S1",
        holding_xml("Apple Inc", "AAPL", "1500.50"),
        holding_xml("Money Fund", "FGXXX", "10", "STIV"),
        holding_xml("No Ticker Co", None, "5"),
    )
    parsed = parse_holdings(xml)
    assert [(h.name, h.ticker, h.value_usd, h.asset_category) for h in parsed] == [
        ("Apple Inc", "AAPL", Decimal("1500.50"), "EC"),
        ("Money Fund", "FGXXX", Decimal("10"), "STIV"),
        ("No Ticker Co", None, Decimal("5"), "EC"),
    ]


def test_invalid_xml_is_refused_and_dangerous_xml_is_not_expanded() -> None:
    with pytest.raises(EdgarError, match="not valid XML"):
        parse_holdings(b"<not-closed>")
    bomb = b'<!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>'
    with pytest.raises(EntitiesForbidden):  # defusedxml refuses entity declarations
        parse_holdings(bomb)


class FakeSec:
    """A transport answering from a dict of url fragments to bodies."""

    def __init__(self, routes: dict[str, bytes]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, int | None]] = []

    def __call__(self, url: str, first_bytes: int | None = None) -> bytes:
        self.calls.append((url, first_bytes))
        for fragment, body in self.routes.items():
            if fragment in url:
                return body
        raise EdgarError(f"unexpected url {url}")


def submissions(*entries: tuple[str, str, str], extra: list[str] | None = None) -> bytes:
    recent = {
        "form": [e[0] for e in entries],
        "reportDate": [e[1] for e in entries],
        "accessionNumber": [e[2] for e in entries],
    }
    files = [{"name": n} for n in (extra or [])]
    return json.dumps({"filings": {"recent": recent, "files": files}}).encode()


def client(routes: dict[str, bytes]) -> tuple[EdgarClient, FakeSec]:
    sec = FakeSec(routes)
    return EdgarClient("Test test@example.com", transport=sec, sleep=lambda _: None), sec


def test_fund_ids_come_from_the_sec_fund_list() -> None:
    funds = {"fields": ["cik", "seriesId", "classId", "symbol"], "data": [[7, "S9", "C9", "SPUS"]]}
    sec_client, _ = client({"company_tickers_mf.json": json.dumps(funds).encode()})
    assert sec_client.fund_ids("spus") == (7, "S9")
    with pytest.raises(EdgarError, match="no ticker"):
        sec_client.fund_ids("NOPE")


def test_month_end_filings_are_listed_newest_first_from_every_page_and_other_forms_skipped() -> (
    None
):
    recent = submissions(
        ("NPORT-P", "2026-07-31", "0001-26-000003"),
        ("NPORT-P", "2026-07-15", "0001-26-000099"),  # not a month end
        ("10-K", "2026-06-30", "0001-26-000002"),  # another form
        ("NPORT-P", "2019-01-31", "0001-19-000001"),  # before `since`
        extra=["older.json"],
    )
    older = json.dumps(
        {"form": ["NPORT-P"], "reportDate": ["2025-10-31"], "accessionNumber": ["0001-25-000007"]}
    ).encode()
    sec_client, _ = client({"CIK0000000007.json": recent, "older.json": older})
    found = sec_client.nport_filings(7, date(2019, 12, 31))
    assert [(f.report_date, f.accession) for f in found] == [
        (date(2026, 7, 31), "0001-26-000003"),
        (date(2025, 10, 31), "0001-25-000007"),
    ]


def test_the_series_is_read_from_the_top_of_a_filing_with_a_range_request() -> None:
    sec_client, sec = client({"primary_doc.xml": filing_xml("S000067283")})
    assert sec_client.series_of(7, "0001-26-000003") == "S000067283"
    url, first_bytes = sec.calls[-1]
    assert "/000126000003/primary_doc.xml" in url and first_bytes == ec.HEADER_BYTES
    other, _ = client({"primary_doc.xml": b"<x>no series here</x>"})
    assert other.series_of(7, "0001-26-000003") is None


def ours(status: str, ticker: str = "AAPL") -> OurRecord:
    return OurRecord(1, ticker, f"{ticker} Co", status, f"why {status}")


def test_equity_holdings_are_counted_by_our_status_by_count_and_by_value() -> None:
    holdings = [
        Holding("Apple", "AAPL", Decimal(600), "EC"),
        Holding("Bank", "JPM", Decimal(300), "EC"),
        Holding("Foreign", "NVO", Decimal(100), "EC"),  # an ADR: not in our universe
        Holding("Money fund", "FGXXX", Decimal(50), "STIV"),  # not equity
        Holding("No ticker", None, Decimal(10), "EC"),  # no ticker, an unknown name: not matched
    ]
    mine = {"AAPL": ours("HALAL", "AAPL"), "JPM": ours("NON_HALAL", "JPM")}
    result = compare_holdings(date(2026, 7, 31), holdings, mine)
    assert result.equities == 4 and result.equity_value == Decimal(1010)
    assert (result.by_status["HALAL"], result.by_status["NON_HALAL"]) == (1, 1)
    assert result.by_status[ec.NOT_OURS] == 2
    assert result.share("HALAL") == Decimal(1) / 4
    assert result.share("HALAL", True) == Decimal(600) / 1010
    assert [h.ticker for h, _ in result.non_halal] == ["JPM"]
    assert [h.name for h in result.not_ours] == ["Foreign", "No ticker"]


def test_an_empty_fund_has_no_shares() -> None:
    result = compare_holdings(date(2026, 7, 31), [], {})
    assert result.share("HALAL") is None and result.share("HALAL", True) is None


def test_share_class_spellings_are_matched() -> None:
    mine = {"BRK.B": ours("HALAL", "BRK.B")}
    result = compare_holdings(
        date(2026, 7, 31), [Holding("Berkshire", "BRK-B", Decimal(1), "EC")], mine
    )
    assert result.by_status["HALAL"] == 1


def test_the_report_has_one_row_per_date_and_lists_the_biggest_non_halal_holdings() -> None:
    holdings = [
        Holding("Bank", "JPM", Decimal(300), "EC"),
        Holding("Apple", "AAPL", Decimal(700), "EC"),
    ]
    mine = {"AAPL": ours("HALAL", "AAPL"), "JPM": ours("NON_HALAL", "JPM")}
    results = [compare_holdings(date(2026, 7, 31), holdings, mine)]
    text = render_markdown("SPUS", results)
    assert "# SPUS" in text and "| 2026-07-31 | 2 | 50.0% | 70.0% | 50.0% | 0.0% | 0.0% |" in text
    assert "- JPM (Bank): we say NON_HALAL: why NON_HALAL" in text
    assert "None." in render_markdown("X", [compare_holdings(date(2026, 7, 31), [], {})])


def test_a_fund_is_analysed_from_filings_cached_and_other_funds_of_the_trust_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    funds = {
        "fields": ["cik", "seriesId", "classId", "symbol"],
        "data": [[7, "S0001", "C", "MINE"]],
    }
    listing = submissions(
        ("NPORT-P", "2026-07-31", "0001-26-000003"), ("NPORT-P", "2026-04-30", "0001-26-000002")
    )
    routes = {
        "company_tickers_mf.json": json.dumps(funds).encode(),
        "CIK0000000007.json": listing,
        "000126000003/primary_doc.xml": filing_xml("S0001", holding_xml("Apple", "AAPL", "100")),
        "000126000002/primary_doc.xml": filing_xml("S0002", holding_xml("Bank", "JPM", "100")),
    }
    sec_client, sec = client(routes)
    monkeypatch.setattr(ec, "our_records", lambda conn, day, method: {"AAPL": ours("HALAL")})
    results = ec.analyse_fund(sec_client, None, "MINE", date(2020, 1, 1), "AAOIFI-v1", tmp_path)  # type: ignore[arg-type]
    assert [(r.report_date, r.by_status["HALAL"]) for r in results] == [(date(2026, 7, 31), 1)]
    first_run_calls = len(sec.calls)
    again = ec.analyse_fund(sec_client, None, "MINE", date(2020, 1, 1), "AAOIFI-v1", tmp_path)  # type: ignore[arg-type]
    assert [r.report_date for r in again] == [date(2026, 7, 31)]
    new_calls = [url for url, _ in sec.calls[first_run_calls:] if "primary_doc" in url]
    assert new_calls == []  # holdings and series were read from the cache


def test_the_transport_retries_network_errors_and_rate_limits_then_gives_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    outcomes: list[Any] = [
        urllib.error.URLError("temporary"),
        urllib.error.HTTPError("https://data.sec.gov/x", 429, "slow down", {}, None),  # type: ignore[arg-type]
        b"the body",
    ]

    class Response:
        def __init__(self, body: bytes) -> None:
            self.body = body

        def read(self) -> bytes:
            return self.body

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def fake_urlopen(request: object, timeout: float = 0) -> Response:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return Response(outcome)

    monkeypatch.setattr(ec.urllib.request, "urlopen", fake_urlopen)
    fetch = ec._urlopen_transport("Test test@example.com", sleeps.append)
    assert fetch("https://data.sec.gov/x.json", None) == b"the body" and sleeps == [2.0, 4.0]

    def always_down(request: object, timeout: float = 0) -> Response:
        raise urllib.error.URLError("down")

    monkeypatch.setattr(ec.urllib.request, "urlopen", always_down)
    with pytest.raises(EdgarError, match="Could not reach the SEC"):
        fetch("https://data.sec.gov/x.json", None)

    def not_found(request: object, timeout: float = 0) -> Response:
        raise urllib.error.HTTPError("https://data.sec.gov/x", 404, "gone", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(ec.urllib.request, "urlopen", not_found)
    with pytest.raises(EdgarError, match="HTTP 404"):
        fetch("https://data.sec.gov/x.json", None)  # not retried
    with pytest.raises(EdgarError, match="outside sec.gov"):
        fetch("https://example.com/x", None)


def test_main_without_a_contact_stops_and_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class NoContact:
        sec_user_agent = None
        log_level = "INFO"

    monkeypatch.setattr(ec, "get_settings", lambda: NoContact())
    monkeypatch.setattr(ec, "configure_logging", lambda *a, **k: None)
    assert ec.main(["--symbol", "SPUS"]) == 1
    assert "HQ_SEC_USER_AGENT is not set" in capsys.readouterr().out


def test_a_weekend_report_date_looks_up_the_last_trading_day() -> None:
    assert ec.trading_day_on_or_before(date(2026, 5, 31)) == date(2026, 5, 29)  # a Sunday
    assert ec.trading_day_on_or_before(date(2026, 7, 31)) == date(2026, 7, 31)  # a Friday


def test_each_report_date_is_counted_once_even_if_the_trust_filed_several_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    funds = {
        "fields": ["cik", "seriesId", "classId", "symbol"],
        "data": [[7, "S0001", "C", "MINE"]],
    }
    listing = submissions(
        ("NPORT-P", "2026-05-31", "0001-26-000005"), ("NPORT-P", "2026-05-31", "0001-26-000004")
    )
    routes = {
        "company_tickers_mf.json": json.dumps(funds).encode(),
        "CIK0000000007.json": listing,
        "000126000005/primary_doc.xml": filing_xml("S0002", holding_xml("Bank", "JPM", "100")),
        "000126000004/primary_doc.xml": filing_xml("S0001", holding_xml("Apple", "AAPL", "100")),
    }
    sec_client, _ = client(routes)
    asked: list[date] = []

    def records(conn: object, day: date, method: str) -> dict[str, OurRecord]:
        asked.append(day)
        return {"AAPL": ours("HALAL")}

    monkeypatch.setattr(ec, "our_records", records)
    results = ec.analyse_fund(sec_client, None, "MINE", date(2020, 1, 1), "AAOIFI-v1", tmp_path)  # type: ignore[arg-type]
    assert [(r.report_date, r.equities) for r in results] == [(date(2026, 5, 31), 1)]
    assert asked == [date(2026, 5, 29)]  # the Friday before the Sunday report date


def test_names_are_reduced_to_what_identifies_a_company() -> None:
    assert ec.name_key("CDW Corp/DE") == ec.name_key("CDW CORP") == "CDW"
    assert ec.name_key("Johnson & Johnson") == "JOHNSON AND JOHNSON"
    assert ec.name_key("The Coca-Cola Company") == "COCA COLA"


def test_old_filings_without_tickers_are_matched_by_name_when_unambiguous() -> None:
    mine = {
        "ABT": OurRecord(1, "ABT", "ABBOTT LABORATORIES", "HALAL", "r"),
        "CDW": OurRecord(2, "CDW", "CDW CORP", "NON_HALAL", "r"),
        "HEI": OurRecord(3, "HEI", "HEICO CORP", "HALAL", "r"),
        "HEI.A": OurRecord(4, "HEI.A", "HEICO CORP", "UNKNOWN", "no data of its own"),
        "TWIN1": OurRecord(5, "TWIN1", "TWIN CORP", "HALAL", "r"),
        "TWIN2": OurRecord(6, "TWIN2", "TWIN CORP", "NON_HALAL", "r"),
    }
    holdings = [
        Holding("Abbott Laboratories", None, Decimal(10), "EC"),
        Holding("CDW Corp/DE", None, Decimal(10), "EC"),
        Holding("HEICO Corp", None, Decimal(10), "EC"),  # two classes: the decided one is used
        Holding("Twin Corp", None, Decimal(10), "EC"),  # two decided classes that disagree
        Holding("Unheard Of Inc", None, Decimal(10), "EC"),
        Holding("", None, Decimal(10), "EC"),  # no ticker and no name: cannot be matched at all
    ]
    result = compare_holdings(date(2022, 2, 28), holdings, mine)
    assert result.equities == 5
    assert (result.by_status["HALAL"], result.by_status["NON_HALAL"]) == (2, 1)
    assert result.by_status[ec.NOT_OURS] == 2  # the ambiguous twin and the unknown company


def test_a_ticker_we_do_not_know_falls_back_to_the_name() -> None:
    mine = {"NEWT": OurRecord(1, "NEWT", "RENAMED CORP", "HALAL", "r")}
    result = compare_holdings(
        date(2024, 1, 31), [Holding("Renamed Corp", "OLDT", Decimal(1), "EC")], mine
    )
    assert result.by_status["HALAL"] == 1
