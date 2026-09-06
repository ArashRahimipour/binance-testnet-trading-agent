"""Proofs for shadow/engine.py: mode/interval guards, kill switch and lock
short-circuits (proven WITHOUT any network mock registered - `responses`
raises immediately if the code under test ever attempts a real request),
candle fetch/store/boundary wiring, the INSUFFICIENT_DATA warm-up guard,
and a full end-to-end cycle (via the real `run_segment`/E1 strategy, exactly
like `test_research_candidates_multitimeframe_breakout.py`'s own engine
test) proving idempotency and single-new-candle incremental processing.

Never connects to Binance - every HTTP call `shadow/engine.py` could make
is mocked via `responses`; a test that expects NO call registers none, so
an unexpected attempt fails loudly instead of silently reaching the network.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import responses
import yaml

from tests.fixtures.exchange_info import make_exchange_info
from tests.fixtures.klines import make_kline_row
from trading_agent.config.loader import load_config
from trading_agent.data.models import Candle, interval_to_ms
from trading_agent.data.storage import CandleStore
from trading_agent.research.candidates.multitimeframe_breakout import (
    MultiTimeframeBreakoutStrategy,
    _scan_setup_events,
)
from trading_agent.risk.kill_switch import KillSwitch
from trading_agent.shadow.boundary import SHADOW_START_BOUNDARY_MS
from trading_agent.shadow.engine import (
    SHADOW_STATUS_INSUFFICIENT_DATA,
    SHADOW_STATUS_KILL_SWITCH_ENGAGED,
    SHADOW_STATUS_NO_NEW_CANDLES,
    SHADOW_STATUS_OK,
    ShadowConfigError,
    run_shadow_cycle,
    shadow_kill_switch_path,
    shadow_lock_path,
)
from trading_agent.shadow.lock import ShadowLock, ShadowLockError
from trading_agent.shadow.store import ShadowStore

PROD_HOST = "https://api.binance.com"
INTERVAL = "1h"
STEP = interval_to_ms(INTERVAL)
_WEEK_MS = 7 * 24 * STEP
_MONDAY_EPOCH_MS = 4 * 24 * 3600 * 1000  # 1970-01-05T00:00:00Z, a Monday

# The smallest Monday-and-4h-aligned timestamp at or after the fixed shadow
# start boundary - `_bullish_candles` below needs Monday/4h grid alignment
# (see test_research_candidates_multitimeframe_breakout.py), and every
# candle this test file builds must also never violate the boundary.
_WEEKS_TO_BOUNDARY = -(-(SHADOW_START_BOUNDARY_MS - _MONDAY_EPOCH_MS) // _WEEK_MS)
START = _MONDAY_EPOCH_MS + _WEEKS_TO_BOUNDARY * _WEEK_MS
assert START >= SHADOW_START_BOUNDARY_MS

MIN_REQUIRED = MultiTimeframeBreakoutStrategy().min_required_candles


def _candle(i: int, close: float, start: int = START) -> Candle:
    open_ = close - 0.05
    return Candle(
        symbol="BTCUSDT", interval=INTERVAL, open_time_ms=start + i * STEP, close_time_ms=start + i * STEP + STEP - 1,
        open=Decimal(str(open_)), high=Decimal(str(close * 1.002)), low=Decimal(str(open_ * 0.998)),
        close=Decimal(str(close)), volume=Decimal(1),
    )


def _bullish_candles(n_hours: int, kick_at: int | None = None, kick_pct: float = 1.03) -> list[Candle]:
    closes: list[float] = []
    price = 100.0
    for i in range(n_hours):
        price += 0.02
        if kick_at is not None and i == kick_at:
            price *= kick_pct
        closes.append(price)
    return [_candle(i, c) for i, c in enumerate(closes)]


def _candle_to_kline_row(c: Candle) -> list:
    return [c.open_time_ms, str(c.open), str(c.high), str(c.low), str(c.close), str(c.volume), c.close_time_ms, "0", 0, "0", "0", "0"]


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


# --- Config guards (no I/O at all). ---


def test_requires_shadow_mode(tmp_path):
    config = load_config(overrides={"mode": "backtest"})
    with pytest.raises(ShadowConfigError):
        run_shadow_cycle(config)


def test_requires_1h_interval(tmp_path):
    bad = _shadow_config(tmp_path, market={"symbol": "BTCUSDT", "interval": "4h"})
    with pytest.raises(ShadowConfigError):
        run_shadow_cycle(bad)


# --- Kill switch / lock short-circuits: NO network mock registered. ---


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


# --- Fetch/store/boundary wiring + INSUFFICIENT_DATA guard. ---


@responses.activate
def test_first_run_floors_fetch_at_the_boundary_and_reports_insufficient_data(tmp_path):
    config = _shadow_config(tmp_path)
    rows = [make_kline_row(START + i * STEP, INTERVAL, close=100.0 + i) for i in range(5)]
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/time", json={"serverTime": rows[-1][6] + 1}, status=200)

    def _klines_callback(request):
        query = request.url
        assert f"startTime={SHADOW_START_BOUNDARY_MS}" in query
        return (200, {}, __import__("json").dumps(rows))

    responses.add_callback(responses.GET, f"{PROD_HOST}/api/v3/klines", callback=_klines_callback)

    result = run_shadow_cycle(config)

    assert result.status == SHADOW_STATUS_INSUFFICIENT_DATA
    assert result.new_candles_fetched == 5
    assert result.segment_length == 5
    assert result.min_required_candles == MIN_REQUIRED

    with CandleStore(config.paths.db_path) as store:
        stored = store.get_candles("BTCUSDT", INTERVAL)
    assert len(stored) == 5
    assert stored[0].open_time_ms == START


# --- Full end-to-end cycle: real E1 signal -> real run_segment -> persisted. ---


def _seed_candles_through_one_confirmed_entry() -> list[Candle]:
    candles = _bullish_candles(MIN_REQUIRED + 40, kick_at=MIN_REQUIRED + 5)
    events = _scan_setup_events(candles)
    setup = next(
        e for e in events if e.confirmation_index is not None and e.confirmation_index >= MIN_REQUIRED - 1
    )
    assert setup.confirmation_index is not None
    trading_end = min(setup.confirmation_index + 2, len(candles) - 1)
    return candles[: trading_end + 1]


@responses.activate
def test_full_cycle_persists_one_entry_and_is_idempotent_on_retry(tmp_path):
    seed_candles = _seed_candles_through_one_confirmed_entry()
    config = _shadow_config(tmp_path)
    with CandleStore(config.paths.db_path) as candle_store:
        candle_store.upsert_candles(seed_candles)

    responses.add(responses.GET, f"{PROD_HOST}/api/v3/time", json={"serverTime": seed_candles[-1].close_time_ms + 1}, status=200)
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/klines", json=[], status=200)
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/exchangeInfo", json=make_exchange_info(min_notional="0.01"), status=200)

    result = run_shadow_cycle(config)

    assert result.status == SHADOW_STATUS_OK
    assert result.new_candles_fetched == 0
    assert result.segment_length == len(seed_candles)

    with ShadowStore(config.paths.db_path) as store:
        state = store.get_run_state()
        assert state.last_processed_close_time_ms == seed_candles[-1].close_time_ms
        trades = store.get_all_trades()
        equity = store.get_equity_curve()
        # exactly one approved entry occurred - it either closed (1 trade) or is still open.
        assert (len(trades) == 1) != (state.open_position is not None)
        assert len(equity) == len(seed_candles) - (MIN_REQUIRED - 1)

    # --- Idempotent retry: no new candle -> short-circuits, no duplicate rows. ---
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/time", json={"serverTime": seed_candles[-1].close_time_ms + 1}, status=200)
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
    seed_candles = _seed_candles_through_one_confirmed_entry()
    config = _shadow_config(tmp_path)
    with CandleStore(config.paths.db_path) as candle_store:
        candle_store.upsert_candles(seed_candles)

    responses.add(responses.GET, f"{PROD_HOST}/api/v3/time", json={"serverTime": seed_candles[-1].close_time_ms + 1}, status=200)
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/klines", json=[], status=200)
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/exchangeInfo", json=make_exchange_info(min_notional="0.01"), status=200)
    first = run_shadow_cycle(config)
    assert first.status == SHADOW_STATUS_OK

    next_candle = _candle(len(seed_candles), float(seed_candles[-1].close) + 0.02)
    responses.add(
        responses.GET, f"{PROD_HOST}/api/v3/time", json={"serverTime": next_candle.close_time_ms + 1}, status=200
    )
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/klines", json=[_candle_to_kline_row(next_candle)], status=200)
    responses.add(responses.GET, f"{PROD_HOST}/api/v3/exchangeInfo", json=make_exchange_info(min_notional="0.01"), status=200)

    second = run_shadow_cycle(config)

    assert second.status == SHADOW_STATUS_OK
    assert second.new_candles_fetched == 1
    assert second.segment_length == len(seed_candles) + 1
    with ShadowStore(config.paths.db_path) as store:
        state = store.get_run_state()
        assert state.last_processed_close_time_ms == next_candle.close_time_ms
        equity = store.get_equity_curve()
        assert equity[-1].timestamp_ms == next_candle.close_time_ms


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
