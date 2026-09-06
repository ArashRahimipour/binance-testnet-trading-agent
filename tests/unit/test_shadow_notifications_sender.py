"""Proofs for `shadow/notifications/sender.py`: the notifications kill
switch, `is_telegram_active` gating, `flush_pending_notifications`'s
absolute never-raises safety net and zero-network-calls-when-inactive
guarantee, and the manual `retry_notification` code path (including its
AMBIGUOUS-may-duplicate warning behavior). Every Telegram call is mocked
via `responses` - a test asserting zero calls registers none, so an
unexpected attempt fails loudly rather than silently reaching the network.
"""

from __future__ import annotations

import responses

from trading_agent.config.models import AppConfig
from trading_agent.risk.kill_switch import KillSwitch
from trading_agent.shadow.notifications.sender import (
    flush_pending_notifications,
    is_telegram_active,
    notifications_kill_switch_path,
    retry_notification,
)
from trading_agent.shadow.store import (
    NOTIFICATION_STATUS_AMBIGUOUS,
    NOTIFICATION_STATUS_FAILED,
    NOTIFICATION_STATUS_PENDING,
    NOTIFICATION_STATUS_SENT,
    NotificationEvent,
    ShadowStore,
)

BOT_TOKEN = "999999:AAFakeSenderTestTokenNeverReal"
CHAT_ID = "12345"
SEND_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"


def _config(tmp_path, telegram_enabled: bool) -> AppConfig:
    return AppConfig(
        mode="shadow",
        telegram={"enabled": telegram_enabled},
        paths={
            "data_dir": str(tmp_path),
            "logs_dir": str(tmp_path),
            "db_path": str(tmp_path / "shadow_agent.db"),
        },
    )


def _event(event_id: str = "entry:1", payload: str = "hello") -> NotificationEvent:
    return NotificationEvent(event_id=event_id, event_type="entry", created_at_ms=1, payload_text=payload)


# --- is_telegram_active ------------------------------------------------------


def test_is_telegram_active_false_when_disabled_in_config(tmp_path):
    config = _config(tmp_path, telegram_enabled=False)
    active, reason = is_telegram_active(config)
    assert active is False
    assert "enabled" in (reason or "")


def test_is_telegram_active_false_when_kill_switch_engaged(tmp_path):
    config = _config(tmp_path, telegram_enabled=True)
    KillSwitch(notifications_kill_switch_path(config)).engage("paused for testing")
    active, reason = is_telegram_active(config)
    assert active is False
    assert "disabled" in (reason or "").lower()


def test_is_telegram_active_true_when_enabled_and_not_killed(tmp_path):
    config = _config(tmp_path, telegram_enabled=True)
    active, reason = is_telegram_active(config)
    assert active is True
    assert reason is None


# --- flush_pending_notifications: disabled/unconfigured => zero calls ------


@responses.activate
def test_flush_makes_zero_calls_when_telegram_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    config = _config(tmp_path, telegram_enabled=False)
    with ShadowStore(config.paths.db_path) as store:
        store.enqueue_notifications_atomically([_event()])
        result = flush_pending_notifications(config, store)
    assert result.attempted == 0
    assert result.skipped_reason is not None
    assert len(responses.calls) == 0
    with ShadowStore(config.paths.db_path) as store:
        assert store.get_notification("entry:1").status == NOTIFICATION_STATUS_PENDING  # type: ignore[union-attr]


@responses.activate
def test_flush_makes_zero_calls_when_kill_switch_engaged(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    config = _config(tmp_path, telegram_enabled=True)
    KillSwitch(notifications_kill_switch_path(config)).engage("paused")
    with ShadowStore(config.paths.db_path) as store:
        store.enqueue_notifications_atomically([_event()])
        result = flush_pending_notifications(config, store)
    assert result.attempted == 0
    assert len(responses.calls) == 0


@responses.activate
def test_flush_makes_zero_calls_when_secrets_missing_even_if_enabled(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    config = _config(tmp_path, telegram_enabled=True)
    with ShadowStore(config.paths.db_path) as store:
        store.enqueue_notifications_atomically([_event()])
        result = flush_pending_notifications(config, store)
    assert result.attempted == 0
    assert "not configured" in (result.skipped_reason or "")
    assert len(responses.calls) == 0


# --- flush_pending_notifications: active path -------------------------------


@responses.activate
def test_flush_sends_pending_and_marks_sent(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    responses.add(responses.POST, SEND_URL, json={"ok": True}, status=200)
    config = _config(tmp_path, telegram_enabled=True)
    with ShadowStore(config.paths.db_path) as store:
        store.enqueue_notifications_atomically([_event()])
        result = flush_pending_notifications(config, store)
        assert result.attempted == 1
        assert result.sent == 1
        assert result.ambiguous == 0
        assert result.failed == 0
        event = store.get_notification("entry:1")
    assert event is not None
    assert event.status == NOTIFICATION_STATUS_SENT
    assert event.sent_at_ms is not None


@responses.activate
def test_flush_marks_ambiguous_on_read_timeout_and_never_auto_retries(tmp_path, monkeypatch):
    import requests

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    responses.add(responses.POST, SEND_URL, body=requests.exceptions.ReadTimeout("timed out"))
    config = _config(tmp_path, telegram_enabled=True)
    with ShadowStore(config.paths.db_path) as store:
        store.enqueue_notifications_atomically([_event()])
        result = flush_pending_notifications(config, store)
        assert result.ambiguous == 1
        event = store.get_notification("entry:1")
    assert event is not None
    assert event.status == NOTIFICATION_STATUS_AMBIGUOUS

    # A SECOND flush call must make ZERO further HTTP attempts - only
    # PENDING notifications are ever picked up automatically.
    calls_before = len(responses.calls)
    with ShadowStore(config.paths.db_path) as store:
        second_result = flush_pending_notifications(config, store)
    assert second_result.attempted == 0
    assert len(responses.calls) == calls_before


@responses.activate
def test_flush_marks_failed_on_definitive_rejection(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    responses.add(responses.POST, SEND_URL, json={"ok": False, "description": "blocked"}, status=200)
    config = _config(tmp_path, telegram_enabled=True)
    with ShadowStore(config.paths.db_path) as store:
        store.enqueue_notifications_atomically([_event()])
        result = flush_pending_notifications(config, store)
        assert result.failed == 1
        event = store.get_notification("entry:1")
    assert event is not None
    assert event.status == NOTIFICATION_STATUS_FAILED
    assert event.last_error is not None
    assert BOT_TOKEN not in event.last_error


@responses.activate
def test_flush_processes_multiple_pending_events_independently(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    responses.add(responses.POST, SEND_URL, json={"ok": True}, status=200)
    responses.add(responses.POST, SEND_URL, json={"ok": False, "description": "nope"}, status=200)
    config = _config(tmp_path, telegram_enabled=True)
    with ShadowStore(config.paths.db_path) as store:
        store.enqueue_notifications_atomically([_event("entry:1"), _event("entry:2")])
        result = flush_pending_notifications(config, store)
    assert result.attempted == 2
    assert result.sent == 1
    assert result.failed == 1


# --- retry_notification ------------------------------------------------------


def test_retry_unknown_event_id_fails_cleanly(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    config = _config(tmp_path, telegram_enabled=True)
    with ShadowStore(config.paths.db_path) as store:
        result = retry_notification(config, store, "does-not-exist")
    assert result.ok is False
    assert "no notification found" in result.detail


def test_retry_refuses_to_resend_an_already_sent_event(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    config = _config(tmp_path, telegram_enabled=True)
    with ShadowStore(config.paths.db_path) as store:
        store.enqueue_notifications_atomically([_event()])
        store.update_notification_status("entry:1", NOTIFICATION_STATUS_SENT, 1, 1, None, 1)
        result = retry_notification(config, store, "entry:1")
    assert result.ok is False
    assert "already SENT" in result.detail


@responses.activate
def test_retry_ambiguous_event_can_succeed_and_updates_status(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    responses.add(responses.POST, SEND_URL, json={"ok": True}, status=200)
    config = _config(tmp_path, telegram_enabled=True)
    with ShadowStore(config.paths.db_path) as store:
        store.enqueue_notifications_atomically([_event()])
        store.update_notification_status("entry:1", NOTIFICATION_STATUS_AMBIGUOUS, 1, 1, "read timeout", None)
        result = retry_notification(config, store, "entry:1")
        assert result.ok is True
        assert result.new_status == NOTIFICATION_STATUS_SENT
        event = store.get_notification("entry:1")
    assert event is not None
    assert event.status == NOTIFICATION_STATUS_SENT


def test_retry_fails_cleanly_when_secrets_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    config = _config(tmp_path, telegram_enabled=True)
    with ShadowStore(config.paths.db_path) as store:
        store.enqueue_notifications_atomically([_event()])
        result = retry_notification(config, store, "entry:1")
    assert result.ok is False
    assert "not configured" in result.detail


# --- flush_pending_notifications never raises -------------------------------


def test_flush_never_raises_even_on_an_unexpected_internal_error(tmp_path, monkeypatch):
    import trading_agent.shadow.notifications.sender as sender_module

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    config = _config(tmp_path, telegram_enabled=True)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated bug")

    monkeypatch.setattr(sender_module, "TelegramClient", _boom)
    with ShadowStore(config.paths.db_path) as store:
        store.enqueue_notifications_atomically([_event()])
        result = flush_pending_notifications(config, store)
    assert result.attempted == 0
    assert result.skipped_reason is not None
    assert "RuntimeError" in result.skipped_reason
    assert "simulated bug" not in result.skipped_reason
