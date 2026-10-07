"""Backup and restore-test of the database (Phase 7; doc 02 §10; PRD §41).

    uv run python -m halal_quant.ops.backup                 # make a backup in the backup folder
    uv run python -m halal_quant.ops.backup --retention     # what retention would delete
    uv run python -m halal_quant.ops.restore_test           # prove the newest backup restores

A backup is a compressed `pg_dump` taken inside the database container (nothing secret travels on a
command line), optionally encrypted with AES-256 using a passphrase held only in `.env`
(`HQ_BACKUP_PASSPHRASE`), plus a JSON manifest: time, size, SHA-256, schema revision and the exact
row count of every table. A backup counts only after `restore_test` has restored it into a scratch
database and found every table's row count equal to the manifest (doc 02: "restore tested").
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess  # nosec B404
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

CONTAINER = "halal-quant-db-1"
DB_USER = "hq_admin"
DEFAULT_DB = "halal_quant"
DEFAULT_DIR = Path(r"E:\Backups")
NAME = re.compile(r"^(?P<db>[a-z_0-9]+)_(?P<stamp>\d{8}T\d{6}Z)\.dump(?P<enc>\.enc)?$")
KEEP_DAILY_DAYS = 30
KEEP_MONTHLY_MONTHS = 12
PASSPHRASE_ENV = "HQ_BACKUP_PASSPHRASE"  # nosec B105


class BackupError(Exception):
    """A backup or restore step failed; the message says what and why."""


def _docker() -> str:
    found = shutil.which("docker")
    if not found:
        raise BackupError("The docker command was not found.")
    return found


def _openssl() -> str:
    found = shutil.which("openssl")
    if not found:
        raise BackupError("openssl was not found, so encrypted backups are not possible.")
    return found


def run(args: list[str], stdin: Any = None, env: dict[str, str] | None = None) -> str:
    """Run one command to completion and return its output (the only place commands are run)."""
    done = subprocess.run(  # noqa: S603  # nosec B603
        args, stdin=stdin, capture_output=True, text=True, env=env, check=False
    )
    if done.returncode != 0:
        raise BackupError(f"{Path(args[0]).name} failed: {done.stderr.strip()[:400]}")
    return done.stdout


def psql(sql: str, db: str, container: str = CONTAINER) -> str:
    """One query inside the container as the admin role (local socket)."""
    return run([_docker(), "exec", container, "psql", "-U", DB_USER, "-d", db, "-tAc", sql]).strip()


def row_counts(db: str, container: str = CONTAINER) -> dict[str, int]:
    """The exact number of rows in every table of the `hq` schema."""
    tables = psql(
        "select table_name from information_schema.tables "
        "where table_schema = 'hq' and table_type = 'BASE TABLE' order by 1",
        db,
        container,
    ).splitlines()
    safe = re.compile(r"^[a-z_0-9]+$")
    counts: dict[str, int] = {}
    for t in tables:
        if not safe.match(t):
            raise BackupError(f"Unexpected table name {t!r}.")
        counts[t] = int(psql(f'select count(*) from hq."{t}"', db, container))  # nosec B608
    return counts


def load_passphrase() -> None:
    """Make the `.env` passphrase available to the encryption command (never as an argument)."""
    from halal_quant.core.settings import get_settings

    secret = get_settings().backup_passphrase
    if secret is not None and secret.get_secret_value() and PASSPHRASE_ENV not in os.environ:
        os.environ[PASSPHRASE_ENV] = secret.get_secret_value()


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class Manifest:
    file: str
    created_at: str
    size_bytes: int
    sha256: str
    encrypted: bool
    database: str
    schema_revision: str
    postgres_version: str
    row_counts: dict[str, int]

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=2, sort_keys=True)


def manifest_path(dump: Path) -> Path:
    return dump.with_name(dump.name + ".manifest.json")


def verified_path(dump: Path) -> Path:
    return dump.with_name(dump.name + ".verified.json")


def load_manifest(dump: Path) -> Manifest:
    return Manifest(**json.loads(manifest_path(dump).read_text(encoding="utf-8")))


def _dump_plain(dump_cmd: list[str], handle: BinaryIO) -> None:
    with subprocess.Popen(  # noqa: S603  # nosec B603
        dump_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as dump:
        if dump.stdout is None or dump.stderr is None:
            raise BackupError("Could not read the dump.")
        shutil.copyfileobj(dump.stdout, handle, 1024 * 1024)
        error = dump.stderr.read()
        if dump.wait():
            raise BackupError(f"The backup failed: {error.decode()[:300]}")


def _dump_encrypted(dump_cmd: list[str], handle: BinaryIO) -> None:
    enc_cmd = [
        _openssl(),
        "enc",
        "-aes-256-cbc",
        "-pbkdf2",
        "-salt",
        "-pass",
        f"env:{PASSPHRASE_ENV}",
    ]
    with subprocess.Popen(  # noqa: S603  # nosec B603
        dump_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as dump:
        if dump.stdout is None or dump.stderr is None:
            raise BackupError("Could not read the dump.")
        with subprocess.Popen(  # noqa: S603  # nosec B603
            enc_cmd, stdin=dump.stdout, stdout=handle, stderr=subprocess.PIPE, env={**os.environ}
        ) as enc:
            dump.stdout.close()  # so the encrypter sees the end of the dump
            enc_error = enc.communicate()[1]
        dump_error = dump.stderr.read()
        dump_code = dump.wait()
    if enc.returncode or dump_code:
        raise BackupError(f"The backup failed: {(dump_error + enc_error).decode()[:300]}")


def make_backup(
    out_dir: Path,
    db: str = DEFAULT_DB,
    container: str = CONTAINER,
    now: datetime | None = None,
) -> Path:
    """Dump the database to `out_dir` (encrypted when HQ_BACKUP_PASSPHRASE is set)."""
    now = now or datetime.now(UTC)
    passphrase = os.environ.get(PASSPHRASE_ENV)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"{db}_{now:%Y%m%dT%H%M%SZ}.dump" + (".enc" if passphrase else "")
    target = out_dir / name
    counts = row_counts(db, container)
    revision = psql("select version_num from hq.alembic_version", db, container)
    version = psql("show server_version", db, container)
    dump_cmd = [_docker(), "exec", container, "pg_dump", "-U", DB_USER, "-Fc", "-Z", "6", db]
    try:
        with target.open("wb") as handle:
            if passphrase:
                _dump_encrypted(dump_cmd, handle)
            else:
                _dump_plain(dump_cmd, handle)
    except BaseException:
        target.unlink(missing_ok=True)  # never leave a half-written file that looks like a backup
        raise
    manifest = Manifest(
        file=name,
        created_at=now.isoformat(),
        size_bytes=target.stat().st_size,
        sha256=sha256_of(target),
        encrypted=bool(passphrase),
        database=db,
        schema_revision=revision,
        postgres_version=version,
        row_counts=counts,
    )
    manifest_path(target).write_text(manifest.to_json(), encoding="utf-8")
    return target


def backups_in(folder: Path) -> list[Path]:
    """Backup files in a folder, oldest first (by the time in their names)."""
    found = [p for p in folder.glob("*.dump*") if NAME.match(p.name)]
    return sorted(found, key=lambda p: NAME.match(p.name).group("stamp"))  # type: ignore[union-attr]


def stamp_of(path: Path) -> datetime:
    match = NAME.match(path.name)
    if not match:
        raise BackupError(f"{path.name} is not a backup file name.")
    return datetime.strptime(match.group("stamp"), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


def retention_plan(
    files: list[Path],
    today: date,
    daily_days: int = KEEP_DAILY_DAYS,
    monthly: int = KEEP_MONTHLY_MONTHS,
) -> tuple[list[Path], list[Path]]:
    """(keep, delete): every backup of the last `daily_days` days, plus the newest of each month
    for the last `monthly` months, and ALWAYS the newest backup and the newest verified one."""
    cutoff = today - timedelta(days=daily_days)
    first_month = (today.year * 12 + today.month - 1) - monthly
    keep: set[Path] = set()
    newest_by_month: dict[tuple[int, int], Path] = {}
    for path in files:
        when = stamp_of(path)
        if when.date() >= cutoff:
            keep.add(path)
        newest_by_month[(when.year, when.month)] = path  # later files overwrite earlier ones
    for (year, month), path in newest_by_month.items():
        if year * 12 + month - 1 >= first_month:
            keep.add(path)
    if files:
        keep.add(files[-1])
        verified = [p for p in files if verified_path(p).exists()]
        if verified:
            keep.add(verified[-1])
    return [p for p in files if p in keep], [p for p in files if p not in keep]


def latest_status(folder: Path) -> dict[str, Any]:
    """What the dashboard shows: the newest backup, its age, and whether a restore test passed."""
    files = backups_in(folder) if folder.exists() else []
    if not files:
        return {"newest": None, "age_hours": None, "verified": False, "encrypted": None}
    newest = files[-1]
    manifest = load_manifest(newest) if manifest_path(newest).exists() else None
    age = (datetime.now(UTC) - stamp_of(newest)).total_seconds() / 3600
    return {
        "newest": newest.name,
        "age_hours": round(age, 1),
        "verified": verified_path(newest).exists(),
        "encrypted": manifest.encrypted if manifest else None,
        "size_gb": round(newest.stat().st_size / 1e9, 2),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Back up the database.")
    parser.add_argument("--out", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--retention", action="store_true", help="show what retention would delete")
    parser.add_argument("--apply", action="store_true", help="with --retention: really delete")
    args = parser.parse_args(argv)
    load_passphrase()
    try:
        if args.retention:
            keep, drop = retention_plan(backups_in(args.out), datetime.now(UTC).date())
            print(f"keep {len(keep)}, delete {len(drop)}")
            for path in drop:
                print(("deleting " if args.apply else "would delete ") + path.name)
                if args.apply:
                    for extra in (path, manifest_path(path), verified_path(path)):
                        extra.unlink(missing_ok=True)
            return 0
        target = make_backup(args.out, args.db)
    except BackupError as exc:
        print(f"BACKUP FAILED: {exc}")
        return 1
    manifest = load_manifest(target)
    state = (
        "encrypted" if manifest.encrypted else "NOT encrypted (set HQ_BACKUP_PASSPHRASE in .env)"
    )
    print(
        f"Backup written: {target} ({manifest.size_bytes / 1e9:.2f} GB, {state}); "
        f"sha256 {manifest.sha256[:16]}..."
    )
    print("It is not trusted until `python -m halal_quant.ops.restore_test` has passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
