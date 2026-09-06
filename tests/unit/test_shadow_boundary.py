from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trading_agent.data.models import Candle
from trading_agent.shadow.boundary import (
    SHADOW_START_BOUNDARY_ISO,
    SHADOW_START_BOUNDARY_MS,
    ShadowBoundaryViolation,
    assert_no_pre_boundary_candles,
    filter_from_boundary,
)

INTERVAL = "1h"
STEP_MS = 3_600_000


def _candle(open_time_ms: int) -> Candle:
    return Candle(
        symbol="BTCUSDT", interval=INTERVAL, open_time_ms=open_time_ms, close_time_ms=open_time_ms + STEP_MS - 1,
        open=Decimal(100), high=Decimal(101), low=Decimal(99), close=Decimal(100), volume=Decimal(1),
    )


def test_boundary_is_exactly_2026_09_06_utc():
    assert SHADOW_START_BOUNDARY_ISO == "2026-09-06T00:00:00Z"
    assert SHADOW_START_BOUNDARY_MS == int(datetime(2026, 9, 6, tzinfo=UTC).timestamp() * 1000)


def test_assert_raises_when_any_candle_is_before_the_boundary():
    candles = [_candle(SHADOW_START_BOUNDARY_MS - STEP_MS), _candle(SHADOW_START_BOUNDARY_MS)]
    with pytest.raises(ShadowBoundaryViolation):
        assert_no_pre_boundary_candles(candles)


def test_assert_passes_when_every_candle_is_at_or_after_the_boundary():
    candles = [_candle(SHADOW_START_BOUNDARY_MS), _candle(SHADOW_START_BOUNDARY_MS + STEP_MS)]
    assert_no_pre_boundary_candles(candles)  # no raise


def test_assert_passes_on_empty_list():
    assert_no_pre_boundary_candles([])  # no raise


def test_filter_from_boundary_drops_only_the_early_candles_and_preserves_order():
    before = _candle(SHADOW_START_BOUNDARY_MS - STEP_MS)
    at = _candle(SHADOW_START_BOUNDARY_MS)
    after = _candle(SHADOW_START_BOUNDARY_MS + STEP_MS)
    result = filter_from_boundary([before, at, after])
    assert result == [at, after]
