import pytest
from pydantic import ValidationError

from halal_quant.core.settings import DbRole, Settings
from halal_quant.db.engine import make_engine
from tests.conftest import FAKE_PASSWORDS


def test_role_urls_use_the_right_user_and_password(fake_settings: Settings) -> None:
    for role in DbRole:
        url = fake_settings.db_url(role)
        assert url.username == role.value
        assert url.password == fake_settings.role_password(role).get_secret_value()
        assert url.host == "127.0.0.1"
        assert url.database == "halal_quant"
    assert fake_settings.admin_url().username == "hq_admin"


def test_passwords_never_appear_in_repr_or_str(fake_settings: Settings) -> None:
    rendered = [repr(fake_settings), str(fake_settings)]
    rendered += [str(fake_settings.db_url(role)) for role in DbRole]
    rendered += [repr(fake_settings.db_url(role)) for role in DbRole]
    rendered.append(str(fake_settings.admin_url()))
    for text in rendered:
        for password in FAKE_PASSWORDS.values():
            assert password not in text


def test_missing_password_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ADMIN", "MIGRATOR", "APP", "READONLY"):
        monkeypatch.delenv(f"HQ_DB_{name}_PASSWORD", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_settings_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in FAKE_PASSWORDS.items():
        monkeypatch.setenv(f"HQ_{key.upper()}", value)
    monkeypatch.setenv("HQ_DB_PORT", "6543")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.db_port == 6543
    assert settings.db_app_password.get_secret_value() == FAKE_PASSWORDS["db_app_password"]


def test_engine_defaults_to_app_role(fake_settings: Settings) -> None:
    engine = make_engine(fake_settings)  # no connection is made until first use
    assert engine.url.username == DbRole.APP.value
    engine.dispose()
