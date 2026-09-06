"""Proofs for the notification outbox added to `shadow/store.py`: atomic
enqueue alongside the trading state it describes, idempotent dedup across
repeated/retried cycles, fault-injected rollback (a Telegram-adjacent
write can never partially commit), and that a notification enqueued in one
process/connection is still there - PENDING - after a simulated restart
(a fresh `ShadowStore` reopening the same on-disk database), proving
crash recovery needs no special-case code: it is just "the same outbox
row, still there."""

from __future__ import annotations

from decimal import Decimal

import pytest
import responses

from trading_agent.metrics.performance import EXIT_REASON_STRATEGY, Trade
from trading_agent.shadow.store import (
    NOTIFICATION_STATUS_PENDING,
    NOTIFICATION_STATUS_SENT,
    NotificationEvent,
    ShadowStore,
    ShadowTradeRecord,
    _InjectedTestFault,
)


def _trade(entry_ms: int = 1_000, exit_ms: int = 2_000) -> Trade:
    return Trade(
        entry_time_ms=entry_ms, exit_time_ms=exit_ms, entry_price=Decimal(100), exit_price=Decimal(101),
        quantity=Decimal(1), fees_paid=Decimal("0.1"), pnl_quote=Decimal("1.0"), exit_reason=EXIT_REASON_STRATEGY,
        entry_fee_quote=Decimal("0.05"), exit_fee_quote=Decimal("0.05"),
        entry_reference_price=Decimal("99.9"), exit_reference_price=Decimal("101.1"),
    )


def _event(event_id: str = "exit:2000", payload: str = "a message") -> NotificationEvent:
    return NotificationEvent(event_id=event_id, event_type="exit", created_at_ms=5_000, payload_text=payload)


# --- atomic enqueue alongside trading state ----------------------------------


def test_notification_is_enqueued_in_the_same_transaction_as_the_trade(tmp_path):
    with ShadowStore(tmp_path / "shadow.db") as store:
        trade = _trade()
        record = ShadowTradeRecord(trade=trade, planned_risk_quote=Decimal("0.5"), net_reward_to_risk=2.0)
        store.record_cycle_atomically(
            [record], [], [], 2_000, None, now_ms=5_000, segment_length=10, status="OK", detail="",
            notification_events=[_event()],
        )
        assert len(store.get_all_trades()) == 1
        notification = store.get_notification("exit:2000")
        assert notification is not None
        assert notification.status == NOTIFICATION_STATUS_PENDING
        assert notification.payload_text == "a message"


def test_notification_rolls_back_with_the_trade_on_a_fault(tmp_path):
    with ShadowStore(tmp_path / "shadow.db") as store:
        trade = _trade()
        record = ShadowTradeRecord(trade=trade, planned_risk_quote=Decimal("0.5"), net_reward_to_risk=2.0)
        store._raise_fault_at = "before_commit"
        with pytest.raises(_InjectedTestFault):
            store.record_cycle_atomically(
                [record], [], [], 2_000, None, now_ms=5_000, segment_length=10, status="OK", detail="",
                notification_events=[_event()],
            )
        store._raise_fault_at = None
        # NEITHER the trade NOR the notification exists - a Telegram-adjacent
        # write can never partially commit relative to the trading fact it
        # describes.
        assert store.get_all_trades() == []
        assert store.get_notification("exit:2000") is None


def test_notification_rolls_back_at_its_own_fault_point(tmp_path):
    with ShadowStore(tmp_path / "shadow.db") as store:
        trade = _trade()
        record = ShadowTradeRecord(trade=trade, planned_risk_quote=Decimal("0.5"), net_reward_to_risk=2.0)
        store._raise_fault_at = "after_notifications"
        with pytest.raises(_InjectedTestFault):
            store.record_cycle_atomically(
                [record], [], [], 2_000, None, now_ms=5_000, segment_length=10, status="OK", detail="",
                notification_events=[_event()],
            )
        store._raise_fault_at = None
        assert store.get_all_trades() == []
        assert store.get_notification("exit:2000") is None
        assert store.get_run_state().total_cycles == 0


# --- idempotent dedup across repeated/retried cycles ------------------------


def test_notification_enqueue_is_idempotent_across_retried_cycles(tmp_path):
    with ShadowStore(tmp_path / "shadow.db") as store:
        trade = _trade()
        record = ShadowTradeRecord(trade=trade, planned_risk_quote=Decimal("0.5"), net_reward_to_risk=2.0)
        for _ in range(3):
            store.record_cycle_atomically(
                [record], [], [], 2_000, None, now_ms=5_000, segment_length=10, status="OK", detail="",
                notification_events=[_event()],
            )
        assert len(store.list_notifications()) == 1
        assert store.get_run_state().total_cycles == 3


def test_notification_enqueue_does_not_clobber_an_already_sent_event(tmp_path):
    """A dedup key can't accidentally reset a notification's delivery
    status back to PENDING if the SAME shadow cycle is ever recomputed and
    re-persisted after the notification was already sent - `ON CONFLICT
    DO NOTHING` means the second insert is a total no-op, status included.
    """
    with ShadowStore(tmp_path / "shadow.db") as store:
        trade = _trade()
        record = ShadowTradeRecord(trade=trade, planned_risk_quote=Decimal("0.5"), net_reward_to_risk=2.0)
        store.record_cycle_atomically(
            [record], [], [], 2_000, None, now_ms=5_000, segment_length=10, status="OK", detail="",
            notification_events=[_event()],
        )
        store.update_notification_status("exit:2000", NOTIFICATION_STATUS_SENT, 1, 5_100, None, 5_100)

        store.record_cycle_atomically(
            [record], [], [], 2_000, None, now_ms=9_000, segment_length=10, status="OK", detail="",
            notification_events=[_event()],
        )
        notification = store.get_notification("exit:2000")
        assert notification is not None
        assert notification.status == NOTIFICATION_STATUS_SENT  # never reset to PENDING


def test_enqueue_notifications_atomically_standalone_dedup(tmp_path):
    with ShadowStore(tmp_path / "shadow.db") as store:
        store.enqueue_notifications_atomically([_event("daily_summary:2026-01-01", "first")])
        store.enqueue_notifications_atomically([_event("daily_summary:2026-01-01", "second - should be ignored")])
        notifications = store.list_notifications()
        assert len(notifications) == 1
        assert notifications[0].payload_text == "first"


def test_enqueue_notifications_atomically_is_a_no_op_for_empty_list(tmp_path):
    with ShadowStore(tmp_path / "shadow.db") as store:
        store.enqueue_notifications_atomically([])
        assert store.list_notifications() == []


# --- get/list/update round-trips --------------------------------------------


def test_list_notifications_filters_by_status(tmp_path):
    with ShadowStore(tmp_path / "shadow.db") as store:
        store.enqueue_notifications_atomically([_event("a"), _event("b"), _event("c")])
        store.update_notification_status("b", NOTIFICATION_STATUS_SENT, 1, 1, None, 1)
        pending = store.list_notifications(status=NOTIFICATION_STATUS_PENDING)
        sent = store.list_notifications(status=NOTIFICATION_STATUS_SENT)
        assert {e.event_id for e in pending} == {"a", "c"}
        assert {e.event_id for e in sent} == {"b"}


def test_get_notification_returns_none_for_unknown_event(tmp_path):
    with ShadowStore(tmp_path / "shadow.db") as store:
        assert store.get_notification("nope") is None


# --- daily-summary dedup state -----------------------------------------------


def test_daily_summary_date_defaults_to_none_and_round_trips(tmp_path):
    with ShadowStore(tmp_path / "shadow.db") as store:
        assert store.get_last_daily_summary_melbourne_date() is None
        store.record_daily_summary_sent("2026-03-01")
        assert store.get_last_daily_summary_melbourne_date() == "2026-03-01"
        store.record_daily_summary_sent("2026-03-02")
        assert store.get_last_daily_summary_melbourne_date() == "2026-03-02"


# --- crash / restart recovery ------------------------------------------------


@responses.activate
def test_a_pending_notification_survives_a_simulated_restart(tmp_path, monkeypatch):
    """Simulates a crash between the trading-state commit and the delivery
    attempt: enqueue via one `ShadowStore` connection (representing the
    cycle that just committed), then - WITHOUT ever calling `flush` -
    close it and open a brand-new `ShadowStore` against the same on-disk
    database (representing the next process start). The notification must
    still be there, PENDING, ready for the new process's own flush call.
    """
    from trading_agent.config.models import AppConfig
    from trading_agent.shadow.notifications.sender import flush_pending_notifications

    db_path = tmp_path / "shadow.db"
    with ShadowStore(db_path) as store:
        trade = _trade()
        record = ShadowTradeRecord(trade=trade, planned_risk_quote=Decimal("0.5"), net_reward_to_risk=2.0)
        store.record_cycle_atomically(
            [record], [], [], 2_000, None, now_ms=5_000, segment_length=10, status="OK", detail="",
            notification_events=[_event()],
        )
    # The process "crashes" here - store is closed, nothing was ever sent.

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "111:RESTARTTEST")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    responses.add(
        responses.POST, "https://api.telegram.org/bot111:RESTARTTEST/sendMessage", json={"ok": True}, status=200,
    )
    config = AppConfig(
        mode="shadow", telegram={"enabled": True},
        paths={"data_dir": str(tmp_path), "logs_dir": str(tmp_path), "db_path": str(db_path)},
    )
    with ShadowStore(db_path) as reopened_store:
        notification = reopened_store.get_notification("exit:2000")
        assert notification is not None
        assert notification.status == NOTIFICATION_STATUS_PENDING

        result = flush_pending_notifications(config, reopened_store)
        assert result.sent == 1
        sent_notification = reopened_store.get_notification("exit:2000")
        assert sent_notification is not None
        assert sent_notification.status == NOTIFICATION_STATUS_SENT
