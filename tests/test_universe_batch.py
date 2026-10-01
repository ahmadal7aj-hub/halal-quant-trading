"""The universe batch command: months, order of steps and failure handling (no database)."""

from collections import Counter
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from halal_quant.universe import batch
from halal_quant.universe.builder import UniverseError, UniverseResult

ROOT = Path(__file__).parents[1]
CONFIG = str(ROOT / "config" / "universe" / "default.yaml")


class FakeEngine:
    @contextmanager
    def begin(self) -> Any:
        yield object()


def result(as_of: date, members: int = 2, reused: bool = False) -> UniverseResult:
    exclusions = Counter(
        {"sharia_non_halal": 50, "below_min_liquidity": 7, "stale_price": 1, "x": 1}
    )
    return UniverseResult(1, as_of, "h" * 64, list(range(members)), exclusions, reused)


def test_main_registers_the_config_then_builds_each_month_for_the_next_trading_day(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    steps: list[Any] = []
    monkeypatch.setattr(batch, "make_engine", lambda settings, role: FakeEngine())
    monkeypatch.setattr(
        batch, "register_config", lambda conn, loaded, actor, reason: steps.append("reg")
    )

    def fake_build(conn: object, as_of: date, universe: Any) -> UniverseResult:
        steps.append(as_of)
        return result(as_of, reused=len(steps) == 3)

    monkeypatch.setattr(batch, "build_universe", fake_build)
    code = batch.main(["--start", "2020-01", "--end", "2020-03", "--config", CONFIG])
    assert code == 0
    # month-end screening dates 31 Jan, 28 Feb, 31 Mar 2020 -> the next trading day each
    assert steps == ["reg", date(2020, 2, 3), date(2020, 3, 2), date(2020, 4, 1)]
    out = capsys.readouterr().out
    assert "universe-v1" in out and "AAOIFI-v1" in out
    assert (
        "2020-02-03: 2 members; top exclusions: sharia_non_halal: 50, below_min_liquidity: 7" in out
    )
    assert "(reused)" in out


def test_main_stops_and_says_so_when_a_month_cannot_be_built(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(batch, "make_engine", lambda settings, role: FakeEngine())
    monkeypatch.setattr(batch, "register_config", lambda *a, **k: None)

    def fake_build(conn: object, as_of: date, universe: Any) -> UniverseResult:
        raise UniverseError("No data-quality run covers the window")

    monkeypatch.setattr(batch, "build_universe", fake_build)
    assert batch.main(["--start", "2020-01", "--end", "2020-03", "--config", CONFIG]) == 1
    assert "2020-02-03: NOT BUILT: No data-quality run covers the window" in capsys.readouterr().out


def test_summary_line_with_no_exclusions() -> None:
    line = batch.summary_line(UniverseResult(1, date(2020, 2, 3), "h", [1, 2, 3]))
    assert line == "2020-02-03: 3 members; top exclusions: none"


def test_main_refuses_a_bad_month() -> None:
    with pytest.raises(SystemExit):
        batch.main(["--start", "January"])
