"""The universe demo command: rendering and the rolled-back rebuild (no database)."""

from collections import Counter
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from halal_quant.universe import show
from halal_quant.universe.builder import UniverseResult


def result(reused: bool = True) -> UniverseResult:
    exclusions = Counter({"sharia_non_halal": 2900, "below_min_price": 152, "mystery_reason": 3})
    return UniverseResult(7, date(2020, 1, 2), "a" * 64, [1, 2, 3], exclusions, reused)


def test_the_funnel_is_in_plain_english_and_unknown_reasons_are_shown_as_they_are() -> None:
    top = [("AAPL", "APPLE INC", Decimal("1304800000000"), "debt 8.28% of market value")]
    text = show.render(result(), "universe-v1", "AAOIFI-v1", top)
    assert "Eligible universe on 2020-01-02" in text and "methodology AAOIFI-v1" in text
    assert "members: 3" in text and "a" * 64 in text
    assert "YES, an identical universe was already stored" in text
    assert "2900  Sharia screen says NON_HALAL" in text
    assert "152  price below the minimum" in text and "3  mystery_reason" in text
    assert "1,304.8bn" in text and "debt 8.28%" in text


def test_a_universe_that_was_never_stored_says_so_and_a_missing_market_value_is_n_a() -> None:
    text = show.render(
        result(reused=False), "universe-v1", "AAOIFI-v1", [("ZZZ", "Zed Co", None, "")]
    )
    assert "this exact universe was not stored before" in text and "n/a" in text


def test_main_rebuilds_inside_a_transaction_that_is_rolled_back(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    events: list[str] = []

    class FakeTransaction:
        def rollback(self) -> None:
            events.append("rollback")

    class FakeConnection:
        def begin(self) -> FakeTransaction:
            events.append("begin")
            return FakeTransaction()

    class FakeEngine:
        @contextmanager
        def connect(self) -> Any:
            yield FakeConnection()

    monkeypatch.setattr(show, "make_engine", lambda settings, role: FakeEngine())
    monkeypatch.setattr(show, "build_universe", lambda conn, as_of, universe: result())
    monkeypatch.setattr(show, "largest_members", lambda conn, res, method, limit: [])
    assert show.main(["--date", "2020-01-02", "--top", "5"]) == 0
    assert events == ["begin", "rollback"]  # nothing is stored
    assert "Eligible universe on 2020-01-02" in capsys.readouterr().out


def test_main_needs_a_date() -> None:
    with pytest.raises(SystemExit):
        show.main([])
