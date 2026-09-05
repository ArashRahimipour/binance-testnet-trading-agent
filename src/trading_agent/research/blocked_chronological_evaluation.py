"""Gap-aware BLOCKED CHRONOLOGICAL EVALUATION of a fixed candidate.

Renamed from "walk-forward" before any real candidate evaluation (code
review correction - see CHANGELOG.md): what this module does is NOT
expanding-window walk-forward optimization, and must never be described
that way. Walk-forward optimization re-fits or re-selects a model on each
successive expanding history and tests it on the following block; NOTHING
here fits, trains, or selects anything per block. Every candidate's
parameters are fixed BEFORE this module ever runs (`research/
candidate_registry.py`, declared in code, never touched by a result) and
stay IDENTICAL across every block of every segment. What actually happens
is much simpler: the same one fixed candidate is independently re-run,
unchanged, over successive non-overlapping chronological blocks of the
same pre-cutoff data, purely to see whether its behavior is consistent
across different stretches of history - a robustness check, not an
optimization procedure.

Only ever called on data that has already passed `research/cutoff.py::
assert_pre_cutoff` - this module re-asserts it defensively at its own entry
point too, so a caller mistake can never silently score a candidate on
already-observed data. (`assert_pre_cutoff` itself is a plain check, not an
access-control mechanism - see its own module docstring for the security
boundary this implies.)

For each gap-free contiguous segment (from `data/gap_detection.py::
partition_into_segments` - a confirmed historical gap always starts a new
segment; a block is NEVER built across one), the segment's own tradable
range is split into `block_count` chronological, non-overlapping blocks.
Each block gets a warm-up-prefixed candle slice (warm-up candles precede
the block, are used only to seed the candidate's own indicators, and never
generate a trade or appear in the block's reported performance - the exact
mechanism already proven by `backtest/engine.py::
run_independent_holdout_evaluation`, reused here unchanged via the same
`run_segment` primitive). Each block is therefore its own independent
`run_segment` call: a fresh portfolio (from `config.backtest.
starting_equity`) and fresh risk state, so NO position or risk state of
any kind crosses a block boundary, and no candle beyond a block's own end
is ever visible to it.

Every block is reported, including ones with zero trades, ones too small
to run at all, and (implicitly, since nothing here filters them out)
blocks with negative or otherwise poor results -
`CandidateBlockedChronologicalResult.blocks` is never trimmed or sorted to
hide anything, and there is no mechanism anywhere in this module that
reports only "the best block."

Candidates go through EXACTLY the same broker/fill/fee/slippage/sizing/
risk-engine/accounting path as the frozen baseline (`run_segment` is the
identical function `run_backtest` and `run_independent_holdout_evaluation`
use) - a candidate strategy object has no way to reach or influence any of
that; it only ever returns a `Signal`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from trading_agent.backtest.engine import run_segment
from trading_agent.config.models import AppConfig
from trading_agent.data.gap_detection import partition_into_segments
from trading_agent.data.models import Candle
from trading_agent.execution.backtest_broker import BacktestBroker
from trading_agent.journal.journal import Journal
from trading_agent.metrics.diagnostics import OpenPositionInfo, RunDiagnostics
from trading_agent.metrics.extended_report import (
    ExtendedDiagnosticsReport,
    compute_extended_diagnostics,
)
from trading_agent.metrics.performance import (
    PerformanceReport,
    compute_buy_and_hold_report,
    compute_performance_report,
)
from trading_agent.research.candidate_registry import CandidateSpec
from trading_agent.research.cutoff import assert_pre_cutoff
from trading_agent.risk.engine import RiskEngine
from trading_agent.sizing.exchange_filters import SymbolFilters

#: Default number of chronological blocks per gap-free segment. Fixed and
#: declared here, not tuned per candidate or per result.
DEFAULT_BLOCK_COUNT = 5


@dataclass(frozen=True, slots=True)
class BlockSpec:
    segment_index: int
    block_index: int
    warm_up_start_time_ms: int | None
    warm_up_candle_count: int
    window_start_time_ms: int | None
    window_end_time_ms: int | None
    candle_count: int
    #: None for a normally-evaluated block; a human-readable reason when
    #: the block could not be run at all (e.g. too few candles). Such a
    #: block is still APPENDED to the result, never silently dropped.
    skipped_reason: str | None


@dataclass(frozen=True, slots=True)
class BlockResult:
    block: BlockSpec
    performance: PerformanceReport | None
    diagnostics: RunDiagnostics | None
    extended: ExtendedDiagnosticsReport | None
    open_position: OpenPositionInfo | None
    ends_with_open_position: bool
    unresolved_pending_signal: str | None


@dataclass(frozen=True, slots=True)
class CandidateBlockedChronologicalResult:
    candidate_id: str
    family: str
    params: dict[str, float | int]
    #: EVERY block this candidate was evaluated on, in chronological order
    #: - including skipped and poor-performing ones. Never trimmed to "the
    #: best block(s)".
    blocks: list[BlockResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _block_boundaries(first_tradable_idx: int, n: int, block_count: int) -> list[tuple[int, int]]:
    """Split `[first_tradable_idx, n)` into `block_count` contiguous,
    chronological, non-overlapping ranges, as evenly as candle-count
    division allows (any remainder candles go to the earliest blocks)."""
    tradable_n = n - first_tradable_idx
    base, remainder = divmod(tradable_n, block_count)
    boundaries: list[tuple[int, int]] = []
    cursor = first_tradable_idx
    for k in range(block_count):
        size = base + (1 if k < remainder else 0)
        end = cursor + size
        boundaries.append((cursor, end))
        cursor = end
    return boundaries


def run_candidate_blocked_chronological_evaluation(
    candidate: CandidateSpec,
    pre_cutoff_candles: list[Candle],
    config: AppConfig,
    filters: SymbolFilters,
    journal: Journal | None = None,
    block_count: int = DEFAULT_BLOCK_COUNT,
) -> CandidateBlockedChronologicalResult:
    """Evaluate one FIXED candidate (no fitting, no selection - its
    parameters never change) across every gap-free segment of
    `pre_cutoff_candles`, in `block_count` chronological, non-overlapping
    blocks. This is a robustness check across different stretches of
    history, NOT walk-forward optimization. Raises `ResearchCutoffViolation`
    (via `assert_pre_cutoff`) if `pre_cutoff_candles` contains anything at
    or after the immutable research cutoff - this is checked here
    regardless of what the caller already did (this function IS one of the
    official candidate-scoring entry points required to fail closed at the
    cutoff - see `research/cutoff.py`).
    """
    assert_pre_cutoff(pre_cutoff_candles)
    if block_count <= 0:
        raise ValueError("block_count must be positive")

    interval = config.market.interval
    strategy = candidate.build()
    min_required = strategy.min_required_candles
    risk_engine = RiskEngine(config.risk)
    broker = BacktestBroker(config.fees)
    starting_equity = Decimal(str(config.backtest.starting_equity))

    blocks: list[BlockResult] = []
    warnings: list[str] = []

    if not pre_cutoff_candles:
        warnings.append("no pre-cutoff candles were supplied - nothing to evaluate.")
        return CandidateBlockedChronologicalResult(candidate.candidate_id, candidate.family, candidate.params, blocks, warnings)

    segmentation = partition_into_segments(pre_cutoff_candles, interval)

    for seg_idx, segment in enumerate(segmentation.segments):
        n = len(segment)
        if n < min_required:
            warnings.append(
                f"segment {seg_idx}: only {n} candle(s), fewer than the {min_required} required for "
                f"{candidate.candidate_id}'s warm-up - entire segment skipped."
            )
            blocks.append(
                BlockResult(
                    block=BlockSpec(seg_idx, 0, None, 0, None, None, n, skipped_reason="segment too small for warm-up"),
                    performance=None, diagnostics=None, extended=None, open_position=None,
                    ends_with_open_position=False, unresolved_pending_signal=None,
                )
            )
            continue

        first_tradable_idx = min_required - 1
        tradable_n = n - first_tradable_idx
        effective_block_count = min(block_count, tradable_n) if tradable_n > 0 else 0
        if effective_block_count < block_count:
            warnings.append(
                f"segment {seg_idx}: only {tradable_n} tradable candle(s) after warm-up - using "
                f"{effective_block_count} block(s) instead of the requested {block_count}."
            )
        if effective_block_count == 0:
            continue

        for block_idx, (start_idx, end_idx) in enumerate(_block_boundaries(first_tradable_idx, n, effective_block_count)):
            if end_idx <= start_idx:
                warnings.append(f"segment {seg_idx} block {block_idx}: empty range - skipped.")
                blocks.append(
                    BlockResult(
                        block=BlockSpec(seg_idx, block_idx, None, 0, None, None, 0, skipped_reason="empty block range"),
                        performance=None, diagnostics=None, extended=None, open_position=None,
                        ends_with_open_position=False, unresolved_pending_signal=None,
                    )
                )
                continue

            warm_up_start_idx = start_idx - (min_required - 1)
            window_slice = segment[warm_up_start_idx:end_idx]
            result = run_segment(
                window_slice, config, filters, strategy, risk_engine, broker, journal, min_required, starting_equity
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
                    f"segment {seg_idx} block {block_idx}: only {report.trade_count} trade(s), below the "
                    f"configured significance threshold of {config.backtest.min_trades_for_significance}."
                )
            if result.pending_signal_note is not None:
                warnings.append(f"segment {seg_idx} block {block_idx}: {result.pending_signal_note}")

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
                )
            )

    return CandidateBlockedChronologicalResult(
        candidate_id=candidate.candidate_id, family=candidate.family, params=candidate.params,
        blocks=blocks, warnings=warnings,
    )
