"""Parsing Sharadar S&P 500 rows and the shared import command (made-up data)."""

from datetime import UTC, date, datetime
from typing import Any

import pytest

from halal_quant.core.settings import Settings
from halal_quant.data.sharadar import cli, index_membership
from halal_quant.data.sharadar.client import Download
from halal_quant.data.sharadar.index_membership import MembershipResult, parse_row


def row(**overrides: str) -> dict[str, str]:
    base = {
        "date": "2026-06-30",
        "action": "historical",
        "ticker": "zqa",
        "name": "Example Corp",
        "contraticker": "N/A",
        "contraname": "N/A",
        "note": "",
    }
    return {**base, **overrides}


def test_a_row_is_parsed_with_blanks_and_na_as_none() -> None:
    parsed, reason = parse_row(row())
    assert reason is None and parsed is not None
    key, values = parsed
    assert key == (date(2026, 6, 30), "historical", "ZQA")
    assert values == {
        "company_name": "Example Corp",
        "contra_ticker": None,
        "contra_name": None,
        "note": None,
    }


def test_actions_are_stored_as_delivered_but_lower_case() -> None:
    parsed, _ = parse_row(row(action=" Added ", contraticker="ZQB", contraname="Other Corp"))
    assert parsed is not None
    assert parsed[0][1] == "added"
    assert parsed[1]["contra_ticker"] == "ZQB"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"date": ""}, "missing_key_field"),
        ({"action": " "}, "missing_key_field"),
        ({"ticker": "N/A"}, "missing_key_field"),
        ({"date": "30/06/2026"}, "bad_date"),
    ],
)
def test_rows_without_a_usable_key_are_skipped_with_a_reason(
    overrides: dict[str, str], reason: str
) -> None:
    assert parse_row(row(**overrides)) == (None, reason)


def test_summary_is_readable() -> None:
    result = MembershipResult(data_version="sharadar.sp500@abc", inserted=5, already_present=1)
    result.actions["current"] += 5
    result.first_date, result.last_date = date(2026, 6, 30), date(2026, 9, 29)
    text = result.summary()
    assert "inserted 5, already present 1" in text and "current: 5" in text
    assert "2026-06-30 to 2026-09-29" in text


def test_main_uses_the_shared_import_command(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_run_import(endpoint: str, required: Any, apply: Any, **params: str) -> int:
        seen.update(endpoint=endpoint, required=tuple(required), params=params)
        return 0

    monkeypatch.setattr(index_membership, "run_import", fake_run_import)
    assert index_membership.main() == 0
    assert seen["endpoint"] == "sp500" and "ticker" in seen["required"] and seen["params"] == {}


def test_main_applies_the_importer_to_the_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    result = MembershipResult(data_version="v")

    def fake_import(conn: Any, download: Download) -> MembershipResult:
        calls.append((conn, download))
        return result

    def fake_run_import(endpoint: str, required: Any, apply: Any, **params: str) -> int:
        download = Download("sp500", [], "0" * 64, datetime.now(UTC))
        assert apply("conn", download) is result
        return 0

    monkeypatch.setattr(index_membership, "import_index_membership", fake_import)
    monkeypatch.setattr(index_membership, "run_import", fake_run_import)
    assert index_membership.main() == 0
    assert calls[0][0] == "conn"


def test_the_shared_command_refuses_to_run_without_a_key(
    monkeypatch: pytest.MonkeyPatch, fake_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: fake_settings)
    assert cli.run_import("sp500", (), lambda conn, download: MembershipResult("v")) == 1
    assert "HQ_SHARADAR_API_KEY is not set" in capsys.readouterr().out
