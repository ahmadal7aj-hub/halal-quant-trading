"""Task 5 acceptance: the audit log is append-only, including for the table owner.

Every test runs inside a transaction that is rolled back, so no test events are left behind
(they could not be deleted afterwards).
"""

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Connection, Engine, select, text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from halal_quant.audit import AuditEvent, audit_event_table, record_event
from halal_quant.core.logging import correlation_scope
from halal_quant.core.settings import DbRole

EVENT = AuditEvent(
    actor="system:integration-test",
    action="test.recorded",
    entity_type="test",
    entity_id="42",
    old_value={"a": 1},
    new_value={"a": 2},
    reason="integration test",
    source="tests/integration",
    details={"data_version": "v-test"},
)


def rolled_back(engine: Engine) -> Iterator[Connection]:
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


@pytest.fixture
def app_conn(engines: dict[DbRole, Engine]) -> Iterator[Connection]:
    yield from rolled_back(engines[DbRole.APP])


@pytest.fixture
def owner_conn(engines: dict[DbRole, Engine]) -> Iterator[Connection]:
    yield from rolled_back(engines[DbRole.MIGRATOR])


def test_app_writes_and_reads_an_event(app_conn: Connection) -> None:
    with correlation_scope("corr-it-1"):
        event_id = record_event(app_conn, EVENT)
    row = app_conn.execute(
        select(audit_event_table).where(audit_event_table.c.event_id == event_id)
    ).one()
    assert row.correlation_id == "corr-it-1"
    assert row.new_value == {"a": 2}
    assert row.details == {"data_version": "v-test"}
    assert row.occurred_at.tzinfo is not None
    assert row.sequence > 0


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE hq.audit_event SET reason = 'changed'",
        "DELETE FROM hq.audit_event",
        "TRUNCATE hq.audit_event",
    ],
)
def test_app_role_cannot_change_or_remove_events(app_conn: Connection, statement: str) -> None:
    with pytest.raises(ProgrammingError, match="permission denied"):
        app_conn.execute(text(statement))


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE hq.audit_event SET reason = 'changed'",
        "DELETE FROM hq.audit_event",
        "TRUNCATE hq.audit_event",
    ],
)
def test_even_the_owner_cannot_change_or_remove_events(
    owner_conn: Connection, statement: str
) -> None:
    record_event(owner_conn, EVENT)  # a row must exist for the row-level trigger to fire
    with pytest.raises(DBAPIError, match="append-only"):
        owner_conn.execute(text(statement))


def test_readonly_role_can_read_but_not_write(engines: dict[DbRole, Engine]) -> None:
    for conn in rolled_back(engines[DbRole.READONLY]):
        conn.execute(select(audit_event_table.c.event_id).limit(1)).all()
        with pytest.raises(ProgrammingError, match="permission denied"):
            record_event(conn, EVENT)


def test_event_rolls_back_with_the_action_it_describes(engines: dict[DbRole, Engine]) -> None:
    with engines[DbRole.APP].connect() as conn:
        transaction = conn.begin()
        event_id = record_event(conn, EVENT)
        transaction.rollback()
        found = conn.execute(
            select(audit_event_table.c.event_id).where(audit_event_table.c.event_id == event_id)
        ).all()
    assert found == []


def test_empty_actor_is_rejected_by_the_database(app_conn: Connection) -> None:
    with pytest.raises(DBAPIError, match="ck_audit_event_actor"):
        app_conn.execute(
            audit_event_table.insert().values(
                event_id=uuid.uuid4(),
                actor="",
                action="x",
                entity_type="x",
                source="x",
                correlation_id="x",
            )
        )
