"""Dashboard login (Phase 6; PRD §34): a salted password hash, a lock-out and no secrets in code.

The owner makes the hash once with `uv run python -m halal_quant.dashboard.auth` (it asks for the
password without showing it) and pastes the printed line into `.env`. Only the hash is stored, so a
leaked `.env` does not reveal the password. With no hash configured the dashboard refuses everyone.
"""

import base64
import getpass
import hashlib
import hmac
import secrets
import sys
import time
from dataclasses import dataclass, field

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 600_000
MIN_LENGTH = 12
MAX_FAILURES = 5
LOCKOUT_SECONDS = 300


def hash_password(password: str, iterations: int = ITERATIONS) -> str:
    """A salted PBKDF2-SHA256 hash in the form `pbkdf2_sha256:iterations:salt:hash`."""
    if len(password) < MIN_LENGTH:
        raise ValueError(f"Use at least {MIN_LENGTH} characters.")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return ":".join(
        [
            ALGORITHM,
            str(iterations),
            base64.b64encode(salt).decode(),
            base64.b64encode(digest).decode(),
        ]
    )


def verify_password(password: str, stored: str | None) -> bool:
    """True only for the right password; a missing or malformed hash always means False."""
    if not stored:
        return False
    try:
        algorithm, iterations, salt_b64, digest_b64 = stored.split(":")
        if algorithm != ALGORITHM:
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


@dataclass
class LoginThrottle:
    """Lock the login after repeated failures (kept per browser session)."""

    failures: int = 0
    locked_until: float = 0.0
    clock: object = field(default=time.monotonic, repr=False)

    def _now(self) -> float:
        return float(self.clock())  # type: ignore[operator]

    def allowed(self) -> bool:
        return self._now() >= self.locked_until

    def seconds_left(self) -> int:
        return max(0, int(self.locked_until - self._now()) + 1) if not self.allowed() else 0

    def record(self, success: bool) -> None:
        if success:
            self.failures, self.locked_until = 0, 0.0
            return
        self.failures += 1
        if self.failures >= MAX_FAILURES:
            self.locked_until = self._now() + LOCKOUT_SECONDS
            self.failures = 0


def main() -> int:
    print("Choose a dashboard password (at least 12 characters). It is not shown or stored.")
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Repeat it: "):
        print("The two entries differ: nothing was made.")
        return 1
    try:
        line = hash_password(first)
    except ValueError as exc:
        print(exc)
        return 1
    print("\nAdd this line to your private .env file (it is a hash, not the password):\n")
    print(f"HQ_DASHBOARD_PASSWORD_HASH={line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
