"""A real backup and restore test on a tiny throwaway database inside the Docker container."""

import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from halal_quant.ops.backup import (
    CONTAINER,
    BackupError,
    load_manifest,
    make_backup,
    manifest_path,
    psql,
    row_counts,
    verified_path,
)
from halal_quant.ops.restore_test import SCRATCH, verify

TEST_DB = "hq_backup_unit_test"


def container_available() -> bool:
    docker = shutil.which("docker")
    if not docker:
        return False
    done = subprocess.run(  # noqa: S603  # nosec B603
        [docker, "inspect", "-f", "{{.State.Running}}", CONTAINER],
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode == 0 and done.stdout.strip() == "true"


pytestmark = pytest.mark.skipif(
    not container_available(), reason="needs the local database container (Docker)"
)


@pytest.fixture
def tiny_db() -> Iterator[str]:
    psql(f"drop database if exists {TEST_DB}", "postgres")
    psql(f"create database {TEST_DB}", "postgres")
    psql(
        "create schema hq; create table hq.alembic_version(version_num text); "
        "insert into hq.alembic_version values ('0019'); "
        "create table hq.things(id int primary key, name text); "
        "insert into hq.things select g, 'row' || g from generate_series(1, 250) g; "
        "create table hq.empty_table(x int)",
        TEST_DB,
    )
    try:
        yield TEST_DB
    finally:
        psql(f"drop database if exists {TEST_DB}", "postgres")
        psql(f"drop database if exists {SCRATCH}", "postgres")


def test_a_plain_backup_restores_and_every_table_matches(
    tiny_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HQ_BACKUP_PASSPHRASE", raising=False)
    dump = make_backup(tmp_path, tiny_db)
    manifest = load_manifest(dump)
    assert manifest.encrypted is False and manifest.row_counts == {
        "alembic_version": 1,
        "empty_table": 0,
        "things": 250,
    }
    assert manifest.schema_revision == "0019" and manifest.size_bytes > 0
    assert verify(dump) == []  # restored into the scratch database and compared
    assert verified_path(dump).exists()
    assert psql(f"select count(*) from pg_database where datname = '{SCRATCH}'", "postgres") == "0"


def test_an_encrypted_backup_is_unreadable_without_the_passphrase_and_restores_with_it(
    tiny_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HQ_BACKUP_PASSPHRASE", "a long test passphrase")
    dump = make_backup(tmp_path, tiny_db)
    assert dump.name.endswith(".dump.enc") and load_manifest(dump).encrypted is True
    assert b"PGDMP" not in dump.read_bytes()[:64]  # not even the dump header is visible
    assert verify(dump) == []
    monkeypatch.setenv("HQ_BACKUP_PASSPHRASE", "the wrong passphrase")
    with pytest.raises(BackupError, match="Decryption failed|pg_restore failed"):
        verify(dump)
    monkeypatch.delenv("HQ_BACKUP_PASSPHRASE")
    with pytest.raises(BackupError, match="set HQ_BACKUP_PASSPHRASE"):
        verify(dump)


def test_a_damaged_file_or_a_manifest_that_disagrees_is_caught(
    tiny_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HQ_BACKUP_PASSPHRASE", raising=False)
    dump = make_backup(tmp_path, tiny_db)
    # a manifest that claims more rows than the backup holds
    manifest = json.loads(manifest_path(dump).read_text())
    manifest["row_counts"]["things"] = 251
    manifest_path(dump).write_text(json.dumps(manifest))
    problems = verify(dump)
    assert any("things: 251 rows in the backup manifest, 250 restored" in p for p in problems)
    assert not verified_path(dump).exists()
    # a file that was changed after the backup was taken
    manifest["row_counts"]["things"] = 250
    manifest_path(dump).write_text(json.dumps(manifest))
    dump.write_bytes(dump.read_bytes() + b"tamper")
    assert "SHA-256 differs" in verify(dump)[0]


def test_the_row_counts_cover_every_table(tiny_db: str) -> None:
    assert row_counts(tiny_db) == {"alembic_version": 1, "empty_table": 0, "things": 250}


def test_a_failed_dump_leaves_no_file_behind(tmp_path: Path) -> None:
    with pytest.raises(BackupError):
        make_backup(tmp_path, "database_that_does_not_exist")
    assert list(tmp_path.iterdir()) == []
