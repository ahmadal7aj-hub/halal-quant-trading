"""The dashboard login: hashing, verification and the lock-out (no real passwords involved)."""

import pytest

from halal_quant.dashboard.auth import (
    LOCKOUT_SECONDS,
    MAX_FAILURES,
    LoginThrottle,
    hash_password,
    verify_password,
)

PASSWORD = "correct horse battery"


def fast(password: str = PASSWORD) -> str:
    return hash_password(password, iterations=1_000)  # fast for tests; the real default is 600,000


def test_a_hash_is_salted_and_never_contains_the_password() -> None:
    a, b = fast(), fast()
    assert a != b  # a fresh salt each time
    assert PASSWORD not in a and a.startswith("pbkdf2_sha256:1000:")
    assert len(a.split(":")) == 4


def test_only_the_right_password_verifies() -> None:
    stored = fast()
    assert verify_password(PASSWORD, stored) is True
    assert verify_password(PASSWORD + "x", stored) is False
    assert verify_password("", stored) is False


@pytest.mark.parametrize(
    "stored",
    [None, "", "plaintext", "md5:1:aa:bb", "pbkdf2_sha256:abc:aa:bb", "pbkdf2_sha256:1:%%%:bb"],
)
def test_a_missing_or_malformed_hash_never_lets_anyone_in(stored: str | None) -> None:
    assert verify_password(PASSWORD, stored) is False


def test_short_passwords_are_refused() -> None:
    with pytest.raises(ValueError, match="at least"):
        hash_password("short")


def test_the_default_strength_is_high() -> None:
    from halal_quant.dashboard.auth import ITERATIONS

    assert ITERATIONS >= 600_000


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_repeated_failures_lock_the_login_then_it_opens_again() -> None:
    clock = Clock()
    throttle = LoginThrottle(clock=clock)
    for _ in range(MAX_FAILURES - 1):
        throttle.record(False)
        assert throttle.allowed()
    throttle.record(False)
    assert not throttle.allowed() and throttle.seconds_left() > 0
    clock.now += LOCKOUT_SECONDS - 1
    assert not throttle.allowed()
    clock.now += 2
    assert throttle.allowed() and throttle.seconds_left() == 0


def test_a_success_resets_the_count() -> None:
    throttle = LoginThrottle(clock=Clock())
    for _ in range(MAX_FAILURES - 1):
        throttle.record(False)
    throttle.record(True)
    for _ in range(MAX_FAILURES - 1):
        throttle.record(False)
    assert throttle.allowed()
