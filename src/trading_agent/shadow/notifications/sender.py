"""Delivery orchestration for the notification outbox - the ONLY place
that ever attempts to actually reach Telegram. Enqueuing (`shadow/store.py::
ShadowStore.record_cycle_atomically`/`enqueue_notifications_atomically`) is
always a separate, already-committed step that happens BEFORE anything
here runs - see that module's docstring for why this makes shadow trading
completely independent of Telegram's availability.

`flush_pending_notifications` is called once per `shadow-run` cycle, AFTER
the cycle's own trading-state transaction has already committed, and is
wrapped in its own absolute safety net: no exception it could ever raise
propagates out of it - see its own docstring. `retry_notification` is the
one manual, `--confirm`-gated, `notification-retry` CLI code path that may
legitimately raise (a maintenance action, not part of the automatic cycle).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from trading_agent.config.loader import load_telegram_secrets
from trading_agent.config.models import AppConfig
from trading_agent.risk.kill_switch import KillSwitch
from trading_agent.shadow.notifications.telegram_client import (
    SEND_OUTCOME_AMBIGUOUS,
    SEND_OUTCOME_FAILED,
    SEND_OUTCOME_SENT,
    TelegramClient,
)
from trading_agent.shadow.store import (
    NOTIFICATION_STATUS_AMBIGUOUS,
    NOTIFICATION_STATUS_FAILED,
    NOTIFICATION_STATUS_PENDING,
    NOTIFICATION_STATUS_SENT,
    ShadowStore,
)

NOTIFICATIONS_DISABLED_FILENAME = "SHADOW_NOTIFICATIONS_DISABLED"

_OUTCOME_TO_STATUS = {
    SEND_OUTCOME_SENT: NOTIFICATION_STATUS_SENT,
    SEND_OUTCOME_AMBIGUOUS: NOTIFICATION_STATUS_AMBIGUOUS,
    SEND_OUTCOME_FAILED: NOTIFICATION_STATUS_FAILED,
}


def notifications_kill_switch_path(config: AppConfig) -> Path:
    return config.paths.data_dir / NOTIFICATIONS_DISABLED_FILENAME


def is_telegram_active(config: AppConfig) -> tuple[bool, str | None]:
    """`(True, None)` only if `config.telegram.enabled` AND the separate
    `notification-disable`/`notification-enable` kill switch is not
    engaged. Does NOT check whether secrets are actually present - that is
    checked (and handled just as safely) at flush time."""
    if not config.telegram.enabled:
        return False, "telegram.enabled is False in config"
    if KillSwitch(notifications_kill_switch_path(config)).is_engaged():
        return False, "notifications are disabled (see `notification-enable` to re-enable)"
    return True, None


@dataclass(frozen=True, slots=True)
class FlushResult:
    attempted: int
    sent: int
    ambiguous: int
    failed: int
    #: Non-None only when NOTHING was attempted - disabled, unconfigured,
    #: or an unexpected error while flushing (see module docstring).
    skipped_reason: str | None


def flush_pending_notifications(config: AppConfig, shadow_store: ShadowStore) -> FlushResult:
    """Attempt to deliver every PENDING notification. NEVER raises - any
    exception here (including one from Telegram, DNS, or a bug in this
    module itself) is caught and reported back as a `skipped_reason`
    rather than ever propagating into a `shadow-run` cycle. This is the
    one function `shadow/engine.py` calls that must be allowed to fail
    silently by design - see its own module docstring.
    """
    try:
        active, reason = is_telegram_active(config)
        if not active:
            return FlushResult(0, 0, 0, 0, skipped_reason=reason)

        secrets = load_telegram_secrets()
        if secrets is None:
            return FlushResult(
                0, 0, 0, 0,
                skipped_reason="Telegram is not configured (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID missing)",
            )

        client = TelegramClient(secrets.bot_token)
        pending = shadow_store.list_notifications(status=NOTIFICATION_STATUS_PENDING)
        sent = ambiguous = failed = 0
        for event in pending:
            now_ms = int(time.time() * 1000)
            result = client.send_message(secrets.chat_id, event.payload_text)
            new_status = _OUTCOME_TO_STATUS[result.outcome]
            shadow_store.update_notification_status(
                event.event_id, new_status, event.attempt_count + result.attempts,
                now_ms, result.safe_error, now_ms if new_status == NOTIFICATION_STATUS_SENT else None,
            )
            if new_status == NOTIFICATION_STATUS_SENT:
                sent += 1
            elif new_status == NOTIFICATION_STATUS_AMBIGUOUS:
                ambiguous += 1
            else:
                failed += 1
        return FlushResult(len(pending), sent, ambiguous, failed, skipped_reason=None)
    except Exception as exc:  # noqa: BLE001 - absolute safety net, see docstring
        # Deliberately never includes `str(exc)` - an unexpected exception
        # is exactly the case least likely to have already been through
        # this module's own redaction, so only the exception TYPE is safe
        # to surface unconditionally.
        return FlushResult(0, 0, 0, 0, skipped_reason=f"unexpected error ({type(exc).__name__}) - see local logs")


@dataclass(frozen=True, slots=True)
class RetryResult:
    ok: bool
    detail: str
    new_status: str | None


def retry_notification(config: AppConfig, shadow_store: ShadowStore, event_id: str) -> RetryResult:
    """The manual `notification-retry` code path - unlike `flush_pending_
    notifications`, this is allowed to raise (it is a deliberate,
    `--confirm`-gated maintenance action, not part of the automatic
    shadow-run cycle) so the CLI can report a real error clearly.
    """
    event = shadow_store.get_notification(event_id)
    if event is None:
        return RetryResult(False, f"no notification found with event_id {event_id!r}", None)
    if event.status == NOTIFICATION_STATUS_SENT:
        return RetryResult(False, "this event is already SENT - refusing to resend automatically", None)

    secrets = load_telegram_secrets()
    if secrets is None:
        return RetryResult(False, "Telegram is not configured (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID missing)", None)

    client = TelegramClient(secrets.bot_token)
    now_ms = int(time.time() * 1000)
    result = client.send_message(secrets.chat_id, event.payload_text)
    new_status = _OUTCOME_TO_STATUS[result.outcome]
    shadow_store.update_notification_status(
        event_id, new_status, event.attempt_count + result.attempts,
        now_ms, result.safe_error, now_ms if new_status == NOTIFICATION_STATUS_SENT else None,
    )
    return RetryResult(True, f"retry outcome: {new_status}", new_status)
