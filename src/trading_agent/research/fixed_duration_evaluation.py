"""DURATION-NORMALIZED sensitivity variant of the blocked chronological
evaluation (`research/blocked_chronological_evaluation.py`, which this
module never modifies, imports mutably, or in any way alters the behavior
of).

MOTIVATION (post-round-1 code review finding, disclosed by the user): the
original method splits every gap-free segment into a FIXED NUMBER of
blocks (`block_count`, default 5) by CANDLE COUNT, regardless of how much
calendar time that segment actually spans. A tiny 297-candle pre-gap
fragment therefore received the same five voting blocks as a 14,000-candle
dominant segment - a real methodology defect that let a handful of
candles vote equally alongside years of history, and let zero-trade
micro-blocks distort `positive_realized_pnl_block_fraction` in
`research/scorecard.py`.

This module fixes the UNIT of a block to a fixed CALENDAR DURATION
(`DEFAULT_BLOCK_DURATION_DAYS`, 365 days) instead of a fixed COUNT per
segment. For each gap-free segment (same `data/gap_detection.py::
partition_into_segments`, so a block still NEVER crosses a confirmed
gap):

  - Required indicator warm-up still precedes every block and never
    trades (identical mechanism to the original module: `first_tradable_idx
    = min_required - 1`, a warm-up-prefixed slice passed into the SAME
    `run_segment` primitive).
  - As many COMPLETE, non-overlapping `DEFAULT_BLOCK_DURATION_DAYS`-day
    blocks as the segment's own post-warm-up duration allows are
    evaluated, each with a FRESH portfolio/risk state (a fresh
    `run_segment` call - no position or risk state of any kind crosses a
    block boundary, exactly like the original method).
  - A segment whose post-warm-up duration cannot supply even ONE complete
    block receives ZERO voting blocks - reported as an
    `InsufficientDurationFragment`, never as a run of negative/zero-trade
    votes (this is the direct fix for the defect above).
  - Any leftover time after a segment's last complete block (less than one
    full `DEFAULT_BLOCK_DURATION_DAYS`-day span) is reported separately as
    a `LeftoverPartialWindow` and is NEVER folded into `CandidateFixedDurationResult.blocks`
    - `research/sensitivity_comparison.py` and any scorecard built from
    `.as_blocked_chronological_result()` therefore never scores it.

SECOND FINDING - CROSS-CANDIDATE DATE MISALIGNMENT (post-commit-50a5a5b code
review correction): calling `run_candidate_fixed_duration_evaluation`
independently for two candidates with DIFFERENT `min_required_candles`
(warm-up length) anchors each candidate's own block 0 at its OWN
`first_tradable_idx = min_required - 1` - so two candidates with different
warm-up requirements end up trading DIFFERENT calendar date ranges, even
though both cover "5 duration-normalized blocks". This is exactly right
when comparing a candidate against ITSELF (or against candidates you do
NOT need identical dates for - see `research/sensitivity_comparison.py`,
which legitimately still uses this independent, per-candidate anchoring
for the nine-candidate report). It is WRONG when the whole point of a
comparison is that both candidates trade the SAME dates (`research/
round2_report.py`'s D1-vs-B1 comparison).

`FixedDurationBlockSchedule` (via `build_fixed_duration_schedule`) fixes
this: it is a schedule of window timestamps computed ONCE, ANCHORED using
the LARGEST warm-up requirement among every candidate that will be
evaluated against it, and independent of any one candidate's own strategy
object. Passing that SAME schedule to `run_candidate_fixed_duration_evaluation`
(via its `schedule=` parameter) for multiple candidates guarantees every
one of them trades the IDENTICAL `(window_start_time_ms, window_end_time_ms)`
per block - each candidate still gets its OWN warm-up length preceding
that window, but the window itself never moves. Every schedule-related
failure mode below is FAIL CLOSED (raises, never silently produces a
misaligned or partial result):
  - a candidate's own warm-up requirement exceeds what the schedule was
    anchored for (`InsufficientWarmUpForScheduleError`);
  - a scheduled window would not resolve to a valid, non-empty,
    single-segment candle range (`ScheduleWindowOutOfRangeError` - the
    concrete way a window "crossing a gap" would manifest, since a
    correctly-built schedule can never actually do this by construction);
  - a scheduled window's own span differs from the schedule's declared
    `block_duration_days` (`ScheduleDurationMismatchError`);
  - two scheduled windows overlap (`ScheduleOverlapError`);
  - a scheduled window would touch or cross the immutable research cutoff
    (`ScheduleCutoffViolationError`);
  - a schedule is applied with a `block_duration_days` that does not match
    the caller's own expectation (`ScheduleDurationMismatchError`).

This module changes NOTHING about any strategy, parameter, threshold,
risk/reward rule, fee, slippage, sizing, or execution behavior - it is a
different BLOCK-CONSTRUCTION methodology only, reusing the exact same
`run_segment` primitive (with the same `use_fixed_risk_reward_policy=True`)
every other research code path uses. It is a SEPARATE, ADDITIONAL
sensitivity lens - see `research/sensitivity_comparison.py` for how its
results are compared against (never used to alter) the original,
unmodified `round_1_original_evaluation`.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from decimal import Decimal
from itertools import pairwise

from trading_agent.backtest.engine import run_segment
from trading_agent.config.models import AppConfig
from trading_agent.data.gap_detection import partition_into_segments
from trading_agent.data.models import Candle, interval_to_ms
from trading_agent.execution.backtest_broker import BacktestBroker
from trading_agent.journal.journal import Journal
from trading_agent.metrics.extended_report import compute_extended_diagnostics
from trading_agent.metrics.performance import (
    compute_buy_and_hold_report,
    compute_performance_report,
)
from trading_agent.research.blocked_chronological_evaluation import (
    BlockResult,
    BlockSpec,
    CandidateBlockedChronologicalResult,
)
from trading_agent.research.candidate_registry import CandidateSpec
from trading_agent.research.cutoff import RESEARCH_CUTOFF_MS, assert_pre_cutoff
from trading_agent.risk.engine import RiskEngine
from trading_agent.sizing.exchange_filters import SymbolFilters
from trading_agent.strategy.base import CandidateStrategy

#: Declared, fixed for every candidate - the calendar duration of one
#: sensitivity block. Never tuned per candidate or per result.
DEFAULT_BLOCK_DURATION_DAYS = 365
_MS_PER_DAY = 24 * 60 * 60 * 1000


class FixedDurationScheduleError(Exception):
    """Base class for every fail-closed fixed-duration schedule violation
    - see the module docstring's "SECOND FINDING" section."""


class InsufficientWarmUpForScheduleError(FixedDurationScheduleError):
    """A candidate's own `min_required_candles` exceeds the warm-up length
    a `FixedDurationBlockSchedule` was anchored for."""


class ScheduleWindowOutOfRangeError(FixedDurationScheduleError):
    """A scheduled window does not resolve to a valid, non-empty,
    single-segment candle range - the concrete way a window could
    otherwise "cross a gap"; unreachable for a schedule this module built
    itself, kept as a fail-closed check against a hand-built or corrupted
    schedule."""


class ScheduleDurationMismatchError(FixedDurationScheduleError):
    """A scheduled window's own span does not equal the schedule's
    declared `block_duration_days`, or a caller applied a schedule built
    for a different `block_duration_days` than it asked for."""


class ScheduleOverlapError(FixedDurationScheduleError):
    """Two windows in the same schedule overlap in time."""


class ScheduleCutoffViolationError(FixedDurationScheduleError):
    """A scheduled window would touch or cross the immutable research
    cutoff (`research/cutoff.py::RESEARCH_CUTOFF_MS`)."""


@dataclass(frozen=True, slots=True)
class InsufficientDurationFragment:
    """A gap-free segment whose own post-warm-up duration could not supply
    even ONE complete `DEFAULT_BLOCK_DURATION_DAYS`-day block - assigned
    ZERO voting blocks, never reported as a negative/zero-trade vote (see
    module docstring)."""

    segment_index: int
    segment_start_time_ms: int
    segment_end_time_ms: int
    candle_count: int
    available_tradable_duration_days: float
    reason: str


@dataclass(frozen=True, slots=True)
class LeftoverPartialWindow:
    """Time left over after a segment's last COMPLETE duration block -
    always strictly less than one full `DEFAULT_BLOCK_DURATION_DAYS`-day
    span. Reported for transparency only; NEVER included in `blocks` and
    therefore never scored by any pass/fail criterion."""

    segment_index: int
    window_start_time_ms: int
    window_end_time_ms: int
    candle_count: int
    duration_days: float
    note: str = (
        "Leftover partial-duration data after this segment's own complete "
        f"{DEFAULT_BLOCK_DURATION_DAYS}-day block(s) - excluded from every pass/fail stability "
        "calculation. Never scored, never a zero-trade vote."
    )


@dataclass(frozen=True, slots=True)
class ScheduledBlockWindow:
    """One block's exact, candidate-agnostic trading window - a pure
    timestamp pair plus its position, computed independently of any
    candidate's own strategy object."""

    segment_index: int
    block_index: int
    window_start_time_ms: int
    window_end_time_ms: int  # exclusive


@dataclass(frozen=True, slots=True)
class FixedDurationBlockSchedule:
    """An immutable, pre-built, candidate-agnostic schedule of duration
    blocks - see the module docstring's "SECOND FINDING" section for why
    this exists. Passing the SAME schedule to `run_candidate_fixed_duration_evaluation`
    for two different candidates guarantees they trade IDENTICAL
    `(window_start_time_ms, window_end_time_ms)` windows, regardless of
    how their own warm-up lengths differ.
    """

    block_duration_days: int
    #: The warm-up length (in candles) this schedule was anchored for -
    #: i.e. `first_tradable_idx = anchor_warm_up_candles_required - 1`
    #: within each segment. Must be >= every candidate's own
    #: `min_required_candles` that will ever be evaluated against this
    #: schedule (see `InsufficientWarmUpForScheduleError`).
    anchor_warm_up_candles_required: int
    windows: tuple[ScheduledBlockWindow, ...] = ()
    fragments: tuple[InsufficientDurationFragment, ...] = ()
    leftovers: tuple[LeftoverPartialWindow, ...] = ()


def _segment_tradable_span(segment: list[Candle], first_tradable_idx: int, step_ms: int) -> tuple[list[int], int, int]:
    open_times = [c.open_time_ms for c in segment]
    tradable_start_ms = open_times[first_tradable_idx]
    # +step_ms: the total covered span includes the LAST candle's own full
    # duration, not just the gap between open timestamps.
    total_tradable_span_ms = (open_times[-1] - tradable_start_ms) + step_ms
    return open_times, tradable_start_ms, total_tradable_span_ms


def build_fixed_duration_schedule(
    pre_cutoff_candles: list[Candle],
    config: AppConfig,
    anchor_warm_up_candles_required: int,
    block_duration_days: int = DEFAULT_BLOCK_DURATION_DAYS,
) -> FixedDurationBlockSchedule:
    """Build a candidate-agnostic schedule of duration blocks, ANCHORED
    using `anchor_warm_up_candles_required` - pass the LARGEST
    `min_required_candles` among every candidate this schedule will be
    applied to (see `InsufficientWarmUpForScheduleError` otherwise).
    Raises `ResearchCutoffViolation` (via `assert_pre_cutoff`) on any
    on-or-after-cutoff candle. Every window is validated (duration,
    non-overlap, no cutoff touch) before this function returns - a
    schedule it hands back is always safe to apply.
    """
    assert_pre_cutoff(pre_cutoff_candles)
    if anchor_warm_up_candles_required <= 0:
        raise ValueError("anchor_warm_up_candles_required must be positive")
    if block_duration_days <= 0:
        raise ValueError("block_duration_days must be positive")
    block_duration_ms = block_duration_days * _MS_PER_DAY

    interval = config.market.interval
    step_ms = interval_to_ms(interval)

    windows: list[ScheduledBlockWindow] = []
    fragments: list[InsufficientDurationFragment] = []
    leftovers: list[LeftoverPartialWindow] = []

    if not pre_cutoff_candles:
        return FixedDurationBlockSchedule(block_duration_days, anchor_warm_up_candles_required)

    segmentation = partition_into_segments(pre_cutoff_candles, interval)

    for seg_idx, segment in enumerate(segmentation.segments):
        n = len(segment)
        if n < anchor_warm_up_candles_required:
            fragments.append(
                InsufficientDurationFragment(
                    segment_index=seg_idx, segment_start_time_ms=segment[0].open_time_ms,
                    segment_end_time_ms=segment[-1].close_time_ms, candle_count=n,
                    available_tradable_duration_days=0.0,
                    reason=(
                        f"only {n} candle(s), fewer than the {anchor_warm_up_candles_required} the schedule "
                        "was anchored for - no tradable candle exists at all."
                    ),
                )
            )
            continue

        first_tradable_idx = anchor_warm_up_candles_required - 1
        open_times, tradable_start_ms, total_tradable_span_ms = _segment_tradable_span(
            segment, first_tradable_idx, step_ms
        )
        possible_blocks = total_tradable_span_ms // block_duration_ms

        if possible_blocks == 0:
            fragments.append(
                InsufficientDurationFragment(
                    segment_index=seg_idx, segment_start_time_ms=segment[0].open_time_ms,
                    segment_end_time_ms=segment[-1].close_time_ms, candle_count=n,
                    available_tradable_duration_days=total_tradable_span_ms / _MS_PER_DAY,
                    reason=(
                        f"only {total_tradable_span_ms / _MS_PER_DAY:.1f} tradable day(s) after warm-up, fewer "
                        f"than one complete {block_duration_days}-day block - NO voting block(s) assigned "
                        "(never reported as zero-trade negative votes)."
                    ),
                )
            )
            continue

        segment_last_close_ms = segment[-1].close_time_ms
        for block_idx in range(possible_blocks):
            window_start_ms = tradable_start_ms + block_idx * block_duration_ms
            window_end_ms = window_start_ms + block_duration_ms
            # By construction (possible_blocks is floored), this can never
            # exceed the segment's own bound - checked anyway, since a
            # schedule must be safe to trust without re-deriving this math.
            if window_end_ms > segment_last_close_ms + 1:
                raise ScheduleWindowOutOfRangeError(  # pragma: no cover - defensive, unreachable by construction
                    f"segment {seg_idx} block {block_idx}: computed window end {window_end_ms} exceeds this "
                    f"segment's own last candle close {segment_last_close_ms}."
                )
            windows.append(ScheduledBlockWindow(seg_idx, block_idx, window_start_ms, window_end_ms))

        leftover_start_ms = tradable_start_ms + possible_blocks * block_duration_ms
        segment_end_ms = open_times[-1] + step_ms
        if leftover_start_ms < segment_end_ms:
            leftover_start_idx = bisect.bisect_left(open_times, leftover_start_ms, lo=first_tradable_idx)
            if leftover_start_idx < n:
                leftovers.append(
                    LeftoverPartialWindow(
                        segment_index=seg_idx,
                        window_start_time_ms=segment[leftover_start_idx].open_time_ms,
                        window_end_time_ms=segment[-1].close_time_ms,
                        candle_count=n - leftover_start_idx,
                        duration_days=(segment_end_ms - leftover_start_ms) / _MS_PER_DAY,
                    )
                )

    schedule = FixedDurationBlockSchedule(
        block_duration_days=block_duration_days,
        anchor_warm_up_candles_required=anchor_warm_up_candles_required,
        windows=tuple(windows),
        fragments=tuple(fragments),
        leftovers=tuple(leftovers),
    )
    _validate_schedule(schedule)
    return schedule


def _validate_schedule(schedule: FixedDurationBlockSchedule) -> None:
    """Fail closed on: wrong-duration windows, overlapping windows, or a
    window touching/crossing the immutable research cutoff. Called once,
    automatically, at the end of `build_fixed_duration_schedule` - a
    schedule that successfully returns from that function has already
    passed this check."""
    expected_duration_ms = schedule.block_duration_days * _MS_PER_DAY
    for window in schedule.windows:
        actual_duration_ms = window.window_end_time_ms - window.window_start_time_ms
        if actual_duration_ms != expected_duration_ms:
            raise ScheduleDurationMismatchError(
                f"segment {window.segment_index} block {window.block_index}: window duration "
                f"{actual_duration_ms}ms != declared {expected_duration_ms}ms."
            )
        if window.window_end_time_ms > RESEARCH_CUTOFF_MS:
            raise ScheduleCutoffViolationError(
                f"segment {window.segment_index} block {window.block_index}: window_end_time_ms="
                f"{window.window_end_time_ms} touches or crosses the immutable research cutoff "
                f"({RESEARCH_CUTOFF_MS})."
            )

    ordered = sorted(schedule.windows, key=lambda w: w.window_start_time_ms)
    for earlier, later in pairwise(ordered):
        if earlier.window_end_time_ms > later.window_start_time_ms:
            raise ScheduleOverlapError(
                f"window (segment={earlier.segment_index}, block={earlier.block_index}) "
                f"[{earlier.window_start_time_ms}, {earlier.window_end_time_ms}) overlaps window "
                f"(segment={later.segment_index}, block={later.block_index}) "
                f"[{later.window_start_time_ms}, {later.window_end_time_ms})."
            )


@dataclass(frozen=True, slots=True)
class CandidateFixedDurationResult:
    candidate_id: str
    family: str
    params: dict[str, float | int]
    #: Only COMPLETE, full-duration blocks - reuses the exact `BlockResult`/
    #: `BlockSpec` types from the original module so this can be scored by
    #: the SAME, unmodified `research/scorecard.py::score_candidate` via
    #: `as_blocked_chronological_result()`.
    blocks: list[BlockResult] = field(default_factory=list)
    fragments: list[InsufficientDurationFragment] = field(default_factory=list)
    leftovers: list[LeftoverPartialWindow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_blocked_chronological_result(self) -> CandidateBlockedChronologicalResult:
        """A read-only view shaped exactly like the original module's own
        result type, so the UNMODIFIED `scorecard.score_candidate` can be
        reused verbatim - no scoring logic is duplicated or reimplemented."""
        return CandidateBlockedChronologicalResult(
            candidate_id=self.candidate_id, family=self.family, params=self.params,
            blocks=self.blocks, warnings=self.warnings,
        )


def _evaluate_duration_block(
    segment: list[Candle],
    seg_idx: int,
    block_idx: int,
    window_start_ms: int,
    window_end_ms: int,
    min_required: int,
    strategy: CandidateStrategy,
    risk_engine: RiskEngine,
    broker: BacktestBroker,
    config: AppConfig,
    filters: SymbolFilters,
    journal: Journal | None,
    interval: str,
    starting_equity: Decimal,
    warnings: list[str],
    strict: bool,
) -> BlockResult:
    open_times = [c.open_time_ms for c in segment]
    start_idx = bisect.bisect_left(open_times, window_start_ms)
    end_idx = bisect.bisect_left(open_times, window_end_ms, lo=start_idx)
    # A schedule window built against a DIFFERENT (or since-changed) candle
    # series can resolve to a segment that runs out of candles before the
    # window's own declared end (e.g. a gap now truncates this "segment"
    # earlier than the schedule assumed) - `bisect_left` would otherwise
    # silently clamp `end_idx` to `len(segment)` and hand back a SHORTER,
    # not-actually-full-duration block. Caught explicitly rather than ever
    # silently under-filling a block.
    window_runs_past_available_candles = end_idx == len(segment) and (
        len(segment) == 0 or segment[-1].close_time_ms + 1 < window_end_ms
    )

    if end_idx <= start_idx or window_runs_past_available_candles:
        if strict:
            raise ScheduleWindowOutOfRangeError(
                f"segment {seg_idx} block {block_idx}: scheduled window [{window_start_ms}, {window_end_ms}) "
                "does not resolve to a valid, complete, non-empty candle range within this segment - this "
                "segment either has no candle in this range at all, or runs out of candles (e.g. a "
                "confirmed gap) before the window's own declared end."
            )
        warnings.append(f"segment {seg_idx} duration-block {block_idx}: empty candle range - skipped.")
        return BlockResult(
            block=BlockSpec(seg_idx, block_idx, None, 0, None, None, 0, skipped_reason="empty duration-block range"),
            performance=None, diagnostics=None, extended=None, open_position=None,
            ends_with_open_position=False, unresolved_pending_signal=None,
        )

    warm_up_start_idx = start_idx - (min_required - 1)
    if warm_up_start_idx < 0:
        raise InsufficientWarmUpForScheduleError(
            f"this candidate requires {min_required} warm-up candle(s) but segment {seg_idx} block "
            f"{block_idx}'s window starts only {start_idx} candle(s) into its own segment - a schedule "
            "must be anchored using the LARGEST warm-up requirement among every candidate it is "
            "applied to (see build_fixed_duration_schedule's anchor_warm_up_candles_required)."
        )

    window_slice = segment[warm_up_start_idx:end_idx]
    result = run_segment(
        window_slice, config, filters, strategy, risk_engine, broker, journal, min_required, starting_equity,
        use_fixed_risk_reward_policy=True,
    )
    window_candles = segment[start_idx:end_idx]
    buy_and_hold = compute_buy_and_hold_report(window_candles, starting_equity, config.fees.taker_fee_pct)
    report = compute_performance_report(
        result.trades, result.equity_curve, interval, config.backtest.min_trades_for_significance,
        buy_and_hold=buy_and_hold,
    )
    window_end_time_ms = segment[end_idx - 1].close_time_ms
    extended = compute_extended_diagnostics(
        trades=result.trades,
        equity_curve=result.equity_curve,
        window_end_time_ms=window_end_time_ms,
        starting_equity=starting_equity,
        ending_cash_quote=result.diagnostics.ending_cash_quote,
        ending_base_quantity=result.diagnostics.ending_base_quantity,
        final_mark_price=segment[end_idx - 1].close,
        reported_ending_equity=result.diagnostics.ending_equity,
        open_position=result.open_position,
        executed_entries=result.diagnostics.executed_entries,
        buy_signals_generated=result.diagnostics.buy_signals_generated,
        exposure_pct=report.exposure_pct,
        total_return_pct=report.total_return_pct,
        annualized_return_pct=report.annualized_return_pct,
        max_drawdown_pct=report.max_drawdown_pct,
        shutdown_activations=result.diagnostics.shutdown_activations,
        rejected_entries_by_reason=result.diagnostics.rejected_entries_by_reason,
    )

    if report.low_trade_count_warning:
        warnings.append(
            f"segment {seg_idx} duration-block {block_idx}: only {report.trade_count} trade(s), below "
            f"the configured significance threshold of {config.backtest.min_trades_for_significance}."
        )
    if result.pending_signal_note is not None:
        warnings.append(f"segment {seg_idx} duration-block {block_idx}: {result.pending_signal_note}")

    return BlockResult(
        block=BlockSpec(
            segment_index=seg_idx,
            block_index=block_idx,
            warm_up_start_time_ms=segment[warm_up_start_idx].open_time_ms,
            warm_up_candle_count=min_required - 1,
            window_start_time_ms=segment[start_idx].open_time_ms,
            window_end_time_ms=window_end_time_ms,
            candle_count=end_idx - start_idx,
            skipped_reason=None,
        ),
        performance=report,
        diagnostics=result.diagnostics,
        extended=extended,
        open_position=result.open_position,
        ends_with_open_position=result.ends_with_open_position,
        unresolved_pending_signal=result.pending_signal_note,
        risk_reward=result.risk_reward,
        trades=tuple(result.trades),
    )


def run_candidate_fixed_duration_evaluation(
    candidate: CandidateSpec,
    pre_cutoff_candles: list[Candle],
    config: AppConfig,
    filters: SymbolFilters,
    journal: Journal | None = None,
    block_duration_days: int = DEFAULT_BLOCK_DURATION_DAYS,
    strategy: CandidateStrategy | None = None,
    schedule: FixedDurationBlockSchedule | None = None,
) -> CandidateFixedDurationResult:
    """Evaluate one FIXED candidate (no fitting, no selection).

    Two modes:

    - `schedule=None` (default): independently derives this candidate's
      OWN block boundaries, anchored at ITS OWN `min_required_candles` -
      exactly the original per-candidate behavior. Correct when comparing
      a candidate only against itself (e.g. `research/
      sensitivity_comparison.py`'s nine-candidate report) - see the
      module docstring's "SECOND FINDING" section for why this is WRONG
      when two different candidates must be compared on identical dates.
    - `schedule=<a FixedDurationBlockSchedule>`: every block's
      `(window_start_time_ms, window_end_time_ms)` comes from the
      schedule verbatim, identical for every candidate it is applied to.
      This candidate still gets its OWN warm-up length preceding that
      window - fails closed (`InsufficientWarmUpForScheduleError`) if its
      own `min_required_candles` exceeds what the schedule was anchored
      for.

    Raises `ResearchCutoffViolation` (via `assert_pre_cutoff`) if
    `pre_cutoff_candles` contains anything at or after the immutable
    research cutoff - checked here regardless of what the caller already
    did, exactly like the original module.

    `strategy`: normally omitted (built fresh via `candidate.build()`,
    exactly like the original module). Pass an already-built instance only
    to observe read-only, decision-irrelevant instance counters a
    candidate strategy may expose for reporting purposes (e.g.
    `research/candidates/breakout_regime_gate.py`'s regime-gate-blocked
    counters) - the SAME strategy object is then used for every block, so
    such counters accumulate across the whole run, exactly as they would
    if built internally.
    """
    assert_pre_cutoff(pre_cutoff_candles)
    if block_duration_days <= 0:
        raise ValueError("block_duration_days must be positive")
    block_duration_ms = block_duration_days * _MS_PER_DAY

    interval = config.market.interval
    step_ms = interval_to_ms(interval)
    strategy = strategy if strategy is not None else candidate.build()
    min_required = strategy.min_required_candles
    risk_engine = RiskEngine(config.risk)
    broker = BacktestBroker(config.fees)
    starting_equity = Decimal(str(config.backtest.starting_equity))

    warnings: list[str] = []

    if not pre_cutoff_candles:
        warnings.append("no pre-cutoff candles were supplied - nothing to evaluate.")
        return CandidateFixedDurationResult(candidate.candidate_id, candidate.family, candidate.params, warnings=warnings)

    segmentation = partition_into_segments(pre_cutoff_candles, interval)

    if schedule is not None:
        if schedule.block_duration_days != block_duration_days:
            raise ScheduleDurationMismatchError(
                f"schedule.block_duration_days={schedule.block_duration_days} != "
                f"block_duration_days={block_duration_days} requested for this evaluation."
            )
        blocks = [
            _evaluate_duration_block(
                segmentation.segments[window.segment_index], window.segment_index, window.block_index,
                window.window_start_time_ms, window.window_end_time_ms, min_required, strategy, risk_engine,
                broker, config, filters, journal, interval, starting_equity, warnings, strict=True,
            )
            for window in schedule.windows
        ]
        return CandidateFixedDurationResult(
            candidate_id=candidate.candidate_id, family=candidate.family, params=candidate.params,
            blocks=blocks, fragments=list(schedule.fragments), leftovers=list(schedule.leftovers),
            warnings=warnings,
        )

    blocks = []
    fragments: list[InsufficientDurationFragment] = []
    leftovers: list[LeftoverPartialWindow] = []

    for seg_idx, segment in enumerate(segmentation.segments):
        n = len(segment)
        if n < min_required:
            fragments.append(
                InsufficientDurationFragment(
                    segment_index=seg_idx, segment_start_time_ms=segment[0].open_time_ms,
                    segment_end_time_ms=segment[-1].close_time_ms, candle_count=n,
                    available_tradable_duration_days=0.0,
                    reason=(
                        f"only {n} candle(s), fewer than the {min_required} required for "
                        f"{candidate.candidate_id}'s own warm-up - no tradable candle exists at all."
                    ),
                )
            )
            continue

        first_tradable_idx = min_required - 1
        open_times, tradable_start_ms, total_tradable_span_ms = _segment_tradable_span(
            segment, first_tradable_idx, step_ms
        )
        possible_blocks = total_tradable_span_ms // block_duration_ms

        if possible_blocks == 0:
            fragments.append(
                InsufficientDurationFragment(
                    segment_index=seg_idx, segment_start_time_ms=segment[0].open_time_ms,
                    segment_end_time_ms=segment[-1].close_time_ms, candle_count=n,
                    available_tradable_duration_days=total_tradable_span_ms / _MS_PER_DAY,
                    reason=(
                        f"only {total_tradable_span_ms / _MS_PER_DAY:.1f} tradable day(s) after warm-up, fewer "
                        f"than one complete {block_duration_days}-day block - NO voting block(s) assigned "
                        "(never reported as zero-trade negative votes)."
                    ),
                )
            )
            continue

        for block_idx in range(possible_blocks):
            window_start_ms = tradable_start_ms + block_idx * block_duration_ms
            window_end_ms = window_start_ms + block_duration_ms
            blocks.append(
                _evaluate_duration_block(
                    segment, seg_idx, block_idx, window_start_ms, window_end_ms, min_required, strategy,
                    risk_engine, broker, config, filters, journal, interval, starting_equity, warnings,
                    strict=False,
                )
            )

        leftover_start_ms = tradable_start_ms + possible_blocks * block_duration_ms
        segment_end_ms = open_times[-1] + step_ms
        if leftover_start_ms < segment_end_ms:
            leftover_start_idx = bisect.bisect_left(open_times, leftover_start_ms, lo=first_tradable_idx)
            if leftover_start_idx < n:
                leftovers.append(
                    LeftoverPartialWindow(
                        segment_index=seg_idx,
                        window_start_time_ms=segment[leftover_start_idx].open_time_ms,
                        window_end_time_ms=segment[-1].close_time_ms,
                        candle_count=n - leftover_start_idx,
                        duration_days=(segment_end_ms - leftover_start_ms) / _MS_PER_DAY,
                    )
                )

    return CandidateFixedDurationResult(
        candidate_id=candidate.candidate_id, family=candidate.family, params=candidate.params,
        blocks=blocks, fragments=fragments, leftovers=leftovers, warnings=warnings,
    )
