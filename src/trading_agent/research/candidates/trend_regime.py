"""Candidate family A: trend-following with a volatility/regime filter.

CORRECTED before any real candidate evaluation (see the "pre-real-evaluation
logic correction" note below) - this replaces a same-candle rule that
required both a bullish EMA crossover AND the EMA separation exceeding an
ATR multiple ON THAT SAME CANDLE. That rule was structurally over-filtered:
EMA separation is close to zero exactly at the moment of a crossover almost
by definition (the two lines have just touched), so requiring a large
separation on the crossover candle itself made confirmation rare or
impossible depending on the threshold, for reasons having nothing to do
with whether a real trend later developed.

The corrected rule is a finite-state, CAUSAL cycle built purely from price
history (no persisted mutable state on the strategy object - every call
recomputes the cycle from `candles` alone, exactly like every other
strategy in this codebase, and exactly what keeps this safe to reuse
across independent chronological evaluation blocks without leaking state):

  1. NEUTRAL/BEARISH state: `fast EMA <= slow EMA`. No cycle is active.
     A bearish crossover here (from a prior bullish state) is the EXIT
     signal (if currently long) - this is what "resets the cycle": once
     seen, the entire prior cycle is over and a brand new one can only
     begin at a future bullish crossover.
  2. ARMED: a bullish crossover has just occurred (`fast EMA` crosses from
     at/below to above `slow EMA`) and the bullish trend state
     (`fast EMA > slow EMA`) has held continuously since. The cycle is
     "armed" but not yet confirmed.
  3. CONFIRMED (may happen on the SAME candle as the crossover or, far
     more commonly, several candles later): the FIRST candle, since the
     armed cycle began, where the normalized EMA separation
     (`|fast-slow| / ATR`) reaches the declared `min_trend_strength_atr`
     threshold. This is the ONLY candle in the entire cycle that may
     produce a BUY - found by scanning forward from the crossover, not by
     re-checking the current candle in isolation, which is what makes
     confirmation "may occur on a later candle" rather than requiring it
     on the crossover candle itself.
  4. ENTERED: once the confirmation candle has been identified (whether or
     not the position is still open - a position can be closed mid-cycle
     by the protective stop-loss, which this strategy never sees directly),
     no further BUY is produced for the remainder of this same cycle. This
     is "one entry per cycle" - enforced by definition, since the
     confirmation candle is a deterministic function of price history and
     is therefore identified identically every time it is looked up,
     regardless of what has happened to the position since.

EXIT remains unfiltered - a bearish crossover always exits an open
position regardless of trend strength, so a genuine reversal is never
withheld because the filter that gated the entry has since relaxed.

Causal and completed-candles-only: every value used is derived from
`candles[:i+1]`, never from anything after it. Execution (fill) still
happens no earlier than the next candle's open - this module only ever
returns a same-candle-close SIGNAL, exactly like every other strategy;
`backtest/engine.py::run_segment` is what enforces the actual next-open
fill delay, identically for every candidate.

A1/A2/A3 (`research/candidate_registry.py`) are UNCHANGED by this
correction - same three (ema_fast, ema_slow, atr_period,
min_trend_strength_atr) tuples as before. Only the entry LOGIC changed;
no configuration was added, removed, or tuned.

SECOND pre-real-evaluation correction (still before any real candidate
evaluation): the initial version of the corrected rule above still had one
flaw - if the bullish state (`fast EMA > slow EMA`) held for the ENTIRE
causally-visible history (e.g. because an evaluation block's own warm-up
window happens to start mid-trend), the crossover search would find no
actual `fast <= slow` transition and fell back to treating the EARLIEST
visible candle as though a crossover had just happened there. That
fallback was a MANUFACTURED crossover, purely an artifact of where the
block/warm-up window happens to begin - not evidence any crossover ever
occurred. A cycle may now arm ONLY from an actually observed
`fast <= slow` -> `fast > slow` transition within `candles`; if none
exists, the strategy reports `HOLD_UNOBSERVED_CYCLE` and produces no BUY,
however long the bullish state has visibly held.
"""

from __future__ import annotations

from trading_agent.data.models import Candle
from trading_agent.indicators.moving_averages import ema
from trading_agent.indicators.volatility import atr
from trading_agent.strategy.base import InsufficientDataError, PositionSide, Signal, SignalType

FAMILY = "trend_regime_filter"


class TrendWithRegimeFilterStrategy:
    def __init__(self, ema_fast: int, ema_slow: int, atr_period: int, min_trend_strength_atr: float) -> None:
        if ema_fast <= 0 or ema_slow <= 0 or atr_period <= 0:
            raise ValueError("periods must be positive")
        if ema_fast >= ema_slow:
            raise ValueError("ema_fast must be strictly less than ema_slow")
        if min_trend_strength_atr < 0:
            raise ValueError("min_trend_strength_atr must be non-negative")
        self.ema_fast_period = ema_fast
        self.ema_slow_period = ema_slow
        self.atr_period = atr_period
        self.min_trend_strength_atr = min_trend_strength_atr
        # +1 so both the current AND previous EMA values exist for
        # crossover detection, and the current ATR value is non-NaN.
        self.min_required_candles = max(ema_slow, atr_period) + 1

    def generate_signal(self, candles: list[Candle], current_position: PositionSide) -> Signal:
        if len(candles) < self.min_required_candles:
            raise InsufficientDataError(
                f"need at least {self.min_required_candles} completed candles, got {len(candles)}"
            )

        closes = [float(c.close) for c in candles]
        fast_values = ema(closes, self.ema_fast_period)
        slow_values = ema(closes, self.ema_slow_period)
        atr_values = atr(candles, self.atr_period)

        current_idx = len(candles) - 1
        earliest_idx = self.min_required_candles - 1
        fast_curr, slow_curr = fast_values[current_idx], slow_values[current_idx]
        atr_curr = atr_values[current_idx]
        last_candle = candles[-1]

        trend_strength_atr = abs(fast_curr - slow_curr) / atr_curr if atr_curr > 0 else 0.0
        in_bullish_state = fast_curr > slow_curr

        inputs = {
            "symbol": last_candle.symbol,
            "close": float(last_candle.close),
            "ema_fast_curr": fast_curr,
            "ema_slow_curr": slow_curr,
            "atr_curr": atr_curr,
            "trend_strength_atr": trend_strength_atr,
            "min_trend_strength_atr": self.min_trend_strength_atr,
            "in_bullish_state": in_bullish_state,
            "current_position": current_position.value,
        }

        if not in_bullish_state:
            fast_prev, slow_prev = fast_values[current_idx - 1], slow_values[current_idx - 1]
            bearish_cross = fast_prev >= slow_prev and fast_curr < slow_curr
            if bearish_cross:
                if current_position == PositionSide.FLAT:
                    return Signal(SignalType.HOLD, "HOLD_ALREADY_FLAT", last_candle.close_time_ms, inputs)
                return Signal(SignalType.EXIT, "BEARISH_EMA_CROSSOVER_CYCLE_RESET", last_candle.close_time_ms, inputs)
            return Signal(SignalType.HOLD, "HOLD_NOT_IN_BULLISH_STATE", last_candle.close_time_ms, inputs)

        # In a bullish state: find the most recent bullish crossover that
        # started THIS still-ongoing run, scanning back from the current
        # candle to (but never past) the earliest causally-visible index.
        # If the bullish state has held for the ENTIRE visible history -
        # i.e. no `fast <= slow` transition is ever observed within
        # `candles` - there is NO evidence a crossover ever happened at
        # all, only that one happened at or before the window this
        # evaluation block can see. Pre-real-evaluation code review
        # correction: the strategy must NEVER manufacture a crossover at
        # the block's own warm-up boundary just because that is as far
        # back as this call happens to look - doing so would silently
        # arm (and could immediately confirm) a cycle from a state
        # transition that was never actually observed, purely as an
        # artifact of where a `blocked_chronological_evaluation.py` block
        # or warm-up window happens to start. Such a state is reported as
        # `HOLD_UNOBSERVED_CYCLE` and can never itself produce a BUY.
        crossover_idx: int | None = None
        for k in range(current_idx, earliest_idx, -1):
            if fast_values[k - 1] <= slow_values[k - 1]:
                crossover_idx = k
                break

        if crossover_idx is None:
            return Signal(SignalType.HOLD, "HOLD_UNOBSERVED_CYCLE", last_candle.close_time_ms, inputs)

        inputs["cycle_crossover_time_ms"] = candles[crossover_idx].close_time_ms

        # The cycle's ONE confirmation candle: the first index in
        # [crossover_idx, current_idx] where normalized separation reaches
        # the threshold - identified identically regardless of what has
        # happened to the position since, which is what makes at most one
        # entry per cycle a structural guarantee, not a runtime flag.
        confirmation_idx: int | None = None
        for j in range(crossover_idx, current_idx + 1):
            atr_j = atr_values[j]
            strength_j = abs(fast_values[j] - slow_values[j]) / atr_j if atr_j > 0 else 0.0
            if strength_j >= self.min_trend_strength_atr:
                confirmation_idx = j
                break

        if confirmation_idx is None:
            return Signal(SignalType.HOLD, "ARMED_AWAITING_TREND_CONFIRMATION", last_candle.close_time_ms, inputs)

        if current_position == PositionSide.LONG:
            return Signal(SignalType.HOLD, "HOLD_ALREADY_LONG", last_candle.close_time_ms, inputs)

        if confirmation_idx == current_idx:
            return Signal(SignalType.BUY, "BULLISH_TREND_CONFIRMED_ENTRY", last_candle.close_time_ms, inputs)

        return Signal(
            SignalType.HOLD, "HOLD_CYCLE_ALREADY_ENTERED_NO_REENTRY_WITHOUT_FRESH_CYCLE",
            last_candle.close_time_ms, inputs,
        )
