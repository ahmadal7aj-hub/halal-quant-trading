import io
import json
import logging
from collections.abc import Iterator

import pytest

from halal_quant.core.logging import (
    MASK,
    SecretMasker,
    configure_logging,
    correlation_scope,
    get_correlation_id,
    secrets_from,
)
from halal_quant.core.settings import DbRole, Settings
from tests.conftest import FAKE_PASSWORDS

SECRETS = list(FAKE_PASSWORDS.values())


@pytest.fixture(autouse=True)
def restore_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)


@pytest.fixture
def stream(fake_settings: Settings) -> io.StringIO:
    """Root logger configured with the fake settings' secrets, writing to a buffer."""
    buffer = io.StringIO()
    configure_logging(fake_settings, level="DEBUG", stream=buffer)
    return buffer


def lines(buffer: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in buffer.getvalue().splitlines()]


def assert_no_secrets(buffer: io.StringIO) -> None:
    output = buffer.getvalue()
    for secret in SECRETS:
        assert secret not in output


# --- PRD Test 12: logs do not expose configured secrets ---------------------------------------


def test_12_secret_in_message_args_and_extra_is_masked(stream: io.StringIO) -> None:
    log = logging.getLogger("t12")
    app_pw = FAKE_PASSWORDS["db_app_password"]
    log.info("connecting with %s", app_pw)
    log.warning(f"inline {FAKE_PASSWORDS['db_admin_password']}")
    log.error("extra", extra={"conn": {"pw": app_pw, "list": [app_pw]}})
    assert_no_secrets(stream)
    assert MASK in lines(stream)[0]["message"]  # type: ignore[operator]


def test_12_secret_in_exception_and_its_cause_is_masked(stream: io.StringIO) -> None:
    log = logging.getLogger("t12")
    try:
        try:
            raise ValueError(f"bad password {FAKE_PASSWORDS['db_migrator_password']}")
        except ValueError as inner:
            raise RuntimeError(f"wrapped {FAKE_PASSWORDS['db_readonly_password']}") from inner
    except RuntimeError:
        log.exception("connection failed")
    assert_no_secrets(stream)
    exception = lines(stream)[0]["exception"]
    assert isinstance(exception, str)
    assert "RuntimeError" in exception and "ValueError" in exception


def test_12_secret_with_json_special_characters_is_masked(fake_settings: Settings) -> None:
    tricky = 'q"uote\back\nslash'
    buffer = io.StringIO()
    configure_logging(fake_settings, stream=buffer, extra_secrets=[tricky])
    logging.getLogger("t12").info("value", extra={"v": tricky, "r": repr(tricky)})
    output = buffer.getvalue()
    assert tricky not in output
    assert json.dumps(tricky)[1:-1] not in output
    assert repr(tricky)[1:-1] not in output


def test_12_every_settings_secret_is_collected(fake_settings: Settings) -> None:
    assert sorted(secrets_from(fake_settings)) == sorted(SECRETS)


def test_12_database_url_password_is_masked(stream: io.StringIO, fake_settings: Settings) -> None:
    url = fake_settings.db_url(DbRole.APP).render_as_string(hide_password=False)
    logging.getLogger("t12").info("url %s", url)
    assert_no_secrets(stream)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("postgresql://bob:hunter22@db/x", f"postgresql://bob:{MASK}@db/x"),
        ("password=hunter22 next", f"password={MASK} next"),
        ('{"api_key": "abc123"}', f'{{"api_key": "{MASK}"}}'),
        ("TOKEN: abc123", f"TOKEN: {MASK}"),
        ("no secrets here", "no secrets here"),
    ],
)
def test_unknown_credentials_are_masked_by_pattern(text: str, expected: str) -> None:
    assert SecretMasker().mask(text) == expected


def test_secret_containing_another_is_masked_whole() -> None:
    masker = SecretMasker(["abc", "abcdef"])
    assert masker.mask("x abcdef y") == f"x {MASK} y"


def test_empty_secret_is_ignored() -> None:
    assert SecretMasker([""]).mask("text") == "text"


# --- Structure, levels, correlation IDs --------------------------------------------------------


def test_log_line_is_structured_json(stream: io.StringIO) -> None:
    logging.getLogger("hq.test").info("hello %s", "world", extra={"rows": 3, "ok": True})
    [entry] = lines(stream)
    assert entry["message"] == "hello world"
    assert entry["level"] == "INFO"
    assert entry["logger"] == "hq.test"
    assert entry["rows"] == 3 and entry["ok"] is True
    assert str(entry["timestamp"]).endswith("+00:00")  # UTC


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
def test_all_prd_levels_are_logged(stream: io.StringIO, level: str) -> None:
    logging.getLogger("hq.test").log(getattr(logging, level), "x")
    assert lines(stream)[0]["level"] == level


def test_level_filters_lower_messages(fake_settings: Settings) -> None:
    buffer = io.StringIO()
    configure_logging(fake_settings, level="warning", stream=buffer)
    logging.getLogger("hq.test").info("hidden")
    logging.getLogger("hq.test").warning("shown")
    assert [e["message"] for e in lines(buffer)] == ["shown"]


def test_unknown_level_fails_closed() -> None:
    with pytest.raises(ValueError, match="Unknown log level"):
        configure_logging(level="LOUD")


def test_configure_twice_keeps_one_handler(fake_settings: Settings) -> None:
    configure_logging(fake_settings, stream=io.StringIO())
    configure_logging(fake_settings, stream=io.StringIO())
    names = [h.get_name() for h in logging.getLogger().handlers]
    assert names.count("halal_quant_json") == 1


def test_correlation_id_is_attached_and_restored(stream: io.StringIO) -> None:
    log = logging.getLogger("hq.test")
    assert get_correlation_id() is None
    with correlation_scope() as outer:
        log.info("a")
        with correlation_scope("fixed-id"):
            log.info("b")
        log.info("c")
    log.info("d")
    ids = [e["correlation_id"] for e in lines(stream)]
    assert ids == [outer, "fixed-id", outer, None]
    assert len(outer) == 32


def test_stack_info_is_included(stream: io.StringIO) -> None:
    logging.getLogger("hq.test").info("with stack", stack_info=True)
    assert "stack" in lines(stream)[0]
