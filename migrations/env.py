"""Alembic environment: runs migrations as the hq_migrator role, in the hq schema."""

from logging.config import fileConfig

from alembic import context

import halal_quant.audit  # noqa: F401  (registers tables on the shared metadata)
import halal_quant.core.config  # noqa: F401
import halal_quant.data.market_data  # noqa: F401
import halal_quant.data.security_master  # noqa: F401
import halal_quant.data.versions  # noqa: F401
from halal_quant.core.settings import DB_SCHEMA, DbRole, get_settings
from halal_quant.db.engine import make_engine, metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = metadata


def include_name(name: str | None, type_: str, parent_names: object) -> bool:
    """Compare only our schema, and ignore Alembic's own version table."""
    if type_ == "schema":
        return name == DB_SCHEMA
    return not (type_ == "table" and name == "alembic_version")


def run_migrations_offline() -> None:
    """Render SQL to stdout instead of running it (`alembic upgrade head --sql`)."""
    context.configure(
        url=get_settings().db_url(DbRole.MIGRATOR).render_as_string(hide_password=True),
        target_metadata=target_metadata,
        literal_binds=True,
        version_table_schema=DB_SCHEMA,
        include_schemas=True,
        include_name=include_name,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # search_path=public so objects in `hq` are always named with their schema; otherwise
    # Alembic's comparison (`alembic check`) confuses the default schema with `hq`.
    engine = make_engine(get_settings(), DbRole.MIGRATOR, search_path="public")
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table_schema=DB_SCHEMA,
            include_schemas=True,
            include_name=include_name,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
