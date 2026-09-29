import pytest
from pydantic import SecretStr

from halal_quant.core.settings import Settings

FAKE_PASSWORDS = {
    "db_admin_password": "admin-pw-for-tests",
    "db_migrator_password": "migrator-pw-for-tests",
    "db_app_password": "app-pw-for-tests",
    "db_readonly_password": "readonly-pw-for-tests",
}


@pytest.fixture
def fake_settings() -> Settings:
    """Settings with made-up passwords; ignores the real .env and environment."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        **{key: SecretStr(value) for key, value in FAKE_PASSWORDS.items()},
    )
