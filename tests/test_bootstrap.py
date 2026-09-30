import psycopg
import pytest

from halal_quant.core.settings import DbRole, Settings
from halal_quant.db import bootstrap

PASSWORDS = {role: f"pw-{role.value}" for role in DbRole}


def render(existing: set[str]) -> list[str]:
    statements = bootstrap.bootstrap_statements("halal_quant", PASSWORDS, existing)
    return [s.as_string() for s in statements]


def test_new_roles_are_created_with_login_and_no_extra_powers() -> None:
    sql = render(existing=set())
    for role in DbRole:
        [create] = [s for s in sql if s.startswith(f'CREATE ROLE "{role.value}"')]
        assert "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE" in create
        assert f"PASSWORD 'pw-{role.value}'" in create


def test_existing_roles_are_altered_so_rerun_rotates_passwords() -> None:
    sql = render(existing={role.value for role in DbRole})
    assert not any(s.startswith("CREATE ROLE") for s in sql)
    assert sum(s.startswith("ALTER ROLE") and "PASSWORD" in s for s in sql) == len(DbRole)


def test_schema_is_owned_by_migrator_and_app_gets_data_access_only() -> None:
    sql = "\n".join(render(existing=set()))
    assert 'CREATE SCHEMA IF NOT EXISTS "hq" AUTHORIZATION "hq_migrator"' in sql
    assert 'GRANT USAGE ON SCHEMA "hq" TO "hq_app", "hq_readonly"' in sql
    assert "GRANT CREATE" not in sql
    assert (
        'FOR ROLE "hq_migrator" IN SCHEMA "hq" GRANT SELECT, INSERT, UPDATE, DELETE '
        'ON TABLES TO "hq_app"'
    ) in sql
    assert 'GRANT SELECT ON TABLES TO "hq_readonly"' in sql


def test_btree_gist_is_installed_by_the_superuser_not_migrations() -> None:
    assert "CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA public" in render(set())


def test_rerun_never_regrants_existing_tables() -> None:
    # Re-granting on ALL TABLES would undo per-table revokes (e.g. append-only audit log).
    assert not any("ALL TABLES" in s for s in render(existing=set()))


def test_password_with_quote_is_escaped() -> None:
    passwords = {**PASSWORDS, DbRole.APP: "it's"}
    sql = [s.as_string() for s in bootstrap.bootstrap_statements("halal_quant", passwords, set())]
    assert any("PASSWORD 'it''s'" in s for s in sql)


def test_main_reports_connection_failure_in_plain_english(
    monkeypatch: pytest.MonkeyPatch, fake_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(_: Settings) -> None:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(bootstrap, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(bootstrap, "run_bootstrap", fail)
    assert bootstrap.main() == 1
    assert "docker compose up" in capsys.readouterr().out


def test_main_success(
    monkeypatch: pytest.MonkeyPatch, fake_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(bootstrap, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(bootstrap, "run_bootstrap", lambda _: None)
    assert bootstrap.main() == 0
    assert "complete" in capsys.readouterr().out
