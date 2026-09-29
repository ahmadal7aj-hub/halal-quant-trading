"""Structured JSON logging with correlation IDs and secret masking (PRD §31, §35, Test 12).

Every log line is one JSON object: UTC timestamp, level, logger, message, correlation ID, any
`extra={...}` fields, and the formatted exception if there is one. Before a line is written,
every configured secret (all `SecretStr` values in `Settings`) and common credential shapes
(`user:password@` in URLs, `password=...`) are replaced with `***`. Masking happens in the
formatter, so it also covers exception messages and tracebacks.

Usage:
    settings = get_settings()
    configure_logging(settings, level=settings.log_level)
    log = logging.getLogger(__name__)
    with correlation_scope():
        log.info("import started", extra={"table": "SEP"})
"""

import json
import logging
import re
import sys
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, TextIO

from pydantic import BaseModel, SecretStr

MASK = "***"
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

# Attributes every LogRecord has; anything else on a record came from `extra=`.
_STANDARD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}

# Credential shapes masked even when the value is not a configured secret.
_PATTERNS = (
    # scheme://user:password@host
    (re.compile(r"(?P<keep>[a-z][a-z0-9+.-]*://[^:/@\s]+:)[^@\s]+(?=@)", re.I), rf"\g<keep>{MASK}"),
    # password=..., api_key: ..., "token": "..."
    (
        re.compile(
            r"(?P<keep>\b(?:password|passwd|pwd|secret|token|api[_-]?key)\b[\"']?\s*[:=]\s*[\"']?)"
            r"[^\s\"',;&]+",
            re.I,
        ),
        rf"\g<keep>{MASK}",
    ),
)


def get_correlation_id() -> str | None:
    return _correlation_id.get()


@contextmanager
def correlation_scope(correlation_id: str | None = None) -> Iterator[str]:
    """Tag every log line inside the block with one ID (a new one unless given)."""
    cid = correlation_id or uuid.uuid4().hex
    token = _correlation_id.set(cid)
    try:
        yield cid
    finally:
        _correlation_id.reset(token)


class SecretMasker:
    """Replaces known secret values and credential patterns with `***`."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        variants: set[str] = set()
        for secret in secrets:
            if secret:
                variants.add(secret)
                variants.add(json.dumps(secret)[1:-1])  # as it appears inside a JSON string
                variants.add(repr(secret)[1:-1])  # as it appears inside a repr()
        # Longest first, so a secret that contains another is masked whole.
        self._secrets = sorted(variants, key=len, reverse=True)

    def mask(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, MASK)
        for pattern, replacement in _PATTERNS:
            text = pattern.sub(replacement, text)
        return text


def secrets_from(model: BaseModel) -> list[str]:
    """Every SecretStr value on a settings object, so new secrets are masked automatically."""
    return [
        value.get_secret_value()
        for value in (getattr(model, name) for name in type(model).model_fields)
        if isinstance(value, SecretStr)
    ]


class JsonFormatter(logging.Formatter):
    def __init__(self, masker: SecretMasker) -> None:
        super().__init__()
        self._masker = masker

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": get_correlation_id(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            entry["stack"] = self.formatStack(record.stack_info)
        entry = {key: self._clean(value) for key, value in entry.items()}
        # Final pass over the whole line in case a secret spans fields or was escaped.
        return self._masker.mask(json.dumps(entry, ensure_ascii=False))

    def _clean(self, value: Any) -> Any:
        if value is None or isinstance(value, bool | int | float):
            return value
        if isinstance(value, dict):
            return {self._masker.mask(str(k)): self._clean(v) for k, v in value.items()}
        if isinstance(value, list | tuple | set | frozenset):
            return [self._clean(v) for v in value]
        return self._masker.mask(str(value))


_HANDLER_NAME = "halal_quant_json"


def configure_logging(
    settings: BaseModel | None = None,
    level: str = "INFO",
    stream: TextIO | None = None,
    extra_secrets: Iterable[str] = (),
) -> SecretMasker:
    """Send all logs as masked JSON lines to `stream` (stderr by default). Safe to call again."""
    level = level.upper()
    if level not in LEVELS:
        raise ValueError(f"Unknown log level {level!r}; use one of {', '.join(LEVELS)}")
    secrets = [*(secrets_from(settings) if settings is not None else []), *extra_secrets]
    masker = SecretMasker(secrets)

    handler = logging.StreamHandler(stream or sys.stderr)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(JsonFormatter(masker))

    root = logging.getLogger()
    for old in [h for h in root.handlers if h.get_name() == _HANDLER_NAME]:
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level)
    return masker
