"""Proofs for research/fixed_duration_evaluation.py: the DURATION-
NORMALIZED sensitivity variant of the original blocked chronological
evaluation (`research/blocked_chronological_evaluation.py`, which this
file never imports mutably and never asserts anything changed about).

Covers: fixed-duration block construction (correct count/boundaries),
short-segment exclusion (insufficient duration -> a fragment, never five
negative zero-trade votes), leftover partial-duration reporting, warm-up
isolation (warm-up candles never trade), no block ever crosses a confirmed
gap, cutoff enforcement, and deterministic output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from itertools import pairwise

import pytest

from tests.fixtures.exchange_info import make_exchange_info
from trading_agent.config.models import AppConfig
from trading_agent.data.models import Candle, interval_to_ms
from trading_agent.research.candidate_registry import CandidateSpec
from trading_agent.research.cutoff import RESEARCH_CUTOFF_MS, ResearchCutoffViolation
from trading_agent.research.fixed_duration_evaluation import (
    DEFAULT_BLOCK_DURATION_DAYS,
    run_candidate_fixed_duration_evaluation,
)
from trading_agent.sizing.exchange_filters import SymbolFilters
from trading_agent.strategy.base import PositionSide, Signal, SignalType

INTERVAL = "4h"
STEP = interval_to_ms(INTERVAL)
CANDLES_PER_DAY = 24 * 60 * 60 * 1000 // STEP  # 6 for a 4h interval
START = RESEARCH_CUTOFF_MS - 4000 * CANDLES_PER_DAY * STEP  # comfortably pre-cutoff


def _candle(i: int, close: float, start: int = START) -> Candle:
    return Candle(
        symbol="BTCUSDT", interval=INTERVAL, open_time_ms=start + i * STEP, close_time_ms=start + i * STEP + STEP - 1,
        open=Decimal(str(close)), high=Decimal(str(close)), low=Decimal(str(close)), close=Decimal(str(close)),
        volume=Decimal(1),
    )


def _flat_candles(n: int, start: int = START, price: float = 100.0) -> list[Candle]:
    return [_candle(i, price, start=start) for i in range(n)]


def _config(**overrides) -> AppConfig:
    backtest_overrides = {"min_trades_for_significance": 3}
    backtest_overrides.update(overrides.pop("backtest", {}))
    return AppConfig(mode="backtest", backtest=backtest_overrides, **overrides)


def _filters(**overrides) -> SymbolFilters:
    return SymbolFilters.from_exchange_info(make_exchange_info(**overrides))


@dataclass
class _NeverTradeStrategy:
    """A minimal test double with a fixed, small warm-up requirement and
    NO trading behavior at all (always HOLD) - isolates the block-
    construction mechanism itself (boundaries, fragments, leftovers) from
    any strategy-specific signal logic."""

    min_required_candles: int = 10

    def generate_signal(self, candles: list[Candle], current_position: PositionSide) -> Signal:
        return Signal(SignalType.HOLD, "TEST_NEVER_TRADE", candles[-1].close_time_ms, {})


@dataclass
class _AlwaysBuyWhenFlatStrategy:
    """Emits BUY on every candle it is ever asked about while flat - used
    to prove warm-up candles never generate a trade (the loop simply never
    CALLS `generate_signal` on them, see `backtest/engine.py::run_segment`)."""

    min_required_candles: int = 10
    calls_seen_close_times: list[int] = field(default_factory=list)

    def generate_signal(self, candles: list[Candle], current_position: PositionSide) -> Signal:
        self.calls_seen_close_times.append(candles[-1].close_time_ms)
        if current_position == PositionSide.FLAT:
            return Signal(SignalType.BUY, "TEST_ALWAYS_BUY", candles[-1].close_time_ms, {})
        return Signal(SignalType.HOLD, "TEST_HOLD_ALREADY_LONG", candles[-1].close_time_ms, {})


def _spec(strategy) -> CandidateSpec:
    return CandidateSpec(candidate_id="test_candidate", family="test_family", params={}, build=lambda: strategy)


# --- Consumed-period rejection. ---


def test_fixed_duration_evaluation_raises_on_post_cutoff_data():
    candles = _flat_candles(400 * CANDLES_PER_DAY)
    post_cutoff = _candle(len(candles), 100.0, start=RESEARCH_CUTOFF_MS)
    with pytest.raises(ResearchCutoffViolation):
        run_candidate_fixed_duration_evaluation(
            _spec(_NeverTradeStrategy()), candles + [post_cutoff], _config(), _filters()
        )


def test_fixed_duration_evaluation_accepts_strictly_pre_cutoff_data():
    candles = _flat_candles(400 * CANDLES_PER_DAY)
    result = run_candidate_fixed_duration_evaluation(_spec(_NeverTradeStrategy()), candles, _config(), _filters())
    assert result.candidate_id == "test_candidate"


# --- Fixed-duration block construction. ---


def test_fixed_duration_construction_produces_correct_block_count_and_boundaries():
    # Exactly 2 complete 365-day blocks' worth of tradable data after a
    # 10-candle warm-up (2 * 365 * CANDLES_PER_DAY tradable candles).
    strategy = _NeverTradeStrategy(min_required_candles=10)
    n_tradable = 2 * DEFAULT_BLOCK_DURATION_DAYS * CANDLES_PER_DAY
    candles = _flat_candles(10 - 1 + n_tradable)  # warm-up + exactly 2 blocks
    result = run_candidate_fixed_duration_evaluation(_spec(strategy), candles, _config(), _filters())

    assert len(result.fragments) == 0
    evaluated = [b for b in result.blocks if b.block.skipped_reason is None]
    assert len(evaluated) == 2
    for block in evaluated:
        assert block.block.candle_count == DEFAULT_BLOCK_DURATION_DAYS * CANDLES_PER_DAY
        assert block.block.warm_up_candle_count == 9
    # Chronological, non-overlapping.
    for earlier, later in pairwise(evaluated):
        assert earlier.block.window_end_time_ms < later.block.window_start_time_ms
    # First block starts exactly at the segment's own first tradable candle.
    assert evaluated[0].block.window_start_time_ms == candles[9].open_time_ms


def test_fixed_duration_construction_reports_zero_leftover_on_an_exact_multiple():
    strategy = _NeverTradeStrategy(min_required_candles=10)
    n_tradable = 2 * DEFAULT_BLOCK_DURATION_DAYS * CANDLES_PER_DAY
    candles = _flat_candles(10 - 1 + n_tradable)
    result = run_candidate_fixed_duration_evaluation(_spec(strategy), candles, _config(), _filters())
    assert result.leftovers == []


# --- Short-segment exclusion: insufficient duration -> a fragment, never negative votes. ---


def test_segment_too_small_for_warm_up_is_a_fragment_with_zero_available_days():
    strategy = _NeverTradeStrategy(min_required_candles=50)
    candles = _flat_candles(10)  # fewer than the 50 required for warm-up
    result = run_candidate_fixed_duration_evaluation(_spec(strategy), candles, _config(), _filters())

    assert result.blocks == []
    assert len(result.fragments) == 1
    fragment = result.fragments[0]
    assert fragment.available_tradable_duration_days == 0.0
    assert "no tradable candle exists at all" in fragment.reason


def test_segment_with_tradable_candles_but_under_one_year_is_a_fragment_not_five_negative_votes():
    strategy = _NeverTradeStrategy(min_required_candles=10)
    # 100 days of tradable data after warm-up - real, but far short of one
    # complete 365-day block.
    candles = _flat_candles(10 - 1 + 100 * CANDLES_PER_DAY)
    result = run_candidate_fixed_duration_evaluation(_spec(strategy), candles, _config(), _filters())

    assert result.blocks == []
    assert len(result.fragments) == 1
    fragment = result.fragments[0]
    assert 99.0 <= fragment.available_tradable_duration_days <= 100.5
    assert "NO voting block(s) assigned" in fragment.reason
    assert "never reported as zero-trade negative votes" in fragment.reason


# --- Leftover partial-duration reporting. ---


def test_leftover_partial_duration_reported_separately_and_excluded_from_blocks():
    strategy = _NeverTradeStrategy(min_required_candles=10)
    # 1 complete year + 35 leftover days.
    n_tradable = DEFAULT_BLOCK_DURATION_DAYS * CANDLES_PER_DAY + 35 * CANDLES_PER_DAY
    candles = _flat_candles(10 - 1 + n_tradable)
    result = run_candidate_fixed_duration_evaluation(_spec(strategy), candles, _config(), _filters())

    evaluated = [b for b in result.blocks if b.block.skipped_reason is None]
    assert len(evaluated) == 1
    assert result.fragments == []
    assert len(result.leftovers) == 1
    leftover = result.leftovers[0]
    assert 34.5 <= leftover.duration_days <= 35.5
    assert "excluded from every pass/fail" in leftover.note
    # The leftover window starts exactly where the one complete block ended.
    assert leftover.window_start_time_ms > evaluated[0].block.window_end_time_ms


# --- Warm-up isolation: warm-up candles never generate a trade. ---


def test_warm_up_candles_are_never_asked_for_a_signal_and_never_trade():
    strategy = _AlwaysBuyWhenFlatStrategy(min_required_candles=10)
    n_tradable = DEFAULT_BLOCK_DURATION_DAYS * CANDLES_PER_DAY
    candles = _flat_candles(10 - 1 + n_tradable)
    result = run_candidate_fixed_duration_evaluation(_spec(strategy), candles, _config(), _filters())

    evaluated = [b for b in result.blocks if b.block.skipped_reason is None]
    assert len(evaluated) == 1
    window_start_ms = evaluated[0].block.window_start_time_ms
    # generate_signal is never called for a candle strictly before the
    # block's own tradable window start.
    assert all(close_time_ms >= window_start_ms for close_time_ms in strategy.calls_seen_close_times)
    # The strategy always buys immediately when flat, so its first (and,
    # since it never exits, only) position opens exactly at the window's
    # own first candle - never during warm-up.
    if evaluated[0].open_position is not None:
        assert evaluated[0].open_position.entry_time_ms >= window_start_ms
    for trade in evaluated[0].trades:
        assert trade.entry_time_ms >= window_start_ms


# --- No block ever crosses a confirmed gap. ---


def test_fixed_duration_blocks_never_cross_a_confirmed_gap():
    strategy = _NeverTradeStrategy(min_required_candles=10)
    n_per_segment = 10 - 1 + 400 * CANDLES_PER_DAY  # > 1 full year each
    segment0 = _flat_candles(n_per_segment, start=START)
    gap_start = segment0[-1].open_time_ms + 2 * STEP  # one missing interval -> confirmed gap
    segment1 = _flat_candles(n_per_segment, start=gap_start)
    combined = segment0 + segment1

    result = run_candidate_fixed_duration_evaluation(_spec(strategy), combined, _config(), _filters())
    evaluated = [b for b in result.blocks if b.block.skipped_reason is None]

    segment_indices = {b.block.segment_index for b in evaluated}
    assert segment_indices == {0, 1}
    for block in evaluated:
        if block.block.segment_index == 0:
            assert block.block.window_end_time_ms <= segment0[-1].close_time_ms
        else:
            assert block.block.window_start_time_ms >= segment1[0].open_time_ms
    # Each segment supplies exactly one complete block plus its own leftover.
    assert len(evaluated) == 2
    assert len(result.leftovers) == 2
    assert {leftover.segment_index for leftover in result.leftovers} == {0, 1}


# --- Deterministic output. ---


def test_fixed_duration_evaluation_is_deterministic():
    strategy_a = _NeverTradeStrategy(min_required_candles=10)
    strategy_b = _NeverTradeStrategy(min_required_candles=10)
    n_tradable = DEFAULT_BLOCK_DURATION_DAYS * CANDLES_PER_DAY + 10 * CANDLES_PER_DAY
    candles = _flat_candles(10 - 1 + n_tradable)

    first = run_candidate_fixed_duration_evaluation(_spec(strategy_a), candles, _config(), _filters())
    second = run_candidate_fixed_duration_evaluation(_spec(strategy_b), candles, _config(), _filters())
    assert first == second
