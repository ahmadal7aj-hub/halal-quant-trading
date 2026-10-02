"""The vintage download command: order of steps, resume and failure handling (no database)."""

from contextlib import contextmanager
from typing import Any

import pytest

from halal_quant.data.sharadar import vintage as cli


class FakeEngine:
    @contextmanager
    def begin(self) -> Any:
        yield object()


def patch(
    monkeypatch: pytest.MonkeyPatch, calls: list[Any], fail_on: str | None = None, new: bool = True
) -> None:
    monkeypatch.setattr(cli, "make_engine", lambda settings, role: FakeEngine())
    monkeypatch.setattr(
        cli, "start_vintage", lambda conn, vid, desc: calls.append(("start", vid)) or new
    )
    monkeypatch.setattr(cli, "finish_vintage", lambda conn, vid: calls.append(("finish", vid)))

    def fake_run_import(endpoint: str, required: Any, apply: Any, **params: str) -> int:
        calls.append((endpoint, params))
        return 1 if fail_on and params.get("ticker", "").startswith(fail_on) else 0

    monkeypatch.setattr(cli, "run_import", fake_run_import)


def test_main_starts_the_vintage_then_downloads_months_and_funds_then_finishes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[Any] = []
    patch(monkeypatch, calls)
    code = cli.main(
        [
            "--vintage",
            "v1",
            "--start",
            "2024-01",
            "--end",
            "2024-02",
            "--benchmark",
            "SPUS",
            "--finish",
        ]
    )
    assert code == 0
    assert calls == [
        ("start", "v1"),
        ("stocks", {"date.gte": "2024-01-01", "date.lte": "2024-01-31"}),
        ("stocks", {"date.gte": "2024-02-01", "date.lte": "2024-02-29"}),
        ("funds", {"ticker": "SPUS"}),
        ("finish", "v1"),
    ]
    assert "Vintage v1: started" in capsys.readouterr().out


def test_without_finish_the_vintage_stays_open_and_defaults_cover_the_four_benchmarks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[Any] = []
    patch(monkeypatch, calls, new=False)
    assert cli.main(["--vintage", "v1", "--no-stocks"]) == 0
    assert [c[1]["ticker"] for c in calls if c[0] == "funds"] == list(cli.DEFAULT_BENCHMARKS)
    assert not any(c[0] == "finish" for c in calls)
    assert "resuming" in capsys.readouterr().out


def test_a_failed_download_stops_everything_and_does_not_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []
    patch(monkeypatch, calls, fail_on="HLAL")
    code = cli.main(
        [
            "--vintage",
            "v1",
            "--no-stocks",
            "--benchmark",
            "SPUS",
            "--benchmark",
            "HLAL",
            "--benchmark",
            "SPY",
            "--finish",
        ]  # fmt: skip
    )
    assert code == 1
    assert [c[1].get("ticker") for c in calls if c[0] == "funds"] == [
        "SPUS",
        "HLAL",
    ]  # SPY never ran
    assert not any(c[0] == "finish" for c in calls)


def test_main_refuses_a_bad_month() -> None:
    with pytest.raises(SystemExit):
        cli.main(["--vintage", "v1", "--start", "January"])
