"""Prove a backup restores: load it into a scratch database and compare every table (Phase 7).

    uv run python -m halal_quant.ops.restore_test [--dump FILE] [--keep]

The scratch database `hq_restore_test` is created, filled with `pg_restore`, checked against the
manifest (the SHA-256 of the file, the schema revision, and the exact row count of every table),
and dropped again, so the live database is never touched. A pass writes `<dump>.verified.json`.
"""

import argparse
import json
import os
import subprocess  # nosec B404
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from halal_quant.ops.backup import (
    CONTAINER,
    DB_USER,
    DEFAULT_DIR,
    PASSPHRASE_ENV,
    BackupError,
    Manifest,
    _docker,
    _openssl,
    backups_in,
    load_manifest,
    load_passphrase,
    psql,
    row_counts,
    sha256_of,
    verified_path,
)

SCRATCH = "hq_restore_test"


def compare(manifest_counts: dict[str, int], restored: dict[str, int]) -> list[str]:
    """Plain-English differences between the manifest and the restored database (empty = equal)."""
    problems = []
    for table in sorted(set(manifest_counts) | set(restored)):
        want, got = manifest_counts.get(table), restored.get(table)
        if want is None:
            problems.append(f"table {table} exists after restore but was not in the manifest")
        elif got is None:
            problems.append(f"table {table} is missing after restore")
        elif want != got:
            problems.append(f"table {table}: {want} rows in the backup manifest, {got} restored")
    return problems


def restore_into_scratch(dump: Path, manifest: Manifest, container: str = CONTAINER) -> None:
    """Create the scratch database and pipe the dump (decrypted first if needed) into pg_restore."""
    psql(f"drop database if exists {SCRATCH}", "postgres", container)
    psql(f"create database {SCRATCH}", "postgres", container)
    restore_cmd = [
        _docker(), "exec", "-i", container, "pg_restore", "-U", DB_USER, "-d", SCRATCH,
        "--no-owner", "--no-privileges", "--exit-on-error",
    ]  # fmt: skip
    with dump.open("rb") as source:
        if manifest.encrypted:
            if not os.environ.get(PASSPHRASE_ENV):
                raise BackupError(f"This backup is encrypted: set {PASSPHRASE_ENV} to restore it.")
            _restore_encrypted(source, restore_cmd)
        else:
            _restore_plain(source, restore_cmd)


def _restore_plain(source: BinaryIO, restore_cmd: list[str]) -> None:
    with subprocess.Popen(  # noqa: S603  # nosec B603
        restore_cmd, stdin=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as restore:
        _, err = restore.communicate()
    if restore.returncode:
        raise BackupError(f"pg_restore failed: {err.decode()[:400]}")


def _restore_encrypted(source: BinaryIO, restore_cmd: list[str]) -> None:
    dec_cmd = [_openssl(), "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-pass", f"env:{PASSPHRASE_ENV}"]
    with subprocess.Popen(  # noqa: S603  # nosec B603
        dec_cmd, stdin=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={**os.environ}
    ) as dec:
        with subprocess.Popen(  # noqa: S603  # nosec B603
            restore_cmd, stdin=dec.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ) as restore:
            if dec.stdout:
                dec.stdout.close()
            _, err = restore.communicate()
        dec_error = dec.stderr.read() if dec.stderr else b""
        dec_code = dec.wait()
    if dec_code:
        raise BackupError("Decryption failed (wrong passphrase or damaged file).")
    if restore.returncode:
        raise BackupError(f"pg_restore failed: {err.decode()[:400]} {dec_error.decode()[:100]}")


def verify(dump: Path, container: str = CONTAINER, keep: bool = False) -> list[str]:
    """Restore `dump` into the scratch database and return the list of problems (empty = passed)."""
    manifest = load_manifest(dump)
    problems: list[str] = []
    if sha256_of(dump) != manifest.sha256:
        return ["The file's SHA-256 differs from the manifest: it was changed or damaged."]
    try:
        restore_into_scratch(dump, manifest, container)
        revision = psql("select version_num from hq.alembic_version", SCRATCH, container)
        if revision != manifest.schema_revision:
            problems.append(
                f"schema revision {revision} after restore, "
                f"manifest says {manifest.schema_revision}"
            )
        problems += compare(manifest.row_counts, row_counts(SCRATCH, container))
    finally:
        if not keep:
            psql(f"drop database if exists {SCRATCH}", "postgres", container)
    if not problems:
        verified_path(dump).write_text(
            json.dumps(
                {
                    "verified_at": datetime.now(UTC).isoformat(),
                    "tables": len(manifest.row_counts),
                    "rows": sum(manifest.row_counts.values()),
                    "sha256": manifest.sha256,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Restore a backup into a scratch database and check it."
    )
    parser.add_argument("--dump", type=Path, help="default: the newest backup in the backup folder")
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--keep", action="store_true", help="leave the scratch database in place")
    args = parser.parse_args(argv)
    load_passphrase()
    dump = args.dump
    if dump is None:
        files = backups_in(args.dir)
        if not files:
            print("No backup found.")
            return 1
        dump = files[-1]
    try:
        problems = verify(dump, keep=args.keep)
    except BackupError as exc:
        print(f"RESTORE TEST FAILED: {exc}")
        return 1
    if problems:
        print("RESTORE TEST FAILED:")
        for problem in problems:
            print(" -", problem)
        return 1
    print(f"RESTORE TEST PASSED: {dump.name} restored and every table matches its manifest.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
