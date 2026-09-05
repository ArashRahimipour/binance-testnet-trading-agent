"""Proofs for `FixedDurationBlockSchedule` / `build_fixed_duration_schedule`
- the fix for the cross-candidate date-misalignment defect in commit
50a5a5b: two candidates with DIFFERENT `min_required_candles` evaluated
independently would anchor block 0 on DIFFERENT calendar dates despite a
comparison claiming "identical dates". A schedule built once, anchored at
the LARGER of the two warm-up requirements, and passed to both candidates
fixes this - these tests prove the fix with ACTUAL TIMESTAMP EQUALITY
assertions, not merely a text check for the word "identical".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from tests.fixtures.exchange_info import make_exchange_info
from trading_agent.config.models import AppConfig
from trading_agent.data.models import Candle, interval_to_ms
from trading_agent.research.candidate_registry import CandidateSpec
from trading_agent.research.cutoff import RESEARCH_CUTOFF_MS, ResearchCutoffViolation
from trading_agent.research.fixed_duration_evaluation import (
    DEFAULT_BLOCK_DURATION_DAYS,
    FixedDurationBlockSchedule,
    InsufficientWarmUpForScheduleError,
    ScheduleCutoffViolationError,
    ScheduledBlockWindow,
    ScheduleDurationMismatchError,
    ScheduleOverlapError,
    ScheduleWindowOutOfRangeError,
    _validate_schedule,
    build_fixed_duration_schedule,
    run_candidate_fixed_duration_evaluation,
)
from trading_agent.research.round2_report import (
    IdenticalTradingWindowAssertionError,
    _assert_identical_trading_windows,
)
from trading_agent.sizing.exchange_filters import SymbolFilters
from trading_agent.strategy.base import PositionSide, Signal, SignalType

INTERVAL = "4h"
STEP = interval_to_ms(INTERVAL)
CANDLES_PER_DAY = 24 * 60 * 60 * 1000 // STEP  # 6 for a 4h interval
START = RESEARCH_CUTOFF_MS - 8000 * CANDLES_PER_DAY * STEP


def _candle(i: int, close: float, start: int = START) -> Candle:
    return Candle(
        symbol="BTCUSDT", interval=INTERVAL, open_time_ms=start + i * STEP, close_time_ms=start + i * STEP + STEP - 1,
        open=Decimal(str(close)), high=Decimal(str(close)), low=Decimal(str(close)), close=Decimal(str(close)),
        volume=Decimal(1),
    )


def _flat_candles(n: int, start: int = START, price: float = 100.0) -> list[Candle]:
    return [_candle(i, price, start=start) for i in range(n)]


def _config() -> AppConfig:
    return AppConfig(mode="backtest", backtest={"min_trades_for_significance": 3})


def _filters() -> SymbolFilters:
    return SymbolFilters.from_exchange_info(make_exchange_info())


@dataclass
class _NeverTradeStrategy:
    min_required_candles: int = 10

    def generate_signal(self, candles: list[Candle], current_position: PositionSide) -> Signal:
        return Signal(SignalType.HOLD, "TEST_NEVER_TRADE", candles[-1].close_time_ms, {})


@dataclass
class _AlwaysBuyWhenFlatStrategy:
    min_required_candles: int = 10
    calls_seen_close_times: list[int] = field(default_factory=list)

    def generate_signal(self, candles: list[Candle], current_position: PositionSide) -> Signal:
        self.calls_seen_close_times.append(candles[-1].close_time_ms)
        if current_position == PositionSide.FLAT:
            return Signal(SignalType.BUY, "TEST_ALWAYS_BUY", candles[-1].close_time_ms, {})
        return Signal(SignalType.HOLD, "TEST_HOLD_ALREADY_LONG", candles[-1].close_time_ms, {})


def _spec(candidate_id: str, strategy) -> CandidateSpec:
    return CandidateSpec(candidate_id=candidate_id, family="test_family", params={}, build=lambda: strategy)


# --- Two candidates with DIFFERENT warm-up requirements, one shared schedule. ---


def test_schedule_produces_identical_start_and_end_timestamps_for_different_warm_up_candidates():
    # "Large" candidate needs 220 candles warm-up (D1-like); "small" needs
    # only 21 (B1-like) - different requirements, deliberately.
    large_min_required = 220
    small_min_required = 21
    n_tradable = 2 * DEFAULT_BLOCK_DURATION_DAYS * CANDLES_PER_DAY
    candles = _flat_candles(large_min_required - 1 + n_tradable)

    schedule = build_fixed_duration_schedule(candles, _config(), anchor_warm_up_candles_required=large_min_required)

    large_strategy = _NeverTradeStrategy(min_required_candles=large_min_required)
    small_strategy = _NeverTradeStrategy(min_required_candles=small_min_required)
    large_result = run_candidate_fixed_duration_evaluation(
        _spec("large", large_strategy), candles, _config(), _filters(), strategy=large_strategy, schedule=schedule
    )
    small_result = run_candidate_fixed_duration_evaluation(
        _spec("small", small_strategy), candles, _config(), _filters(), strategy=small_strategy, schedule=schedule
    )

    large_keys = [(b.block.segment_index, b.block.window_start_time_ms, b.block.window_end_time_ms) for b in large_result.blocks]
    small_keys = [(b.block.segment_index, b.block.window_start_time_ms, b.block.window_end_time_ms) for b in small_result.blocks]
    assert len(large_keys) == 2
    assert large_keys == small_keys  # EXACT timestamp equality, not a text check
    _assert_identical_trading_windows(large_result.blocks, small_result.blocks)  # must not raise


def test_candidate_specific_warm_up_lengths_differ_while_trading_dates_remain_identical():
    large_min_required = 220
    small_min_required = 21
    n_tradable = DEFAULT_BLOCK_DURATION_DAYS * CANDLES_PER_DAY
    candles = _flat_candles(large_min_required - 1 + n_tradable)
    schedule = build_fixed_duration_schedule(candles, _config(), anchor_warm_up_candles_required=large_min_required)

    large_strategy = _NeverTradeStrategy(min_required_candles=large_min_required)
    small_strategy = _NeverTradeStrategy(min_required_candles=small_min_required)
    large_result = run_candidate_fixed_duration_evaluation(
        _spec("large", large_strategy), candles, _config(), _filters(), strategy=large_strategy, schedule=schedule
    )
    small_result = run_candidate_fixed_duration_evaluation(
        _spec("small", small_strategy), candles, _config(), _filters(), strategy=small_strategy, schedule=schedule
    )

    assert large_result.blocks[0].block.warm_up_candle_count == large_min_required - 1
    assert small_result.blocks[0].block.warm_up_candle_count == small_min_required - 1
    assert large_result.blocks[0].block.warm_up_candle_count != small_result.blocks[0].block.warm_up_candle_count
    # ...but the trading window itself is identical.
    assert large_result.blocks[0].block.window_start_time_ms == small_result.blocks[0].block.window_start_time_ms
    assert large_result.blocks[0].block.window_end_time_ms == small_result.blocks[0].block.window_end_time_ms


def test_both_candidates_receive_exactly_five_complete_365_day_blocks():
    large_min_required = 220
    n_tradable = 5 * DEFAULT_BLOCK_DURATION_DAYS * CANDLES_PER_DAY
    candles = _flat_candles(large_min_required - 1 + n_tradable)
    schedule = build_fixed_duration_schedule(candles, _config(), anchor_warm_up_candles_required=large_min_required)

    assert len(schedule.windows) == 5
    for window in schedule.windows:
        assert window.window_end_time_ms - window.window_start_time_ms == DEFAULT_BLOCK_DURATION_DAYS * 24 * 60 * 60 * 1000

    large_strategy = _NeverTradeStrategy(min_required_candles=large_min_required)
    small_strategy = _NeverTradeStrategy(min_required_candles=21)
    large_result = run_candidate_fixed_duration_evaluation(
        _spec("large", large_strategy), candles, _config(), _filters(), strategy=large_strategy, schedule=schedule
    )
    small_result = run_candidate_fixed_duration_evaluation(
        _spec("small", small_strategy), candles, _config(), _filters(), strategy=small_strategy, schedule=schedule
    )
    assert len(large_result.blocks) == 5
    assert len(small_result.blocks) == 5


def test_warm_up_candles_are_never_traded_under_a_shared_schedule():
    large_min_required = 220
    n_tradable = DEFAULT_BLOCK_DURATION_DAYS * CANDLES_PER_DAY
    candles = _flat_candles(large_min_required - 1 + n_tradable)
    schedule = build_fixed_duration_schedule(candles, _config(), anchor_warm_up_candles_required=large_min_required)

    strategy = _AlwaysBuyWhenFlatStrategy(min_required_candles=21)
    result = run_candidate_fixed_duration_evaluation(
        _spec("small", strategy), candles, _config(), _filters(), strategy=strategy, schedule=schedule
    )
    window_start_ms = result.blocks[0].block.window_start_time_ms
    assert all(close_time_ms >= window_start_ms for close_time_ms in strategy.calls_seen_close_times)


# --- A one-candle timestamp difference fails. ---


def test_one_candle_timestamp_difference_between_candidates_fails_the_identical_window_assertion():
    from trading_agent.research.blocked_chronological_evaluation import BlockResult, BlockSpec

    def _fake_block(seg_idx: int, start_ms: int, end_ms: int) -> BlockResult:
        return BlockResult(
            block=BlockSpec(seg_idx, 0, None, 0, start_ms, end_ms, 1, skipped_reason=None),
            performance=None, diagnostics=None, extended=None, open_position=None,
            ends_with_open_position=False, unresolved_pending_signal=None,
        )

    d1_blocks = [_fake_block(0, 1_000_000, 2_000_000)]
    b1_blocks_matching = [_fake_block(0, 1_000_000, 2_000_000)]
    b1_blocks_shifted = [_fake_block(0, 1_000_000 + STEP, 2_000_000 + STEP)]  # exactly one candle later

    _assert_identical_trading_windows(d1_blocks, b1_blocks_matching)  # must not raise
    with pytest.raises(IdenticalTradingWindowAssertionError):
        _assert_identical_trading_windows(d1_blocks, b1_blocks_shifted)


# --- Fail closed: insufficient warm-up. ---


def test_insufficient_warm_up_for_schedule_fails_closed():
    small_anchor = 21
    n_tradable = DEFAULT_BLOCK_DURATION_DAYS * CANDLES_PER_DAY
    candles = _flat_candles(small_anchor - 1 + n_tradable)
    # Schedule anchored for only 21 candles of warm-up...
    schedule = build_fixed_duration_schedule(candles, _config(), anchor_warm_up_candles_required=small_anchor)

    # ...but this candidate needs 220 - it cannot possibly have enough
    # preceding history for block 0's own window start.
    demanding_strategy = _NeverTradeStrategy(min_required_candles=220)
    with pytest.raises(InsufficientWarmUpForScheduleError):
        run_candidate_fixed_duration_evaluation(
            _spec("demanding", demanding_strategy), candles, _config(), _filters(),
            strategy=demanding_strategy, schedule=schedule,
        )


def test_build_fixed_duration_schedule_requires_positive_anchor():
    with pytest.raises(ValueError):
        build_fixed_duration_schedule(_flat_candles(50), _config(), anchor_warm_up_candles_required=0)


# --- Fail closed: consumed-cutoff violation. ---


def test_build_fixed_duration_schedule_rejects_post_cutoff_candles():
    candles = _flat_candles(400 * CANDLES_PER_DAY)
    post_cutoff = _candle(len(candles), 100.0, start=RESEARCH_CUTOFF_MS)
    with pytest.raises(ResearchCutoffViolation):
        build_fixed_duration_schedule(candles + [post_cutoff], _config(), anchor_warm_up_candles_required=10)


def test_validate_schedule_fails_closed_on_a_window_touching_the_cutoff():
    duration_ms = DEFAULT_BLOCK_DURATION_DAYS * 24 * 60 * 60 * 1000
    bad_window = ScheduledBlockWindow(0, 0, RESEARCH_CUTOFF_MS - duration_ms + 1, RESEARCH_CUTOFF_MS + 1)
    bad_schedule = FixedDurationBlockSchedule(
        block_duration_days=DEFAULT_BLOCK_DURATION_DAYS, anchor_warm_up_candles_required=10, windows=(bad_window,),
    )
    with pytest.raises(ScheduleCutoffViolationError):
        _validate_schedule(bad_schedule)


# --- Fail closed: overlapping windows. ---


def test_validate_schedule_fails_closed_on_overlapping_windows():
    duration_ms = DEFAULT_BLOCK_DURATION_DAYS * 24 * 60 * 60 * 1000
    window_a = ScheduledBlockWindow(0, 0, START, START + duration_ms)
    window_b = ScheduledBlockWindow(0, 1, START + duration_ms - 1, START + 2 * duration_ms - 1)  # overlaps by 1ms
    bad_schedule = FixedDurationBlockSchedule(
        block_duration_days=DEFAULT_BLOCK_DURATION_DAYS, anchor_warm_up_candles_required=10,
        windows=(window_a, window_b),
    )
    with pytest.raises(ScheduleOverlapError):
        _validate_schedule(bad_schedule)


# --- Fail closed: wrong-duration window. ---


def test_validate_schedule_fails_closed_on_a_window_with_the_wrong_duration():
    bad_window = ScheduledBlockWindow(0, 0, START, START + 100)  # nowhere near 365 days
    bad_schedule = FixedDurationBlockSchedule(
        block_duration_days=DEFAULT_BLOCK_DURATION_DAYS, anchor_warm_up_candles_required=10, windows=(bad_window,),
    )
    with pytest.raises(ScheduleDurationMismatchError):
        _validate_schedule(bad_schedule)


def test_run_candidate_fixed_duration_evaluation_rejects_mismatched_schedule_duration():
    n_tradable = DEFAULT_BLOCK_DURATION_DAYS * CANDLES_PER_DAY
    candles = _flat_candles(9 + n_tradable)
    schedule = build_fixed_duration_schedule(candles, _config(), anchor_warm_up_candles_required=10)
    strategy = _NeverTradeStrategy(min_required_candles=10)
    with pytest.raises(ScheduleDurationMismatchError):
        run_candidate_fixed_duration_evaluation(
            _spec("test", strategy), candles, _config(), _filters(), strategy=strategy, schedule=schedule,
            block_duration_days=180,
        )


# --- Fail closed: a scheduled window that runs into a gap (or otherwise
# no longer has enough candles by the time it is actually applied). ---


def test_scheduled_window_that_runs_out_of_candles_before_its_declared_end_fails_closed():
    # Build a schedule against a LONG, ungapped candle series...
    min_required = 10
    n_tradable = DEFAULT_BLOCK_DURATION_DAYS * CANDLES_PER_DAY
    full_candles = _flat_candles(min_required - 1 + n_tradable)
    schedule = build_fixed_duration_schedule(full_candles, _config(), anchor_warm_up_candles_required=min_required)
    assert len(schedule.windows) == 1

    # ...but apply it against a TRUNCATED series (as if a gap had cut this
    # segment short before the window's own declared end) - the window can
    # no longer resolve to a complete, full-duration candle range.
    truncated_candles = full_candles[: len(full_candles) // 2]
    strategy = _NeverTradeStrategy(min_required_candles=min_required)
    with pytest.raises(ScheduleWindowOutOfRangeError):
        run_candidate_fixed_duration_evaluation(
            _spec("test", strategy), truncated_candles, _config(), _filters(), strategy=strategy, schedule=schedule,
        )
