"""Candidate family A: trend-following with a volatility/regime filter.

An EMA(fast)/EMA(slow) crossover - the same mechanism as the frozen,
rejected v0.1 baseline - but an entry additionally requires the two EMAs'
spread to exceed `min_trend_strength_atr` multiples of the current ATR
before a BUY is taken. Economic rationale: a bare EMA crossover fires on
marginal, noise-driven separations as readily as on a genuine trend: this
filter only enters when the fast/slow separation is large relative to
recent volatility, i.e. a real, ATR-confirmed trend rather than a
crossover any small wiggle in price would have produced. EXIT is
deliberately NOT filtered - once in a position, a bearish crossover exits
regardless of trend strength, so a genuine reversal is never ignored
because the filter that gated the entry has since relaxed.

Causal and completed-candles-only, identically to `strategy/trend_baseline.py`.
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

        fast_prev, fast_curr = fast_values[-2], fast_values[-1]
        slow_prev, slow_curr = slow_values[-2], slow_values[-1]
        atr_curr = atr_values[-1]
        last_candle = candles[-1]

        trend_strength_atr = abs(fast_curr - slow_curr) / atr_curr if atr_curr > 0 else 0.0

        inputs = {
            "symbol": last_candle.symbol,
            "close": float(last_candle.close),
            "ema_fast_curr": fast_curr,
            "ema_slow_curr": slow_curr,
            "atr_curr": atr_curr,
            "trend_strength_atr": trend_strength_atr,
            "min_trend_strength_atr": self.min_trend_strength_atr,
            "current_position": current_position.value,
        }

        bullish_cross = fast_prev <= slow_prev and fast_curr > slow_curr
        bearish_cross = fast_prev >= slow_prev and fast_curr < slow_curr

        if bullish_cross:
            if current_position == PositionSide.LONG:
                return Signal(SignalType.HOLD, "HOLD_ALREADY_LONG", last_candle.close_time_ms, inputs)
            if trend_strength_atr < self.min_trend_strength_atr:
                return Signal(SignalType.HOLD, "HOLD_CROSSOVER_BELOW_REGIME_FILTER", last_candle.close_time_ms, inputs)
            return Signal(SignalType.BUY, "BULLISH_EMA_CROSSOVER_REGIME_CONFIRMED", last_candle.close_time_ms, inputs)

        if bearish_cross:
            if current_position == PositionSide.FLAT:
                return Signal(SignalType.HOLD, "HOLD_ALREADY_FLAT", last_candle.close_time_ms, inputs)
            return Signal(SignalType.EXIT, "BEARISH_EMA_CROSSOVER", last_candle.close_time_ms, inputs)

        return Signal(SignalType.HOLD, "HOLD_NO_CROSSOVER", last_candle.close_time_ms, inputs)
