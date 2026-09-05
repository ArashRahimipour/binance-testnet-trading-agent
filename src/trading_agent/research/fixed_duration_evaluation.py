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
from trading_agent.research.cutoff import assert_pre_cutoff
from trading_agent.risk.engine import RiskEngine
from trading_agent.sizing.exchange_filters import SymbolFilters
from trading_agent.strategy.base import CandidateStrategy

#: Declared, fixed for every candidate - the calendar duration of one
#: sensitivity block. Never tuned per candidate or per result.
DEFAULT_BLOCK_DURATION_DAYS = 365
_MS_PER_DAY = 24 * 60 * 60 * 1000
_BLOCK_DURATION_MS = DEFAULT_BLOCK_DURATION_DAYS * _MS_PER_DAY


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


def run_candidate_fixed_duration_evaluation(
    candidate: CandidateSpec,
    pre_cutoff_candles: list[Candle],
    config: AppConfig,
    filters: SymbolFilters,
    journal: Journal | None = None,
    block_duration_days: int = DEFAULT_BLOCK_DURATION_DAYS,
    strategy: CandidateStrategy | None = None,
) -> CandidateFixedDurationResult:
    """Evaluate one FIXED candidate (no fitting, no selection) across every
    gap-free segment of `pre_cutoff_candles`, in as many complete
    `block_duration_days`-day blocks as each segment's own post-warm-up
    duration allows. Raises `ResearchCutoffViolation` (via
    `assert_pre_cutoff`) if `pre_cutoff_candles` contains anything at or
    after the immutable research cutoff - checked here regardless of what
    the caller already did, exactly like the original module.

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

    blocks: list[BlockResult] = []
    fragments: list[InsufficientDurationFragment] = []
    leftovers: list[LeftoverPartialWindow] = []
    warnings: list[str] = []

    if not pre_cutoff_candles:
        warnings.append("no pre-cutoff candles were supplied - nothing to evaluate.")
        return CandidateFixedDurationResult(candidate.candidate_id, candidate.family, candidate.params, warnings=warnings)

    segmentation = partition_into_segments(pre_cutoff_candles, interval)

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
        open_times = [c.open_time_ms for c in segment]
        tradable_start_ms = open_times[first_tradable_idx]
        # +step_ms: the total covered span includes the LAST candle's own
        # full duration, not just the gap between open timestamps.
        total_tradable_span_ms = (open_times[-1] - tradable_start_ms) + step_ms
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
            start_idx = bisect.bisect_left(open_times, window_start_ms, lo=first_tradable_idx)
            end_idx = bisect.bisect_left(open_times, window_end_ms, lo=start_idx)
            if end_idx <= start_idx:
                warnings.append(f"segment {seg_idx} duration-block {block_idx}: empty candle range - skipped.")
                blocks.append(
                    BlockResult(
                        block=BlockSpec(
                            seg_idx, block_idx, None, 0, None, None, 0, skipped_reason="empty duration-block range"
                        ),
                        performance=None, diagnostics=None, extended=None, open_position=None,
                        ends_with_open_position=False, unresolved_pending_signal=None,
                    )
                )
                continue

            warm_up_start_idx = start_idx - (min_required - 1)
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

            blocks.append(
                BlockResult(
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
