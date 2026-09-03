import json
import logging

from trading_agent.logging_setup import JsonFormatter, SecretRedactionFilter


def _make_record(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="trading_agent.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def test_known_secret_value_is_redacted():
    record = _make_record("connecting with api_key=abc123supersecretvalue")
    filt = SecretRedactionFilter(known_secrets=["abc123supersecretvalue"])
    filt.filter(record)
    assert "abc123supersecretvalue" not in record.msg
    assert "redacted" in record.msg


def test_key_shaped_pattern_is_redacted_even_if_unknown():
    record = _make_record("api_secret=Zx9LongRandomLookingSecretValue1234")
    filt = SecretRedactionFilter(known_secrets=[])
    filt.filter(record)
    assert "Zx9LongRandomLookingSecretValue1234" not in record.msg


def test_json_formatter_produces_valid_json():
    record = _make_record("hello world")
    formatted = JsonFormatter().format(record)
    payload = json.loads(formatted)
    assert payload["message"] == "hello world"
    assert payload["level"] == "INFO"
