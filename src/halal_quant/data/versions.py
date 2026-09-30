"""Data-version register: which download a set of rows came from (BRD §6.4, PRD §20).

A version is the dataset name plus a fingerprint of the exact bytes downloaded, so the same
data always gets the same version and changed data gets a new one. Rows in the price and
corporate-action tables carry this string in `data_version`.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Connection,
    DateTime,
    Identity,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import insert

from halal_quant.db.engine import metadata

data_version_table = Table(
    "data_version",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("version", Text, nullable=False, unique=True),
    Column("source", Text, nullable=False),
    Column("dataset", Text, nullable=False),
    Column("sha256", Text, nullable=False),
    Column("row_count", BigInteger, nullable=False),
    Column("downloaded_at", DateTime(timezone=True), nullable=False),
    Column("registered_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("dataset", "sha256", name="uq_data_version_dataset_sha256"),
    CheckConstraint("row_count >= 0", name="ck_data_version_row_count"),
    CheckConstraint("source <> '' AND dataset <> ''", name="ck_data_version_names"),
)


@dataclass(frozen=True)
class DataVersion:
    version: str
    source: str
    dataset: str
    sha256: str
    row_count: int
    downloaded_at: datetime


def register_data_version(conn: Connection, data: DataVersion) -> None:
    """Record a download. Downloading identical data again keeps the first record."""
    conn.execute(
        insert(data_version_table)
        .values(
            version=data.version,
            source=data.source,
            dataset=data.dataset,
            sha256=data.sha256,
            row_count=data.row_count,
            downloaded_at=data.downloaded_at,
        )
        .on_conflict_do_nothing(constraint="uq_data_version_dataset_sha256")
    )


def version_label(dataset: str, sha256: str) -> str:
    """The stable name of a download, e.g. `sharadar.tickers.stocks@3f9a1c0b7d2e`."""
    return f"{dataset}@{sha256[:12]}"
