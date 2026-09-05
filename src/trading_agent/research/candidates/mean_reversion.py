"""Candidate family C: conservative mean reversion restricted to non-trending regimes.

Bollinger-Band-style entry: BUY only when price dips meaningfully below a
rolling mean (oversold), AND a separate, fixed-period EMA-spread regime
filter confirms the market is NOT currently trending. Economic rationale:
mean reversion (buying dips, selling rallies) is only a defensible bet in
genuinely range-bound conditions - fighting an established trend by buying
its dips is a classic, well-documented way to lose money, so this family
deliberately refuses to trade at all once its own regime filter detects a
real trend. EXIT takes profit at the mean (the basis) OR bails immediately
if the regime shifts into a trend while the position is open - a real
trend emerging is grounds to abandon the mean-reversion thesis, not to
keep holding for a reversion that may no longer come.

The regime filter's own EMA pair (10/40) is a FIXED, non-tunable internal
constant - not part of this family's declared candidate search space - so
the only two parameters that vary across candidates are the Bollinger
Band's own period/width and the regime threshold.

Causal and completed-candles-only.
"""

from __future__ import annotations

import statistics

from trading_agent.data.models import Candle
from trading_agent.indicators.moving_averages import ema
from trading_agent.indicators.volatility import atr
from trading_agent.strategy.base import InsufficientDataError, PositionSide, Signal, SignalType

FAMILY = "conservative_mean_reversion_non_trending"

#: Fixed regime-detection EMA pair - not part of the declared candidate
#: search space (see module docstring).
_REGIME_EMA_FAST = 10
_REGIME_EMA_SLOW = 40


class ConservativeMeanReversionStrategy:
    def __init__(self, bb_period: int, bb_std_mult: float, atr_period: int, max_trend_strength_atr: float) -> None:
        if bb_period <= 0 or atr_period <= 0:
            raise ValueError("periods must be positive")
        if bb_std_mult <= 0:
            raise ValueError("bb_std_mult must be positive")
        if max_trend_strength_atr < 0:
            raise ValueError("max_trend_strength_atr must be non-negative")
        self.bb_period = bb_period
        self.bb_std_mult = bb_std_mult
        self.atr_period = atr_period
        self.max_trend_strength_atr = max_trend_strength_atr
        self.min_required_candles = max(bb_period, atr_period, _REGIME_EMA_SLOW) + 1

    def generate_signal(self, candles: list[Candle], current_position: PositionSide) -> Signal:
        if len(candles) < self.min_required_candles:
            raise InsufficientDataError(
                f"need at least {self.min_required_candles} completed candles, got {len(candles)}"
            )

        closes = [float(c.close) for c in candles]
        last_candle = candles[-1]
        close = closes[-1]

        window = closes[-self.bb_period :]
        basis = statistics.fmean(window)
        std = statistics.pstdev(window)
        lower_band = basis - self.bb_std_mult * std
        upper_band = basis + self.bb_std_mult * std

        atr_curr = atr(candles, self.atr_period)[-1]
        regime_fast = ema(closes, _REGIME_EMA_FAST)[-1]
        regime_slow = ema(closes, _REGIME_EMA_SLOW)[-1]
        trend_strength_atr = abs(regime_fast - regime_slow) / atr_curr if atr_curr > 0 else 0.0
        range_bound = trend_strength_atr <= self.max_trend_strength_atr

        inputs = {
            "symbol": last_candle.symbol,
            "close": close,
            "basis": basis,
            "lower_band": lower_band,
            "upper_band": upper_band,
            "trend_strength_atr": trend_strength_atr,
            "max_trend_strength_atr": self.max_trend_strength_atr,
            "range_bound": range_bound,
            "current_position": current_position.value,
        }

        if current_position == PositionSide.FLAT:
            if range_bound and close < lower_band:
                return Signal(SignalType.BUY, "OVERSOLD_DIP_IN_RANGE_BOUND_REGIME", last_candle.close_time_ms, inputs)
            return Signal(SignalType.HOLD, "HOLD_NO_QUALIFYING_DIP", last_candle.close_time_ms, inputs)

        if not range_bound:
            return Signal(SignalType.EXIT, "REGIME_SHIFTED_TO_TRENDING_ABANDON_REVERSION_THESIS", last_candle.close_time_ms, inputs)
        if close >= basis:
            return Signal(SignalType.EXIT, "REVERTED_TO_MEAN", last_candle.close_time_ms, inputs)
        return Signal(SignalType.HOLD, "HOLD_AWAITING_REVERSION", last_candle.close_time_ms, inputs)
