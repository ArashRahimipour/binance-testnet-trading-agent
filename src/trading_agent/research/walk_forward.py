"""Gap-aware, chronological, expanding-window walk-forward development.

Only ever called on data that has already passed `research/cutoff.py::
assert_pre_cutoff` - this module re-asserts it defensively at its own entry
point too, so a caller mistake can never silently score a candidate on
already-observed data.

For each gap-free contiguous segment (from `data/gap_detection.py::
partition_into_segments` - a confirmed historical gap always starts a new
segment; folds are NEVER built across one), the segment's own tradable
range is split into `fold_count` chronological, non-overlapping,
EXPANDING-forward folds. Each fold gets a warm-up-prefixed candle slice
(warm-up candles precede the fold, are used only to seed the candidate's
own indicators, and never generate a trade or appear in the fold's
reported performance - the exact mechanism already proven by
`backtest/engine.py::run_independent_holdout_evaluation`, reused here
unchanged via the same `run_segment` primitive). Each fold is therefore its
own independent `run_segment` call: a fresh portfolio (from
`config.backtest.starting_equity`) and fresh risk state, so NO position or
risk state of any kind crosses a fold boundary, and no candle beyond a
fold's own end is ever visible to it.

Every fold is reported, including ones with zero trades, ones with a fold
too small to run at all, and (implicitly, since nothing here filters them
out) folds with negative or otherwise poor results - `CandidateWalkForwardResult.
folds` is never trimmed or sorted to hide anything, and there is no
mechanism anywhere in this module that reports only "the best fold."

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

#: Default number of chronological folds per gap-free segment. Fixed and
#: declared here, not tuned per candidate or per result.
DEFAULT_FOLD_COUNT = 5


@dataclass(frozen=True, slots=True)
class FoldSpec:
    segment_index: int
    fold_index: int
    warm_up_start_time_ms: int | None
    warm_up_candle_count: int
    window_start_time_ms: int | None
    window_end_time_ms: int | None
    candle_count: int
    #: None for a normally-evaluated fold; a human-readable reason when the
    #: fold could not be run at all (e.g. too few candles). Such a fold is
    #: still APPENDED to the result, never silently dropped.
    skipped_reason: str | None


@dataclass(frozen=True, slots=True)
class FoldResult:
    fold: FoldSpec
    performance: PerformanceReport | None
    diagnostics: RunDiagnostics | None
    extended: ExtendedDiagnosticsReport | None
    open_position: OpenPositionInfo | None
    ends_with_open_position: bool
    unresolved_pending_signal: str | None


@dataclass(frozen=True, slots=True)
class CandidateWalkForwardResult:
    candidate_id: str
    family: str
    params: dict[str, float | int]
    #: EVERY fold this candidate was evaluated on, in chronological order -
    #: including skipped and poor-performing ones. Never trimmed to "the
    #: best fold(s)".
    folds: list[FoldResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _fold_boundaries(first_tradable_idx: int, n: int, fold_count: int) -> list[tuple[int, int]]:
    """Split `[first_tradable_idx, n)` into `fold_count` contiguous,
    chronological, non-overlapping ranges, as evenly as candle-count
    division allows (any remainder candles go to the earliest folds)."""
    tradable_n = n - first_tradable_idx
    base, remainder = divmod(tradable_n, fold_count)
    boundaries: list[tuple[int, int]] = []
    cursor = first_tradable_idx
    for k in range(fold_count):
        size = base + (1 if k < remainder else 0)
        end = cursor + size
        boundaries.append((cursor, end))
        cursor = end
    return boundaries


def run_candidate_walk_forward(
    candidate: CandidateSpec,
    pre_cutoff_candles: list[Candle],
    config: AppConfig,
    filters: SymbolFilters,
    journal: Journal | None = None,
    fold_count: int = DEFAULT_FOLD_COUNT,
) -> CandidateWalkForwardResult:
    """Evaluate one candidate across every gap-free segment of
    `pre_cutoff_candles`, in `fold_count`-fold chronological, expanding-
    window folds. Raises `ResearchCutoffViolation` (via `assert_pre_cutoff`)
    if `pre_cutoff_candles` contains anything at or after the immutable
    research cutoff - this is checked here regardless of what the caller
    already did.
    """
    assert_pre_cutoff(pre_cutoff_candles)
    if fold_count <= 0:
        raise ValueError("fold_count must be positive")

    interval = config.market.interval
    strategy = candidate.build()
    min_required = strategy.min_required_candles
    risk_engine = RiskEngine(config.risk)
    broker = BacktestBroker(config.fees)
    starting_equity = Decimal(str(config.backtest.starting_equity))

    folds: list[FoldResult] = []
    warnings: list[str] = []

    if not pre_cutoff_candles:
        warnings.append("no pre-cutoff candles were supplied - nothing to evaluate.")
        return CandidateWalkForwardResult(candidate.candidate_id, candidate.family, candidate.params, folds, warnings)

    segmentation = partition_into_segments(pre_cutoff_candles, interval)

    for seg_idx, segment in enumerate(segmentation.segments):
        n = len(segment)
        if n < min_required:
            warnings.append(
                f"segment {seg_idx}: only {n} candle(s), fewer than the {min_required} required for "
                f"{candidate.candidate_id}'s warm-up - entire segment skipped."
            )
            folds.append(
                FoldResult(
                    fold=FoldSpec(seg_idx, 0, None, 0, None, None, n, skipped_reason="segment too small for warm-up"),
                    performance=None, diagnostics=None, extended=None, open_position=None,
                    ends_with_open_position=False, unresolved_pending_signal=None,
                )
            )
            continue

        first_tradable_idx = min_required - 1
        tradable_n = n - first_tradable_idx
        effective_fold_count = min(fold_count, tradable_n) if tradable_n > 0 else 0
        if effective_fold_count < fold_count:
            warnings.append(
                f"segment {seg_idx}: only {tradable_n} tradable candle(s) after warm-up - using "
                f"{effective_fold_count} fold(s) instead of the requested {fold_count}."
            )
        if effective_fold_count == 0:
            continue

        for fold_idx, (start_idx, end_idx) in enumerate(_fold_boundaries(first_tradable_idx, n, effective_fold_count)):
            if end_idx <= start_idx:
                warnings.append(f"segment {seg_idx} fold {fold_idx}: empty range - skipped.")
                folds.append(
                    FoldResult(
                        fold=FoldSpec(seg_idx, fold_idx, None, 0, None, None, 0, skipped_reason="empty fold range"),
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
                    f"segment {seg_idx} fold {fold_idx}: only {report.trade_count} trade(s), below the "
                    f"configured significance threshold of {config.backtest.min_trades_for_significance}."
                )
            if result.pending_signal_note is not None:
                warnings.append(f"segment {seg_idx} fold {fold_idx}: {result.pending_signal_note}")

            folds.append(
                FoldResult(
                    fold=FoldSpec(
                        segment_index=seg_idx,
                        fold_index=fold_idx,
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

    return CandidateWalkForwardResult(
        candidate_id=candidate.candidate_id, family=candidate.family, params=candidate.params,
        folds=folds, warnings=warnings,
    )
