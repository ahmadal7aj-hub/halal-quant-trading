"""Read-only client for the Zoya Shariah compliance API (task 15 cross-check; S3 provider record).

Zoya is used only as an outside opinion to compare our own screen against (BRD §21, doc 01 §3):
it never decides anything here. The Personal Use licence allows non-public display only, so the
data stays on this machine and reports show counts and a few example names, never the full list.

The API is GraphQL over HTTPS. The key goes in the `Authorization` header (`live-...`), so, like
the Sharadar client, every failure is turned into a `ZoyaError` that never contains the key.
Tests inject a fake `transport`; nothing in the test suite talks to the internet (rulebook C4).
"""

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import SecretStr

LIVE_URL = "https://api.zoya.finance/graphql"
PAGE_LIMIT = 1000  # the documented maximum for stock reports
FUND_PAGE_LIMIT = 2000  # default for fund reports; the documented maximum is 4,000
TIMEOUT_SECONDS = 60
MAX_RETRIES = 3
PAUSE_SECONDS = 0.15  # the limit is 10 requests a second; stay well under it

STATUSES = ("COMPLIANT", "NON_COMPLIANT", "QUESTIONABLE", "UNRATED")

REPORTS_QUERY = """
query Reports($input: BasicReportsInput) {
  basicCompliance { reports(input: $input) {
    items { symbol name exchange status purificationRatio reportDate }
    nextToken
  } }
}
"""
FUNDS_QUERY = """
query Funds($input: BasicFundsInput) {
  basicCompliance { funds(input: $input) {
    items { symbol name exchange status purificationRatio holdingsAsOfDate reportDate }
    nextToken
  } }
}
"""
REPORT_QUERY = """
query Report($symbol: String!) {
  basicCompliance { report(symbol: $symbol) {
    symbol name exchange status purificationRatio reportDate
  } }
}
"""

# Takes (query, variables), returns the decoded JSON. Raises ZoyaError, never leaks the key.
Transport = Callable[[str, dict[str, Any]], dict[str, Any]]


class ZoyaError(Exception):
    """A Zoya request failed or returned something unexpected. Never contains the key."""


@dataclass(frozen=True)
class ZoyaReport:
    symbol: str
    name: str
    exchange: str | None
    status: str  # one of STATUSES
    purification_ratio: Decimal | None
    report_date: date | None


def authorization_value(api_key: str) -> str:
    """The header value: keys are used as shown (`live-...`); a bare key is treated as live."""
    key = api_key.strip()
    return key if key.startswith(("live-", "sandbox-")) else f"live-{key}"


def _urlopen_transport(api_key: str) -> Transport:
    header = authorization_value(api_key)

    def post(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps({"query": query, "variables": variables}).encode()
        # The URL is a constant https address on the Zoya API.
        request = urllib.request.Request(  # nosec B310
            LIVE_URL,
            data=body,
            headers={"Content-Type": "application/json", "Authorization": header},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # nosec B310
                decoded: dict[str, Any] = json.loads(response.read())
                return decoded
        except urllib.error.HTTPError as exc:
            detail = exc.read(300).decode("utf-8", "replace").replace(api_key, "***")
            raise ZoyaError(f"Zoya answered HTTP {exc.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ZoyaError("Could not reach Zoya (network error or timeout).") from None
        except json.JSONDecodeError:
            raise ZoyaError("Zoya returned something that is not JSON.") from None

    return post


def parse_report(item: dict[str, Any]) -> ZoyaReport:
    """One GraphQL item as a report; anything malformed raises ZoyaError."""
    try:
        status = str(item["status"])
        if status not in STATUSES:
            raise ZoyaError(f"Zoya returned an unknown status {status!r}.")
        ratio = item.get("purificationRatio")
        raw_date = item.get("reportDate")
        return ZoyaReport(
            symbol=str(item["symbol"]).strip().upper(),
            name=str(item.get("name") or ""),
            exchange=item.get("exchange"),
            status=status,
            purification_ratio=None if ratio is None else Decimal(str(ratio)),
            report_date=None if not raw_date else date.fromisoformat(str(raw_date)[:10]),
        )
    except (KeyError, InvalidOperation, ValueError) as exc:
        raise ZoyaError(f"Zoya returned a malformed report: {type(exc).__name__}.") from None


class ZoyaClient:
    def __init__(
        self,
        api_key: SecretStr,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._transport = transport or _urlopen_transport(api_key.get_secret_value())
        self._sleep = sleep

    def _call(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self._transport(query, variables)
                break
            except ZoyaError as exc:
                if "HTTP 429" not in str(exc) or attempt == MAX_RETRIES:
                    raise
                self._sleep(attempt * 2.0)  # rate limited: back off, then retry
        errors = response.get("errors")
        if errors and not response.get("data"):
            messages = "; ".join(str(e.get("message", "unknown error")) for e in errors[:3])
            raise ZoyaError(f"Zoya reported an error: {messages}")
        data: dict[str, Any] = response.get("data") or {}
        return data

    def _pages(self, query: str, field: str, limit: int) -> Iterator[dict[str, Any]]:
        token: str | None = None
        while True:
            payload: dict[str, Any] = {"limit": limit}
            if token:
                payload["nextToken"] = token
            data = self._call(query, {"input": payload})
            try:
                page = data["basicCompliance"][field]
                yield from page["items"]
                token = page.get("nextToken")
            except (KeyError, TypeError):
                raise ZoyaError(f"Zoya's {field} response had an unexpected shape.") from None
            if not token:
                return
            self._sleep(PAUSE_SECONDS)

    def report(self, symbol: str) -> ZoyaReport | None:
        """The report for one US stock, or None if Zoya has none."""
        data = self._call(REPORT_QUERY, {"symbol": symbol})
        item = (data.get("basicCompliance") or {}).get("report")
        return None if item is None else parse_report(item)

    def fetch_us_reports(self) -> list[ZoyaReport]:
        """Every US stock report, following the pagination to the end."""
        return [parse_report(item) for item in self._pages(REPORTS_QUERY, "reports", PAGE_LIMIT)]

    def fetch_funds(self) -> list[ZoyaReport]:
        """Every fund (ETF) report (same fields; holdings dates are not kept)."""
        return [parse_report(item) for item in self._pages(FUNDS_QUERY, "funds", FUND_PAGE_LIMIT)]
