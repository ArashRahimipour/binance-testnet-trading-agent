"""Proofs for `shadow/engine.py::_maybe_send_daily_summary`: at most one
SHADOW daily summary per Melbourne calendar date, triggered on the first
call at or after 08:00 LOCAL Melbourne time - correctly, across both of
Melbourne's DST regimes (AEDT, UTC+11, in summer; AEST, UTC+10, in
winter), never a fixed UTC offset. `datetime.now` is monkeypatched inside
`trading_agent.shadow.engine`'s own namespace so each test controls
exactly what "now" the function under test sees; the Melbourne-local
value handed to it is itself produced by a REAL `astimezone()` conversion
through `zoneinfo.ZoneInfo("Australia/Melbourne")`, so a wrong DST table
(or missing tzdata) would fail these tests, not just a wrong gate check.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import responses
import yaml

import trading_agent.shadow.engine as engine_module
from trading_agent.config.loader import load_config
from trading_agent.shadow.engine import _maybe_send_daily_summary
from trading_agent.shadow.store import ShadowStore

MELBOURNE = ZoneInfo("Australia/Melbourne")
BOT_TOKEN = "222222:AADailySummaryTestToken"
CHAT_ID = "7"
SEND_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"


def _config(tmp_path):
    config: dict = {
        "mode": "shadow",
        "market": {"symbol": "BTCUSDT", "interval": "1h"},
        "telegram": {"enabled": True},
        "paths": {
            "data_dir": str(tmp_path),
            "logs_dir": str(tmp_path),
            "db_path": str(tmp_path / "shadow_agent.db"),
        },
    }
    path = tmp_path / "shadow.yaml"
    path.write_text(yaml.safe_dump(config))
    return load_config(path)


class _FixedDatetime(datetime):
    """A `datetime` subclass whose `.now(tz)` always returns a fixed,
    caller-supplied instant - used to control exactly what `shadow/
    engine.py::_maybe_send_daily_summary` believes "now" is, without
    touching the real system clock."""

    _fixed: datetime

    @classmethod
    def now(cls, tz=None):
        return cls._fixed if tz is None else cls._fixed.astimezone(tz)


def _freeze_at(monkeypatch, utc_instant: datetime) -> datetime:
    """Freeze `_maybe_send_daily_summary`'s notion of "now" at the given
    UTC instant, converted to Melbourne local time via a REAL `zoneinfo`
    conversion (never hand-computed) - returns that Melbourne-local value
    for the test's own assertions."""
    melbourne_now = utc_instant.astimezone(MELBOURNE)
    fixed = type("_Fixed", (_FixedDatetime,), {"_fixed": melbourne_now})
    monkeypatch.setattr(engine_module, "datetime", fixed)
    return melbourne_now


def _outbox(config) -> list:
    with ShadowStore(config.paths.db_path) as store:
        return store.list_notifications()


# --- basic gate: before/after 08:00 local -----------------------------------


def test_no_summary_before_8am_local_time(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _freeze_at(monkeypatch, datetime(2026, 6, 15, 21, 30, tzinfo=UTC))  # ~07:30 AEST
    _maybe_send_daily_summary(config)
    assert _outbox(config) == []


@responses.activate
def test_summary_sent_at_or_after_8am_local_time(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    responses.add(responses.POST, SEND_URL, json={"ok": True}, status=200)
    config = _config(tmp_path)
    melbourne_now = _freeze_at(monkeypatch, datetime(2026, 6, 15, 22, 5, tzinfo=UTC))  # ~08:05 AEST
    assert melbourne_now.hour == 8
    _maybe_send_daily_summary(config)
    notifications = _outbox(config)
    assert len(notifications) == 1
    assert notifications[0].event_type == "daily_summary"
    assert notifications[0].event_id == f"daily_summary:{melbourne_now.date().isoformat()}"
    assert notifications[0].status == "SENT"
    assert "SHADOW DAILY SUMMARY" in notifications[0].payload_text
    assert "Australia/Melbourne" in notifications[0].payload_text


# --- at most once per Melbourne calendar date -------------------------------


@responses.activate
def test_at_most_one_summary_per_melbourne_date(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    responses.add(responses.POST, SEND_URL, json={"ok": True}, status=200)
    config = _config(tmp_path)
    _freeze_at(monkeypatch, datetime(2026, 6, 15, 22, 5, tzinfo=UTC))  # 08:05 AEST
    _maybe_send_daily_summary(config)
    assert len(_outbox(config)) == 1
    calls_after_first = len(responses.calls)

    # A later cycle the SAME Melbourne day (e.g. 14:00 local) must not
    # enqueue - or send - a second summary.
    _freeze_at(monkeypatch, datetime(2026, 6, 16, 4, 0, tzinfo=UTC))  # 14:00 AEST, same date
    _maybe_send_daily_summary(config)
    assert len(_outbox(config)) == 1
    assert len(responses.calls) == calls_after_first


@responses.activate
def test_a_new_melbourne_date_sends_a_second_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    responses.add(responses.POST, SEND_URL, json={"ok": True}, status=200)
    responses.add(responses.POST, SEND_URL, json={"ok": True}, status=200)
    config = _config(tmp_path)
    day1 = _freeze_at(monkeypatch, datetime(2026, 6, 15, 22, 5, tzinfo=UTC))  # day 1, 08:05 AEST
    _maybe_send_daily_summary(config)
    day2 = _freeze_at(monkeypatch, datetime(2026, 6, 16, 22, 5, tzinfo=UTC))  # day 2, 08:05 AEST
    _maybe_send_daily_summary(config)
    notifications = _outbox(config)
    assert len(notifications) == 2
    event_ids = {n.event_id for n in notifications}
    assert event_ids == {
        f"daily_summary:{day1.date().isoformat()}",
        f"daily_summary:{day2.date().isoformat()}",
    }


# --- slept-through-8am: fires on the first later successful cycle ----------


@responses.activate
def test_fires_on_first_cycle_after_a_missed_8am(tmp_path, monkeypatch):
    """If the machine was asleep/offline through 08:00, the first cycle
    that runs AFTER that (still the same Melbourne date) still sends the
    summary - there is no separate schedule to have "missed"."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    responses.add(responses.POST, SEND_URL, json={"ok": True}, status=200)
    config = _config(tmp_path)
    melbourne_now = _freeze_at(monkeypatch, datetime(2026, 6, 15, 23, 45, tzinfo=UTC))  # ~09:45 AEST
    assert melbourne_now.hour == 9
    _maybe_send_daily_summary(config)
    notifications = _outbox(config)
    assert len(notifications) == 1
    assert notifications[0].event_id == f"daily_summary:{melbourne_now.date().isoformat()}"


# --- DST correctness: the SAME local hour must gate identically in ---------
# --- both AEDT (summer, UTC+11) and AEST (winter, UTC+10). -----------------


def test_dst_summer_aedt_offset_is_utc_plus_11(tmp_path, monkeypatch):
    config = _config(tmp_path)
    melbourne_now = _freeze_at(monkeypatch, datetime(2026, 1, 14, 20, 59, tzinfo=UTC))
    assert melbourne_now.utcoffset().total_seconds() == 11 * 3600
    assert melbourne_now.hour == 7
    _maybe_send_daily_summary(config)
    assert _outbox(config) == []  # 07:59 local - too early, even in AEDT


@responses.activate
def test_dst_summer_aedt_8am_boundary_fires(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    responses.add(responses.POST, SEND_URL, json={"ok": True}, status=200)
    config = _config(tmp_path)
    melbourne_now = _freeze_at(monkeypatch, datetime(2026, 1, 14, 21, 1, tzinfo=UTC))
    assert melbourne_now.utcoffset().total_seconds() == 11 * 3600
    assert melbourne_now.hour == 8
    _maybe_send_daily_summary(config)
    assert len(_outbox(config)) == 1


def test_dst_winter_aest_offset_is_utc_plus_10(tmp_path, monkeypatch):
    config = _config(tmp_path)
    melbourne_now = _freeze_at(monkeypatch, datetime(2026, 7, 14, 21, 59, tzinfo=UTC))
    assert melbourne_now.utcoffset().total_seconds() == 10 * 3600
    assert melbourne_now.hour == 7
    _maybe_send_daily_summary(config)
    assert _outbox(config) == []  # 07:59 local - too early, even in AEST


@responses.activate
def test_dst_winter_aest_8am_boundary_fires(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    responses.add(responses.POST, SEND_URL, json={"ok": True}, status=200)
    config = _config(tmp_path)
    melbourne_now = _freeze_at(monkeypatch, datetime(2026, 7, 14, 22, 1, tzinfo=UTC))
    assert melbourne_now.utcoffset().total_seconds() == 10 * 3600
    assert melbourne_now.hour == 8
    _maybe_send_daily_summary(config)
    assert len(_outbox(config)) == 1


@responses.activate
def test_across_the_actual_2026_dst_start_transition(tmp_path, monkeypatch):
    """2026-10-04 02:00 AEST clocks jump to 03:00 AEDT. Both just-before
    (01:59 AEST, well before 8am) and 08:05 AEDT the SAME calendar day are
    exercised against the real transition, proving the code reacts to
    whatever `zoneinfo` says the local hour is, not a cached offset."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    responses.add(responses.POST, SEND_URL, json={"ok": True}, status=200)
    config = _config(tmp_path)

    before = _freeze_at(monkeypatch, datetime(2026, 10, 3, 15, 59, tzinfo=UTC))  # 01:59 AEST
    assert before.hour == 1
    _maybe_send_daily_summary(config)
    assert _outbox(config) == []

    after = _freeze_at(monkeypatch, datetime(2026, 10, 3, 21, 5, tzinfo=UTC))  # 08:05 AEDT, same date
    assert after.utcoffset().total_seconds() == 11 * 3600
    assert after.hour == 8
    assert after.date() == before.date()
    _maybe_send_daily_summary(config)
    notifications = _outbox(config)
    assert len(notifications) == 1
    assert notifications[0].event_id == f"daily_summary:{after.date().isoformat()}"


@responses.activate
def test_across_the_actual_2026_dst_end_transition(tmp_path, monkeypatch):
    """2026-04-05 03:00 AEDT clocks fall back to 02:00 AEST (the 02:00-
    03:00 hour occurs twice). 08:05 local that same day is unambiguous and
    must still fire exactly once."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    responses.add(responses.POST, SEND_URL, json={"ok": True}, status=200)
    config = _config(tmp_path)
    melbourne_now = _freeze_at(monkeypatch, datetime(2026, 4, 4, 22, 5, tzinfo=UTC))  # 08:05 AEST post-fallback
    assert melbourne_now.utcoffset().total_seconds() == 10 * 3600
    assert melbourne_now.hour == 8
    _maybe_send_daily_summary(config)
    assert len(_outbox(config)) == 1
