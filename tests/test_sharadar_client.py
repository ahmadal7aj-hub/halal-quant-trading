"""The Sharadar client never leaks the API key, and pages correctly (no real network calls)."""

import hashlib
import io
import logging
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from pydantic import SecretStr

from halal_quant.core.logging import configure_logging
from halal_quant.core.settings import Settings
from halal_quant.data.sharadar import client as client_module
from halal_quant.data.sharadar.client import SharadarClient, SharadarError

KEY = "fake-key-for-tests"


def csv_page(rows: list[tuple[str, str]]) -> bytes:
    lines = ["permaticker,ticker", *(f"{a},{b}" for a, b in rows)]
    return ("\n".join(lines) + "\n").encode()


class FakeTransport:
    def __init__(self, pages: list[bytes]) -> None:
        self.pages = pages
        self.urls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        return self.pages[len(self.urls) - 1]


def make(pages: list[bytes], page_size: int = 2) -> tuple[SharadarClient, FakeTransport]:
    transport = FakeTransport(pages)
    return SharadarClient(SecretStr(KEY), transport, page_size=page_size), transport


def test_pages_are_followed_until_a_short_page() -> None:
    client, transport = make(
        [csv_page([("1", "AAA"), ("2", "BBB")]), csv_page([("3", "CCC")])], page_size=2
    )
    download = client.fetch_csv("tickers", table="stocks")
    assert [r["ticker"] for r in download.rows] == ["AAA", "BBB", "CCC"]
    queries = [parse_qs(urlparse(u).query) for u in transport.urls]
    assert [q["skip"] for q in queries] == [["0"], ["2"]]
    assert all(q["limit"] == ["2"] and q["table"] == ["stocks"] for q in queries)
    assert all(q["api_key"] == [KEY] and q["format"] == ["csv"] for q in queries)


def test_an_exact_multiple_of_the_page_size_ends_on_an_empty_page() -> None:
    client, transport = make([csv_page([("1", "A"), ("2", "B")]), csv_page([])], page_size=2)
    assert len(client.fetch_csv("tickers").rows) == 2
    assert len(transport.urls) == 2


def test_fingerprint_covers_the_exact_bytes_and_changes_with_them() -> None:
    pages = [csv_page([("1", "AAA")])]
    first = make(pages)[0].fetch_csv("tickers")
    again = make(pages)[0].fetch_csv("tickers")
    other = make([csv_page([("1", "AAB")])])[0].fetch_csv("tickers")
    assert first.sha256 == hashlib.sha256(pages[0]).hexdigest() == again.sha256
    assert other.sha256 != first.sha256
    assert first.downloaded_at.tzinfo is not None


def test_missing_columns_fail_closed() -> None:
    client, _ = make([csv_page([("1", "AAA")])])
    with pytest.raises(SharadarError, match="missing expected columns.*'sector'"):
        client.fetch_csv("tickers", ["ticker", "sector"])


def http_error(code: int, body: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        f"https://api.sharadar.com/v1.0/data/tickers?api_key={KEY}",
        code,
        "err",
        None,  # type: ignore[arg-type]
        io.BytesIO(body.encode()),
    )


def real_transport(monkeypatch: pytest.MonkeyPatch, raises: BaseException) -> SharadarClient:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise raises

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    return SharadarClient(SecretStr(KEY))


def test_http_errors_never_contain_the_key_or_url(monkeypatch: pytest.MonkeyPatch) -> None:
    client = real_transport(monkeypatch, http_error(403, f'{{"error":"Exceeds free tier {KEY}"}}'))
    with pytest.raises(SharadarError) as excinfo:
        client.fetch_csv("stocks", ticker="GE")
    text = str(excinfo.value)
    assert "HTTP 403" in text and "Exceeds free tier" in text
    assert KEY not in text and "api_key" not in text and "https://" not in text
    assert excinfo.value.__cause__ is None and excinfo.value.__suppress_context__


@pytest.mark.parametrize("failure", [urllib.error.URLError("dns"), TimeoutError(), OSError("x")])
def test_network_errors_are_plain_english(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    client = real_transport(monkeypatch, failure)
    with pytest.raises(SharadarError, match="Could not reach Sharadar") as excinfo:
        client.fetch_csv("tickers")
    assert KEY not in str(excinfo.value)


def test_the_real_transport_refuses_other_hosts() -> None:
    transport = client_module._urlopen_transport(KEY)
    with pytest.raises(SharadarError, match="outside the Sharadar API"):
        transport("https://example.com/?api_key=x")


def test_the_key_is_masked_in_logs(
    fake_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = fake_settings.model_copy(update={"sharadar_api_key": SecretStr(KEY)})
    configure_logging(settings, level="INFO")
    logging.getLogger("test").info("called https://x/?api_key=%s", KEY)
    logging.getLogger("test").error("raw key %s", KEY)
    output = capsys.readouterr().err + capsys.readouterr().out
    assert KEY not in output


def test_the_real_transport_returns_the_response_body(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read(self) -> bytes:
            return csv_page([("1", "AAA")])

    seen: list[str] = []

    def fake_urlopen(request: urllib.request.Request, timeout: int) -> Response:
        seen.append(request.full_url)
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    download = SharadarClient(SecretStr(KEY)).fetch_csv("tickers", table="stocks")
    assert [r["ticker"] for r in download.rows] == ["AAA"]
    assert seen[0].startswith("https://api.sharadar.com/v1.0/data/tickers?")
