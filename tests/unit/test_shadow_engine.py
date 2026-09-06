"""Proofs for shadow/engine.py: mode/interval guards, kill switch/lock/
bootstrap-not-verified short-circuits (all proven WITHOUT any network mock
registered - `responses` raises immediately if the code under test ever
attempts a real request), the INSUFFICIENT_DATA settling-buffer guard, and
a full end-to-end cycle (via the real `run_segment`/E1 strategy) proving:

  - NO trade, equity change, or open position can ever result from a 4h
    setup that closed strictly before the fixed shadow start boundary
    (`test_a_setup_that_closed_in_the_last_warmup_hour_never_produces_a_trade`),
  - the first GENUINELY post-boundary setup IS caught at the earliest
    possible opportunity - immediately after bootstrap, not after a
    315-day organic wait
    (`test_immediate_post_boundary_eligibility_no_315_day_wait`),
  - idempotency and single-new-candle incremental processing continue to
    hold exactly as before bootstrap existed.

Never connects to Binance - every HTTP call `shadow/engine.py` could make
is mocked via `responses`; a test that expects NO call registers none, so
an unexpected attempt fails loudly instead of silently reaching the network.
Bootstrap state itself is seeded directly (bypassing the network fetch,
which is `shadow/bootstrap.py`'s own module's job to test - see
`tests/unit/test_shadow_bootstrap.py`) so these tests stay fast.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
import responses
import yaml

from tests.fixtures.exchange_info import make_exchange_info
from trading_agent.config.loader import load_config
from trading_agent.data.models import Candle, interval_to_ms
from trading_agent.data.storage import CandleStore
from trading_agent.research.candidates.multitimeframe_breakout import (
    CONFIRMATION_WINDOW_1H_CANDLES,
    MultiTimeframeBreakoutStrategy,
    _scan_setup_events,
)
from trading_agent.risk.kill_switch import KillSwitch
from trading_agent.shadow.bootstrap import (
    compute_effective_min_required_candles,
    compute_warmup_candle_count,
)
from trading_agent.shadow.boundary import SHADOW_START_BOUNDARY_MS, ShadowConfigError
from trading_agent.shadow.engine import (
    SHADOW_STATUS_BOOTSTRAP_INVALID,
    SHADOW_STATUS_INSUFFICIENT_DATA,
    SHADOW_STATUS_KILL_SWITCH_ENGAGED,
    SHADOW_STATUS_NO_NEW_CANDLES,
    SHADOW_STATUS_NOT_BOOTSTRAPPED,
    SHADOW_STATUS_OK,
    run_shadow_cycle,
    shadow_kill_switch_path,
    shadow_lock_path,
)
from trading_agent.shadow.lock import ShadowLock, ShadowLockError
from trading_agent.shadow.store import ShadowStore

PROD_HOST = "https://api.binance.com"
INTERVAL = "1h"
STEP = interval_to_ms(INTERVAL)

STRATEGY_MIN_REQUIRED = MultiTimeframeBreakoutStrategy().min_required_candles
WARMUP_CANDLE_COUNT = compute_warmup_candle_count(STRATEGY_MIN_REQUIRED)
EFFECTIVE_MIN_REQUIRED = compute_effective_min_required_candles(WARMUP_CANDLE_COUNT)
WARMUP_START_MS = SHADOW_START_BOUNDARY_MS - WARMUP_CANDLE_COUNT * STEP
#: Index of the first candle at or after the boundary, in a 0-indexed
#: candle array starting at WARMUP_START_MS - equal to WARMUP_CANDLE_COUNT
#: since indices [0, WARMUP_CANDLE_COUNT) are exactly the warm-up range.
BOUNDARY_IDX = WARMUP_CANDLE_COUNT
#: Index of the first OFFICIALLY evaluated (trade-affecting) candle - see
#: shadow/bootstrap.py's "THE SETTLING BUFFER" section.
OFFICIAL_EVAL_IDX = EFFECTIVE_MIN_REQUIRED - 1
assert OFFICIAL_EVAL_IDX == BOUNDARY_IDX + CONFIRMATION_WINDOW_1H_CANDLES


def _candle(i: int, close: float) -> Candle:
    open_ = close - 0.05
    open_time_ms = WARMUP_START_MS + i * STEP
    return Candle(
        symbol="BTCUSDT", interval=INTERVAL, open_time_ms=open_time_ms, close_time_ms=open_time_ms + STEP - 1,
        open=Decimal(str(open_)), high=Decimal(str(close * 1.002)), low=Decimal(str(open_ * 0.998)),
        close=Decimal(str(close)), volume=Decimal(1),
    )


def _full_candles(n_hours: int, kick_at: int | None = None, kick_pct: float = 1.03) -> list[Candle]:
    """A steady, mild uptrend (0.02/hour, identical recipe to
    `test_research_candidates_multitimeframe_breakout.py::_bullish_candles`)
    over `n_hours` candles STARTING EXACTLY AT `WARMUP_START_MS` (never an
    arbitrary test-chosen start - warm-up's own timestamps are entirely
    determined by the fixed boundary), with an optional one-off
    multiplicative "kick" at index `kick_at` to reliably trigger E1's
    volatility-normalized Donchian breakout."""
    closes: list[float] = []
    price = 100.0
    for i in range(n_hours):
        price += 0.02
        if kick_at is not None and i == kick_at:
            price *= kick_pct
        closes.append(price)
    return [_candle(i, c) for i, c in enumerate(closes)]


def _shadow_config(tmp_path, **overrides):
    config: dict = {
        "mode": "shadow",
        "market": {"symbol": "BTCUSDT", "interval": INTERVAL},
        "risk": {"max_risk_per_trade_pct": 0.01},
        "backtest": {"starting_equity": 50.0},
        "paths": {
            "data_dir": str(tmp_path),
            "logs_dir": str(tmp_path),
            "db_path": str(tmp_path / "shadow_agent.db"),
        },
    }
    config.update(overrides)
    path = tmp_path / "shadow.yaml"
    path.write_text(yaml.safe_dump(config))
    return load_config(path)


def _direct_bootstrap(config, warmup_candles: list[Candle]) -> None:
    """Seed a valid bootstrap state directly (bypassing the network fetch
    `shadow/bootstrap.py::run_shadow_bootstrap` itself performs and
    already has its own dedicated tests for - see
    `tests/unit/test_shadow_bootstrap.py`), so these engine-level tests
    stay fast and focused on `run_shadow_cycle`'s own behavior."""
    assert len(warmup_candles) == WARMUP_CANDLE_COUNT
    assert warmup_candles[0].open_time_ms == WARMUP_START_MS
    assert warmup_candles[-1].close_time_ms == SHADOW_START_BOUNDARY_MS - 1
    with CandleStore(config.paths.db_path) as candle_store:
        candle_store.upsert_candles(warmup_candles)
    with ShadowStore(config.paths.db_path) as shadow_store:
        shadow_store.record_bootstrap_atomically(
            warmup_candles, WARMUP_START_MS, warmup_candles[-1].open_time_ms,
            WARMUP_CANDLE_COUNT, EFFECTIVE_MIN_REQUIRED, bootstrapped_at_ms=1, source="test-direct-seed",
        )


# --- Config guards (no I/O at all). ---


def test_requires_shadow_mode(tmp_path):
    config = load_config(overrides={"mode": "backtest"})
    with pytest.raises(ShadowConfigError):
        run_shadow_cycle(config)


def test_requires_1h_interval(tmp_path):
    bad = _shadow_config(tmp_path, market={"symbol": "BTCUSDT", "interval": "4h"})
    with pytest.raises(ShadowConfigError):
        run_shadow_cycle(bad)


# --- Kill switch / lock / not-bootstrapped short-circuits: NO network mock registered. ---


@responses.activate
def test_kill_switch_short_circuits_before_any_network_call(tmp_path):
    config = _shadow_config(tmp_path)
    KillSwitch(shadow_kill_switch_path(config)).engage("test")

    result = run_shadow_cycle(config)

    assert result.status == SHADOW_STATUS_KILL_SWITCH_ENGAGED
    assert result.new_candles_fetched == 0
    assert result.new_trades_persisted == 0


@responses.activate
def test_lock_held_by_another_process_short_circuits_before_any_network_call(tmp_path):
    config = _shadow_config(tmp_path)
    holder = ShadowLock(shadow_lock_path(config))
    holder.acquire()
    try:
        with pytest.raises(ShadowLockError):
            run_shadow_cycle(config)
    finally:
        holder.release()


@responses.activate
def test_shadow_run_refuses_without_bootstrap_and_makes_no_network_call(tmp_path):
    config = _shadow_config(tmp_path)

    result = run_shadow_cycle(config)

    assert result.status == SHADOW_STATUS_NOT_BOOTSTRAPPED
    assert result.new_candles_fetched == 0
    assert "shadow-bootstrap" in result.detail


@responses.activate
def test_shadow_run_refuses_if_bootstrapped_warmup_candles_are_missing(tmp_path):
    """A real re-verification, not a cached flag: if the recorded
    bootstrap state exists but the underlying candles do not (corruption,
    partial restore, manual tampering), refuse rather than proceed on
    incomplete indicator history."""
    config = _shadow_config(tmp_path)
    with ShadowStore(config.paths.db_path) as shadow_store:
        shadow_store.record_bootstrap_atomically(
            [], WARMUP_START_MS, SHADOW_START_BOUNDARY_MS - STEP,
            WARMUP_CANDLE_COUNT, EFFECTIVE_MIN_REQUIRED, bootstrapped_at_ms=1, source="test",
        )

    result = run_shadow_cycle(config)

    assert result.status == SHADOW_STATUS_BOOTSTRAP_INVALID
    assert result.new_candles_fetched == 0


# --- INSUFFICIENT_DATA: bootstrapped, but the settling buffer has not filled yet. ---


@responses.activate
def test_insufficient_data_immediately_after_bootstrap_before_any_forward_candle(tmp_path):
    config = _shadow_config(tmp_path)
    warmup_candles = _full_candles(WARMUP_CANDLE_COUNT)
    _direct_bootstrap(config, warmup_candles)

    responses.add(
        responses.GET, f"{PROD_HOST}/api/v3/time",
        json={"serverTime": warmup_candles[-1].close_time_ms + 1}, status=200,
    )
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/klines", json=[], status=200)

    result = run_shadow_cycle(config)

    assert result.status == SHADOW_STATUS_INSUFFICIENT_DATA
    assert result.segment_length == WARMUP_CANDLE_COUNT
    assert result.min_required_candles == EFFECTIVE_MIN_REQUIRED
    with ShadowStore(config.paths.db_path) as store:
        assert store.get_all_trades() == []
        assert store.get_equity_curve() == []
        assert store.get_run_state().open_position is None


# --- No leakage: a setup that closed strictly before the boundary. ---


@responses.activate
def test_a_setup_that_closed_in_the_last_warmup_hour_never_produces_a_trade(tmp_path):
    """The one edge case `shadow/bootstrap.py`'s settling buffer exists
    for: a 4h Donchian breakout whose OWN bucket closes at the very last
    warm-up hour (`BOUNDARY_IDX - 1`) has a confirmation window
    `[BOUNDARY_IDX, BOUNDARY_IDX + 3]` entirely inside the settling
    buffer. It must never generate a trade, move equity, or leave an open
    position - proving "the first eligible 4h setup must close at or
    after the boundary" holds even in the single case where it otherwise
    would not.
    """
    config = _shadow_config(tmp_path)
    kick_at = BOUNDARY_IDX - 1  # the last warm-up hour - also the last hour of its own 4h bucket.
    # Confirms 4h-bucket alignment this proof depends on: the candle right
    # after kick_at is exactly the boundary candle, and the boundary
    # itself is always 4h-grid-aligned (every midnight UTC is, since 24h
    # is an exact multiple of 4h) - checked on the ABSOLUTE timestamp, not
    # the array index (which need not itself be a multiple of 4).
    assert WARMUP_START_MS + (kick_at + 1) * STEP == SHADOW_START_BOUNDARY_MS
    assert SHADOW_START_BOUNDARY_MS % (4 * STEP) == 0

    n_total = OFFICIAL_EVAL_IDX + 3  # covers the whole dangling window plus a couple of official candles
    all_candles = _full_candles(n_total, kick_at=kick_at)
    warmup_candles = all_candles[:WARMUP_CANDLE_COUNT]
    forward_candles = all_candles[WARMUP_CANDLE_COUNT:]

    _direct_bootstrap(config, warmup_candles)
    with CandleStore(config.paths.db_path) as candle_store:
        candle_store.upsert_candles(forward_candles)

    responses.add(
        responses.GET, f"{PROD_HOST}/api/v3/time", json={"serverTime": all_candles[-1].close_time_ms + 1}, status=200
    )
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/klines", json=[], status=200)
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/exchangeInfo", json=make_exchange_info(min_notional="0.01"), status=200)

    # Sanity check on the fixture itself: the dangling setup really is
    # detected, really is unconfirmed within warm-up alone, and its window
    # really does extend into the settling buffer - otherwise this test
    # would be proving nothing.
    events = _scan_setup_events(warmup_candles)
    dangling = [e for e in events if e.setup_hour_index == kick_at]
    assert len(dangling) == 1
    assert dangling[0].confirmation_index is None
    assert dangling[0].window_end_index == BOUNDARY_IDX + 3

    result = run_shadow_cycle(config)

    assert result.status == SHADOW_STATUS_OK
    with ShadowStore(config.paths.db_path) as store:
        assert store.get_all_trades() == [], "a pre-boundary-closed setup must never produce a closed trade"
        state = store.get_run_state()
        assert state.open_position is None, "a pre-boundary-closed setup must never produce an open position"
        # Equity is recorded starting at the first OFFICIALLY evaluated
        # candle - never earlier - and every one of those points must
        # itself be at or after the boundary.
        equity = store.get_equity_curve()
        assert equity, "the official evaluation window should still produce equity points"
        assert all(p.timestamp_ms >= SHADOW_START_BOUNDARY_MS for p in equity)
        assert equity[0].timestamp_ms == all_candles[OFFICIAL_EVAL_IDX].close_time_ms


# --- Immediate post-boundary eligibility: no 315-day wait. ---


@responses.activate
def test_immediate_post_boundary_eligibility_no_315_day_wait(tmp_path):
    """The positive counterpart: a GENUINELY fresh 4h setup - one whose
    own bucket opens exactly at the boundary - is caught and fills within
    HOURS of the boundary, not after ~315 days of organic accumulation."""
    config = _shadow_config(tmp_path)
    kick_at = BOUNDARY_IDX + 3  # the last hour of the FIRST wholly-post-boundary 4h bucket.
    assert WARMUP_START_MS + (kick_at + 1) * STEP == SHADOW_START_BOUNDARY_MS + CONFIRMATION_WINDOW_1H_CANDLES * STEP

    n_total = OFFICIAL_EVAL_IDX + 5  # confirmation + one candle for the fill + a little margin
    all_candles = _full_candles(n_total, kick_at=kick_at)
    warmup_candles = all_candles[:WARMUP_CANDLE_COUNT]
    forward_candles = all_candles[WARMUP_CANDLE_COUNT:]

    _direct_bootstrap(config, warmup_candles)
    with CandleStore(config.paths.db_path) as candle_store:
        candle_store.upsert_candles(forward_candles)

    responses.add(
        responses.GET, f"{PROD_HOST}/api/v3/time", json={"serverTime": all_candles[-1].close_time_ms + 1}, status=200
    )
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/klines", json=[], status=200)
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/exchangeInfo", json=make_exchange_info(min_notional="0.01"), status=200)

    result = run_shadow_cycle(config)

    assert result.status == SHADOW_STATUS_OK
    assert result.segment_length == n_total

    with ShadowStore(config.paths.db_path) as store:
        state = store.get_run_state()
        trades = store.get_all_trades()
        # Exactly one approved entry - either already closed or still open.
        assert (len(trades) == 1) != (state.open_position is not None)
        entry_time_ms = trades[0].trade.entry_time_ms if trades else state.open_position.entry_time_ms

    # The entry fills at the NEXT candle's open after confirmation, i.e.
    # exactly the first officially-evaluated candle plus one hour - a
    # matter of HOURS past the boundary, never 315 days.
    expected_entry_time_ms = all_candles[OFFICIAL_EVAL_IDX + 1].open_time_ms
    assert entry_time_ms == expected_entry_time_ms
    hours_after_boundary = (entry_time_ms - SHADOW_START_BOUNDARY_MS) / STEP
    assert 0 < hours_after_boundary <= CONFIRMATION_WINDOW_1H_CANDLES + 2

    # And nothing at all was recorded before the boundary.
    with ShadowStore(config.paths.db_path) as store:
        equity = store.get_equity_curve()
        assert all(p.timestamp_ms >= SHADOW_START_BOUNDARY_MS for p in equity)


# --- Idempotency / incremental processing, now under a bootstrapped store. ---


def _seed_candles_through_confirmed_entry():
    kick_at = BOUNDARY_IDX + 3
    n_total = OFFICIAL_EVAL_IDX + 5
    all_candles = _full_candles(n_total, kick_at=kick_at)
    return all_candles[:WARMUP_CANDLE_COUNT], all_candles[WARMUP_CANDLE_COUNT:]


@responses.activate
def test_full_cycle_is_idempotent_on_retry(tmp_path):
    config = _shadow_config(tmp_path)
    warmup_candles, forward_candles = _seed_candles_through_confirmed_entry()
    _direct_bootstrap(config, warmup_candles)
    with CandleStore(config.paths.db_path) as candle_store:
        candle_store.upsert_candles(forward_candles)

    last_close_ms = forward_candles[-1].close_time_ms
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/time", json={"serverTime": last_close_ms + 1}, status=200)
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/klines", json=[], status=200)
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/exchangeInfo", json=make_exchange_info(min_notional="0.01"), status=200)

    result = run_shadow_cycle(config)
    assert result.status == SHADOW_STATUS_OK

    with ShadowStore(config.paths.db_path) as store:
        trades = store.get_all_trades()
        equity = store.get_equity_curve()

    responses.add(responses.GET, f"{PROD_HOST}/api/v3/time", json={"serverTime": last_close_ms + 1}, status=200)
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/klines", json=[], status=200)

    second = run_shadow_cycle(config)

    assert second.status == SHADOW_STATUS_NO_NEW_CANDLES
    assert second.new_trades_persisted == 0
    assert second.new_equity_points_persisted == 0
    with ShadowStore(config.paths.db_path) as store:
        assert len(store.get_all_trades()) == len(trades)
        assert len(store.get_equity_curve()) == len(equity)
        assert store.get_run_state().total_cycles == 2


@responses.activate
def test_one_new_candle_advances_the_high_water_mark_by_exactly_one(tmp_path):
    config = _shadow_config(tmp_path)
    warmup_candles, forward_candles = _seed_candles_through_confirmed_entry()
    _direct_bootstrap(config, warmup_candles)
    with CandleStore(config.paths.db_path) as candle_store:
        candle_store.upsert_candles(forward_candles)

    last_close_ms = forward_candles[-1].close_time_ms
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/time", json={"serverTime": last_close_ms + 1}, status=200)
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/klines", json=[], status=200)
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/exchangeInfo", json=make_exchange_info(min_notional="0.01"), status=200)
    first = run_shadow_cycle(config)
    assert first.status == SHADOW_STATUS_OK

    next_open_time_ms = forward_candles[-1].open_time_ms + STEP
    next_close = float(forward_candles[-1].close) + 0.02
    next_candle = Candle(
        symbol="BTCUSDT", interval=INTERVAL, open_time_ms=next_open_time_ms, close_time_ms=next_open_time_ms + STEP - 1,
        open=Decimal(str(next_close - 0.05)), high=Decimal(str(next_close * 1.002)),
        low=Decimal(str((next_close - 0.05) * 0.998)), close=Decimal(str(next_close)), volume=Decimal(1),
    )
    next_row = [
        next_candle.open_time_ms, str(next_candle.open), str(next_candle.high), str(next_candle.low),
        str(next_candle.close), str(next_candle.volume), next_candle.close_time_ms, "0", 0, "0", "0", "0",
    ]
    responses.add(
        responses.GET, f"{PROD_HOST}/api/v3/time", json={"serverTime": next_candle.close_time_ms + 1}, status=200
    )
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/klines", json=[next_row], status=200)
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/exchangeInfo", json=make_exchange_info(min_notional="0.01"), status=200)

    second = run_shadow_cycle(config)

    assert second.status == SHADOW_STATUS_OK
    assert second.new_candles_fetched == 1
    assert second.segment_length == WARMUP_CANDLE_COUNT + len(forward_candles) + 1
    with ShadowStore(config.paths.db_path) as store:
        state = store.get_run_state()
        assert state.last_processed_close_time_ms == next_candle.close_time_ms
        equity = store.get_equity_curve()
        assert equity[-1].timestamp_ms == next_candle.close_time_ms


# --- Telegram notifications wired into run_shadow_cycle --------------------


@responses.activate
def test_new_open_position_enqueues_and_sends_an_entry_notification(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "444444:AAEntryWiringTestToken")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "55")
    config = _shadow_config(tmp_path, telegram={"enabled": True})

    kick_at = BOUNDARY_IDX + 3
    n_total = OFFICIAL_EVAL_IDX + 5
    all_candles = _full_candles(n_total, kick_at=kick_at)
    warmup_candles = all_candles[:WARMUP_CANDLE_COUNT]
    forward_candles = all_candles[WARMUP_CANDLE_COUNT:]
    _direct_bootstrap(config, warmup_candles)
    with CandleStore(config.paths.db_path) as candle_store:
        candle_store.upsert_candles(forward_candles)

    responses.add(
        responses.GET, f"{PROD_HOST}/api/v3/time", json={"serverTime": all_candles[-1].close_time_ms + 1}, status=200
    )
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/klines", json=[], status=200)
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/exchangeInfo", json=make_exchange_info(min_notional="0.01"), status=200)
    responses.add(
        responses.POST, "https://api.telegram.org/bot444444:AAEntryWiringTestToken/sendMessage",
        json={"ok": True}, status=200,
    )

    result = run_shadow_cycle(config)
    assert result.status == SHADOW_STATUS_OK

    with ShadowStore(config.paths.db_path) as store:
        state = store.get_run_state()
        notifications = store.list_notifications()
    assert state.open_position is not None
    entry_events = [n for n in notifications if n.event_type == "entry"]
    assert len(entry_events) == 1
    assert entry_events[0].event_id == f"entry:{state.open_position.entry_time_ms}"
    assert entry_events[0].status == "SENT"
    assert "SHADOW ENTRY" in entry_events[0].payload_text
    assert "NO REAL OR TESTNET ORDER WAS PLACED" in entry_events[0].payload_text
    assert "444444:AAEntryWiringTestToken" not in entry_events[0].payload_text


@responses.activate
def test_notifications_disabled_by_default_makes_zero_telegram_calls(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    config = _shadow_config(tmp_path)  # telegram.enabled defaults to False
    assert config.telegram.enabled is False

    kick_at = BOUNDARY_IDX + 3
    n_total = OFFICIAL_EVAL_IDX + 5
    all_candles = _full_candles(n_total, kick_at=kick_at)
    warmup_candles = all_candles[:WARMUP_CANDLE_COUNT]
    forward_candles = all_candles[WARMUP_CANDLE_COUNT:]
    _direct_bootstrap(config, warmup_candles)
    with CandleStore(config.paths.db_path) as candle_store:
        candle_store.upsert_candles(forward_candles)

    responses.add(
        responses.GET, f"{PROD_HOST}/api/v3/time", json={"serverTime": all_candles[-1].close_time_ms + 1}, status=200
    )
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/klines", json=[], status=200)
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/exchangeInfo", json=make_exchange_info(min_notional="0.01"), status=200)
    # Deliberately NO Telegram mock registered - any attempt fails loudly.

    result = run_shadow_cycle(config)
    assert result.status == SHADOW_STATUS_OK

    with ShadowStore(config.paths.db_path) as store:
        notifications = store.list_notifications()
    entry_events = [n for n in notifications if n.event_type == "entry"]
    assert len(entry_events) == 1
    assert entry_events[0].status == "PENDING"  # enqueued, but never sent while disabled
    assert not any(call.request.url.startswith("https://api.telegram.org") for call in responses.calls)


@responses.activate
def test_an_entry_and_its_own_exit_within_one_cycle_both_send_notifications(tmp_path, monkeypatch):
    """`run_shadow_cycle` recomputes the ENTIRE latest segment every time
    it runs - so a hypothetical position can both open AND close within a
    single cycle's evaluation window (e.g. after a long kill-switch pause,
    or simply a fast-moving take-profit). Both the entry and the exit must
    still get their own notification - the entry is never silently
    skipped just because the position is no longer open by the time the
    cycle finishes."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "555555:AASameCycleTestToken")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "66")
    config = _shadow_config(tmp_path, telegram={"enabled": True})

    kick_at = BOUNDARY_IDX + 3
    entry_idx = OFFICIAL_EVAL_IDX + 1
    target_hit_idx = entry_idx + 5
    n_total = target_hit_idx + 3
    all_candles = _full_candles(n_total, kick_at=kick_at)
    # Blow well past any plausible take-profit target on one later candle,
    # guaranteeing the freshly-opened position closes within this SAME
    # cycle's segment (never touching risk/reward math itself - just
    # engineering a price path that reliably crosses whatever target that
    # unmodified math already computed).
    inflated = replace(all_candles[target_hit_idx], high=all_candles[target_hit_idx].high * 2)
    all_candles[target_hit_idx] = inflated

    warmup_candles = all_candles[:WARMUP_CANDLE_COUNT]
    forward_candles = all_candles[WARMUP_CANDLE_COUNT:]
    _direct_bootstrap(config, warmup_candles)
    with CandleStore(config.paths.db_path) as candle_store:
        candle_store.upsert_candles(forward_candles)

    responses.add(
        responses.GET, f"{PROD_HOST}/api/v3/time", json={"serverTime": all_candles[-1].close_time_ms + 1}, status=200
    )
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/klines", json=[], status=200)
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/exchangeInfo", json=make_exchange_info(min_notional="0.01"), status=200)
    responses.add(
        responses.POST, "https://api.telegram.org/bot555555:AASameCycleTestToken/sendMessage",
        json={"ok": True}, status=200,
    )

    result = run_shadow_cycle(config)
    assert result.status == SHADOW_STATUS_OK

    with ShadowStore(config.paths.db_path) as store:
        trades = store.get_all_trades()
        state = store.get_run_state()
        notifications = store.list_notifications()
    assert len(trades) == 1  # opened AND closed within this one cycle
    assert state.open_position is None

    entry_events = [n for n in notifications if n.event_type == "entry"]
    exit_events = [n for n in notifications if n.event_type == "exit"]
    assert len(entry_events) == 1
    assert len(exit_events) == 1
    assert entry_events[0].event_id == f"entry:{trades[0].trade.entry_time_ms}"
    assert exit_events[0].event_id == f"exit:{trades[0].trade.exit_time_ms}"
    assert entry_events[0].status == "SENT"
    assert exit_events[0].status == "SENT"
    assert "SHADOW EXIT" in exit_events[0].payload_text
    assert "Closed trade count (to date): 1" in exit_events[0].payload_text


@responses.activate
def test_a_trade_opened_in_an_earlier_cycle_gets_only_one_entry_notification(tmp_path, monkeypatch):
    """An entry notified when the position first opened (cycle 1) must
    NEVER be re-sent when that same position closes in a LATER cycle
    (cycle 2) - only the exit is new at that point."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "666666:AATwoCycleTestToken")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "77")
    config = _shadow_config(tmp_path, telegram={"enabled": True})

    kick_at = BOUNDARY_IDX + 3
    n_total = OFFICIAL_EVAL_IDX + 5
    all_candles = _full_candles(n_total, kick_at=kick_at)
    warmup_candles = all_candles[:WARMUP_CANDLE_COUNT]
    forward_candles = all_candles[WARMUP_CANDLE_COUNT:]
    _direct_bootstrap(config, warmup_candles)
    with CandleStore(config.paths.db_path) as candle_store:
        candle_store.upsert_candles(forward_candles)

    telegram_url = "https://api.telegram.org/bot666666:AATwoCycleTestToken/sendMessage"
    responses.add(
        responses.GET, f"{PROD_HOST}/api/v3/time", json={"serverTime": all_candles[-1].close_time_ms + 1}, status=200
    )
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/klines", json=[], status=200)
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/exchangeInfo", json=make_exchange_info(min_notional="0.01"), status=200)
    responses.add(responses.POST, telegram_url, json={"ok": True}, status=200)

    first = run_shadow_cycle(config)
    assert first.status == SHADOW_STATUS_OK
    with ShadowStore(config.paths.db_path) as store:
        assert store.get_run_state().open_position is not None
        # Filtered by event_type since a real daily summary may ALSO have
        # fired this cycle, depending on the actual wall-clock Melbourne
        # time the test happens to run at - irrelevant to what this test
        # is proving (entry de-duplication across cycles).
        first_cycle_entries = [n for n in store.list_notifications() if n.event_type == "entry"]
        assert len(first_cycle_entries) == 1

    # A second cycle with one more candle that blows through the target,
    # closing the SAME position that was already notified as an entry.
    # `_candle(n_total, ...)` continues exactly where `all_candles` left
    # off (index n_total is the very next hour after `all_candles[-1]`).
    next_candle = _candle(n_total, close=float(all_candles[-1].close) * 3)
    with CandleStore(config.paths.db_path) as candle_store:
        candle_store.upsert_candles([next_candle])

    responses.add(
        responses.GET, f"{PROD_HOST}/api/v3/time", json={"serverTime": next_candle.close_time_ms + 1}, status=200
    )
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/klines", json=[], status=200)
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/exchangeInfo", json=make_exchange_info(min_notional="0.01"), status=200)
    responses.add(responses.POST, telegram_url, json={"ok": True}, status=200)

    second = run_shadow_cycle(config)
    assert second.status == SHADOW_STATUS_OK
    with ShadowStore(config.paths.db_path) as store:
        trades = store.get_all_trades()
        notifications = store.list_notifications()
    assert len(trades) == 1
    entry_events = [n for n in notifications if n.event_type == "entry"]
    exit_events = [n for n in notifications if n.event_type == "exit"]
    assert len(entry_events) == 1  # NOT duplicated on the closing cycle
    assert len(exit_events) == 1


# --- Source-level regression lock: shadow/ can never reach order placement. ---


def test_shadow_package_never_imports_the_order_placement_adapter():
    shadow_dir = Path(__file__).resolve().parents[2] / "src" / "trading_agent" / "shadow"
    py_files = list(shadow_dir.glob("*.py"))
    assert py_files, "expected shadow/ source files to exist"
    for path in py_files:
        text = path.read_text()
        # Dotted module-path form only matches a real `import`/`from` statement -
        # the prose in these files' own docstrings refers to it with a slash
        # ("execution/testnet_adapter.py"), which this deliberately does not match.
        assert "trading_agent.execution.testnet_adapter" not in text, (
            f"{path} must never import execution/testnet_adapter.py"
        )
        assert "import testnet_adapter" not in text, f"{path} must never import execution/testnet_adapter.py"
