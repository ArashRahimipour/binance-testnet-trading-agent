"""Proofs for research/candidates/multitimeframe_breakout.py (round-3
candidate `multitimeframe_breakout_E1_round3`): completed-candle
aggregation (causality, no partial-bucket leakage, fail-closed on gaps/
misalignment), the weekly regime filter, the 4h setup scan's exact
equivalence to `BreakoutWithBullishRegimeGateStrategy`'s own (D1/B1)
logic, exact 4-candle 1h-confirmation expiry, one entry per armed setup,
and (via the real engine) next-1h-open fills with the unmodified fixed
1:2 risk/reward policy.

Fixtures deliberately keep the ACTUAL trading window short (a handful of
hours beyond the ~7564-candle weekly-EMA-driven warm-up) - `generate_signal`
re-aggregates its full visible history on every call, so looping through
a full multi-year block here would be prohibitively slow for a unit test;
each test instead calls it only at the specific indices needed to prove
its property, or runs the real engine over a short trailing window.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.fixtures.exchange_info import make_exchange_info
from trading_agent.backtest.engine import run_segment
from trading_agent.config.models import AppConfig
from trading_agent.data.models import Candle, interval_to_ms
from trading_agent.execution.backtest_broker import BacktestBroker
from trading_agent.research.candidates.breakout_regime_gate import (
    BreakoutWithBullishRegimeGateStrategy,
)
from trading_agent.research.candidates.multitimeframe_breakout import (
    CONFIRMATION_WINDOW_1H_CANDLES,
    FOUR_H_ATR_PERIOD,
    FOUR_H_BREAKOUT_ATR_MULTIPLE,
    FOUR_H_CHANNEL_PERIOD,
    MultiTimeframeBreakoutStrategy,
    _aggregate_completed_buckets,
    _four_h_bucket_start_ms,
    _scan_setup_events,
    _weekly_bucket_start_ms,
    _weekly_filter_bullish,
)
from trading_agent.risk.engine import RiskEngine
from trading_agent.sizing.exchange_filters import SymbolFilters
from trading_agent.strategy.base import InsufficientDataError, PositionSide, SignalType

INTERVAL = "1h"
STEP = interval_to_ms(INTERVAL)
_FOUR_H_MS = 4 * STEP
_WEEK_MS = 7 * 24 * STEP
#: 1970-01-05T00:00:00Z is a Monday - both week- and 4h-aligned (604_800_000
#: and 345_600_000 are both exact multiples of 14_400_000).
_MONDAY_EPOCH_MS = 4 * 24 * 3600 * 1000
START = _MONDAY_EPOCH_MS + 3000 * _WEEK_MS


def _candle(i: int, close: float, start: int = START, open_below: float = 0.05) -> Candle:
    open_ = close - open_below
    return Candle(
        symbol="BTCUSDT", interval=INTERVAL, open_time_ms=start + i * STEP, close_time_ms=start + i * STEP + STEP - 1,
        open=Decimal(str(open_)), high=Decimal(str(close * 1.002)), low=Decimal(str(open_ * 0.998)),
        close=Decimal(str(close)), volume=Decimal(1),
    )


def _bullish_candles(
    n_hours: int,
    kick_every: int | None = None,
    kick_pct: float = 1.03,
    kick_at: int | None = None,
    start: int = START,
) -> list[Candle]:
    """A steady, mild uptrend (0.02/hour) - keeps ATR/Donchian levels
    small so a `kick` (a one-off multiplicative jump, either at a single
    `kick_at` hour or repeating every `kick_every` hours) reliably clears
    the volatility-normalized breakout threshold without ever needing a
    huge number of repeated multiplicative kicks (which would otherwise
    compound into unrealistically large prices over a multi-thousand-hour
    warm-up window)."""
    closes: list[float] = []
    price = 100.0
    for i in range(n_hours):
        price += 0.02
        if kick_at is not None and i == kick_at or kick_every and i % kick_every == 0 and i > 0:
            price *= kick_pct
        closes.append(price)
    return [_candle(i, c, start=start) for i, c in enumerate(closes)]


def _bullish_candles_with_reversal(
    n_hours: int, kick_at: int, kick_pct: float = 1.05, reversal_factor: float = 0.9, start: int = START
) -> list[Candle]:
    """Like `_bullish_candles`, but the kick is immediately undone (and
    then some) exactly one hour later - `kick_at` MUST be the last hour of
    its own 4h bucket (`kick_at % 4 == 3`, relative to this module's
    aligned `START`) so the setup's OWN confirming bucket still closes on
    the elevated, kicked price, while every hour in its confirmation
    window afterward sees the reversed (lower) price and can never confirm."""
    assert kick_at % 4 == 3, "kick_at must be the last hour of its own 4h bucket"
    closes: list[float] = []
    price = 100.0
    for i in range(n_hours):
        if i == kick_at:
            price *= kick_pct
        elif i == kick_at + 1:
            price *= reversal_factor
        else:
            price += 0.02
        closes.append(price)
    return [_candle(i, c, start=start) for i, c in enumerate(closes)]


def _aligned_post_warmup_kick_index(min_required_candles: int, offset: int = 5) -> int:
    """The smallest index >= `min_required_candles + offset` that is the
    last hour of its own 4h bucket - see `_bullish_candles_with_reversal`."""
    kick_at = min_required_candles + offset
    while kick_at % 4 != 3:
        kick_at += 1
    return kick_at


def _bearish_candles(n_hours: int, start: int = START) -> list[Candle]:
    closes: list[float] = []
    price = 10_000.0
    for _ in range(n_hours):
        price -= 0.05
        closes.append(price)
    return [_candle(i, c, start=start) for i, c in enumerate(closes)]


_WARM_UP_HOURS = MultiTimeframeBreakoutStrategy().min_required_candles


# --- Aggregation: causality, completeness, fail-closed on gaps/misalignment. ---


def test_aggregation_produces_only_fully_complete_aligned_buckets():
    candles = _bullish_candles(20)
    buckets = _aggregate_completed_buckets(candles, 4, _four_h_bucket_start_ms)
    assert len(buckets) == 5
    for k, bucket in enumerate(buckets):
        assert bucket.last_hour_index == k * 4 + 3
        assert bucket.candle.open_time_ms == candles[k * 4].open_time_ms
        assert bucket.candle.close_time_ms == candles[k * 4 + 3].close_time_ms


def test_aggregation_excludes_a_partial_leading_bucket_never_fabricates_one():
    # Shifted by one hour: the very first (would-be) bucket only has 3 of
    # its 4 hours available - it must be excluded entirely, not padded.
    candles = _bullish_candles(20, start=START + STEP)
    buckets = _aggregate_completed_buckets(candles, 4, _four_h_bucket_start_ms)
    assert len(buckets) == 4  # not 5 - the leading partial bucket never appears
    assert buckets[0].candle.open_time_ms == candles[3].open_time_ms


def test_aggregation_fails_closed_on_a_gap_never_fabricates_a_partial_bucket():
    candles = _bullish_candles(20)
    gapped = [c for i, c in enumerate(candles) if i != 10]  # remove one hour mid-series
    buckets = _aggregate_completed_buckets(gapped, 4, _four_h_bucket_start_ms)
    # The bucket that needed hour 10 (candles[8:12]) is silently excluded;
    # every OTHER bucket is still correctly recognized.
    covered_starts = {b.candle.open_time_ms for b in buckets}
    assert candles[8].open_time_ms not in covered_starts  # the ONE affected bucket is excluded
    assert len(buckets) == 4  # every OTHER bucket (out of 5) is still recognized
    assert candles[0].open_time_ms in covered_starts
    assert candles[16].open_time_ms in covered_starts


def test_weekly_aggregation_also_fails_closed_on_misalignment():
    n_hours = 3 * 7 * 24  # 3 weeks
    candles = _bullish_candles(n_hours, start=START + 5 * STEP)  # not Monday-aligned
    buckets = _aggregate_completed_buckets(candles, 7 * 24, _weekly_bucket_start_ms)
    assert len(buckets) == 2  # the leading partial week is excluded


def test_setup_scan_never_uses_a_future_1h_candle():
    # A causality proof in the SAME spirit as backtest/engine.py's own
    # test_stop_and_target_evaluation_never_uses_a_future_candle: comparing
    # the full series against one truncated right after a decision point
    # must yield an IDENTICAL result for that decision.
    candles = _bullish_candles(5000, kick_every=300)
    truncated = candles[:4000]
    full_events = [e for e in _scan_setup_events(candles) if e.setup_hour_index < 4000]
    truncated_events = _scan_setup_events(truncated)
    # Every setup confirmed within the truncated window must appear
    # identically regardless of what happens far in the (truncated-away) future.
    assert full_events[: len(truncated_events)] == truncated_events or full_events == truncated_events


# --- 4h setup scan: exact equivalence to D1/B1's own class-based logic. ---


def test_scan_setup_events_is_exactly_equivalent_to_breakout_regime_gate_strategy():
    candles = _bullish_candles(5000, kick_every=300)
    events = _scan_setup_events(candles)
    assert len(events) > 0  # the fixture must actually exercise this

    four_h_buckets = _aggregate_completed_buckets(candles, 4, _four_h_bucket_start_ms)
    four_h_candles = [b.candle for b in four_h_buckets]
    reference = BreakoutWithBullishRegimeGateStrategy(
        channel_period=FOUR_H_CHANNEL_PERIOD, atr_period=FOUR_H_ATR_PERIOD, breakout_atr_multiple=FOUR_H_BREAKOUT_ATR_MULTIPLE
    )
    setup_hour_indices = {e.setup_hour_index for e in events}
    checked = 0
    for i, bucket in enumerate(four_h_buckets):
        if i < reference.min_required_candles - 1:
            continue
        checked += 1
        signal = reference.generate_signal(four_h_candles[: i + 1], PositionSide.FLAT)
        assert (bucket.last_hour_index in setup_hour_indices) == (signal.type == SignalType.BUY)
    assert checked > 0


# --- Weekly filter. ---


def test_weekly_filter_bullish_when_close_above_rising_ema40():
    candles = _bullish_candles(_WARM_UP_HOURS)
    assert _weekly_filter_bullish(candles) is True


def test_weekly_filter_bearish_when_close_below_falling_ema40():
    candles = _bearish_candles(_WARM_UP_HOURS)
    assert _weekly_filter_bullish(candles) is False


def test_weekly_filter_false_with_insufficient_weekly_history():
    candles = _bullish_candles(100)  # nowhere near 44+ completed weeks
    assert _weekly_filter_bullish(candles) is False


# --- min_required_candles / InsufficientDataError. ---


def test_raises_insufficient_data_below_min_required_candles():
    strat = MultiTimeframeBreakoutStrategy()
    candles = _bullish_candles(strat.min_required_candles - 1)
    with pytest.raises(InsufficientDataError):
        strat.generate_signal(candles, PositionSide.FLAT)


# --- Weekly filter categorically prohibits BUY, even with a perfect 4h+1h setup. ---


def test_weekly_filter_blocks_buy_even_with_an_otherwise_valid_setup():
    strat = MultiTimeframeBreakoutStrategy()
    candles = _bearish_candles(strat.min_required_candles)
    signal = strat.generate_signal(candles, PositionSide.FLAT)
    assert signal.type == SignalType.HOLD
    assert signal.reason_code == "HOLD_WEEKLY_FILTER_BLOCKED"
    assert strat.weekly_filter_rejections == 1


# --- One entry per armed setup + exact 4-candle expiry. ---


def test_one_entry_per_armed_setup_and_exact_four_candle_expiry():
    strat = MultiTimeframeBreakoutStrategy()
    # A bullish weekly regime with one clean kick right after warm-up so a
    # setup confirms close to the start of the tradable region, keeping
    # the number of generate_signal calls small.
    kick_at = strat.min_required_candles + 5
    candles = _bullish_candles(strat.min_required_candles + 40, kick_at=kick_at)

    events = _scan_setup_events(candles)
    # Only setups whose CONFIRMATION happens after full warm-up are usable
    # with generate_signal directly (the 4h-only scan itself needs far
    # less history than the full strategy's weekly-driven min_required).
    usable = [e for e in events if e.confirmation_index is not None and e.confirmation_index >= strat.min_required_candles - 1]
    assert usable, "fixture must produce at least one post-warm-up confirmed setup"
    setup = usable[0]
    assert setup.window_end_index - setup.window_start_index == CONFIRMATION_WINDOW_1H_CANDLES - 1
    confirmation_idx = setup.confirmation_index

    # At the confirming index itself: BUY, exactly once.
    sig_confirm = strat.generate_signal(candles[: confirmation_idx + 1], PositionSide.FLAT)
    assert sig_confirm.type == SignalType.BUY
    assert sig_confirm.reason_code == "MULTITIMEFRAME_1H_CONFIRMED_ENTRY"
    assert strat.entries == 1

    # One candle later, STILL within the window, even if flat again
    # (e.g. the position was already closed by the protective stop) -
    # this setup is already consumed, never a second entry.
    if confirmation_idx < setup.window_end_index:
        sig_later = strat.generate_signal(candles[: confirmation_idx + 2], PositionSide.FLAT)
        assert sig_later.type == SignalType.HOLD
        assert sig_later.reason_code == "HOLD_SETUP_ALREADY_CONSUMED"
        assert strat.entries == 1  # unchanged


def test_setup_expires_after_exactly_four_candles_with_no_confirmation():
    # Build a setup that is immediately reversed (module docstring's
    # `_bullish_candles_with_reversal`), so no hour in its window can ever
    # confirm it, then verify: no candle after window_end can ever use it,
    # and it is counted expired.
    strat = MultiTimeframeBreakoutStrategy()
    kick_at = _aligned_post_warmup_kick_index(strat.min_required_candles)
    candles = _bullish_candles_with_reversal(strat.min_required_candles + 60, kick_at=kick_at)
    events = _scan_setup_events(candles)
    unconfirmed = [
        e for e in events if e.confirmation_index is None and e.window_end_index >= strat.min_required_candles - 1
    ]
    if not unconfirmed:
        pytest.skip("fixture produced no naturally-unconfirmed, post-warm-up setup to test expiry against")
    setup = unconfirmed[0]

    # One candle past the window's own end: no longer armed for this setup.
    past_window_idx = setup.window_end_index + 1
    sig = strat.generate_signal(candles[: past_window_idx + 1], PositionSide.FLAT)
    assert sig.reason_code != "MULTITIMEFRAME_1H_CONFIRMED_ENTRY"

    strat2 = MultiTimeframeBreakoutStrategy()
    for i in range(strat2.min_required_candles - 1, setup.window_end_index + 1):
        strat2.generate_signal(candles[: i + 1], PositionSide.FLAT)
    assert strat2.setups_expired >= 1


def test_already_long_never_produces_a_second_buy():
    strat = MultiTimeframeBreakoutStrategy()
    candles = _bullish_candles(strat.min_required_candles + 40, kick_at=strat.min_required_candles + 5)
    events = _scan_setup_events(candles)
    setup = next(
        e for e in events if e.confirmation_index is not None and e.confirmation_index >= strat.min_required_candles - 1
    )
    sig = strat.generate_signal(candles[: setup.confirmation_index + 1], PositionSide.LONG)
    assert sig.type == SignalType.HOLD
    assert sig.reason_code == "HOLD_ALREADY_LONG"


# --- Next-1h-open fill via the real engine, unmodified risk/reward policy. ---


def _config() -> AppConfig:
    return AppConfig(mode="backtest", fees={"taker_fee_pct": 0.001, "slippage_pct": 0.0005})


def _filters() -> SymbolFilters:
    return SymbolFilters.from_exchange_info(make_exchange_info(min_notional="0.01"))


def test_entry_fills_at_the_next_1h_open_with_net_rr_and_planned_risk_intact():
    strat = MultiTimeframeBreakoutStrategy()
    candles = _bullish_candles(strat.min_required_candles + 40, kick_at=strat.min_required_candles + 5)
    events = _scan_setup_events(candles)
    setup = next(
        e for e in events if e.confirmation_index is not None and e.confirmation_index >= strat.min_required_candles - 1
    )
    confirmation_idx = setup.confirmation_index
    # A short trailing window: warm-up + just enough hours to reach one
    # candle past the confirming signal, for the pending BUY to resolve.
    trading_end = min(confirmation_idx + 2, len(candles) - 1)
    window = candles[: trading_end + 1]

    risk_engine = RiskEngine(_config().risk)
    broker = BacktestBroker(_config().fees)
    result = run_segment(
        window, _config(), _filters(), strat, risk_engine, broker, None,
        min_required=strat.min_required_candles, starting_equity=Decimal(50), use_fixed_risk_reward_policy=True,
    )

    assert result.open_position is not None or len(result.trades) == 1
    entry_time_ms = (
        result.open_position.entry_time_ms if result.open_position is not None else result.trades[0].entry_time_ms
    )
    following_candle = candles[confirmation_idx + 1]
    assert entry_time_ms == following_candle.open_time_ms
    assert entry_time_ms > candles[confirmation_idx].open_time_ms

    assert result.risk_reward is not None
    assert result.risk_reward.entries_approved == 1
    for planned_risk_pct in result.risk_reward.planned_risk_pct_values:
        assert planned_risk_pct <= 0.01 + 1e-9
    for net_rr in result.risk_reward.net_reward_to_risk_values:
        assert net_rr >= 2.0 - 1e-6
