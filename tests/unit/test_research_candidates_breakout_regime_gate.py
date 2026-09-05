"""Proofs for research/candidates/breakout_regime_gate.py: B1's own
breakout/exit logic passes through unchanged, the causal EMA200
bullish-regime gate blocks/allows a BUY correctly, the lookback index
never wraps negative (the concrete look-ahead risk a fixed lookback
introduces), the read-only gate-blocked counters, and (via the engine)
that D1 still fills at the NEXT candle's open like every other candidate -
this candidate changes nothing about entry/exit timing.
"""

from __future__ import annotations

from decimal import Decimal

from tests.fixtures.exchange_info import make_exchange_info
from trading_agent.backtest.engine import run_segment
from trading_agent.config.models import AppConfig
from trading_agent.data.models import Candle, interval_to_ms
from trading_agent.execution.backtest_broker import BacktestBroker
from trading_agent.research.candidates.breakout_regime_gate import (
    EMA_REGIME_PERIOD,
    EMA_REGIME_SLOPE_LOOKBACK_CANDLES,
    BreakoutWithBullishRegimeGateStrategy,
)
from trading_agent.risk.engine import RiskEngine
from trading_agent.sizing.exchange_filters import SymbolFilters
from trading_agent.strategy.base import InsufficientDataError, PositionSide, SignalType

INTERVAL = "4h"
STEP = interval_to_ms(INTERVAL)
START = 1_600_000_000_000


def _candle(i: int, close: float, start: int = START) -> Candle:
    return Candle(
        symbol="BTCUSDT", interval=INTERVAL, open_time_ms=start + i * STEP, close_time_ms=start + i * STEP + STEP - 1,
        open=Decimal(str(close)), high=Decimal(str(close)), low=Decimal(str(close)), close=Decimal(str(close)),
        volume=Decimal(1),
    )


def _strategy() -> BreakoutWithBullishRegimeGateStrategy:
    return BreakoutWithBullishRegimeGateStrategy(channel_period=20, atr_period=14, breakout_atr_multiple=0.25)


def _uptrend_then_breakout_candles(n_uptrend: int = 250, plateau: int = 25) -> list[Candle]:
    closes: list[float] = []
    price = 100.0
    for _ in range(n_uptrend):
        price += 0.3
        closes.append(price)
    for _ in range(plateau):
        closes.append(price)
    closes.append(price * 1.05)  # confirmed breakout candle
    return [_candle(i, c) for i, c in enumerate(closes)]


def _downtrend_then_local_breakout_candles(n_downtrend: int = 250, plateau: int = 25) -> list[Candle]:
    closes: list[float] = []
    price = 500.0
    for _ in range(n_downtrend):
        price -= 0.3
        closes.append(price)
    for _ in range(plateau):
        closes.append(price)
    closes.append(price * 1.05)  # local breakout only - long-term regime is bearish
    return [_candle(i, c) for i, c in enumerate(closes)]


# --- min_required_candles and InsufficientDataError. ---


def test_min_required_candles_covers_both_the_breakout_and_the_ema200_lookback():
    strat = _strategy()
    assert strat.min_required_candles == EMA_REGIME_PERIOD + EMA_REGIME_SLOPE_LOOKBACK_CANDLES


def test_raises_insufficient_data_below_min_required_candles():
    strat = _strategy()
    candles = _uptrend_then_breakout_candles()[: strat.min_required_candles - 1]
    try:
        strat.generate_signal(candles, PositionSide.FLAT)
        raise AssertionError("expected InsufficientDataError")
    except InsufficientDataError:
        pass


# --- The lookback index never wraps negative (the concrete look-ahead risk). ---


def test_regime_gate_lookback_index_never_wraps_negative_at_minimum_required_candles():
    strat = _strategy()
    candles = _uptrend_then_breakout_candles()[: strat.min_required_candles]
    current_idx = len(candles) - 1
    # A negative Python list index would silently (and INCORRECTLY) read
    # from the END of the array instead of raising - i.e. a real
    # look-ahead bug. This proves the lookback stays in-bounds by
    # construction at the earliest allowed call.
    assert current_idx - EMA_REGIME_SLOPE_LOOKBACK_CANDLES >= 0
    strat.generate_signal(candles, PositionSide.FLAT)  # must not raise


# --- Bullish regime: gate passes, underlying breakout BUY is preserved. ---


def test_gate_passes_and_preserves_the_underlying_buy_in_a_bullish_rising_regime():
    strat = _strategy()
    candles = _uptrend_then_breakout_candles()
    signal = strat.generate_signal(candles, PositionSide.FLAT)
    assert signal.type == SignalType.BUY
    assert signal.reason_code == "BREAKOUT_CONFIRMED_BULLISH_REGIME"
    assert signal.inputs["above_ema200"] is True
    assert signal.inputs["ema200_rising"] is True
    assert strat.breakout_signals_evaluated == 1
    assert strat.breakout_signals_blocked_by_regime_gate == 0


# --- Bearish regime: gate blocks the underlying breakout's BUY. ---


def test_gate_blocks_the_underlying_buy_in_a_bearish_falling_regime():
    strat = _strategy()
    candles = _downtrend_then_local_breakout_candles()
    signal = strat.generate_signal(candles, PositionSide.FLAT)
    assert signal.type == SignalType.HOLD
    assert signal.reason_code == "HOLD_REGIME_GATE_BLOCKED"
    assert signal.inputs["above_ema200"] is False
    assert signal.inputs["ema200_rising"] is False
    assert strat.breakout_signals_evaluated == 1
    assert strat.breakout_signals_blocked_by_regime_gate == 1


# --- HOLD (no breakout at all) and EXIT pass through completely ungated. ---


def test_no_underlying_breakout_signal_passes_through_as_hold_without_consulting_the_gate():
    strat = _strategy()
    # Flat plateau the whole way - no breakout ever confirmed.
    candles = [_candle(i, 100.0) for i in range(strat.min_required_candles + 5)]
    signal = strat.generate_signal(candles, PositionSide.FLAT)
    assert signal.type == SignalType.HOLD
    assert signal.reason_code != "HOLD_REGIME_GATE_BLOCKED"
    assert strat.breakout_signals_evaluated == 0  # gate never even consulted


def test_channel_breakdown_exit_is_never_gated():
    strat = _strategy()
    candles = _uptrend_then_breakout_candles()
    # A sharp breakdown candle below the recent channel low, while LONG.
    breakdown_close = min(float(c.close) for c in candles[-21:-1]) - 1.0
    candles = candles + [_candle(len(candles), breakdown_close)]
    signal = strat.generate_signal(candles, PositionSide.LONG)
    assert signal.type == SignalType.EXIT
    assert signal.reason_code == "CHANNEL_BREAKDOWN"
    # The gate is entry-only - it is never consulted for an EXIT decision.
    assert strat.breakout_signals_evaluated == 0


# --- Next-open execution: D1 changes nothing about entry timing. ---


def _config() -> AppConfig:
    return AppConfig(mode="backtest", stop_loss={"stop_distance_pct": 0.2}, fees={"taker_fee_pct": 0.0, "slippage_pct": 0.0})


def _filters() -> SymbolFilters:
    return SymbolFilters.from_exchange_info(make_exchange_info(min_notional="0.01"))


def test_d1_buy_signal_fills_at_the_next_candles_open_not_the_signal_candle():
    strat_for_finding = _strategy()
    candles = _uptrend_then_breakout_candles()
    min_required = strat_for_finding.min_required_candles

    # Find the FIRST candle (scanning forward, exactly as the engine's own
    # loop would) whose completed close produces a BUY - an uninterrupted
    # uptrend can already satisfy the breakout condition well before the
    # designated plateau/breakout section, so this is found directly
    # rather than assumed.
    signal_idx = None
    for i in range(min_required - 1, len(candles)):
        sig = strat_for_finding.generate_signal(candles[: i + 1], PositionSide.FLAT)
        if sig.type == SignalType.BUY:
            signal_idx = i
            break
    assert signal_idx is not None, "expected the uptrend to eventually produce a BUY"
    assert signal_idx + 1 < len(candles), "need a following candle for the BUY to resolve against"

    strat = _strategy()  # fresh instance for the real engine run
    risk_engine = RiskEngine(_config().risk)
    broker = BacktestBroker(_config().fees)
    result = run_segment(
        candles, _config(), _filters(), strat, risk_engine, broker, None,
        min_required=min_required, starting_equity=Decimal(50), use_fixed_risk_reward_policy=True,
    )

    assert result.open_position is not None or len(result.trades) == 1
    entry_time_ms = (
        result.open_position.entry_time_ms if result.open_position is not None else result.trades[0].entry_time_ms
    )
    # The fill happens at the FOLLOWING candle's open, never at the
    # signal candle itself.
    following = candles[signal_idx + 1]
    assert entry_time_ms == following.open_time_ms
    assert entry_time_ms > candles[signal_idx].open_time_ms
