"""Baseline trend-following strategy: EMA(fast)/EMA(slow) crossover.

This is an intentionally simple, explainable research baseline - not a
claim of profitability, and not a final strategy. See STRATEGY.md.

Rules (evaluated only on completed 4h candles):
  - BUY when the fast EMA crosses from at/below the slow EMA to above it,
    and the agent is not already long.
  - EXIT when the fast EMA crosses from at/above the slow EMA to below it,
    and the agent currently holds a position.
  - HOLD otherwise, including when a crossover condition is met but acting
    on it would mean buying while already long, or selling while already
    flat (there is nothing to sell).
"""

from __future__ import annotations

from trading_agent.data.models import Candle
from trading_agent.indicators.moving_averages import ema
from trading_agent.strategy.base import InsufficientDataError, PositionSide, Signal, SignalType


class EmaCrossoverTrendStrategy:
    def __init__(self, ema_fast: int, ema_slow: int) -> None:
        if ema_fast <= 0 or ema_slow <= 0:
            raise ValueError("EMA periods must be positive")
        if ema_fast >= ema_slow:
            raise ValueError("ema_fast must be strictly less than ema_slow")
        self.ema_fast_period = ema_fast
        self.ema_slow_period = ema_slow

    def generate_signal(self, candles: list[Candle], current_position: PositionSide) -> Signal:
        """Generate a signal from a series of *completed* candles.

        `candles` must be sorted ascending by open time and contain only
        completed candles (the data layer's responsibility, not re-checked
        here). At least `ema_slow + 1` candles are required so that both the
        current and previous EMA values exist.
        """
        min_required = self.ema_slow_period + 1
        if len(candles) < min_required:
            raise InsufficientDataError(
                f"need at least {min_required} completed candles, got {len(candles)}"
            )

        closes = [float(c.close) for c in candles]
        fast_values = ema(closes, self.ema_fast_period)
        slow_values = ema(closes, self.ema_slow_period)

        fast_prev, fast_curr = fast_values[-2], fast_values[-1]
        slow_prev, slow_curr = slow_values[-2], slow_values[-1]
        last_candle = candles[-1]

        inputs = {
            "symbol": last_candle.symbol,
            "interval": last_candle.interval,
            "close": float(last_candle.close),
            "ema_fast_period": self.ema_fast_period,
            "ema_slow_period": self.ema_slow_period,
            "ema_fast_prev": fast_prev,
            "ema_fast_curr": fast_curr,
            "ema_slow_prev": slow_prev,
            "ema_slow_curr": slow_curr,
            "current_position": current_position.value,
        }

        bullish_cross = fast_prev <= slow_prev and fast_curr > slow_curr
        bearish_cross = fast_prev >= slow_prev and fast_curr < slow_curr

        if bullish_cross:
            if current_position == PositionSide.LONG:
                return Signal(SignalType.HOLD, "HOLD_ALREADY_LONG", last_candle.close_time_ms, inputs)
            return Signal(SignalType.BUY, "BULLISH_EMA_CROSSOVER", last_candle.close_time_ms, inputs)

        if bearish_cross:
            if current_position == PositionSide.FLAT:
                return Signal(SignalType.HOLD, "HOLD_ALREADY_FLAT", last_candle.close_time_ms, inputs)
            return Signal(SignalType.EXIT, "BEARISH_EMA_CROSSOVER", last_candle.close_time_ms, inputs)

        return Signal(SignalType.HOLD, "HOLD_NO_CROSSOVER", last_candle.close_time_ms, inputs)
