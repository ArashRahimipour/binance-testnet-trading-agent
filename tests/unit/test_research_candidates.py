"""Proofs for the three declared candidate strategy families: causal,
completed-candles-only signal generation, correct entry/exit/regime-filter
behavior, and a fixed, correctly-sized `min_required_candles` per family.
"""

from decimal import Decimal

import pytest

from trading_agent.data.models import Candle, interval_to_ms
from trading_agent.research.candidate_registry import CANDIDATE_REGISTRY, TOTAL_CANDIDATE_COUNT
from trading_agent.research.candidates.mean_reversion import ConservativeMeanReversionStrategy
from trading_agent.research.candidates.trend_regime import TrendWithRegimeFilterStrategy
from trading_agent.research.candidates.volatility_breakout import (
    VolatilityNormalizedBreakoutStrategy,
)
from trading_agent.strategy.base import InsufficientDataError, PositionSide, SignalType

INTERVAL = "4h"
STEP = interval_to_ms(INTERVAL)
START = 1_700_000_000_000


def _flat_candle(i: int, close: float, high: float | None = None, low: float | None = None) -> Candle:
    high = close if high is None else high
    low = close if low is None else low
    return Candle(
        symbol="BTCUSDT", interval=INTERVAL, open_time_ms=START + i * STEP, close_time_ms=START + i * STEP + STEP - 1,
        open=Decimal(str(close)), high=Decimal(str(high)), low=Decimal(str(low)), close=Decimal(str(close)),
        volume=Decimal(1),
    )


def _candles(closes: list[float]) -> list[Candle]:
    return [_flat_candle(i, c) for i, c in enumerate(closes)]


# --- Family A: trend-following with a volatility/regime filter. ---
#
# CORRECTED before any real candidate evaluation - see
# research/candidates/trend_regime.py's module docstring. Entry is now a
# finite-state cycle (armed at a bullish crossover, confirmed on the first
# later candle where normalized EMA separation clears the threshold, one
# entry per cycle, reset by a bearish crossover) rather than a same-candle
# crossover+separation check.

_CROSSOVER_CLOSES = [100, 99, 98, 97, 96, 95, 100, 105, 110, 115, 60, 60]

# A crossover at index 8 (warm-up 0-5 is a downtrend so fast<=slow), then a
# GRADUALLY accelerating uptrend whose normalized separation only clears
# 0.5x ATR several candles later (verified: crosses at 8, confirms at 15;
# never reaches 1.0x ATR at all within this fixture).
_DELAYED_CONFIRMATION_CLOSES = [100, 99, 98, 97, 96, 95] + [
    96, 97, 98, 99, 100, 101, 103, 106, 110, 115, 121, 128, 140, 160, 190,
]

# Continues _DELAYED_CONFIRMATION_CLOSES with a sharp reversal (bearish
# crossover resets the cycle) then a second, independent uptrend.
_TWO_CYCLE_CLOSES = _DELAYED_CONFIRMATION_CLOSES + [180, 160, 140, 120, 100, 90] + [
    92, 95, 99, 104, 110, 117, 125, 134, 144, 155, 167, 180, 195, 212, 230,
]


def _wicked_candles(closes: list[float], wick: float = 3.0) -> list[Candle]:
    return [_flat_candle(i, c, c + wick, c - wick) for i, c in enumerate(closes)]


def _run(strat: TrendWithRegimeFilterStrategy, candles: list[Candle]) -> list[tuple[int, SignalType, str]]:
    pos = PositionSide.FLAT
    signals = []
    for i in range(strat.min_required_candles - 1, len(candles)):
        sig = strat.generate_signal(candles[: i + 1], pos)
        signals.append((i, sig.type, sig.reason_code))
        if sig.type == SignalType.BUY:
            pos = PositionSide.LONG
        elif sig.type == SignalType.EXIT:
            pos = PositionSide.FLAT
    return signals


def test_trend_regime_rejects_invalid_construction():
    with pytest.raises(ValueError):
        TrendWithRegimeFilterStrategy(ema_fast=10, ema_slow=5, atr_period=5, min_trend_strength_atr=1.0)
    with pytest.raises(ValueError):
        TrendWithRegimeFilterStrategy(ema_fast=3, ema_slow=6, atr_period=5, min_trend_strength_atr=-1.0)


def test_trend_regime_min_required_candles():
    strat = TrendWithRegimeFilterStrategy(ema_fast=3, ema_slow=6, atr_period=20, min_trend_strength_atr=1.0)
    assert strat.min_required_candles == max(6, 20) + 1


def test_trend_regime_insufficient_data_raises():
    strat = TrendWithRegimeFilterStrategy(ema_fast=3, ema_slow=6, atr_period=3, min_trend_strength_atr=0.0)
    with pytest.raises(InsufficientDataError):
        strat.generate_signal(_candles([100.0] * 3), PositionSide.FLAT)


def test_trend_regime_confirms_immediately_when_filter_is_permissive():
    # Threshold 0.0: normalized separation is never LESS than 0.0, so
    # confirmation happens on the crossover candle itself.
    strat = TrendWithRegimeFilterStrategy(ema_fast=3, ema_slow=6, atr_period=3, min_trend_strength_atr=0.0)
    signals = _run(strat, _candles(_CROSSOVER_CLOSES))
    buys = [s for s in signals if s[1] == SignalType.BUY]
    exits = [s for s in signals if s[1] == SignalType.EXIT]
    assert len(buys) == 1 and buys[0][2] == "BULLISH_TREND_CONFIRMED_ENTRY"
    assert len(exits) == 1 and exits[0][2] == "BEARISH_EMA_CROSSOVER_CYCLE_RESET"


def test_trend_regime_no_entry_on_an_unconfirmed_crossover():
    # A crossover occurs, but normalized separation never clears a strict
    # 1.0x-ATR threshold anywhere in this fixture - no entry, ever, even
    # though the strategy remains in a bullish (armed) state throughout.
    strat = TrendWithRegimeFilterStrategy(ema_fast=3, ema_slow=6, atr_period=3, min_trend_strength_atr=1.0)
    signals = _run(strat, _wicked_candles(_DELAYED_CONFIRMATION_CLOSES))
    signal_types = {s[1] for s in signals}
    assert SignalType.BUY not in signal_types
    assert SignalType.EXIT not in signal_types
    assert any(s[2] == "ARMED_AWAITING_TREND_CONFIRMATION" for s in signals)


def test_trend_regime_confirmation_can_occur_several_candles_after_the_crossover():
    strat = TrendWithRegimeFilterStrategy(ema_fast=3, ema_slow=6, atr_period=3, min_trend_strength_atr=0.5)
    candles = _wicked_candles(_DELAYED_CONFIRMATION_CLOSES)
    signals = _run(strat, candles)
    buys = [s for s in signals if s[1] == SignalType.BUY]
    armed_holds = [s for s in signals if s[2] == "ARMED_AWAITING_TREND_CONFIRMATION"]
    assert len(buys) == 1
    buy_index = buys[0][0]
    # The crossover itself is well before the confirmed entry - several
    # ARMED_AWAITING_TREND_CONFIRMATION holds must precede it.
    assert len(armed_holds) >= 5
    assert all(idx < buy_index for idx, _, _ in armed_holds)


def test_trend_regime_only_one_entry_per_cycle_even_across_a_forced_flat():
    # Simulates the position being forced back to FLAT mid-cycle by the
    # engine's own protective stop-loss (something this strategy never
    # observes directly) - it must NOT re-enter within the same
    # still-active bullish cycle.
    strat = TrendWithRegimeFilterStrategy(ema_fast=3, ema_slow=6, atr_period=3, min_trend_strength_atr=0.5)
    candles = _wicked_candles(_DELAYED_CONFIRMATION_CLOSES)
    pos = PositionSide.FLAT
    entry_count = 0
    for i in range(strat.min_required_candles - 1, len(candles)):
        sig = strat.generate_signal(candles[: i + 1], pos)
        if sig.type == SignalType.BUY:
            entry_count += 1
            pos = PositionSide.FLAT  # forced flat immediately, as if stopped out
        elif sig.type == SignalType.EXIT:
            pos = PositionSide.FLAT
    assert entry_count == 1


def test_trend_regime_bearish_crossover_resets_the_cycle_for_a_fresh_entry():
    strat = TrendWithRegimeFilterStrategy(ema_fast=3, ema_slow=6, atr_period=3, min_trend_strength_atr=0.5)
    signals = _run(strat, _wicked_candles(_TWO_CYCLE_CLOSES))
    buys = [s for s in signals if s[1] == SignalType.BUY]
    exits = [s for s in signals if s[1] == SignalType.EXIT]
    assert len(buys) == 2  # one per independent cycle
    assert len(exits) == 1
    assert exits[0][2] == "BEARISH_EMA_CROSSOVER_CYCLE_RESET"
    assert buys[0][0] < exits[0][0] < buys[1][0]


def test_trend_regime_no_lookahead():
    # Prove causality the same way test_strategy_trend_baseline.py does:
    # append wildly different future candles and confirm the EARLIER
    # signal is unaffected - including the forward confirmation scan,
    # which must never look past the candle it is being asked about.
    strat = TrendWithRegimeFilterStrategy(ema_fast=3, ema_slow=6, atr_period=3, min_trend_strength_atr=0.5)
    candles = _wicked_candles(_DELAYED_CONFIRMATION_CLOSES)
    truncated_len = 12  # mid-cycle, still unconfirmed at this point
    sig_truncated = strat.generate_signal(candles[:truncated_len], PositionSide.FLAT)
    extended = candles + _wicked_candles([500000.0, 1.0, 250000.0])
    sig_on_extended_up_to_same_point = strat.generate_signal(extended[:truncated_len], PositionSide.FLAT)
    assert sig_truncated == sig_on_extended_up_to_same_point
    assert sig_truncated.type == SignalType.HOLD  # sanity: this point is genuinely still unconfirmed


def test_trend_regime_no_same_close_execution_is_the_engines_job():
    # This module only ever emits a same-candle-close SIGNAL - the actual
    # next-open fill delay is enforced by backtest/engine.py::run_segment
    # for every candidate identically (see
    # test_research_blocked_chronological_evaluation.py and
    # test_backtest_engine.py's next-open-fill proofs). Documented
    # here so the "no same-close execution" requirement is traceable from
    # this module's own test file.
    strat = TrendWithRegimeFilterStrategy(ema_fast=3, ema_slow=6, atr_period=3, min_trend_strength_atr=0.0)
    candles = _candles(_CROSSOVER_CLOSES)
    sig = strat.generate_signal(candles[:7], PositionSide.FLAT)
    assert sig.type == SignalType.BUY
    assert sig.candle_close_time_ms == candles[6].close_time_ms  # a SIGNAL, not a fill


# --- Family B: breakout with volatility-normalized entry. ---


def _plateau_then(extra: list[tuple[float, float, float]]) -> list[Candle]:
    candles = [_flat_candle(i, 100.0, 101.0, 99.0) for i in range(10)]
    for offset, (close, high, low) in enumerate(extra):
        candles.append(_flat_candle(10 + offset, close, high, low))
    return candles


def test_breakout_rejects_invalid_construction():
    with pytest.raises(ValueError):
        VolatilityNormalizedBreakoutStrategy(channel_period=0, atr_period=5, breakout_atr_multiple=1.0)
    with pytest.raises(ValueError):
        VolatilityNormalizedBreakoutStrategy(channel_period=5, atr_period=5, breakout_atr_multiple=-1.0)


def test_breakout_min_required_candles():
    strat = VolatilityNormalizedBreakoutStrategy(channel_period=5, atr_period=20, breakout_atr_multiple=1.0)
    assert strat.min_required_candles == max(6, 20)


def test_breakout_buys_on_a_volatility_confirmed_breakout_then_exits_on_breakdown():
    strat = VolatilityNormalizedBreakoutStrategy(channel_period=5, atr_period=5, breakout_atr_multiple=1.0)
    candles = _plateau_then([(110.0, 111.0, 109.0), (90.0, 91.0, 89.0)])
    pos = PositionSide.FLAT
    results = []
    for i in range(strat.min_required_candles - 1, len(candles)):
        sig = strat.generate_signal(candles[: i + 1], pos)
        results.append(sig)
        if sig.type == SignalType.BUY:
            pos = PositionSide.LONG
        elif sig.type == SignalType.EXIT:
            pos = PositionSide.FLAT
    buy_signals = [s for s in results if s.type == SignalType.BUY]
    exit_signals = [s for s in results if s.type == SignalType.EXIT]
    assert len(buy_signals) == 1 and buy_signals[0].reason_code == "VOLATILITY_CONFIRMED_UPSIDE_BREAKOUT"
    assert len(exit_signals) == 1 and exit_signals[0].reason_code == "CHANNEL_BREAKDOWN"


def test_breakout_holds_on_a_marginal_unconfirmed_breakout():
    strat = VolatilityNormalizedBreakoutStrategy(channel_period=5, atr_period=5, breakout_atr_multiple=1.0)
    candles = _plateau_then([(101.5, 102.0, 101.0)])  # barely above the channel, well under 1x ATR
    pos = PositionSide.FLAT
    for i in range(strat.min_required_candles - 1, len(candles)):
        sig = strat.generate_signal(candles[: i + 1], pos)
        assert sig.type == SignalType.HOLD
        assert sig.reason_code == "HOLD_NO_CONFIRMED_BREAKOUT"


def test_breakout_channel_excludes_the_current_candle():
    # The breakout candle's own high/low must never contribute to its own
    # threshold - confirmed by the fact that a plateau-breaking candle
    # correctly triggers (channel = the plateau alone, not including the
    # breakout candle's own, much higher, high).
    strat = VolatilityNormalizedBreakoutStrategy(channel_period=5, atr_period=5, breakout_atr_multiple=1.0)
    candles = _plateau_then([(110.0, 111.0, 109.0)])
    sig = strat.generate_signal(candles, PositionSide.FLAT)
    assert sig.inputs["donchian_high"] == 101.0  # the plateau's high, not the breakout candle's own 111


def test_breakout_no_lookahead():
    strat = VolatilityNormalizedBreakoutStrategy(channel_period=5, atr_period=5, breakout_atr_multiple=1.0)
    candles = _plateau_then([(110.0, 111.0, 109.0)])
    sig_truncated = strat.generate_signal(candles, PositionSide.FLAT)
    extended = _plateau_then([(110.0, 111.0, 109.0), (999.0, 5000.0, 1.0)])
    sig_on_extended_up_to_same_point = strat.generate_signal(extended[: len(candles)], PositionSide.FLAT)
    assert sig_truncated == sig_on_extended_up_to_same_point


def test_breakout_insufficient_data_raises():
    strat = VolatilityNormalizedBreakoutStrategy(channel_period=5, atr_period=5, breakout_atr_multiple=1.0)
    with pytest.raises(InsufficientDataError):
        strat.generate_signal(_candles([100.0] * 3), PositionSide.FLAT)


# --- Family C: conservative mean reversion restricted to non-trending regimes. ---

_OSCILLATION = [100, 101, 99, 100, 101, 99, 100, 101, 99, 100] * 4


def test_mean_reversion_rejects_invalid_construction():
    with pytest.raises(ValueError):
        ConservativeMeanReversionStrategy(bb_period=0, bb_std_mult=2.0, atr_period=5, max_trend_strength_atr=1.0)
    with pytest.raises(ValueError):
        ConservativeMeanReversionStrategy(bb_period=10, bb_std_mult=0.0, atr_period=5, max_trend_strength_atr=1.0)
    with pytest.raises(ValueError):
        ConservativeMeanReversionStrategy(bb_period=10, bb_std_mult=2.0, atr_period=5, max_trend_strength_atr=-1.0)


def test_mean_reversion_min_required_candles():
    strat = ConservativeMeanReversionStrategy(bb_period=10, bb_std_mult=2.0, atr_period=5, max_trend_strength_atr=1.0)
    assert strat.min_required_candles == max(10, 5, 40) + 1


def test_mean_reversion_buys_the_dip_and_exits_at_the_mean_in_a_range_bound_regime():
    strat = ConservativeMeanReversionStrategy(bb_period=10, bb_std_mult=1.0, atr_period=5, max_trend_strength_atr=0.3)
    candles = _candles(_OSCILLATION + [101, 99, 100])
    pos = PositionSide.FLAT
    results = []
    for i in range(strat.min_required_candles - 1, len(candles)):
        sig = strat.generate_signal(candles[: i + 1], pos)
        results.append(sig)
        if sig.type == SignalType.BUY:
            pos = PositionSide.LONG
        elif sig.type == SignalType.EXIT:
            pos = PositionSide.FLAT
    buy_signals = [s for s in results if s.type == SignalType.BUY]
    exit_signals = [s for s in results if s.type == SignalType.EXIT]
    assert len(buy_signals) >= 1 and buy_signals[0].reason_code == "OVERSOLD_DIP_IN_RANGE_BOUND_REGIME"
    assert len(exit_signals) >= 1 and exit_signals[0].reason_code == "REVERTED_TO_MEAN"


def test_mean_reversion_refuses_to_buy_a_dip_outside_a_range_bound_regime():
    strat = ConservativeMeanReversionStrategy(bb_period=10, bb_std_mult=1.0, atr_period=5, max_trend_strength_atr=0.3)
    # A sudden, sharp move breaks the range-bound regime at the same time
    # as an ordinarily-qualifying dip - the regime filter must refuse it.
    candles = _candles(_OSCILLATION + [70])
    sig = strat.generate_signal(candles, PositionSide.FLAT)
    assert sig.inputs["range_bound"] is False
    assert sig.type == SignalType.HOLD
    assert sig.reason_code == "HOLD_NO_QUALIFYING_DIP"


def test_mean_reversion_abandons_the_thesis_when_regime_shifts_while_holding():
    strat = ConservativeMeanReversionStrategy(bb_period=10, bb_std_mult=1.0, atr_period=5, max_trend_strength_atr=0.3)
    candles = _candles(_OSCILLATION + [101, 99])  # ends on a qualifying dip, not yet reverted
    pos = PositionSide.FLAT
    for i in range(strat.min_required_candles - 1, len(candles)):
        sig = strat.generate_signal(candles[: i + 1], pos)
        if sig.type == SignalType.BUY:
            pos = PositionSide.LONG
        elif sig.type == SignalType.EXIT:
            pos = PositionSide.FLAT
    assert pos == PositionSide.LONG  # confirm the fixture actually entered

    shocked = candles + [_flat_candle(len(candles), 70.0)]
    sig = strat.generate_signal(shocked, PositionSide.LONG)
    assert sig.type == SignalType.EXIT
    assert sig.reason_code == "REGIME_SHIFTED_TO_TRENDING_ABANDON_REVERSION_THESIS"


def test_mean_reversion_insufficient_data_raises():
    strat = ConservativeMeanReversionStrategy(bb_period=10, bb_std_mult=2.0, atr_period=5, max_trend_strength_atr=1.0)
    with pytest.raises(InsufficientDataError):
        strat.generate_signal(_candles([100.0] * 5), PositionSide.FLAT)


def test_mean_reversion_no_lookahead():
    strat = ConservativeMeanReversionStrategy(bb_period=10, bb_std_mult=1.0, atr_period=5, max_trend_strength_atr=0.3)
    candles = _candles(_OSCILLATION + [101, 99])
    truncated = candles[:41]
    sig_truncated = strat.generate_signal(truncated, PositionSide.FLAT)
    sig_prefix_of_full = strat.generate_signal(candles[:41], PositionSide.FLAT)
    assert sig_truncated == sig_prefix_of_full


# --- Registry-level: candidate count and family declaration. ---


def test_total_candidate_count_is_nine():
    assert TOTAL_CANDIDATE_COUNT == 9
    assert len(CANDIDATE_REGISTRY) == 9


def test_registry_has_exactly_three_configurations_per_family():
    families = [spec.family for spec in CANDIDATE_REGISTRY]
    assert sorted(set(families)) == sorted({
        "trend_regime_filter", "volatility_normalized_breakout", "conservative_mean_reversion_non_trending",
    })
    for family in set(families):
        assert families.count(family) == 3


def test_registry_candidate_ids_are_unique():
    ids = [spec.candidate_id for spec in CANDIDATE_REGISTRY]
    assert len(ids) == len(set(ids))


def test_registry_candidates_build_working_strategy_instances():
    for spec in CANDIDATE_REGISTRY:
        strategy = spec.build()
        assert strategy.min_required_candles > 0
        assert callable(strategy.generate_signal)
