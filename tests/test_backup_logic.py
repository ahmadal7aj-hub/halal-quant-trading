"""Backup naming, retention, comparison and status logic (no database or Docker needed)."""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from halal_quant.ops.backup import (
    BackupError,
    Manifest,
    backups_in,
    latest_status,
    load_manifest,
    manifest_path,
    retention_plan,
    stamp_of,
    verified_path,
)
from halal_quant.ops.restore_test import compare


def touch(folder: Path, stamp: str, encrypted: bool = False, verified: bool = False) -> Path:
    path = folder / f"halal_quant_{stamp}.dump{'.enc' if encrypted else ''}"
    path.write_bytes(b"x")
    if verified:
        verified_path(path).write_text("{}")
    return path


def test_names_are_parsed_and_foreign_files_are_ignored(tmp_path: Path) -> None:
    a = touch(tmp_path, "20261002T120000Z")
    b = touch(tmp_path, "20261003T120000Z", encrypted=True)
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / "halal_quant_2026-10-02.dump").write_bytes(b"old style")  # the manual 2 Oct dump
    assert backups_in(tmp_path) == [a, b]
    assert stamp_of(b) == datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    with pytest.raises(BackupError, match="not a backup"):
        stamp_of(tmp_path / "notes.txt")


def test_retention_keeps_30_days_one_per_month_and_the_newest_verified(tmp_path: Path) -> None:
    files = [
        touch(tmp_path, "20250103T000000Z"),  # old month, early
        touch(tmp_path, "20250128T000000Z"),  # same month, later: this one is the month's keeper
        touch(tmp_path, "20250215T000000Z"),  # month older than 12 months from Oct 2026: dropped
        touch(tmp_path, "20251115T000000Z"),  # within 12 months: kept as the month's newest
        touch(tmp_path, "20251120T000000Z", verified=True),
        touch(tmp_path, "20260815T000000Z"),  # older than 30 days; not its month's newest: dropped
        touch(tmp_path, "20260820T000000Z"),  # older than 30 days but the newest of August: kept
        touch(tmp_path, "20261001T000000Z"),  # in the last 30 days
        touch(tmp_path, "20261005T000000Z"),  # the newest overall
    ]
    keep, drop = retention_plan(files, date(2026, 10, 7))
    names = {p.name[12:20] for p in keep}
    assert "20261005" in names and "20261001" in names  # recent ones
    assert "20251120" in names  # newest of its month (and verified)
    assert "20260820" in names and "20260815" not in names  # one per older month
    assert "20250103" not in names and "20250215" not in names  # too old
    assert len(keep) + len(drop) == len(files)


def test_the_newest_backup_and_the_newest_verified_one_are_never_deleted(tmp_path: Path) -> None:
    old_verified = touch(tmp_path, "20230101T000000Z", verified=True)
    newest = touch(tmp_path, "20230102T000000Z")
    keep, drop = retention_plan([old_verified, newest], date(2026, 10, 7))
    assert set(keep) == {old_verified, newest} and drop == []
    assert retention_plan([], date(2026, 10, 7)) == ([], [])


def test_compare_reports_every_kind_of_difference() -> None:
    assert compare({"a": 1, "b": 2}, {"a": 1, "b": 2}) == []
    problems = compare({"a": 1, "b": 2, "c": 3}, {"a": 1, "b": 5, "d": 4})
    text = " | ".join(problems)
    assert "table b: 2 rows" in text and "5 restored" in text
    assert "table c is missing" in text and "table d exists after restore" in text


def make_manifest(**changes: object) -> Manifest:
    base = {
        "file": "x.dump",
        "created_at": "2026-10-07T00:00:00+00:00",
        "size_bytes": 1,
        "sha256": "a" * 64,
        "encrypted": False,
        "database": "halal_quant",
        "schema_revision": "0019",
        "postgres_version": "18.6",
        "row_counts": {"t": 3},
    }
    return Manifest(**{**base, **changes})  # type: ignore[arg-type]


def test_a_manifest_round_trips_through_its_file(tmp_path: Path) -> None:
    dump = touch(tmp_path, "20261007T000000Z")
    manifest = make_manifest(row_counts={"t": 3, "u": 0})
    manifest_path(dump).write_text(manifest.to_json(), encoding="utf-8")
    assert load_manifest(dump) == manifest
    assert json.loads(manifest.to_json())["schema_revision"] == "0019"


def test_the_status_shown_on_the_dashboard(tmp_path: Path) -> None:
    assert latest_status(tmp_path / "missing")["newest"] is None
    assert latest_status(tmp_path)["verified"] is False
    dump = touch(tmp_path, "20261007T000000Z", verified=True)
    manifest_path(dump).write_text(make_manifest(encrypted=True).to_json(), encoding="utf-8")
    status = latest_status(tmp_path)
    assert status["newest"] == dump.name and status["verified"] is True
    assert status["encrypted"] is True and status["age_hours"] > 0
