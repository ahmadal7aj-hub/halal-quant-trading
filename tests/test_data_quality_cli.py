"""The data-quality command: arguments, report output (no database)."""

from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from halal_quant.data import quality
from halal_quant.data.quality import Finding, QualityResult


class FakeEngine:
    @contextmanager
    def begin(self) -> Any:
        yield object()


def test_main_runs_the_checks_prints_the_report_and_writes_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    seen: dict[str, Any] = {}

    def fake_run_checks(conn: object, first: date, last: date, as_of: date | None) -> QualityResult:
        seen.update(first=first, last=last, as_of=as_of)
        result = QualityResult(run_id=3, checks_run=["price_gap"])
        result.add(Finding(9, "price_gap", date(2019, 5, 1), "7 trading days with no price"))
        return result

    monkeypatch.setattr(quality, "make_engine", lambda settings, role: FakeEngine())
    monkeypatch.setattr(quality, "run_checks", fake_run_checks)
    report = tmp_path / "r.md"
    code = quality.main(
        ["--start", "2019", "--end", "2020", "--as-of", "2020-06-30", "--report", str(report)]
    )
    assert code == 0
    assert seen == {
        "first": date(2019, 1, 1),
        "last": date(2020, 12, 31),
        "as_of": date(2020, 6, 30),
    }
    assert "| price_gap | 1 |" in capsys.readouterr().out
    assert "security 9 on 2019-05-01" in report.read_text(encoding="utf-8")
