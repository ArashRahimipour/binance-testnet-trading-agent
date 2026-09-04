from decimal import Decimal

import pytest

from trading_agent.data.exceptions import (
    DuplicateCandleError,
    EmptyDataError,
    OutOfOrderCandleError,
)
from trading_agent.data.gap_detection import partition_into_segments
from trading_agent.data.models import Candle, interval_to_ms

INTERVAL = "4h"
STEP = interval_to_ms(INTERVAL)
START = 1_700_000_000_000

# The exact real-world gap discovered during the first multi-year download.
REAL_GAP_PREVIOUS_OPEN_TIME_MS = 1582099200000
REAL_GAP_NEXT_OPEN_TIME_MS = 1582128000000


def _candle(open_time_ms: int) -> Candle:
    return Candle(
        symbol="BTCUSDT", interval=INTERVAL,
        open_time_ms=open_time_ms, close_time_ms=open_time_ms + STEP - 1,
        open=Decimal(100), high=Decimal(101), low=Decimal(99), close=Decimal(100), volume=Decimal(1),
    )


def test_no_gap_produces_a_single_segment():
    candles = [_candle(START + i * STEP) for i in range(5)]
    result = partition_into_segments(candles, INTERVAL)
    assert result.segments == [candles]
    assert result.gaps == []


def test_the_exact_reported_timestamps_produce_one_confirmed_gap():
    candles = [_candle(REAL_GAP_PREVIOUS_OPEN_TIME_MS), _candle(REAL_GAP_NEXT_OPEN_TIME_MS)]
    result = partition_into_segments(candles, INTERVAL)
    assert len(result.gaps) == 1
    gap = result.gaps[0]
    assert gap.previous_open_time_ms == REAL_GAP_PREVIOUS_OPEN_TIME_MS
    assert gap.next_open_time_ms == REAL_GAP_NEXT_OPEN_TIME_MS
    assert gap.expected_open_time_ms == REAL_GAP_PREVIOUS_OPEN_TIME_MS + STEP
    assert gap.missing_intervals == 1
    assert len(result.segments) == 2
    assert result.segments[0] == [candles[0]]
    assert result.segments[1] == [candles[1]]


def test_one_missing_interval():
    candles = [_candle(START), _candle(START + 2 * STEP)]
    result = partition_into_segments(candles, INTERVAL)
    assert result.gaps[0].missing_intervals == 1


def test_several_consecutive_missing_intervals():
    candles = [_candle(START), _candle(START + 5 * STEP)]  # 4 missing intervals
    result = partition_into_segments(candles, INTERVAL)
    assert len(result.gaps) == 1
    assert result.gaps[0].missing_intervals == 4


def test_multiple_gaps_produce_multiple_segments():
    candles = [
        _candle(START),
        _candle(START + STEP),
        _candle(START + 3 * STEP),  # gap 1: 1 missing interval
        _candle(START + 4 * STEP),
        _candle(START + 7 * STEP),  # gap 2: 2 missing intervals
    ]
    result = partition_into_segments(candles, INTERVAL)
    assert len(result.gaps) == 2
    assert len(result.segments) == 3
    assert [len(s) for s in result.segments] == [2, 2, 1]
    assert result.gaps[0].missing_intervals == 1
    assert result.gaps[1].missing_intervals == 2


def test_duplicate_candles_are_rejected():
    candles = [_candle(START), _candle(START)]
    with pytest.raises(DuplicateCandleError):
        partition_into_segments(candles, INTERVAL)


def test_out_of_order_candles_are_rejected():
    candles = [_candle(START + STEP), _candle(START)]
    with pytest.raises(OutOfOrderCandleError):
        partition_into_segments(candles, INTERVAL)


def test_empty_input_is_rejected():
    with pytest.raises(EmptyDataError):
        partition_into_segments([], INTERVAL)


def test_no_candle_is_ever_fabricated_to_bridge_a_gap():
    candles = [_candle(REAL_GAP_PREVIOUS_OPEN_TIME_MS), _candle(REAL_GAP_NEXT_OPEN_TIME_MS)]
    result = partition_into_segments(candles, INTERVAL)
    all_open_times = {c.open_time_ms for segment in result.segments for c in segment}
    assert all_open_times == {REAL_GAP_PREVIOUS_OPEN_TIME_MS, REAL_GAP_NEXT_OPEN_TIME_MS}
