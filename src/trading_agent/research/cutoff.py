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

WHAT IS - AND IS NOT - THE SECURITY BOUNDARY HERE (pre-real-evaluation code
review correction): `assert_pre_cutoff` is a plain runtime check, not an
access-control mechanism - calling it is what makes a code path safe, and
NOTHING enforces that every path calls it. In particular,
`backtest/engine.py::run_segment` - the shared low-level simulation
primitive every code path in this project eventually calls (the frozen
baseline, the continuous backtest, the independent holdout evaluation, AND
every research candidate) - is deliberately GENERIC: it accepts whatever
candles it is given, with no cutoff check of its own, because it is also
the correct and only tool for legitimately simulating the ALREADY-CONSUMED
period (`research/frozen_baseline.py::reproduce_frozen_baseline_report`
does exactly this, on purpose). `run_segment` is therefore NOT itself a
security boundary and must never be treated as one.

The actual boundary is enforced only at the OFFICIAL, higher-level
candidate-scoring entry points, each of which calls `assert_pre_cutoff`
(directly, or transitively through one that does) before any candle ever
reaches a candidate:
  - `research/blocked_chronological_evaluation.py::
    run_candidate_blocked_chronological_evaluation` - asserts at its own
    entry, regardless of what the caller already did.
  - The `research-backtest` CLI command (`cli/main.py::
    research_backtest_cmd`) - calls `split_at_cutoff` before ever handing
    candles to a candidate, and every candidate run then goes through
    `run_candidate_blocked_chronological_evaluation` above, which asserts
    again independently.
`tests/unit/test_research_cutoff.py` contains a source-scanning
architectural test proving both of these facts hold for the actual code,
not just for this docstring.
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
