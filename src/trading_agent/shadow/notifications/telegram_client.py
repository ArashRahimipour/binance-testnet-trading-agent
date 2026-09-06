"""A minimal, restricted Telegram Bot API client for shadow-mode
notifications only.

`TELEGRAM_API_HOST` is the ONLY host this client ever contacts - there is
no signing, no order-placement method, no other endpoint of any kind.
Telegram's Bot API embeds the bot token in the request URL path itself
(there is no header-based alternative) - every code path below therefore
redacts the token out of any string derived from an exception or an HTTP
response BEFORE that string is ever returned, logged, or persisted (see
`_redact`), since `requests`' own exception messages and a connection
error can otherwise include the full request URL verbatim.

OUTCOME CLASSIFICATION (see `send_message`): a definitive HTTP response -
200 with `{"ok": true}`, ANY other status code, or `{"ok": false}` - is
never ambiguous; only a genuine uncertainty about whether the request
reached Telegram at all is classified as `AMBIGUOUS` or retried as a safe
pre-transmission failure. This project makes no claim of mathematically
guaranteed exactly-once delivery - Telegram's Bot API has no client-
supplied idempotency key, so an `AMBIGUOUS` outcome (a read timeout after
the request was already sent) is reported honestly as "may have been
delivered", never silently assumed either way.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import requests

TELEGRAM_API_HOST = "https://api.telegram.org"

DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_READ_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_SAFE_RETRIES = 3
DEFAULT_BACKOFF_SECONDS = 1.0

SEND_OUTCOME_SENT = "SENT"
SEND_OUTCOME_AMBIGUOUS = "AMBIGUOUS"
SEND_OUTCOME_FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class TelegramSendResult:
    outcome: str
    attempts: int
    #: Already secret-redacted - safe to persist verbatim as
    #: `shadow_notification_outbox.last_error`. `None` only when
    #: `outcome == SEND_OUTCOME_SENT`.
    safe_error: str | None


def _redact(text: str, secret: str) -> str:
    """Replace every occurrence of `secret` (the bot token) in `text` -
    called on every string this module could ever surface, since the
    token lives in the request URL and can appear in `requests`' own
    exception messages."""
    if not secret:
        return text
    return text.replace(secret, "***REDACTED***")


class TelegramClient:
    def __init__(
        self,
        bot_token: str,
        timeout_seconds: tuple[float, float] = (DEFAULT_CONNECT_TIMEOUT_SECONDS, DEFAULT_READ_TIMEOUT_SECONDS),
    ) -> None:
        self._bot_token = bot_token
        self._timeout = timeout_seconds
        self._session = requests.Session()

    def send_message(
        self,
        chat_id: str,
        text: str,
        max_safe_retries: int = DEFAULT_MAX_SAFE_RETRIES,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> TelegramSendResult:
        """Send one message via `POST /bot<token>/sendMessage`.

        - A confirmed HTTP 200 with `{"ok": true}` -> `SENT`.
        - Any OTHER definitive response (non-200, or 200 with
          `{"ok": false}`, or an unparseable 200 body) -> `FAILED`
          (Telegram is known NOT to have delivered it) - never retried
          automatically here.
        - A read timeout (the request was already transmitted; the
          response never arrived) -> `AMBIGUOUS` immediately, never
          auto-retried - the caller (`sender.py`) never re-sends this
          automatically, exactly per the no-automatic-retry-on-ambiguity
          mandate.
        - A connection-level failure BEFORE the request could have reached
          the wire (DNS failure, connection refused, connect timeout) -> a
          SAFE pre-transmission failure: retried up to `max_safe_retries`
          times with linear backoff (`sleep_fn` injectable for tests),
          `FAILED` only once every retry is exhausted.
        - Any other, unclassified `requests` exception is treated
          conservatively as `AMBIGUOUS` rather than assumed safe to retry.
        """
        url = f"{TELEGRAM_API_HOST}/bot{self._bot_token}/sendMessage"
        attempts = 0
        last_safe_error: str | None = None

        for attempt in range(max_safe_retries):
            attempts = attempt + 1
            try:
                response = self._session.post(
                    url, json={"chat_id": chat_id, "text": text}, timeout=self._timeout
                )
            except requests.exceptions.ReadTimeout as exc:
                return TelegramSendResult(SEND_OUTCOME_AMBIGUOUS, attempts, _redact(str(exc), self._bot_token))
            except requests.exceptions.ConnectionError as exc:
                # Includes ConnectTimeout (a ConnectionError subclass in
                # `requests`) - the request never reached the wire.
                last_safe_error = _redact(str(exc), self._bot_token)
                if attempts < max_safe_retries:
                    sleep_fn(backoff_seconds * attempts)
                    continue
                return TelegramSendResult(SEND_OUTCOME_FAILED, attempts, last_safe_error)
            except requests.exceptions.RequestException as exc:
                return TelegramSendResult(SEND_OUTCOME_AMBIGUOUS, attempts, _redact(str(exc), self._bot_token))

            return self._classify_response(response, attempts)

        return TelegramSendResult(SEND_OUTCOME_FAILED, attempts, last_safe_error)

    def _classify_response(self, response: requests.Response, attempts: int) -> TelegramSendResult:
        if response.status_code == 200:
            try:
                body = response.json()
            except ValueError:
                return TelegramSendResult(SEND_OUTCOME_FAILED, attempts, "malformed 200 response body")
            if body.get("ok") is True:
                return TelegramSendResult(SEND_OUTCOME_SENT, attempts, None)
            return TelegramSendResult(
                SEND_OUTCOME_FAILED, attempts,
                _redact(f"Telegram rejected the message: {body}", self._bot_token),
            )
        return TelegramSendResult(
            SEND_OUTCOME_FAILED, attempts,
            _redact(f"HTTP {response.status_code}: {response.text[:200]}", self._bot_token),
        )
