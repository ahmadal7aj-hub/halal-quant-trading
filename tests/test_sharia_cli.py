"""The screening command: arguments and the order of its steps (no database)."""

from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from halal_quant.sharia import classification
from halal_quant.sharia.classification import ClassificationSummary

ROOT = Path(__file__).parents[1]


class FakeEngine:
    @contextmanager
    def begin(self) -> Any:
        yield object()


def test_main_registers_the_config_then_screens_each_month(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    steps: list[Any] = []
    monkeypatch.setattr(classification, "make_engine", lambda settings, role: FakeEngine())
    monkeypatch.setattr(
        classification, "register_config", lambda conn, loaded, actor, reason: steps.append("reg")
    )

    def fake_classify(conn: object, day: date, config: Any, manual: Any) -> ClassificationSummary:
        steps.append(day)
        return ClassificationSummary(day)

    monkeypatch.setattr(classification, "classify_date", fake_classify)
    code = classification.main(
        [
            "--start", "2020-01", "--end", "2020-03",
            "--config", str(ROOT / "config/sharia/aaoifi_v1.yaml"),
            "--manual-list", str(ROOT / "config/sharia/manual_list.yaml"),
        ]
    )  # fmt: skip
    assert code == 0
    assert steps == ["reg", date(2020, 1, 31), date(2020, 2, 28), date(2020, 3, 31)]
    out = capsys.readouterr().out
    assert "AAOIFI-v1" in out and "manual-list-v1" in out and "2020-02-28: inserted 0" in out


def test_main_refuses_a_bad_month() -> None:
    with pytest.raises(SystemExit):
        classification.main(["--start", "January"])


def test_the_summary_line_lists_statuses() -> None:
    summary = ClassificationSummary(date(2020, 1, 31), inserted=3, already_present=1)
    summary.by_status.update({"HALAL": 2, "UNKNOWN": 1})
    assert summary.line() == ("2020-01-31: inserted 3, already present 1; HALAL: 2, UNKNOWN: 1")


def test_sic_codes_are_read_as_numbers_and_bad_ones_are_ignored() -> None:
    assert classification._sic("6035") == 6035
    assert classification._sic(None) is None and classification._sic("") is None
    assert classification._sic("n/a") is None
