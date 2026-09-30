"""Read-only client for the Sharadar REST API (R1, DS-1).

Sharadar wants the API key inside the request URL, so a URL must never reach a log line, an
error message or a traceback. This client keeps the URL private: every failure is turned into a
`SharadarError` whose text names the table and the HTTP status but never the URL or the key.

`fetch_csv` pages through a table (the API returns at most 10,000 rows per request), and
fingerprints the exact bytes received so the download can be registered as a data version.
Tests inject a fake `transport`; nothing in the test suite talks to the internet (rulebook C4).
"""

import csv
import hashlib
import io
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import SecretStr

BASE_URL = "https://api.sharadar.com/v1.0/data"
PAGE_SIZE = 10_000
TIMEOUT_SECONDS = 120

# Takes a full URL, returns the response body. Raises SharadarError, never leaks the URL.
Transport = Callable[[str], bytes]


class SharadarError(Exception):
    """A Sharadar request failed or returned something unexpected. Never contains the key."""


@dataclass(frozen=True)
class Download:
    """A complete table download and its fingerprint."""

    endpoint: str
    rows: list[dict[str, str]]
    sha256: str  # of the raw bytes of every page, in order
    downloaded_at: datetime  # UTC


def _urlopen_transport(api_key: str) -> Transport:
    def fetch(url: str) -> bytes:
        if not url.startswith(BASE_URL + "/"):
            raise SharadarError("Refusing to call a URL outside the Sharadar API.")
        # The URL was checked above to be an https address on the Sharadar API.
        request = urllib.request.Request(url)  # nosec B310
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # nosec B310
                body: bytes = response.read()
                return body
        except urllib.error.HTTPError as exc:
            detail = exc.read(300).decode("utf-8", "replace").replace(api_key, "***")
            raise SharadarError(f"Sharadar answered HTTP {exc.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise SharadarError("Could not reach Sharadar (network error or timeout).") from None

    return fetch


class SharadarClient:
    def __init__(
        self,
        api_key: SecretStr,
        transport: Transport | None = None,
        page_size: int = PAGE_SIZE,
    ) -> None:
        self._api_key = api_key
        self._transport = transport or _urlopen_transport(api_key.get_secret_value())
        self._page_size = page_size

    def _url(self, endpoint: str, params: dict[str, str]) -> str:
        query = urllib.parse.urlencode(
            {"api_key": self._api_key.get_secret_value(), "format": "csv", **params}
        )
        return f"{BASE_URL}/{endpoint}?{query}"

    def fetch_csv(
        self, endpoint: str, required_columns: Iterable[str] = (), **params: str
    ) -> Download:
        """Download every row of a table (following pages) as dictionaries of text values."""
        digest = hashlib.sha256()
        rows: list[dict[str, str]] = []
        skip = 0
        while True:
            body = self._transport(
                self._url(endpoint, {**params, "limit": str(self._page_size), "skip": str(skip)})
            )
            digest.update(body)
            reader = csv.DictReader(io.StringIO(body.decode("utf-8")))
            missing = set(required_columns) - set(reader.fieldnames or [])
            if missing:
                raise SharadarError(
                    f"The {endpoint} table is missing expected columns: {sorted(missing)}."
                )
            page = list(reader)
            rows.extend(page)
            if len(page) < self._page_size:
                break
            skip += self._page_size
        return Download(
            endpoint=endpoint,
            rows=rows,
            sha256=digest.hexdigest(),
            downloaded_at=datetime.now(UTC),
        )
