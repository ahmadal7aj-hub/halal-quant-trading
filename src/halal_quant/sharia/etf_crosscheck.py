"""Compare our Halal screen with what Sharia ETFs actually held (task 15; doc 01 §3).

    uv run python -m halal_quant.sharia.etf_crosscheck --symbol SPUS [--symbol HLAL]
        [--since 2019-12-31] [--report path.md]

SPUS and HLAL follow professional Sharia screens (S&P and FTSE Russell). Their quarterly N-PORT
filings on SEC EDGAR (free) list every holding with its ticker and value. For each report date we
look up our own classification in effect on that date and count how many of their holdings we
call HALAL, NON_HALAL or UNKNOWN. A high HALAL share on every date means our historical screen
agrees with an independent professional one; a pattern of NON_HALAL shows where our rules are
stricter than theirs.

SEC asks automated clients to identify themselves, so requests carry `HQ_SEC_USER_AGENT`
(a name and contact) and are paced below the SEC limit of 10 requests a second. Fetched filings
are cached under `private/sec/` (git-ignored) so a re-run costs nothing.
"""

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from defusedxml import ElementTree
from sqlalchemy import Connection

from halal_quant.core.logging import configure_logging, correlation_scope
from halal_quant.core.settings import DbRole, get_settings
from halal_quant.data.calendar import is_trading_day, previous_trading_day
from halal_quant.db.engine import make_engine
from halal_quant.sharia.crosscheck import OurRecord, normalise_symbol, our_records

NPORT_NS = "{http://www.sec.gov/edgar/nport}"
TICKERS_URL = "https://www.sec.gov/files/company_tickers_mf.json"
PAUSE_SECONDS = 0.15  # the SEC limit is 10 requests a second
HEADER_BYTES = 4000  # enough to read the series id at the top of a filing
EXAMPLES = 12
NOT_OURS = "NOT_IN_OUR_UNIVERSE"
ATTEMPTS = 5
RETRY_CODES = (429, 500, 502, 503, 504)  # rate limited or a server hiccup: try again

# Takes (url, number of leading bytes or None for the whole body), returns the body.
Transport = Callable[[str, int | None], bytes]


class EdgarError(Exception):
    """A SEC request failed or returned something unexpected."""


@dataclass(frozen=True)
class Holding:
    name: str
    ticker: str | None
    value_usd: Decimal
    asset_category: str  # N-PORT code: EC = common equity, STIV = short-term investment ...


@dataclass(frozen=True)
class Filing:
    report_date: date
    accession: str


@dataclass
class QuarterResult:
    report_date: date
    equities: int = 0
    equity_value: Decimal = Decimal(0)
    by_status: Counter[str] = field(default_factory=Counter)  # count of holdings
    value_by_status: dict[str, Decimal] = field(default_factory=dict)
    not_ours: list[Holding] = field(default_factory=list)
    non_halal: list[tuple[Holding, OurRecord]] = field(default_factory=list)

    def share(self, status: str, by_value: bool = False) -> Decimal | None:
        """The share of the equity holdings with this status, by count or by value."""
        if by_value:
            part = self.value_by_status.get(status, Decimal(0))
            return None if not self.equity_value else part / self.equity_value
        return None if not self.equities else Decimal(self.by_status[status]) / self.equities


def _urlopen_transport(user_agent: str, sleep: Callable[[float], None] = time.sleep) -> Transport:
    def fetch(url: str, first_bytes: int | None = None) -> bytes:
        if not url.startswith(("https://www.sec.gov/", "https://data.sec.gov/")):
            raise EdgarError("Refusing to call a URL outside sec.gov.")
        headers = {"User-Agent": user_agent, "Accept-Encoding": "identity"}
        if first_bytes:
            headers["Range"] = f"bytes=0-{first_bytes - 1}"
        problem = "Could not reach the SEC (network error or timeout)."
        for attempt in range(1, ATTEMPTS + 1):
            request = urllib.request.Request(url, headers=headers)  # nosec B310
            try:
                with urllib.request.urlopen(request, timeout=90) as response:  # nosec B310
                    body: bytes = response.read()
                    return body
            except urllib.error.HTTPError as exc:
                problem = f"SEC answered HTTP {exc.code} for {url.rsplit('/', 1)[-1]}"
                if exc.code not in RETRY_CODES:
                    raise EdgarError(problem) from None
            except (urllib.error.URLError, TimeoutError, OSError):
                problem = "Could not reach the SEC (network error or timeout)."
            if attempt < ATTEMPTS:
                sleep(2.0 * attempt)  # back off, then try again
        raise EdgarError(problem)

    return fetch


class EdgarClient:
    def __init__(
        self,
        user_agent: str,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._get = transport or _urlopen_transport(user_agent)
        self._sleep = sleep

    def _json(self, url: str) -> Any:
        self._sleep(PAUSE_SECONDS)
        try:
            return json.loads(self._get(url, None))
        except json.JSONDecodeError:
            raise EdgarError("The SEC returned something that is not JSON.") from None

    def fund_ids(self, symbol: str) -> tuple[int, str]:
        """(trust CIK, series id) of a fund ticker, from the SEC's fund ticker list."""
        data = self._json(TICKERS_URL)
        fields = data["fields"]
        for row in data["data"]:
            record = dict(zip(fields, row, strict=False))
            if str(record.get("symbol", "")).upper() == symbol.upper():
                return int(record["cik"]), str(record["seriesId"])
        raise EdgarError(f"The SEC fund list has no ticker {symbol}.")

    def nport_filings(self, cik: int, since: date) -> list[Filing]:
        """Month-end N-PORT filings of the trust (all its funds), newest first.

        Funds have their own fiscal calendars, so every month-end report date is kept.
        """
        submissions = self._json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
        blocks = [submissions["filings"]["recent"]]
        for extra in submissions["filings"].get("files", []):
            blocks.append(self._json(f"https://data.sec.gov/submissions/{extra['name']}"))
        found: dict[str, Filing] = {}
        for block in blocks:
            filed = zip(block["form"], block["reportDate"], block["accessionNumber"], strict=False)
            for form, reported, accession in filed:
                if form != "NPORT-P" or not reported:
                    continue
                day = date.fromisoformat(reported)
                if day >= since and day.day >= 28:
                    found[accession] = Filing(day, accession)
        return sorted(found.values(), key=lambda f: f.report_date, reverse=True)

    def _document(self, cik: int, accession: str, first_bytes: int | None) -> bytes:
        self._sleep(PAUSE_SECONDS)
        folder = accession.replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/primary_doc.xml"
        return self._get(url, first_bytes)

    def series_of(self, cik: int, accession: str) -> str | None:
        """The fund series a filing is about, read from the top of the document."""
        head = self._document(cik, accession, HEADER_BYTES).decode("utf-8", "replace")
        found = re.search(r"<seriesId>(S\d+)</seriesId>", head)
        return found.group(1) if found else None

    def holdings(self, cik: int, accession: str) -> list[Holding]:
        return parse_holdings(self._document(cik, accession, None))


def parse_holdings(xml: bytes) -> list[Holding]:
    """Every holding of an N-PORT filing (safe XML parsing)."""
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        raise EdgarError("A SEC filing was not valid XML.") from None
    holdings = []
    for item in root.iter(f"{NPORT_NS}invstOrSec"):
        ticker_element = item.find(f"{NPORT_NS}identifiers/{NPORT_NS}ticker")
        value = item.findtext(f"{NPORT_NS}valUSD") or "0"
        holdings.append(
            Holding(
                name=(item.findtext(f"{NPORT_NS}name") or "").strip(),
                ticker=None if ticker_element is None else (ticker_element.get("value") or None),
                value_usd=Decimal(value),
                asset_category=(item.findtext(f"{NPORT_NS}assetCat") or "").strip(),
            )
        )
    return holdings


LEGAL_WORDS = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "LIMITED", "LLC", "PLC",
    "NV", "SA", "THE", "DE", "NEW",
}  # fmt: skip


def name_key(name: str) -> str:
    """A company name reduced to what identifies it: upper case, no punctuation or legal words."""
    words = re.sub(r"[^A-Z0-9 ]", " ", name.upper().replace("&", " AND ").replace("/", " ")).split()
    return " ".join(w for w in words if w not in LEGAL_WORDS)


def _by_name(ours: Mapping[str, OurRecord]) -> dict[str, OurRecord | None]:
    """Our records by name key. Several classes of one company share a name: take the class that
    has a decision (not UNKNOWN); if they still disagree the name is ambiguous (None)."""
    grouped: dict[str, list[OurRecord]] = {}
    for record in ours.values():
        grouped.setdefault(name_key(record.company_name), []).append(record)
    chosen: dict[str, OurRecord | None] = {}
    for key, records in grouped.items():
        decided = [r for r in records if r.status != "UNKNOWN"]
        statuses = {r.status for r in decided}
        chosen[key] = decided[0] if len(statuses) == 1 else (None if decided else records[0])
    return chosen


def compare_holdings(
    report_date: date, holdings: Sequence[Holding], ours: Mapping[str, OurRecord]
) -> QuarterResult:
    """Count how our classification rates a fund's common-equity holdings on one date.

    A holding is matched by ticker; older filings carry names only, and a holding whose ticker
    is missing or unknown to us is then matched by company name when that is unambiguous.
    """
    result = QuarterResult(report_date)
    value: dict[str, Decimal] = {}
    names = _by_name(ours)
    for holding in holdings:
        if holding.asset_category != "EC":
            continue
        if not holding.ticker and not holding.name:
            continue
        result.equities += 1
        result.equity_value += holding.value_usd
        record = ours.get(normalise_symbol(holding.ticker)) if holding.ticker else None
        if record is None and holding.name:
            record = names.get(name_key(holding.name))
        status = record.status if record else NOT_OURS
        result.by_status[status] += 1
        value[status] = value.get(status, Decimal(0)) + holding.value_usd
        if record is None:
            result.not_ours.append(holding)
        elif record.status != "HALAL":
            result.non_halal.append((holding, record))
    result.value_by_status = value
    result.non_halal.sort(key=lambda pair: pair[0].value_usd, reverse=True)
    result.not_ours.sort(key=lambda h: h.value_usd, reverse=True)
    return result


def _pct(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def render_markdown(symbol: str, results: Sequence[QuarterResult]) -> str:
    """One fund over time: the share of its equity holdings we call HALAL, by count and value."""
    lines = [
        f"# {symbol}: how our Halal screen rates the fund's holdings, by report date",
        "",
        "Source: the fund's N-PORT filings on SEC EDGAR. Our classification is the one in",
        "effect on each report date (information available then). NOT_IN_OUR_UNIVERSE = not US",
        "common stock in our master (ADRs, preferred, ETFs) or a ticker we cannot match.",
        "",
        "| Report date | Equities | HALAL (count) | HALAL (value) | NON_HALAL | UNKNOWN "
        "| Not in our universe |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r.report_date} | {r.equities} | {_pct(r.share('HALAL'))} "
            f"| {_pct(r.share('HALAL', True))} | {_pct(r.share('NON_HALAL'))} "
            f"| {_pct(r.share('UNKNOWN'))} | {_pct(r.share(NOT_OURS))} |"
        )
    if results:
        latest = results[0]
        lines += ["", f"## Largest holdings on {latest.report_date} that we do not call HALAL", ""]
        for holding, record in latest.non_halal[:EXAMPLES]:
            lines.append(
                f"- {holding.ticker} ({holding.name}): we say {record.status}: "
                f"{record.reason[:100]}"
            )
        if not latest.non_halal:
            lines.append("None.")
        if latest.not_ours:
            names = ", ".join(str(h.ticker or h.name) for h in latest.not_ours[:EXAMPLES])
            lines += ["", f"Not matched to our universe (largest): {names}"]
    return "\n".join(lines) + "\n"


def _holding_json(h: Holding) -> dict[str, str | None]:
    return {
        "name": h.name,
        "ticker": h.ticker,
        "value": str(h.value_usd),
        "category": h.asset_category,
    }


def _load_cached(path: Path) -> list[Holding]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    return [Holding(r["name"], r["ticker"], Decimal(r["value"]), r["category"]) for r in rows]


def trading_day_on_or_before(day: date) -> date:
    """`day` itself if the NYSE traded, otherwise the last trading day before it."""
    return day if is_trading_day(day) else previous_trading_day(day)


def analyse_fund(
    client: EdgarClient,
    conn: Connection,
    symbol: str,
    since: date,
    methodology: str,
    cache_dir: Path | None = None,
) -> list[QuarterResult]:
    """Every quarter-end filing of one fund since `since`, compared with our classification."""
    cik, series = client.fund_ids(symbol)
    series_file = cache_dir / f"series_{cik}.json" if cache_dir else None
    known: dict[str, str | None] = {}
    if series_file and series_file.exists():
        known = json.loads(series_file.read_text(encoding="utf-8"))
    results: dict[date, QuarterResult] = {}
    for filing in client.nport_filings(cik, since):
        if filing.accession not in known:
            known[filing.accession] = client.series_of(cik, filing.accession)
            if series_file:
                series_file.parent.mkdir(parents=True, exist_ok=True)
                series_file.write_text(json.dumps(known), encoding="utf-8")
        if known[filing.accession] != series or filing.report_date in results:
            continue  # another fund of the same trust, or a date already done
        cached = cache_dir / f"{symbol}_{filing.report_date}.json" if cache_dir else None
        if cached and cached.exists():
            holdings = _load_cached(cached)
        else:
            holdings = client.holdings(cik, filing.accession)
            if cached:
                cached.parent.mkdir(parents=True, exist_ok=True)
                payload = json.dumps([_holding_json(h) for h in holdings])
                cached.write_text(payload, encoding="utf-8")
        # Report dates are month-ends and can fall on a weekend: use the last trading day.
        on = trading_day_on_or_before(filing.report_date)
        results[filing.report_date] = compare_holdings(
            filing.report_date, holdings, our_records(conn, on, methodology)
        )
    return sorted(results.values(), key=lambda r: r.report_date, reverse=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare our Halal screen with ETF holdings.")
    parser.add_argument("--symbol", action="append", required=True, help="fund ticker, repeatable")
    parser.add_argument("--since", type=date.fromisoformat, default=date(2019, 12, 31))
    parser.add_argument("--methodology", default="AAOIFI-v1")
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--cache", type=Path, default=Path("private/sec"))
    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings, level=settings.log_level)
    if not settings.sec_user_agent:
        print("HQ_SEC_USER_AGENT is not set. Add a name and contact to .env (the SEC requires it).")
        return 1
    client = EdgarClient(settings.sec_user_agent)
    documents = []
    with correlation_scope(), make_engine(settings, DbRole.READONLY).connect() as conn:
        for symbol in args.symbol:
            results = analyse_fund(client, conn, symbol, args.since, args.methodology, args.cache)
            documents.append(render_markdown(symbol, results))
    text = "\n".join(documents)
    print(text)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
