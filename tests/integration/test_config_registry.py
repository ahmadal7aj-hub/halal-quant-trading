"""PRD Test 11: a configuration change creates an audit record.

Runs inside rolled-back transactions: audit events can never be deleted afterwards.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, select
from sqlalchemy.exc import ProgrammingError

from halal_quant.audit import audit_event_table
from halal_quant.core.config import (
    ConfigError,
    UniverseConfig,
    config_version_table,
    load_config,
    register_config,
)
from halal_quant.core.logging import correlation_scope
from halal_quant.core.settings import DbRole

UNIVERSE = Path(__file__).parents[2] / "config/universe/default.yaml"


@pytest.fixture
def conn(engines: dict[DbRole, Engine]) -> Iterator[Connection]:
    with engines[DbRole.APP].connect() as connection:
        transaction = connection.begin()
        try:
            yield connection
        finally:
            transaction.rollback()


def variant(tmp_path: Path, version: str, min_price: str) -> Path:
    text = UNIVERSE.read_text(encoding="utf-8")
    text = text.replace("version: universe-v1", f"version: {version}")
    text = text.replace('min_price_usd: "5.00"', f'min_price_usd: "{min_price}"')
    path = tmp_path / f"{version}-{min_price}.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def events(conn: Connection, correlation_id: str) -> list[dict[str, object]]:
    rows = conn.execute(
        select(audit_event_table)
        .where(audit_event_table.c.correlation_id == correlation_id)
        .order_by(audit_event_table.c.sequence)
    ).mappings()
    return [dict(row) for row in rows]


def test_11_config_change_creates_an_audit_record(conn: Connection, tmp_path: Path) -> None:
    first = load_config(variant(tmp_path, "it-v1", "5.00"), UniverseConfig)
    second = load_config(variant(tmp_path, "it-v2", "7.50"), UniverseConfig)
    with correlation_scope("test-11") as cid:
        register_config(conn, first, actor="owner", reason="first version")
        register_config(conn, second, actor="owner", reason="raise minimum price")
    registered, changed = events(conn, cid)[-2:]

    # real data now holds a registered universe version, so either wording is valid here
    assert registered["action"] in {"config.registered", "config.changed"}
    assert changed["action"] == "config.changed"
    assert changed["actor"] == "owner"
    assert changed["reason"] == "raise minimum price"
    assert changed["entity_type"] == "config:universe" and changed["entity_id"] == "it-v2"
    assert changed["old_value"]["min_price_usd"] == "5.00"  # type: ignore[index]
    assert changed["new_value"]["min_price_usd"] == "7.50"  # type: ignore[index]
    assert changed["occurred_at"] is not None  # timestamp
    assert changed["details"]["previous_version"] == "it-v1"  # type: ignore[index]


def test_registering_the_same_config_again_writes_nothing(conn: Connection, tmp_path: Path) -> None:
    loaded = load_config(variant(tmp_path, "it-same", "5.00"), UniverseConfig)
    with correlation_scope() as cid:
        register_config(conn, loaded, actor="owner", reason="once")
        register_config(conn, loaded, actor="owner", reason="twice")
    assert len(events(conn, cid)) == 1


def test_changed_content_without_a_new_version_is_refused(conn: Connection, tmp_path: Path) -> None:
    original = load_config(variant(tmp_path, "it-fixed", "5.00"), UniverseConfig)
    edited = load_config(variant(tmp_path, "it-fixed", "9.00"), UniverseConfig)
    with correlation_scope() as cid:
        register_config(conn, original, actor="owner", reason="original")
        with pytest.raises(ConfigError, match="new version"):
            register_config(conn, edited, actor="owner", reason="sneaky edit")
    assert len(events(conn, cid)) == 1  # only the original


def test_app_role_cannot_rewrite_config_history(conn: Connection) -> None:
    with pytest.raises(ProgrammingError, match="permission denied"):
        conn.execute(config_version_table.update().values(sha256="x"))
