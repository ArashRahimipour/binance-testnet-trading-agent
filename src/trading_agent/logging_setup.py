"""Structured JSON logging with mandatory secret redaction.

Every log record is filtered through `SecretRedactionFilter` before it can
reach any handler. This is a defense-in-depth measure: application code
should never log secrets in the first place, but a bug that tries to must
not be able to leak a real key or secret into logs on disk.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

_REDACTED = "***redacted***"

# Matches common patterns for API keys/secrets appearing in a log message,
# in addition to explicit known-secret substitution performed by the filter.
_SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|api[_-]?secret|secret|signature)\s*[=:]\s*[\'\"]?[A-Za-z0-9\-_]{8,}[\'\"]?"
)


class SecretRedactionFilter(logging.Filter):
    """Redacts known secret values and key/secret-shaped substrings."""

    def __init__(self, known_secrets: list[str] | None = None) -> None:
        super().__init__()
        self._known_secrets = [s for s in (known_secrets or []) if s]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        record.msg = self._redact(message)
        record.args = ()
        return True

    def _redact(self, message: str) -> str:
        for secret in self._known_secrets:
            if secret:
                message = message.replace(secret, _REDACTED)
        return _SECRET_PATTERN.sub(lambda m: m.group(1) + "=" + _REDACTED, message)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp_utc": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        return json.dumps(payload, default=str)


def configure_logging(
    logs_dir: str | Path = "logs",
    level: int = logging.INFO,
    known_secrets: list[str] | None = None,
) -> logging.Logger:
    logger = logging.getLogger("trading_agent")
    logger.setLevel(level)
    logger.handlers.clear()

    redaction_filter = SecretRedactionFilter(known_secrets)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(JsonFormatter())
    stream_handler.addFilter(redaction_filter)
    logger.addHandler(stream_handler)

    logs_path = Path(logs_dir)
    logs_path.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(logs_path / "trading_agent.log")
    file_handler.setFormatter(JsonFormatter())
    file_handler.addFilter(redaction_filter)
    logger.addHandler(file_handler)

    logger.propagate = False
    return logger
