"""Proofs for the immutable research cutoff (research/cutoff.py)."""

import inspect
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trading_agent.backtest.engine import run_segment
from trading_agent.cli.main import research_backtest_cmd
from trading_agent.data.models import Candle, interval_to_ms
from trading_agent.research.blocked_chronological_evaluation import (
    run_candidate_blocked_chronological_evaluation,
)
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


# --- Architectural (source-scanning): the cutoff boundary is honestly
# documented AND actually enforced only where claimed - a pre-real-
# evaluation code review correction. `assert_pre_cutoff` is a plain check,
# not an access-control mechanism; these tests prove, from the real source
# of the real functions (not just from a docstring's claim), that (a)
# every OFFICIAL candidate-scoring entry point calls it, and (b) the
# shared low-level `run_segment` primitive - deliberately generic, and
# also the correct tool for reproducing the already-consumed period -
# does NOT itself call it and so is never mistaken for the boundary.


def test_run_candidate_blocked_chronological_evaluation_calls_assert_pre_cutoff():
    source = inspect.getsource(run_candidate_blocked_chronological_evaluation)
    assert "assert_pre_cutoff(" in source


def test_research_backtest_cli_command_enforces_the_cutoff_before_scoring_any_candidate():
    # The CLI command must not hand a candidate raw, unfiltered candles -
    # it must call `split_at_cutoff` first, and every candidate run must
    # go through the (independently cutoff-asserting) blocked
    # chronological evaluator, never `run_segment` directly.
    source = inspect.getsource(research_backtest_cmd.callback)
    assert "split_at_cutoff(" in source
    assert "run_candidate_blocked_chronological_evaluation(" in source
    assert "run_segment(" not in source


def test_run_segment_is_not_itself_a_cutoff_security_boundary():
    # Documents and locks in the honest architectural claim: the shared
    # low-level primitive has no cutoff check of its own - it is generic
    # by design (it is also what legitimately replays the consumed
    # period for the frozen baseline), so it must never be relied upon as
    # the enforcement point.
    source = inspect.getsource(run_segment)
    assert "assert_pre_cutoff" not in source
    assert "ResearchCutoffViolation" not in source
