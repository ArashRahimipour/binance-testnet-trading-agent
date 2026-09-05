"""Candidate family B: breakout with volatility-normalized entry.

A classic Donchian-channel breakout: BUY when the close clears the highest
high of the PRECEDING `channel_period` candles (never including the
current one - see the slicing below), EXIT when the close breaks below the
lowest low of the same lookback. Economic rationale for a plain channel
breakout: markets making genuine new local highs/lows on real
participation tend to continue briefly (momentum); the volatility
normalization guards against noise - a breakout of only a few ticks above
the channel, well within the recent typical bar range (ATR), is more
likely microstructure noise than a real move, so entry additionally
requires the breakout distance to exceed `breakout_atr_multiple` times the
current ATR.

Causal and completed-candles-only: the channel for candle i's signal is
built ONLY from candles strictly before i.
"""

from __future__ import annotations

from trading_agent.data.models import Candle
from trading_agent.indicators.volatility import atr
from trading_agent.strategy.base import InsufficientDataError, PositionSide, Signal, SignalType

FAMILY = "volatility_normalized_breakout"


class VolatilityNormalizedBreakoutStrategy:
    def __init__(self, channel_period: int, atr_period: int, breakout_atr_multiple: float) -> None:
        if channel_period <= 0 or atr_period <= 0:
            raise ValueError("periods must be positive")
        if breakout_atr_multiple < 0:
            raise ValueError("breakout_atr_multiple must be non-negative")
        self.channel_period = channel_period
        self.atr_period = atr_period
        self.breakout_atr_multiple = breakout_atr_multiple
        # +1: the channel needs `channel_period` candles STRICTLY BEFORE
        # the current one, so `channel_period + 1` candles total.
        self.min_required_candles = max(channel_period + 1, atr_period)

    def generate_signal(self, candles: list[Candle], current_position: PositionSide) -> Signal:
        if len(candles) < self.min_required_candles:
            raise InsufficientDataError(
                f"need at least {self.min_required_candles} completed candles, got {len(candles)}"
            )

        last_candle = candles[-1]
        close = float(last_candle.close)
        # The channel is built from the candles PRECEDING this one only -
        # candles[-1] (the current, just-completed candle) never
        # contributes to its own breakout threshold.
        preceding = candles[-(self.channel_period + 1) : -1]
        donchian_high = max(float(c.high) for c in preceding)
        donchian_low = min(float(c.low) for c in preceding)
        atr_curr = atr(candles, self.atr_period)[-1]

        inputs = {
            "symbol": last_candle.symbol,
            "close": close,
            "donchian_high": donchian_high,
            "donchian_low": donchian_low,
            "atr_curr": atr_curr,
            "breakout_atr_multiple": self.breakout_atr_multiple,
            "current_position": current_position.value,
        }

        if current_position == PositionSide.FLAT:
            breakout_distance = close - donchian_high
            if breakout_distance > 0 and (atr_curr <= 0 or breakout_distance >= self.breakout_atr_multiple * atr_curr):
                return Signal(SignalType.BUY, "VOLATILITY_CONFIRMED_UPSIDE_BREAKOUT", last_candle.close_time_ms, inputs)
            return Signal(SignalType.HOLD, "HOLD_NO_CONFIRMED_BREAKOUT", last_candle.close_time_ms, inputs)

        if close < donchian_low:
            return Signal(SignalType.EXIT, "CHANNEL_BREAKDOWN", last_candle.close_time_ms, inputs)
        return Signal(SignalType.HOLD, "HOLD_WITHIN_CHANNEL", last_candle.close_time_ms, inputs)
