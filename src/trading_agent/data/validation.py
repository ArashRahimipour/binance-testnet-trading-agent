"""Fail-closed candle validation.

Every function here either returns the input unchanged or raises. There is
no "best effort" repair path - a caller that gets an exception must refuse
to trade rather than guess at a fix, per the project's non-negotiable rule
that missing/stale/duplicate/out-of-order data blocks trading.
"""

from __future__ import annotations

from trading_agent.data.exceptions import (
    DuplicateCandleError,
    EmptyDataError,
    GapDetectedError,
    OutOfOrderCandleError,
    StaleDataError,
)
from trading_agent.data.models import Candle, interval_to_ms


def validate_candle_sequence(candles: list[Candle], interval: str) -> list[Candle]:
    """Check for duplicates, ordering, and gaps in a candle series."""
    if not candles:
        raise EmptyDataError("no candles to validate")

    expected_gap_ms = interval_to_ms(interval)
    seen_open_times: set[int] = set()
    previous: Candle | None = None

    for candle in candles:
        if candle.open_time_ms in seen_open_times:
            raise DuplicateCandleError(f"duplicate candle at open_time_ms={candle.open_time_ms}")
        seen_open_times.add(candle.open_time_ms)

        if previous is not None:
            if candle.open_time_ms <= previous.open_time_ms:
                raise OutOfOrderCandleError(
                    f"candle at open_time_ms={candle.open_time_ms} is not after "
                    f"previous open_time_ms={previous.open_time_ms}"
                )
            actual_gap_ms = candle.open_time_ms - previous.open_time_ms
            if actual_gap_ms != expected_gap_ms:
                raise GapDetectedError(
                    f"expected {expected_gap_ms}ms between candles, got {actual_gap_ms}ms "
                    f"between open_time_ms={previous.open_time_ms} and {candle.open_time_ms}"
                )
        previous = candle

    return candles


def validate_not_stale(
    candles: list[Candle], reference_time_ms: int, max_age_seconds: int
) -> list[Candle]:
    """Reject the series if the most recent completed candle is too old."""
    if not candles:
        raise EmptyDataError("no candles to check for staleness")

    latest = candles[-1]
    age_seconds = (reference_time_ms - latest.close_time_ms) / 1000
    if age_seconds > max_age_seconds:
        raise StaleDataError(
            f"latest completed candle is {age_seconds:.0f}s old "
            f"(max allowed {max_age_seconds}s)"
        )
    return candles
