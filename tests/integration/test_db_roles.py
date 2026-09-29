"""Task 3 acceptance: migrations are applied and each role has exactly its intended rights."""

from collections.abc import Iterator

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, text
from sqlalchemy.exc import ProgrammingError

from halal_quant.core.settings import DbRole, Settings
from halal_quant.db.bootstrap import run_bootstrap

PROBE = "hq.it_probe"


def assert_denied(engine: Engine, statement: str) -> None:
    with (
        pytest.raises(ProgrammingError, match="permission denied|must be owner"),
        engine.begin() as conn,
    ):
        conn.execute(text(statement))


@pytest.fixture
def probe_table(engines: dict[DbRole, Engine]) -> Iterator[str]:
    """A table created by the migrator, as a migration would create it."""
    with engines[DbRole.MIGRATOR].begin() as conn:
        conn.execute(text(f"CREATE TABLE {PROBE} (id int PRIMARY KEY, note text)"))
    yield PROBE
    with engines[DbRole.MIGRATOR].begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {PROBE}"))


def test_database_is_at_the_latest_migration(engines: dict[DbRole, Engine]) -> None:
    head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    with engines[DbRole.READONLY].connect() as conn:
        current = conn.execute(text("SELECT version_num FROM hq.alembic_version")).scalar_one()
    assert current == head


def test_app_role_cannot_change_the_schema(engines: dict[DbRole, Engine], probe_table: str) -> None:
    app = engines[DbRole.APP]
    assert_denied(app, "CREATE TABLE hq.app_made (id int)")
    assert_denied(app, "CREATE TABLE public.app_made (id int)")
    assert_denied(app, f"ALTER TABLE {probe_table} ADD COLUMN extra int")
    assert_denied(app, f"DROP TABLE {probe_table}")


def test_app_role_cannot_touch_the_migration_version(engines: dict[DbRole, Engine]) -> None:
    app = engines[DbRole.APP]
    assert_denied(app, "UPDATE hq.alembic_version SET version_num = 'x'")
    assert_denied(app, "DELETE FROM hq.alembic_version")


def test_app_role_can_read_and_write_data(engines: dict[DbRole, Engine], probe_table: str) -> None:
    with engines[DbRole.APP].begin() as conn:
        conn.execute(text(f"INSERT INTO {probe_table} VALUES (1, 'a')"))
        conn.execute(text(f"UPDATE {probe_table} SET note = 'b' WHERE id = 1"))
        assert conn.execute(text(f"SELECT note FROM {probe_table}")).scalar_one() == "b"
        conn.execute(text(f"DELETE FROM {probe_table}"))


def test_readonly_role_can_only_read(engines: dict[DbRole, Engine], probe_table: str) -> None:
    readonly = engines[DbRole.READONLY]
    with readonly.connect() as conn:
        assert conn.execute(text(f"SELECT count(*) FROM {probe_table}")).scalar_one() == 0
    assert_denied(readonly, f"INSERT INTO {probe_table} VALUES (1, 'a')")
    assert_denied(readonly, "CREATE TABLE hq.ro_made (id int)")


def test_roles_have_no_admin_powers(engines: dict[DbRole, Engine]) -> None:
    with engines[DbRole.READONLY].connect() as conn:
        rows = conn.execute(
            text(
                "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole FROM pg_roles"
                " WHERE rolname = ANY(:names)"
            ),
            {"names": [role.value for role in DbRole]},
        ).all()
    assert {row.rolname for row in rows} == {role.value for role in DbRole}
    assert not any(row.rolsuper or row.rolcreatedb or row.rolcreaterole for row in rows)


def test_bootstrap_rerun_keeps_everything_working(
    settings: Settings, engines: dict[DbRole, Engine]
) -> None:
    run_bootstrap(settings)
    with engines[DbRole.APP].connect() as conn:
        assert conn.execute(text("SELECT current_user")).scalar_one() == DbRole.APP.value
    assert_denied(engines[DbRole.APP], "CREATE TABLE hq.app_made (id int)")
