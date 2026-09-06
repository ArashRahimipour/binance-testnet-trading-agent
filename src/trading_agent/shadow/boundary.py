"""The immutable forward-only shadow-mode start boundary.

`SHADOW_START_BOUNDARY_MS` is fixed at 2026-09-06T00:00:00Z - deliberately a
plain module constant, never a config field (a config field could be moved
earlier by a user, which would violate the "must be fixed at or after"
mandate). Every shadow-mode code path that touches a candle must filter or
assert against this boundary before that candle can ever reach the E1
strategy, the simulation engine, or a persisted shadow record - see
`shadow/engine.py::run_shadow_cycle`, which calls `assert_no_pre_boundary_candles`
on every batch of freshly fetched candles before they are ever stored.

This is a different boundary from, and entirely independent of,
`research/cutoff.py::RESEARCH_CUTOFF_MS` (2025-05-16T00:00:00Z, the
immutable pre-cutoff development/scoring boundary for candidate research).
Nothing in this module reads, writes, or otherwise interacts with
`research/cutoff.py` - E1 is already frozen; shadow mode never re-scores or
re-develops it, it only observes it going forward.
"""

from __future__ import annotations

from datetime import UTC, datetime

from trading_agent.data.models import Candle

#: 2026-09-06T00:00:00Z in epoch milliseconds - the FIRST instant shadow
#: mode may ever fetch, backfill, store, or score a candle from.
SHADOW_START_BOUNDARY_MS: int = int(datetime(2026, 9, 6, tzinfo=UTC).timestamp() * 1000)

SHADOW_START_BOUNDARY_ISO = "2026-09-06T00:00:00Z"


class ShadowBoundaryViolation(Exception):
    """Raised when a shadow-mode code path would touch a candle strictly
    before the fixed shadow start boundary."""


def assert_no_pre_boundary_candles(candles: list[Candle]) -> None:
    """Raise `ShadowBoundaryViolation` if ANY candle opens strictly before
    the fixed shadow start boundary. Call this on every batch of candles
    shadow mode is about to store or score - never assume a caller (or the
    exchange's own response to a bounded request) already filtered
    correctly.
    """
    offending = [c for c in candles if c.open_time_ms < SHADOW_START_BOUNDARY_MS]
    if offending:
        raise ShadowBoundaryViolation(
            f"{len(offending)} candle(s) open strictly before the fixed shadow start boundary "
            f"({SHADOW_START_BOUNDARY_ISO}, {SHADOW_START_BOUNDARY_MS}ms). Shadow mode must never "
            "backfill or score a candle before this boundary."
        )


def filter_from_boundary(candles: list[Candle]) -> list[Candle]:
    """Drop any candle opening strictly before the fixed shadow start
    boundary, preserving order. A purely defensive filter - in normal
    operation `shadow/engine.py` never requests candles before the
    boundary in the first place, so this should never actually remove
    anything."""
    return [c for c in candles if c.open_time_ms >= SHADOW_START_BOUNDARY_MS]
