"""Proofs for the immutable research cutoff (research/cutoff.py)."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trading_agent.data.models import Candle, interval_to_ms
from trading_agent.research.cutoff import (
    RESEARCH_CUTOFF_MS,
    ResearchCutoffViolation,
    assert_pre_cutoff,
    split_at_cutoff,
)

INTERVAL = "4h"
STEP = interval_to_ms(INTERVAL)


def _candle(open_time_ms: int) -> Candle:
    return Candle(
        symbol="BTCUSDT", interval=INTERVAL, open_time_ms=open_time_ms, close_time_ms=open_time_ms + STEP - 1,
        open=Decimal(100), high=Decimal(105), low=Decimal(95), close=Decimal(100), volume=Decimal(1),
    )


def test_cutoff_is_exactly_2025_05_16_utc():
    expected = int(datetime(2025, 5, 16, tzinfo=UTC).timestamp() * 1000)
    assert RESEARCH_CUTOFF_MS == expected


def test_assert_pre_cutoff_passes_for_strictly_earlier_candles():
    candles = [_candle(RESEARCH_CUTOFF_MS - 2 * STEP), _candle(RESEARCH_CUTOFF_MS - STEP)]
    assert_pre_cutoff(candles)  # must not raise


def test_assert_pre_cutoff_rejects_a_candle_exactly_at_the_cutoff():
    candles = [_candle(RESEARCH_CUTOFF_MS - STEP), _candle(RESEARCH_CUTOFF_MS)]
    with pytest.raises(ResearchCutoffViolation):
        assert_pre_cutoff(candles)


def test_assert_pre_cutoff_rejects_a_candle_after_the_cutoff():
    candles = [_candle(RESEARCH_CUTOFF_MS + STEP)]
    with pytest.raises(ResearchCutoffViolation):
        assert_pre_cutoff(candles)


def test_assert_pre_cutoff_passes_for_empty_input():
    assert_pre_cutoff([])  # must not raise


def test_split_at_cutoff_partitions_correctly_and_preserves_order():
    pre1 = _candle(RESEARCH_CUTOFF_MS - 3 * STEP)
    pre2 = _candle(RESEARCH_CUTOFF_MS - STEP)
    consumed1 = _candle(RESEARCH_CUTOFF_MS)
    consumed2 = _candle(RESEARCH_CUTOFF_MS + STEP)
    pre, consumed = split_at_cutoff([pre1, pre2, consumed1, consumed2])
    assert pre == [pre1, pre2]
    assert consumed == [consumed1, consumed2]


def test_split_at_cutoff_result_never_fails_assert_pre_cutoff():
    pre, _consumed = split_at_cutoff(
        [_candle(RESEARCH_CUTOFF_MS - STEP), _candle(RESEARCH_CUTOFF_MS), _candle(RESEARCH_CUTOFF_MS + STEP)]
    )
    assert_pre_cutoff(pre)  # must not raise - split_at_cutoff's "pre" half is always development-safe
