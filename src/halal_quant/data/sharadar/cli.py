"""The command-line shell shared by every Sharadar import: key check, download, import, report.

Keeping it in one place keeps the key handling in one place: the key is read from settings (a
`SecretStr`), handed only to `SharadarClient`, and never printed.
"""

from collections.abc import Callable, Iterable
from typing import Protocol

from sqlalchemy import Connection

from halal_quant.core.logging import configure_logging, correlation_scope
from halal_quant.core.settings import DbRole, get_settings
from halal_quant.data.sharadar.client import Download, SharadarClient, SharadarError
from halal_quant.db.engine import make_engine

MAX_REVIEW_LINES_PRINTED = 20


class Report(Protocol):
    needs_review: list[str]

    def summary(self) -> str: ...


def run_import(
    endpoint: str,
    required_columns: Iterable[str],
    apply: Callable[[Connection, Download], Report],
    **params: str,
) -> int:
    """Download one Sharadar table and apply it in a single transaction. Returns an exit code."""
    settings = get_settings()
    configure_logging(settings, level=settings.log_level)
    if settings.sharadar_api_key is None:
        print("HQ_SHARADAR_API_KEY is not set. Add it to .env (never in chat or git).")
        return 1
    client = SharadarClient(settings.sharadar_api_key)
    with correlation_scope():
        try:
            download = client.fetch_csv(endpoint, required_columns, **params)
        except SharadarError as exc:
            print(f"Sharadar download failed: {exc}")
            return 1
        print(f"Downloaded {len(download.rows)} rows. Importing...")
        engine = make_engine(settings, DbRole.APP)
        with engine.begin() as conn:
            report = apply(conn, download)
        engine.dispose()
    print(report.summary())
    for note in report.needs_review[:MAX_REVIEW_LINES_PRINTED]:
        print(f"  review: {note}")
    return 0
