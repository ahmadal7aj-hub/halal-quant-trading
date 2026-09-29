"""SQLAlchemy engines, one per database role."""

from sqlalchemy import Engine, MetaData, create_engine

from halal_quant.core.settings import DB_SCHEMA, DbRole, Settings

# Shared metadata for all tables; every table lives in the `hq` schema.
metadata = MetaData(schema=DB_SCHEMA)


def make_engine(settings: Settings, role: DbRole = DbRole.APP) -> Engine:
    """Engine for one role. The app uses `DbRole.APP`; only migrations use `MIGRATOR`."""
    return create_engine(
        settings.db_url(role),
        pool_pre_ping=True,
        connect_args={"options": f"-c search_path={DB_SCHEMA}"},
    )
