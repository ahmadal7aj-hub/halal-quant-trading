"""Application settings, read from environment variables or a local `.env` file.

Secrets are held as `SecretStr` so they never appear in logs, reprs or tracebacks.
Nothing here has a secret default: a missing password stops the app (fail closed).
"""

from enum import StrEnum
from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

DB_SCHEMA = "hq"


class DbRole(StrEnum):
    """PostgreSQL login roles, from most to least privileged (doc 02 §7)."""

    MIGRATOR = "hq_migrator"  # owns the schema; used only by Alembic
    APP = "hq_app"  # reads and writes data; cannot change the schema
    READONLY = "hq_readonly"  # reporting and inspection; SELECT only


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HQ_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    db_host: str = "127.0.0.1"
    db_port: int = Field(default=5432, ge=1, le=65535)
    db_name: str = "halal_quant"

    # Superuser created by the Postgres container; used only by the bootstrap command.
    db_admin_user: str = "hq_admin"
    db_admin_password: SecretStr

    db_migrator_password: SecretStr
    db_app_password: SecretStr
    db_readonly_password: SecretStr

    def role_password(self, role: DbRole) -> SecretStr:
        return {
            DbRole.MIGRATOR: self.db_migrator_password,
            DbRole.APP: self.db_app_password,
            DbRole.READONLY: self.db_readonly_password,
        }[role]

    def admin_url(self) -> URL:
        return self._url(self.db_admin_user, self.db_admin_password)

    def db_url(self, role: DbRole) -> URL:
        return self._url(role.value, self.role_password(role))

    def _url(self, user: str, password: SecretStr) -> URL:
        # URL masks the password in str()/repr(); it is only revealed to the driver.
        return URL.create(
            "postgresql+psycopg",
            username=user,
            password=password.get_secret_value(),
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # required fields come from the environment
