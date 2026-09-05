"""Immutable research cutoff enforcement.

Everything used to DEVELOP or SCORE a strategy candidate must predate
2025-05-16T00:00:00Z. The stretch from that timestamp through 2026-09-04
(when the frozen v0.1 EMA baseline was formally rejected against it) has
already been observed - reported, discussed, and used to reject that
baseline - so it is PERMANENTLY CONSUMED for candidate development: it can
never become a training signal, an optimization target, a filter/threshold
choice, a ranking signal, or a deployment decision for any NEW candidate.
The only thing it may ever be used for is reproducing the ALREADY-FROZEN
baseline's own report (see `research/frozen_baseline.py`), which takes no
candidate parameter at all - structurally incapable of scoring anything new.
"""

from __future__ import annotations

from datetime import UTC, datetime

from trading_agent.data.models import Candle

#: 2025-05-16T00:00:00Z in epoch milliseconds - the FIRST instant of the
#: already-observed, permanently consumed period. Any development/scoring
#: run touching a candle at or after this timestamp is rejected outright.
RESEARCH_CUTOFF_MS: int = int(datetime(2025, 5, 16, tzinfo=UTC).timestamp() * 1000)

RESEARCH_CUTOFF_ISO = "2025-05-16T00:00:00Z"


class ResearchCutoffViolation(Exception):
    """Raised when a development/optimization run would touch on-or-after-cutoff data."""


def assert_pre_cutoff(candles: list[Candle]) -> None:
    """Raise `ResearchCutoffViolation` if ANY candle is at or after the
    immutable research cutoff. Call this at the entry point of every
    candidate-development or candidate-scoring code path - never assume a
    caller has already filtered correctly.
    """
    offending = [c for c in candles if c.open_time_ms >= RESEARCH_CUTOFF_MS]
    if offending:
        raise ResearchCutoffViolation(
            f"{len(offending)} candle(s) at or after the immutable research cutoff "
            f"({RESEARCH_CUTOFF_ISO}, {RESEARCH_CUTOFF_MS}ms) were included in a candidate "
            "development/scoring run. That period is already observed and permanently "
            "consumed (see the frozen v0.1 EMA baseline's rejection). Filter to "
            "strictly-before-cutoff data first - see `split_at_cutoff`."
        )


def split_at_cutoff(candles: list[Candle]) -> tuple[list[Candle], list[Candle]]:
    """Split `candles` (any order) into (strictly-pre-cutoff, on-or-after-
    cutoff), each preserving the input's relative order. The second list
    may ONLY ever be used to reproduce the frozen baseline's own report
    (`research/frozen_baseline.py`) - never to develop or score a candidate.
    """
    pre = [c for c in candles if c.open_time_ms < RESEARCH_CUTOFF_MS]
    consumed = [c for c in candles if c.open_time_ms >= RESEARCH_CUTOFF_MS]
    return pre, consumed
