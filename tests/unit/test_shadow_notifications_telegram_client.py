"""Proofs for `shadow/notifications/telegram_client.py`'s outcome
classification, bounded safe retry, and secret redaction. Every HTTP call
is mocked via `responses` - this module never touches a real network, and
a test that expects zero calls simply registers none (an unexpected
attempt then fails loudly with a `ConnectionError` from `responses`
itself, rather than silently reaching Telegram).
"""

from __future__ import annotations

import requests
import responses

from trading_agent.shadow.notifications.telegram_client import (
    SEND_OUTCOME_AMBIGUOUS,
    SEND_OUTCOME_FAILED,
    SEND_OUTCOME_SENT,
    TELEGRAM_API_HOST,
    TelegramClient,
)

BOT_TOKEN = "123456789:AAFakeTestTokenNeverRealABCDEFGHIJK"
CHAT_ID = "42"
URL = f"{TELEGRAM_API_HOST}/bot{BOT_TOKEN}/sendMessage"


def _no_sleep(_seconds: float) -> None:
    """Injected in place of `time.sleep` so retry-backoff tests run instantly."""


@responses.activate
def test_confirmed_200_ok_true_is_sent():
    responses.add(responses.POST, URL, json={"ok": True, "result": {"message_id": 1}}, status=200)
    client = TelegramClient(BOT_TOKEN)
    result = client.send_message(CHAT_ID, "hello")
    assert result.outcome == SEND_OUTCOME_SENT
    assert result.attempts == 1
    assert result.safe_error is None


@responses.activate
def test_http_500_is_failed_not_retried_by_this_call():
    responses.add(responses.POST, URL, json={"description": "boom"}, status=500)
    client = TelegramClient(BOT_TOKEN)
    result = client.send_message(CHAT_ID, "hello")
    assert result.outcome == SEND_OUTCOME_FAILED
    assert result.attempts == 1
    assert result.safe_error is not None
    assert "500" in result.safe_error


@responses.activate
def test_200_with_ok_false_is_failed():
    responses.add(responses.POST, URL, json={"ok": False, "description": "chat not found"}, status=200)
    client = TelegramClient(BOT_TOKEN)
    result = client.send_message(CHAT_ID, "hello")
    assert result.outcome == SEND_OUTCOME_FAILED
    assert "chat not found" in (result.safe_error or "")


@responses.activate
def test_malformed_200_body_is_failed():
    responses.add(responses.POST, URL, body="not json", status=200, content_type="text/plain")
    client = TelegramClient(BOT_TOKEN)
    result = client.send_message(CHAT_ID, "hello")
    assert result.outcome == SEND_OUTCOME_FAILED
    assert result.safe_error == "malformed 200 response body"


@responses.activate
def test_read_timeout_is_ambiguous_and_never_retried():
    responses.add(responses.POST, URL, body=requests.exceptions.ReadTimeout("timed out"))
    client = TelegramClient(BOT_TOKEN)
    result = client.send_message(CHAT_ID, "hello", max_safe_retries=5, sleep_fn=_no_sleep)
    assert result.outcome == SEND_OUTCOME_AMBIGUOUS
    assert result.attempts == 1  # never auto-retried - see module docstring


@responses.activate
def test_connection_error_retries_then_fails_when_exhausted():
    responses.add(responses.POST, URL, body=requests.exceptions.ConnectionError("refused"))
    responses.add(responses.POST, URL, body=requests.exceptions.ConnectionError("refused"))
    responses.add(responses.POST, URL, body=requests.exceptions.ConnectionError("refused"))
    client = TelegramClient(BOT_TOKEN)
    result = client.send_message(CHAT_ID, "hello", max_safe_retries=3, sleep_fn=_no_sleep)
    assert result.outcome == SEND_OUTCOME_FAILED
    assert result.attempts == 3


@responses.activate
def test_connection_error_recovers_within_retry_budget():
    responses.add(responses.POST, URL, body=requests.exceptions.ConnectionError("refused"))
    responses.add(responses.POST, URL, json={"ok": True}, status=200)
    client = TelegramClient(BOT_TOKEN)
    result = client.send_message(CHAT_ID, "hello", max_safe_retries=3, sleep_fn=_no_sleep)
    assert result.outcome == SEND_OUTCOME_SENT
    assert result.attempts == 2


@responses.activate
def test_connect_timeout_is_treated_as_a_safe_pre_transmission_failure():
    # requests.exceptions.ConnectTimeout is a ConnectionError subclass (but
    # NOT a ReadTimeout subclass) - the request never reached the wire.
    responses.add(responses.POST, URL, body=requests.exceptions.ConnectTimeout("connect timed out"))
    responses.add(responses.POST, URL, json={"ok": True}, status=200)
    client = TelegramClient(BOT_TOKEN)
    result = client.send_message(CHAT_ID, "hello", max_safe_retries=3, sleep_fn=_no_sleep)
    assert result.outcome == SEND_OUTCOME_SENT
    assert result.attempts == 2


@responses.activate
def test_unclassified_request_exception_is_conservatively_ambiguous():
    responses.add(responses.POST, URL, body=requests.exceptions.ChunkedEncodingError("truncated"))
    client = TelegramClient(BOT_TOKEN)
    result = client.send_message(CHAT_ID, "hello", max_safe_retries=3, sleep_fn=_no_sleep)
    assert result.outcome == SEND_OUTCOME_AMBIGUOUS
    assert result.attempts == 1


@responses.activate
def test_bot_token_is_redacted_from_every_possible_error_string():
    responses.add(responses.POST, URL, body=requests.exceptions.ConnectionError(f"failed to connect to {URL}"))
    client = TelegramClient(BOT_TOKEN, timeout_seconds=(1.0, 1.0))
    result = client.send_message(CHAT_ID, "hello", max_safe_retries=1, sleep_fn=_no_sleep)
    assert result.outcome == SEND_OUTCOME_FAILED
    assert result.safe_error is not None
    assert BOT_TOKEN not in result.safe_error
    assert "***REDACTED***" in result.safe_error


@responses.activate
def test_bot_token_is_redacted_from_a_read_timeout_error_string():
    responses.add(responses.POST, URL, body=requests.exceptions.ReadTimeout(f"read timed out for {URL}"))
    client = TelegramClient(BOT_TOKEN)
    result = client.send_message(CHAT_ID, "hello")
    assert result.safe_error is not None
    assert BOT_TOKEN not in result.safe_error


@responses.activate
def test_bot_token_is_redacted_from_a_rejection_response_body():
    responses.add(responses.POST, URL, json={"ok": False, "description": f"bad token {BOT_TOKEN}"}, status=200)
    client = TelegramClient(BOT_TOKEN)
    result = client.send_message(CHAT_ID, "hello")
    assert result.safe_error is not None
    assert BOT_TOKEN not in result.safe_error


@responses.activate
def test_bot_token_is_redacted_from_a_non_200_response_body():
    responses.add(responses.POST, URL, body=f"unauthorized for {BOT_TOKEN}", status=401)
    client = TelegramClient(BOT_TOKEN)
    result = client.send_message(CHAT_ID, "hello")
    assert result.safe_error is not None
    assert BOT_TOKEN not in result.safe_error


@responses.activate
def test_request_body_is_json_with_chat_id_and_text():
    responses.add(responses.POST, URL, json={"ok": True}, status=200)
    client = TelegramClient(BOT_TOKEN)
    client.send_message(CHAT_ID, "the message text")
    assert len(responses.calls) == 1
    sent_body = responses.calls[0].request.body
    assert b'"chat_id": "42"' in sent_body or b'"chat_id":"42"' in sent_body
    assert b"the message text" in sent_body
