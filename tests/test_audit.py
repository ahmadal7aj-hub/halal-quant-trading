import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from halal_quant.audit import AuditEvent, record_event
from halal_quant.core.logging import MASK, SecretMasker, correlation_scope


class FakeConnection:
    """Captures the INSERT instead of sending it to a database."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def execute(self, statement: Any) -> None:
        self.rows.append(dict(statement.compile().params))


def event(**overrides: Any) -> AuditEvent:
    fields: dict[str, Any] = {
        "actor": "system:test",
        "action": "config.changed",
        "entity_type": "config",
        "source": "tests",
    }
    return AuditEvent(**{**fields, **overrides})


def test_event_is_written_with_the_current_correlation_id() -> None:
    conn = FakeConnection()
    with correlation_scope("run-42"):
        event_id = record_event(conn, event())  # type: ignore[arg-type]
    [row] = conn.rows
    assert row["correlation_id"] == "run-42"
    assert row["event_id"] == event_id
    assert row["actor"] == "system:test"


def test_event_outside_a_scope_gets_a_new_correlation_id() -> None:
    conn = FakeConnection()
    record_event(conn, event())  # type: ignore[arg-type]
    record_event(conn, event())  # type: ignore[arg-type]
    ids = [row["correlation_id"] for row in conn.rows]
    assert all(len(i) == 32 for i in ids) and ids[0] != ids[1]


def test_values_are_stored_as_json_safe_types() -> None:
    conn = FakeConnection()
    when = datetime(2026, 9, 29, tzinfo=UTC)
    ref = uuid.UUID(int=1)
    record_event(
        conn,  # type: ignore[arg-type]
        event(old_value={"limit": Decimal("0.30")}, new_value=[when, ref], details={"n": 1}),
    )
    [row] = conn.rows
    assert row["old_value"] == {"limit": "0.30"}  # Decimal kept exact, as text
    assert row["new_value"] == ["2026-09-29T00:00:00Z", str(ref)]
    assert row["details"] == {"n": 1}


def test_masker_removes_secrets_from_every_text_field() -> None:
    conn = FakeConnection()
    masker = SecretMasker(["hunter22"])
    record_event(
        conn,  # type: ignore[arg-type]
        event(
            reason="rotated hunter22",
            new_value={"url": "postgresql://u:hunter22@h/db", "list": ["hunter22", 5]},
            details={"hunter22": "key"},
        ),
        masker,
    )
    [row] = conn.rows
    assert "hunter22" not in repr(row)
    assert row["reason"] == f"rotated {MASK}"
    assert row["new_value"]["list"] == [MASK, 5]


@pytest.mark.parametrize("field", ["actor", "action", "entity_type", "source"])
def test_required_text_fields_cannot_be_empty(field: str) -> None:
    with pytest.raises(ValidationError):
        event(**{field: ""})


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        event(timestamp="2020-01-01")  # the database sets the time, not the caller


def test_events_are_immutable() -> None:
    e = event()
    with pytest.raises(ValidationError):
        e.actor = "someone else"  # type: ignore[misc]
