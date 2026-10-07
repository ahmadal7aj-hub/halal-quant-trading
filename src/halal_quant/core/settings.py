"""Application settings, read from environment variables or a local `.env` file.

Secrets are held as `SecretStr` so they never appear in logs, reprs or tracebacks.
Nothing here has a secret default: a missing password stops the app (fail closed).
"""

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

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

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    db_host: str = "127.0.0.1"
    db_port: int = Field(default=5432, ge=1, le=65535)
    db_name: str = "halal_quant"

    # Superuser created by the Postgres container; used only by the bootstrap command.
    db_admin_user: str = "hq_admin"
    db_admin_password: SecretStr

    db_migrator_password: SecretStr
    db_app_password: SecretStr
    db_readonly_password: SecretStr

    # Sharadar (Nasdaq Data Link) API key. Optional: only data imports need it. The owner puts it
    # in .env; it is a SecretStr, so logging masks it automatically.
    sharadar_api_key: SecretStr | None = None

    # Zoya API key (one month of the Personal Use Basic plan, for the task 15 cross-check).
    # Optional, entered by the owner in .env; a SecretStr, so logging masks it automatically.
    zoya_api_key: SecretStr | None = None

    # SEC EDGAR asks every automated client to identify itself with a contact, for example
    # "HalalQuantResearch you@example.com". Not a secret, but personal, so it lives in .env.
    sec_user_agent: str | None = None

    # Dashboard login: a salted PBKDF2 hash made by `python -m halal_quant.dashboard.auth` (never
    # the password itself). Without it the dashboard refuses everyone (fail closed).
    dashboard_password_hash: SecretStr | None = None

    # Where backups are written and read (default `E:\Backups`), and the passphrase that encrypts
    # them. The passphrase is the owner's: keep a copy in a password manager, or the backups cannot
    # be restored. Without it backups are made unencrypted and the dashboard warns.
    backup_dir: Path | None = None
    backup_passphrase: SecretStr | None = None

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
