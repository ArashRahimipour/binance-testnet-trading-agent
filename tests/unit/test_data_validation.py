from decimal import Decimal

import pytest

from trading_agent.data.exceptions import (
    DuplicateCandleError,
    EmptyDataError,
    GapDetectedError,
    OutOfOrderCandleError,
    StaleDataError,
)
from trading_agent.data.models import Candle, interval_to_ms
from trading_agent.data.validation import validate_candle_sequence, validate_not_stale

INTERVAL = "4h"
STEP = interval_to_ms(INTERVAL)
START = 1_700_000_000_000


def _candle(open_time_ms: int, close: float = 100.0) -> Candle:
    return Candle(
        symbol="BTCUSDT",
        interval=INTERVAL,
        open_time_ms=open_time_ms,
        close_time_ms=open_time_ms + STEP - 1,
        open=Decimal(str(close)),
        high=Decimal(str(close + 1)),
        low=Decimal(str(close - 1)),
        close=Decimal(str(close)),
        volume=Decimal(10),
    )


def _series(n: int) -> list[Candle]:
    return [_candle(START + i * STEP) for i in range(n)]


def test_valid_series_passes():
    candles = _series(5)
    assert validate_candle_sequence(candles, INTERVAL) == candles


def test_empty_series_rejected():
    with pytest.raises(EmptyDataError):
        validate_candle_sequence([], INTERVAL)


def test_duplicate_open_time_rejected():
    candles = _series(3)
    candles.append(_candle(candles[1].open_time_ms))
    with pytest.raises(DuplicateCandleError):
        validate_candle_sequence(candles, INTERVAL)


def test_out_of_order_rejected():
    candles = _series(3)
    # Make the last candle's open_time earlier than the one before it,
    # without producing a duplicate or landing on a coincidentally valid gap.
    candles[2] = _candle(candles[1].open_time_ms - 1000)
    with pytest.raises(OutOfOrderCandleError):
        validate_candle_sequence(candles, INTERVAL)


def test_gap_rejected():
    candles = _series(3)
    # Introduce a gap by skipping one interval's worth of time.
    candles[2] = _candle(candles[2].open_time_ms + STEP)
    with pytest.raises(GapDetectedError):
        validate_candle_sequence(candles, INTERVAL)


def test_not_stale_passes_when_recent():
    candles = _series(2)
    reference_time_ms = candles[-1].close_time_ms + 1
    assert validate_not_stale(candles, reference_time_ms, max_age_seconds=999_999) == candles


def test_stale_data_rejected():
    candles = _series(2)
    reference_time_ms = candles[-1].close_time_ms + 10_000_000
    with pytest.raises(StaleDataError):
        validate_not_stale(candles, reference_time_ms, max_age_seconds=60)


def test_stale_check_on_empty_raises_empty_data_error():
    with pytest.raises(EmptyDataError):
        validate_not_stale([], reference_time_ms=0, max_age_seconds=60)
