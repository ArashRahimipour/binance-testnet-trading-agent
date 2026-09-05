"""Candidate family D (ROUND 2): breakout_B1's own breakout/exit logic,
gated behind exactly one causal long-term bullish-regime filter.

This is an explicitly RESULT-INFORMED round-2 hypothesis - see
`research/candidate_registry_round2.py`'s module docstring for why it is
NOT a pre-round-1, blind candidate and must never be described as an
untouched test. It exists because round 1's real pre-cutoff evaluation
showed breakout_B1 had broad trade-level profitability but sustained
losses during an unfavorable 2021-2022 regime (see CHANGELOG.md).

Economic hypothesis: restricting B1's long-only breakouts to periods where
price sits above a RISING 200-period EMA - a simple, causal proxy for "a
long-term bullish regime is in effect" - may improve chronological
stability without changing breakout sensitivity or the risk policy at all.

Preserves B1's breakout/channel-breakdown signal EXACTLY (delegates to
`VolatilityNormalizedBreakoutStrategy` internally, same three parameters,
unchanged) and adds exactly one gate in FRONT of a would-be BUY:

  1. the signal candle's own completed CLOSE must be above `EMA(200)`
     (computed through and including that same candle);
  2. that SAME EMA(200) value must be strictly greater than its own value
     `EMA_REGIME_SLOPE_LOOKBACK_CANDLES` (20) completed candles earlier -
     i.e. the long-term average itself must be RISING, not merely price
     being above a flat or falling one.

Both conditions are evaluated on the SAME completed candle the underlying
breakout signal would fire on - causal, completed-candles-only, no
look-ahead. If either fails, this candidate reports HOLD (`HOLD_
REGIME_GATE_BLOCKED`) instead of BUY. The gate touches ONLY the entry
decision: B1's own channel-breakdown EXIT is never gated (retained exactly
as B1 defines it), and this module changes nothing about entry price,
stop-loss, take-profit, sizing, fees, or slippage - `backtest/engine.py`'s
next-candle-open fill rule and the fixed 1:2 risk/reward policy apply to
this candidate identically to every other one.

`breakout_signals_evaluated`/`breakout_signals_blocked_by_regime_gate` are
READ-ONLY, decision-irrelevant instance counters recording how often the
underlying breakout would have fired versus how often the gate blocked it
- provided purely so `research/round2_report.py` can report "percentage of
signals blocked by the EMA200 regime gate" without altering
`generate_signal`'s own return value in any way. Like every other
candidate in this codebase, `generate_signal` itself remains a pure
function of `(candles, current_position)` for trading-decision purposes;
only this side-channel telemetry is stateful.
"""

from __future__ import annotations

from trading_agent.data.models import Candle
from trading_agent.indicators.moving_averages import ema
from trading_agent.research.candidates.volatility_breakout import (
    VolatilityNormalizedBreakoutStrategy,
)
from trading_agent.strategy.base import InsufficientDataError, PositionSide, Signal, SignalType

FAMILY = "breakout_bullish_regime_gate"

#: Declared, fixed BEFORE this candidate was ever scored - never tuned
#: after seeing a D1 result (see module docstring and the round-2
#: no-further-tuning instruction in candidate_registry_round2.py).
EMA_REGIME_PERIOD = 200
EMA_REGIME_SLOPE_LOOKBACK_CANDLES = 20


class BreakoutWithBullishRegimeGateStrategy:
    def __init__(self, channel_period: int, atr_period: int, breakout_atr_multiple: float) -> None:
        #: B1's own breakout/channel-breakdown logic, delegated to
        #: verbatim - see module docstring.
        self._breakout = VolatilityNormalizedBreakoutStrategy(channel_period, atr_period, breakout_atr_multiple)
        self.channel_period = channel_period
        self.atr_period = atr_period
        self.breakout_atr_multiple = breakout_atr_multiple
        self.min_required_candles = max(
            self._breakout.min_required_candles, EMA_REGIME_PERIOD + EMA_REGIME_SLOPE_LOOKBACK_CANDLES
        )
        #: Read-only telemetry only - see module docstring. Never consulted
        #: by generate_signal's own decision.
        self.breakout_signals_evaluated = 0
        self.breakout_signals_blocked_by_regime_gate = 0

    def generate_signal(self, candles: list[Candle], current_position: PositionSide) -> Signal:
        if len(candles) < self.min_required_candles:
            raise InsufficientDataError(
                f"need at least {self.min_required_candles} completed candles, got {len(candles)}"
            )

        signal = self._breakout.generate_signal(candles, current_position)
        if signal.type != SignalType.BUY:
            # EXIT and HOLD are never gated - B1's own channel-breakdown
            # exit and no-breakout HOLD pass through completely unchanged.
            return signal

        self.breakout_signals_evaluated += 1

        closes = [float(c.close) for c in candles]
        ema_values = ema(closes, EMA_REGIME_PERIOD)
        current_idx = len(candles) - 1
        ema_curr = ema_values[current_idx]
        ema_prior = ema_values[current_idx - EMA_REGIME_SLOPE_LOOKBACK_CANDLES]
        close_curr = closes[current_idx]

        above_rising_ema = close_curr > ema_curr > ema_prior

        inputs = dict(signal.inputs)
        inputs.update(
            {
                "ema200_curr": ema_curr,
                "ema200_prior": ema_prior,
                "above_ema200": close_curr > ema_curr,
                "ema200_rising": ema_curr > ema_prior,
            }
        )

        if above_rising_ema:
            return Signal(SignalType.BUY, "BREAKOUT_CONFIRMED_BULLISH_REGIME", signal.candle_close_time_ms, inputs)

        self.breakout_signals_blocked_by_regime_gate += 1
        return Signal(SignalType.HOLD, "HOLD_REGIME_GATE_BLOCKED", signal.candle_close_time_ms, inputs)
