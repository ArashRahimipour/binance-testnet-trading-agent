"""Proofs for shadow/bootstrap.py: warm-up sizing arithmetic, the actual
fetch/store/provenance round trip (using the real, unmodified E1
min_required_candles - no shortcuts), fail-closed behavior on a confirmed
gap or short history (nothing is ever written in either case), idempotent
re-running, and `verify_bootstrap_complete`'s real (not cached-flag)
re-derivation from the actual stored candles.

Never connects to Binance - `data/historical_fetch.py::fetch_historical_range`
(unmodified) is exercised against a stateful synthetic klines mock
(`tests/fixtures/klines.py::make_stateful_klines_callback`) that serves a
precomputed candle list, so this proves REAL pagination/gap-detection
behavior without a live network call.
"""

from __future__ import annotations

import responses
import yaml

from tests.fixtures.klines import make_kline_series, make_stateful_klines_callback
from trading_agent.config.loader import load_config
from trading_agent.data.models import interval_to_ms
from trading_agent.data.storage import CandleStore
from trading_agent.research.candidates.multitimeframe_breakout import (
    CONFIRMATION_WINDOW_1H_CANDLES,
    MultiTimeframeBreakoutStrategy,
)
from trading_agent.shadow.bootstrap import (
    BOOTSTRAP_STATUS_ALREADY_BOOTSTRAPPED,
    BOOTSTRAP_STATUS_GAP_IN_WARMUP_DATA,
    BOOTSTRAP_STATUS_OK,
    WARMUP_SAFETY_MARGIN_CANDLES,
    compute_effective_min_required_candles,
    compute_warmup_candle_count,
    run_shadow_bootstrap,
    verify_bootstrap_complete,
)
from trading_agent.shadow.boundary import SHADOW_START_BOUNDARY_MS, ShadowConfigError
from trading_agent.shadow.store import ShadowStore

PROD_HOST = "https://api.binance.com"
INTERVAL = "1h"
STEP = interval_to_ms(INTERVAL)

STRATEGY_MIN_REQUIRED = MultiTimeframeBreakoutStrategy().min_required_candles
WARMUP_CANDLE_COUNT = compute_warmup_candle_count(STRATEGY_MIN_REQUIRED)
EFFECTIVE_MIN_REQUIRED = compute_effective_min_required_candles(WARMUP_CANDLE_COUNT)
WARMUP_START_MS = SHADOW_START_BOUNDARY_MS - WARMUP_CANDLE_COUNT * STEP


def _shadow_config(tmp_path):
    config = {
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
    path = tmp_path / "shadow.yaml"
    path.write_text(yaml.safe_dump(config))
    return load_config(path)


def test_warmup_candle_count_and_effective_min_required_arithmetic():
    # warmup = E1's own bare minimum - 1 + the documented safety margin.
    assert WARMUP_CANDLE_COUNT == STRATEGY_MIN_REQUIRED - 1 + WARMUP_SAFETY_MARGIN_CANDLES
    # effective = warmup + E1's own confirmation window + 1 (the settling buffer).
    assert EFFECTIVE_MIN_REQUIRED == WARMUP_CANDLE_COUNT + CONFIRMATION_WINDOW_1H_CANDLES + 1


def test_bootstrap_requires_shadow_mode():
    config = load_config(overrides={"mode": "backtest"})
    try:
        run_shadow_bootstrap(config)
        raise AssertionError("expected ShadowConfigError")
    except ShadowConfigError:
        pass


def test_bootstrap_requires_1h_interval(tmp_path):
    config = _shadow_config(tmp_path)
    bad = config.model_copy(update={"market": config.market.model_copy(update={"interval": "4h"})})
    try:
        run_shadow_bootstrap(bad)
        raise AssertionError("expected ShadowConfigError")
    except ShadowConfigError:
        pass


@responses.activate
def test_bootstrap_fetches_and_stores_exact_warmup_range_with_provenance(tmp_path):
    config = _shadow_config(tmp_path)
    rows = make_kline_series(WARMUP_START_MS, INTERVAL, WARMUP_CANDLE_COUNT)
    responses.add_callback(responses.GET, f"{PROD_HOST}/api/v3/klines", callback=make_stateful_klines_callback(rows))

    result = run_shadow_bootstrap(config)

    assert result.status == BOOTSTRAP_STATUS_OK
    assert result.warmup_candle_count == WARMUP_CANDLE_COUNT
    assert result.warmup_start_time_ms == WARMUP_START_MS
    assert result.warmup_end_time_ms == SHADOW_START_BOUNDARY_MS - STEP
    assert result.effective_min_required_candles == EFFECTIVE_MIN_REQUIRED

    with CandleStore(config.paths.db_path) as candle_store:
        stored = candle_store.get_candles("BTCUSDT", INTERVAL)
    assert len(stored) == WARMUP_CANDLE_COUNT
    assert stored[0].open_time_ms == WARMUP_START_MS
    assert stored[-1].close_time_ms == SHADOW_START_BOUNDARY_MS - 1

    with ShadowStore(config.paths.db_path) as shadow_store:
        state = shadow_store.get_bootstrap_state()
        assert state is not None
        assert state.warmup_candle_count == WARMUP_CANDLE_COUNT
        assert state.effective_min_required_candles == EFFECTIVE_MIN_REQUIRED
        assert shadow_store.get_warmup_candle_count() == WARMUP_CANDLE_COUNT


@responses.activate
def test_bootstrap_second_call_reports_already_bootstrapped_and_makes_no_network_call(tmp_path):
    config = _shadow_config(tmp_path)
    rows = make_kline_series(WARMUP_START_MS, INTERVAL, WARMUP_CANDLE_COUNT)
    responses.add_callback(responses.GET, f"{PROD_HOST}/api/v3/klines", callback=make_stateful_klines_callback(rows))

    first = run_shadow_bootstrap(config)
    assert first.status == BOOTSTRAP_STATUS_OK
    calls_after_first = len(responses.calls)

    second = run_shadow_bootstrap(config)

    assert second.status == BOOTSTRAP_STATUS_ALREADY_BOOTSTRAPPED
    assert second.warmup_candle_count == WARMUP_CANDLE_COUNT
    assert len(responses.calls) == calls_after_first  # no new request at all


@responses.activate
def test_bootstrap_fails_closed_on_a_gap_and_stores_nothing(tmp_path):
    config = _shadow_config(tmp_path)
    rows = make_kline_series(WARMUP_START_MS, INTERVAL, WARMUP_CANDLE_COUNT)
    # Remove one candle from the middle of the range to create a genuine,
    # permanent gap (the narrow-recovery retry inside fetch_historical_range
    # will also come back empty for it, since it's really absent here too).
    del rows[WARMUP_CANDLE_COUNT // 2]
    responses.add_callback(responses.GET, f"{PROD_HOST}/api/v3/klines", callback=make_stateful_klines_callback(rows))

    result = run_shadow_bootstrap(config)

    assert result.status == BOOTSTRAP_STATUS_GAP_IN_WARMUP_DATA
    assert "confirmed gap" in result.detail

    with CandleStore(config.paths.db_path) as candle_store:
        assert candle_store.get_candles("BTCUSDT", INTERVAL) == []
    with ShadowStore(config.paths.db_path) as shadow_store:
        assert shadow_store.get_bootstrap_state() is None
        assert shadow_store.get_warmup_candle_count() == 0


@responses.activate
def test_verify_bootstrap_complete_fails_before_any_bootstrap(tmp_path):
    config = _shadow_config(tmp_path)
    with CandleStore(config.paths.db_path) as candle_store, ShadowStore(config.paths.db_path) as shadow_store:
        verification = verify_bootstrap_complete(config, candle_store, shadow_store)
    assert verification.ok is False
    assert verification.state is None
    assert "shadow-bootstrap" in verification.reason


@responses.activate
def test_verify_bootstrap_complete_passes_after_a_real_bootstrap(tmp_path):
    config = _shadow_config(tmp_path)
    rows = make_kline_series(WARMUP_START_MS, INTERVAL, WARMUP_CANDLE_COUNT)
    responses.add_callback(responses.GET, f"{PROD_HOST}/api/v3/klines", callback=make_stateful_klines_callback(rows))
    assert run_shadow_bootstrap(config).status == BOOTSTRAP_STATUS_OK

    with CandleStore(config.paths.db_path) as candle_store, ShadowStore(config.paths.db_path) as shadow_store:
        verification = verify_bootstrap_complete(config, candle_store, shadow_store)
    assert verification.ok is True
    assert verification.state is not None


@responses.activate
def test_verify_bootstrap_complete_fails_closed_if_warmup_candles_are_later_deleted(tmp_path):
    """Real re-derivation, not a cached flag: if the underlying candles
    are ever missing/corrupted after a successful bootstrap, verification
    must fail rather than trust the stored bootstrap-state row alone."""
    config = _shadow_config(tmp_path)
    rows = make_kline_series(WARMUP_START_MS, INTERVAL, WARMUP_CANDLE_COUNT)
    responses.add_callback(responses.GET, f"{PROD_HOST}/api/v3/klines", callback=make_stateful_klines_callback(rows))
    assert run_shadow_bootstrap(config).status == BOOTSTRAP_STATUS_OK

    with CandleStore(config.paths.db_path) as candle_store:
        candle_store._conn.execute(
            "DELETE FROM candles WHERE open_time_ms = ?", (WARMUP_START_MS,)
        )
        candle_store._conn.commit()

    with CandleStore(config.paths.db_path) as candle_store, ShadowStore(config.paths.db_path) as shadow_store:
        verification = verify_bootstrap_complete(config, candle_store, shadow_store)
    assert verification.ok is False
    assert "missing or corrupted" in verification.reason
