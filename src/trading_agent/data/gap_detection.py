"""Gap-tolerant candle sequence analysis for HISTORICAL RESEARCH data only.

This is a deliberately separate code path from `data/validation.py`'s
`validate_candle_sequence`, which remains the ONLY validation live/Testnet
signal generation ever uses (see `execution/live_runner.py`) - it still
rejects a gap outright, exactly as before. Nothing in this module is
imported by `execution/live_runner.py` or any other live/Testnet code
path; it exists only for `data/historical_fetch.py` (gap confirmation
during download) and `backtest/engine.py` (segmenting a downloaded series
around confirmed gaps).

Duplicates and out-of-order candles are NEVER tolerated here either - they
raise immediately, exactly like `validate_candle_sequence`. The only
difference is what happens to a GAP: instead of raising, it is recorded
(`GapRecord`) and the series is split into a new contiguous segment at
that point. Nothing here ever fabricates, interpolates, or otherwise
invents an OHLCV value to paper over a gap.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from trading_agent.data.exceptions import (
    DuplicateCandleError,
    EmptyDataError,
    OutOfOrderCandleError,
)
from trading_agent.data.models import Candle, interval_to_ms


@dataclass(frozen=True, slots=True)
class GapRecord:
    """One confirmed missing stretch between two known-good candles.

    `missing_intervals` is how many candle periods are absent - e.g. one
    missing 4h candle between two otherwise-adjacent candles is
    `missing_intervals=1`, not a fractional or approximate count.
    """

    expected_open_time_ms: int  # where the first missing candle would have opened
    previous_open_time_ms: int  # the last known-good candle before the gap
    next_open_time_ms: int  # the first known-good candle after the gap
    missing_intervals: int


@dataclass(frozen=True, slots=True)
class SegmentationResult:
    segments: list[list[Candle]] = field(default_factory=list)
    gaps: list[GapRecord] = field(default_factory=list)


def partition_into_segments(candles: list[Candle], interval: str) -> SegmentationResult:
    """Split `candles` into contiguous, gap-free segments.

    Duplicates and out-of-order candles raise immediately - see the module
    docstring. A gap between two otherwise-valid, ordered candles is
    recorded as a `GapRecord` and starts a new segment; it is never
    silently bridged or filled in.
    """
    if not candles:
        raise EmptyDataError("no candles to analyze for gaps")

    expected_gap_ms = interval_to_ms(interval)
    segments: list[list[Candle]] = [[]]
    gaps: list[GapRecord] = []
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
                missing_intervals = actual_gap_ms // expected_gap_ms - 1
                gaps.append(
                    GapRecord(
                        expected_open_time_ms=previous.open_time_ms + expected_gap_ms,
                        previous_open_time_ms=previous.open_time_ms,
                        next_open_time_ms=candle.open_time_ms,
                        missing_intervals=missing_intervals,
                    )
                )
                segments.append([])

        segments[-1].append(candle)
        previous = candle

    return SegmentationResult(segments=segments, gaps=gaps)
