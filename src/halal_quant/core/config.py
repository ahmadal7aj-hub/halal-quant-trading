"""Versioned configuration (PRD §29, Test 11; rulebook B6).

Important parameters live in YAML files under `config/`, never in code. Each file has a
`version`; loading validates it strictly and fails closed with a plain-English error. A run
records the `ConfigRef` (version + content hash) it used. The hash covers the validated values,
not the file's bytes, so comments and line endings (CRLF on Windows) don't change it.

`register_config` stores each version in the `config_version` table and writes an audit event
whenever a configuration first appears or changes (old value, new value, actor, reason).
Editing a file without giving it a new version is refused.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import (
    BigInteger,
    Column,
    Connection,
    DateTime,
    Identity,
    Table,
    Text,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB

from halal_quant.audit import AuditEvent, record_event
from halal_quant.db.engine import metadata

Ratio = Annotated[Decimal, Field(gt=0, lt=1)]
Money = Annotated[Decimal, Field(gt=0)]
Status = Literal["proposed", "approved", "retired"]


class ConfigError(Exception):
    """A configuration file is missing, unreadable or invalid. Nothing may run with it."""


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ExcludedBusiness(_Strict):
    category: str = Field(min_length=1)
    industries: list[str]
    sic_ranges: list[tuple[int, int]]


class ShariaConfig(_Strict):
    config_type: Literal["sharia_methodology"]
    version: str = Field(min_length=1)
    status: Status
    approved_by: str | None = None
    approved_on: date | None = None
    max_debt_to_market_value: Ratio
    max_cash_and_investments_to_market_value: Ratio
    max_prohibited_income_to_revenue: Ratio
    max_report_age_months: int = Field(gt=0)
    market_value_method: Literal["screening_date"]
    screening_frequency: Literal["monthly_last_trading_day"]
    excluded_businesses: list[ExcludedBusiness] = Field(min_length=1)


class UniverseConfig(_Strict):
    config_type: Literal["universe"]
    version: str = Field(min_length=1)
    status: Status
    approved_by: str | None = None
    approved_on: date | None = None
    sharia_methodology: str = Field(min_length=1)
    min_price_usd: Money
    min_median_daily_dollar_volume_usd: Money
    liquidity_window_trading_days: int = Field(gt=0)
    max_price_age_trading_days: int = Field(gt=0)


@dataclass(frozen=True)
class ConfigRef:
    """What a run records about the configuration it used."""

    config_type: str
    version: str
    sha256: str


@dataclass(frozen=True)
class LoadedConfig[T: BaseModel]:
    config: T
    ref: ConfigRef
    path: Path


def load_config[T: BaseModel](path: Path, model: type[T]) -> LoadedConfig[T]:
    """Read and validate one config file. Any problem raises ConfigError (fail closed)."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"Config file {path} could not be read: {exc.strerror}.") from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Config file {path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Config file {path} must contain a mapping of settings.")
    try:
        config = model.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '(file)'}: {err['msg']}"
            for err in exc.errors()
        )
        raise ConfigError(f"Config file {path} is invalid: {problems}") from exc
    canonical = json.dumps(config.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    ref = ConfigRef(
        config_type=str(data["config_type"]),
        version=str(data["version"]),
        sha256=hashlib.sha256(canonical.encode()).hexdigest(),
    )
    return LoadedConfig(config=config, ref=ref, path=path)


config_version_table = Table(
    "config_version",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("config_type", Text, nullable=False),
    Column("version", Text, nullable=False),
    Column("sha256", Text, nullable=False),
    Column("content", JSONB, nullable=False),
    Column("registered_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("registered_by", Text, nullable=False),
    UniqueConstraint("config_type", "version", name="uq_config_version_type_version"),
)


def register_config(
    conn: Connection, loaded: LoadedConfig[Any], actor: str, reason: str
) -> ConfigRef:
    """Record this config version and audit it if it is new. Same content again is a no-op."""
    ref = loaded.ref
    content = loaded.config.model_dump(mode="json")
    table = config_version_table

    recorded_sha = conn.execute(
        select(table.c.sha256).where(
            table.c.config_type == ref.config_type, table.c.version == ref.version
        )
    ).scalar_one_or_none()
    if recorded_sha == ref.sha256:
        return ref
    if recorded_sha is not None:
        raise ConfigError(
            f"{loaded.path} was changed but its version is still {ref.version!r}. "
            "Give the changed configuration a new version; recorded versions are never rewritten."
        )

    previous = conn.execute(
        select(table.c.version, table.c.content)
        .where(table.c.config_type == ref.config_type)
        .order_by(table.c.id.desc())
        .limit(1)
    ).one_or_none()
    conn.execute(
        table.insert().values(
            config_type=ref.config_type,
            version=ref.version,
            sha256=ref.sha256,
            content=content,
            registered_by=actor,
        )
    )
    record_event(
        conn,
        AuditEvent(
            actor=actor,
            action="config.changed" if previous else "config.registered",
            entity_type=f"config:{ref.config_type}",
            entity_id=ref.version,
            old_value=previous.content if previous else None,
            new_value=content,
            reason=reason,
            source=loaded.path.as_posix(),
            details={
                "sha256": ref.sha256,
                "previous_version": previous.version if previous else None,
            },
        ),
    )
    return ref
