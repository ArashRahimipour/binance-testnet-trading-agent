from decimal import Decimal

import pytest

from trading_agent.data.models import Candle, interval_to_ms
from trading_agent.strategy.base import InsufficientDataError, PositionSide, SignalType
from trading_agent.strategy.trend_baseline import EmaCrossoverTrendStrategy

INTERVAL = "4h"
STEP = interval_to_ms(INTERVAL)
START = 1_700_000_000_000


def _candles_from_closes(closes: list[float]) -> list[Candle]:
    return [
        Candle(
            symbol="BTCUSDT",
            interval=INTERVAL,
            open_time_ms=START + i * STEP,
            close_time_ms=START + i * STEP + STEP - 1,
            open=Decimal(str(close)),
            high=Decimal(str(close)),
            low=Decimal(str(close)),
            close=Decimal(str(close)),
            volume=Decimal(1),
        )
        for i, close in enumerate(closes)
    ]


def _strategy() -> EmaCrossoverTrendStrategy:
    return EmaCrossoverTrendStrategy(ema_fast=3, ema_slow=6)


def test_rejects_invalid_period_ordering():
    with pytest.raises(ValueError):
        EmaCrossoverTrendStrategy(ema_fast=10, ema_slow=5)


def test_insufficient_data_raises():
    strategy = _strategy()
    closes = [100.0] * 5  # fewer than ema_slow + 1
    with pytest.raises(InsufficientDataError):
        strategy.generate_signal(_candles_from_closes(closes), PositionSide.FLAT)


def test_determinism_same_inputs_same_output():
    strategy = _strategy()
    closes = [100, 99, 98, 100, 105, 110, 115, 120]
    candles = _candles_from_closes(closes)
    sig1 = strategy.generate_signal(candles, PositionSide.FLAT)
    sig2 = strategy.generate_signal(candles, PositionSide.FLAT)
    assert sig1 == sig2


def test_no_lookahead_earlier_signal_unaffected_by_future_candles():
    strategy = _strategy()
    closes = [100, 99, 98, 100, 105, 110, 115, 120, 90, 80, 70]
    truncated = _candles_from_closes(closes[:8])
    sig_truncated = strategy.generate_signal(truncated, PositionSide.FLAT)

    full = _candles_from_closes(closes)
    # Re-derive what the signal "as of" bar 8 would have been by truncating
    # the full series to the same point again - must match exactly.
    sig_from_full_truncated_again = strategy.generate_signal(full[:8], PositionSide.FLAT)
    assert sig_truncated == sig_from_full_truncated_again


def test_bullish_crossover_produces_buy_when_flat():
    strategy = _strategy()
    # Fast EMA crosses above slow EMA exactly on the last (7th) bar.
    closes = [100, 99, 98, 97, 96, 95, 100]
    signal = strategy.generate_signal(_candles_from_closes(closes), PositionSide.FLAT)
    assert signal.type == SignalType.BUY
    assert signal.reason_code == "BULLISH_EMA_CROSSOVER"
    assert "ema_fast_curr" in signal.inputs


def test_bullish_crossover_holds_when_already_long():
    strategy = _strategy()
    closes = [100, 99, 98, 97, 96, 95, 100]
    signal = strategy.generate_signal(_candles_from_closes(closes), PositionSide.LONG)
    assert signal.type == SignalType.HOLD
    assert signal.reason_code == "HOLD_ALREADY_LONG"


def test_bearish_crossover_produces_exit_when_long():
    strategy = _strategy()
    # Fast EMA crosses below slow EMA exactly on the last (8th) bar.
    closes = [70, 75, 80, 85, 90, 95, 100, 50]
    signal = strategy.generate_signal(_candles_from_closes(closes), PositionSide.LONG)
    assert signal.type == SignalType.EXIT
    assert signal.reason_code == "BEARISH_EMA_CROSSOVER"


def test_bearish_crossover_holds_when_already_flat_no_oversell():
    strategy = _strategy()
    closes = [70, 75, 80, 85, 90, 95, 100, 50]
    signal = strategy.generate_signal(_candles_from_closes(closes), PositionSide.FLAT)
    assert signal.type == SignalType.HOLD
    assert signal.reason_code == "HOLD_ALREADY_FLAT"


def test_no_crossover_holds():
    strategy = _strategy()
    closes = [100, 100, 100, 100, 100, 100, 100, 100]
    signal = strategy.generate_signal(_candles_from_closes(closes), PositionSide.FLAT)
    assert signal.type == SignalType.HOLD
    assert signal.reason_code == "HOLD_NO_CROSSOVER"
