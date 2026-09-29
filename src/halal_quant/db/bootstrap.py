"""Create the database roles and the `hq` schema. Safe to run repeatedly.

Run as the Postgres superuser, once after the database is created (Docker or CI):

    uv run python -m halal_quant.db.bootstrap

Roles (doc 02 §7, least privilege):
- `hq_migrator` owns schema `hq`; only Alembic migrations connect as this role.
- `hq_app` gets data access (SELECT/INSERT/UPDATE/DELETE) on tables the migrator creates,
  but cannot create, alter or drop anything. Append-only tables revoke UPDATE/DELETE in
  their own migration.
- `hq_readonly` gets SELECT only.

Re-running also resets the role passwords from settings, which is how passwords are rotated.
"""

import sys
from collections.abc import Mapping

import psycopg
from psycopg import sql

from halal_quant.core.settings import DB_SCHEMA, DbRole, Settings, get_settings

APP_TABLE_PRIVILEGES = "SELECT, INSERT, UPDATE, DELETE"


def bootstrap_statements(
    db_name: str,
    passwords: Mapping[DbRole, str],
    existing_roles: set[str],
) -> list[sql.Composed]:
    """Build the bootstrap SQL. Pure function, so it can be unit-tested without a database."""
    schema = sql.Identifier(DB_SCHEMA)
    database = sql.Identifier(db_name)
    migrator = sql.Identifier(DbRole.MIGRATOR.value)
    app = sql.Identifier(DbRole.APP.value)
    readonly = sql.Identifier(DbRole.READONLY.value)

    statements: list[sql.Composed] = []
    for role in DbRole:
        verb = "ALTER" if role.value in existing_roles else "CREATE"
        statements.append(
            sql.SQL(
                verb + " ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION"
                " PASSWORD {}"
            ).format(sql.Identifier(role.value), sql.Literal(passwords[role]))
        )
        statements.append(
            sql.SQL("ALTER ROLE {} SET search_path = {}").format(sql.Identifier(role.value), schema)
        )

    statements += [
        # Only our roles may connect; nobody but the owner may create objects in `public`.
        sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(database),
        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}, {}, {}").format(
            database, migrator, app, readonly
        ),
        sql.SQL("REVOKE CREATE ON SCHEMA public FROM PUBLIC").format(),
        sql.SQL("CREATE SCHEMA IF NOT EXISTS {} AUTHORIZATION {}").format(schema, migrator),
        sql.SQL("ALTER SCHEMA {} OWNER TO {}").format(schema, migrator),
        sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(schema),
        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}, {}").format(schema, app, readonly),
        # Privileges on tables and sequences the migrator creates from now on.
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} GRANT "
            + APP_TABLE_PRIVILEGES
            + " ON TABLES TO {}"
        ).format(migrator, schema, app),
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} GRANT SELECT ON TABLES TO {}"
        ).format(migrator, schema, readonly),
        sql.SQL(
            "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {}"
            " GRANT USAGE, SELECT ON SEQUENCES TO {}"
        ).format(migrator, schema, app),
    ]
    return statements


def run_bootstrap(settings: Settings) -> None:
    passwords = {role: settings.role_password(role).get_secret_value() for role in DbRole}
    with psycopg.connect(
        host=settings.db_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user=settings.db_admin_user,
        password=settings.db_admin_password.get_secret_value(),
        autocommit=True,
    ) as conn:
        rows = conn.execute(
            "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
            ([role.value for role in DbRole],),
        ).fetchall()
        existing = {row[0] for row in rows}
        with conn.transaction():
            for statement in bootstrap_statements(settings.db_name, passwords, existing):
                conn.execute(statement)


def main() -> int:
    try:
        settings = get_settings()
        run_bootstrap(settings)
    except psycopg.OperationalError as exc:
        # The driver's message names host/port/user but never the password.
        print(f"Database bootstrap failed: could not connect to PostgreSQL ({exc}).")
        print("Is the database running? Try: docker compose up -d --wait")
        return 1
    print(f"Database bootstrap complete: roles and schema '{DB_SCHEMA}' are ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
