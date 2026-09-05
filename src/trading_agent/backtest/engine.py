"""End-to-end backtest engine.

Execution timing (review Finding 3): a signal is DETECTED from candle i's
close, but can only be FILLED no earlier than candle i+1's open - you
cannot realistically react to a candle closing and get filled at that same
close price in the same instant. So the loop below queues an actionable
signal at the end of processing candle i and resolves it (applying
adverse, configurable slippage and fees to candle i+1's open) at the START
of processing candle i+1. A signal generated on the very last candle in the
series has no i+1 to resolve against and is reported as unexecuted rather
than silently filled or dropped.

Protective stop-loss (review Finding 4, cost-awareness added in round 2
Finding 6): since `max_risk_per_trade_pct` is a genuine risk-budget figure
here (see sizing/position_sizer.py::compute_risk_based_buy_quantity, which
sizes against entry/exit slippage and fees too, not just the bare
entry-to-stop price gap), every backtest entry carries a stop price at a
fixed percentage below its fill price (config.stop_loss). Each subsequent
candle's LOW is checked against the active stop - if touched, the position
is closed intrabar at (the worse of) the stop price or that candle's open,
modeling gap risk conservatively. An ordinary (non-gap) stop's expected
loss is therefore bounded by the risk budget; a gap through the stop can
still exceed it - a disclosed limitation of a fixed-percentage stop, not
something position sizing alone can prevent. This is backtest-only in this
revision; automatic entry is disabled on Testnet pending a verified
exchange-resident protective order (see execution/live_runner.py and
RISK_POLICY.md). Each completed `Trade.exit_reason` records whether it
closed via this stop ("STOP_LOSS") or via the strategy's own EXIT signal
("STRATEGY_EXIT") - see metrics/performance.py.

Every proposed trade goes through the same RiskEngine and OrderValidator
that a testnet run would use; only the broker is swapped for
`BacktestBroker`'s simulated fills.

Historical-gap segmentation (`config.backtest.gap_policy`): a downloaded
historical series can contain a CONFIRMED gap (see data/gap_detection.py,
data/historical_fetch.py) - a real, permanent hole in the exchange's own
record, never fabricated or interpolated over. Two policies:

  - "reject" (the original strict behavior): any gap raises, exactly like
    live/Testnet's `validate_candle_sequence` - never used by live/Testnet
    itself, which calls that function directly and never reads this
    config at all.
  - "segment" (the default for this research-only command): the series is
    split into independent contiguous segments at each gap. Each segment
    runs as its own fresh backtest - fresh portfolio (from
    `config.backtest.starting_equity`), fresh indicator warm-up, fresh
    day/cooldown state - so nothing is ever carried across a gap. A signal
    still pending at the end of a non-final segment is cancelled, not
    carried forward. A position still open at the end of a non-final
    segment is a genuinely unresolved research condition - no exit price
    is ever invented for it, and by default
    (`exclude_open_position_segments`) that segment's already-completed
    trades are excluded from any aggregate.

Two independent evaluation modes (see the corrected-reporting round):

  1. `run_backtest` - the CONTINUOUS OPERATIONAL SIMULATION: what would
     actually have happened if the system began trading at the first
     candle and retained its risk state (peak equity, drawdown, cooldowns,
     daily counters) continuously for as long as data allows within each
     contiguous segment. When exactly one segment is actually backtested
     (the overwhelmingly common case - no confirmed gap), `result.reports`
     still exposes the familiar `"train"/"validation"/"test"/"overall"`
     keys, informational chronological slices of that ONE continuous run
     (fixed strategy parameters throughout - never refit per slice). This
     is why a risk shutdown (e.g. `MAX_DRAWDOWN_SHUTDOWN`) latched during
     the portion of the run labeled "train" mechanically persists,
     unchanged, through everything subsequently labeled "validation" and
     "test": these are timeline labels on one uninterrupted simulation,
     not independent evaluations. `result.diagnostics` makes this
     mechanism directly inspectable (signal/rejection counts by exact
     reason code, first shutdown activation with the equity/drawdown that
     triggered it, whether it stayed latched, first/last executed trade
     timestamps, ending cash/asset/equity - see `RunDiagnostics`) so this
     is never left to be inferred from the numbers alone. When MORE than
     one segment is actually backtested (a confirmed gap was found),
     `result.reports` is empty and `result.segments[i].performance` holds
     a full, independent `PerformanceReport` per segment instead - see the
     module-level note below on why a single naively-concatenated
     "overall" is never produced in that case.

  2. `run_independent_holdout_evaluation` - INDEPENDENT FIXED-PARAMETER
     HOLDOUT EVALUATION, NOT walk-forward optimization: train/validation/
     test windows that use the SAME fixed strategy parameters but each
     start with a completely FRESH configured starting balance and fresh
     risk state (peak equity, drawdown, cooldowns, day counters all reset
     to their initial values). A window may look back at PRECEDING candles
     from the same gap-free segment for indicator warm-up only - those
     warm-up candles never generate a trade or contribute to the window's
     performance, never reach into a different segment across a confirmed
     gap, and no candle beyond the window's own end is ever visible to it.
     A position or pending signal open at a window's end is reported as
     such but is NEVER carried into the next window. This directly answers
     "what would validation/test have looked like on their own merits,
     without inheriting train's risk state" - see `HoldoutEvaluationResult`.

Gap-segment aggregate reporting: the equity curve of one segment is NOT
commensurable with another's (each restarts from the same baseline
`starting_equity` rather than continuing the previous segment's ending
balance) - concatenating them and computing an ordinary percentage
return/drawdown/Sharpe/Sortino over the naive concatenation would silently
describe an account that never existed. This module therefore never does
that: with more than one segment, only a full per-segment
`PerformanceReport` (independent, correct) and an explicitly-labeled
`AggregateTradeStats` (only mathematically valid trade-level sums/ratios -
total realized PnL in quote currency, overall win rate - never a
percentage return or drawdown) are produced.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal

from trading_agent.backtest.risk_reward import (
    EXCHANGE_FILTER_REJECTION_REASONS,
    RR_REJECTED_NET_REWARD_TO_RISK_BELOW_MINIMUM,
    RiskRewardDiagnostics,
    RiskRewardPlan,
    build_realized_plan,
    plan_risk_reward_entry,
)
from trading_agent.config.models import AppConfig
from trading_agent.data.gap_detection import GapRecord, partition_into_segments
from trading_agent.data.models import Candle
from trading_agent.data.validation import validate_candle_sequence
from trading_agent.execution.backtest_broker import BacktestBroker, SimulatedFill
from trading_agent.execution.order_validator import validate_order
from trading_agent.journal.journal import Journal
from trading_agent.metrics.diagnostics import OpenPositionInfo, RunDiagnostics, ShutdownActivation
from trading_agent.metrics.extended_report import (
    ExtendedDiagnosticsReport,
    compute_extended_diagnostics,
)
from trading_agent.metrics.performance import (
    EXIT_REASON_STOP_LOSS,
    EXIT_REASON_STRATEGY,
    EXIT_REASON_TAKE_PROFIT,
    EquityPoint,
    PerformanceReport,
    Trade,
    compute_buy_and_hold_report,
    compute_performance_report,
)
from trading_agent.portfolio.state import PortfolioState, apply_buy, apply_sell
from trading_agent.risk.engine import RiskEngine
from trading_agent.risk.limits import RiskContext, TradeIntent
from trading_agent.sizing.exchange_filters import SymbolFilters
from trading_agent.sizing.position_sizer import (
    SizingDecision,
    compute_risk_based_buy_quantity,
    compute_sell_quantity,
)
from trading_agent.strategy.base import PositionSide, SignalGenerator, SignalType
from trading_agent.strategy.trend_baseline import EmaCrossoverTrendStrategy

_MS_PER_DAY = 24 * 60 * 60 * 1000

SPLIT_NAMES = ("train", "validation", "test")

#: Label attached to every result of `run_independent_holdout_evaluation` -
#: printed verbatim by the CLI so this is never confused with genuine
#: rolling walk-forward re-optimization (strategy parameters are always
#: fixed here, never fit to any window).
HOLDOUT_EVALUATION_LABEL = (
    "INDEPENDENT FIXED-PARAMETER HOLDOUT EVALUATION - NOT walk-forward optimization. "
    "Train/validation/test windows share identical, fixed strategy parameters. Each window "
    "starts from a fresh configured starting balance and fresh risk state (peak equity, "
    "drawdown, cooldowns, and daily counters all reset); no position or pending signal ever "
    "carries from one window into the next; validation/test may look back only at preceding "
    "candles from the same gap-free segment for indicator warm-up, and those warm-up candles "
    "never generate a trade or contribute to the reported performance."
)


@dataclass(frozen=True, slots=True)
class SegmentReport:
    index: int
    start_time_ms: int
    end_time_ms: int
    candle_count: int
    trade_count: int
    ends_with_open_position: bool
    excluded_from_overall: bool
    cancelled_pending_signal: str | None
    skipped_insufficient_candles: bool
    #: Full, independent performance report for this segment alone (its
    #: own continuous run, starting fresh from `config.backtest.
    #: starting_equity`). None only when the segment was skipped entirely
    #: for having too few candles for indicator warm-up.
    performance: PerformanceReport | None = None
    diagnostics: RunDiagnostics | None = None
    open_position: OpenPositionInfo | None = None
    extended: ExtendedDiagnosticsReport | None = None


@dataclass(frozen=True, slots=True)
class AggregateTradeStats:
    """An EXPLICITLY-LABELED aggregate across multiple independent
    segments - contains ONLY figures that remain mathematically valid when
    summed/ratioed across independently-restarted equity curves (money
    amounts, trade counts, a win-rate ratio). It deliberately has no
    return/drawdown/Sharpe/Sortino field: those require one single valid
    continuous equity curve, which a naive concatenation across a gap is
    not - see the module docstring.
    """

    segments_included: int
    total_trades: int
    total_realized_pnl_quote: Decimal
    win_rate: float | None
    total_strategy_exits: int
    total_stop_loss_exits: int
    note: str = (
        "Trade-level aggregate ONLY across independently-restarted segments (each began from "
        "the same baseline starting_equity, not from the previous segment's ending balance). "
        "Money amounts and counts sum validly; there is deliberately no aggregate percentage "
        "return, drawdown, Sharpe, or Sortino here - those require one single continuous equity "
        "curve, which this is not. Read each segment's own PerformanceReport for those."
    )


@dataclass(frozen=True, slots=True)
class BacktestResult:
    trades: list[Trade]
    equity_curve: list[EquityPoint]
    #: Populated with the familiar "train"/"validation"/"test"/"overall"
    #: keys ONLY when exactly one segment was actually backtested (the
    #: no-confirmed-gap case). Empty when more than one segment ran - see
    #: `segments[i].performance` and `aggregate_trade_stats` instead.
    reports: dict[str, PerformanceReport]
    warnings: list[str]
    unexecuted_final_signal: str | None  # reason a queued signal on the last candle went unfilled
    gaps: list[GapRecord] = field(default_factory=list)
    segments: list[SegmentReport] = field(default_factory=list)
    aggregate_trade_stats: AggregateTradeStats | None = None
    #: Diagnostics for the single segment backtested, when `reports` is
    #: populated (see above). None when multiple segments ran - use each
    #: segment's own `diagnostics` in that case.
    diagnostics: RunDiagnostics | None = None
    #: Extended diagnostics (accounting identity, PnL breakdown, time-based
    #: and trade-distribution stats, bootstrap CI, rolling windows) keyed
    #: the same as `reports` - populated only alongside `reports`.
    extended_reports: dict[str, ExtendedDiagnosticsReport] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HoldoutWindowReport:
    segment_index: int
    label: str  # "train" | "validation" | "test"
    warm_up_start_time_ms: int
    warm_up_candle_count: int
    window_start_time_ms: int
    window_end_time_ms: int
    candle_count: int
    performance: PerformanceReport
    diagnostics: RunDiagnostics
    ends_with_open_position: bool
    unresolved_pending_signal: str | None
    open_position: OpenPositionInfo | None
    extended: ExtendedDiagnosticsReport


@dataclass(frozen=True, slots=True)
class HoldoutEvaluationResult:
    label: str
    windows: list[HoldoutWindowReport]
    warnings: list[str]


@dataclass
class _OpenTrade:
    entry_time_ms: int
    entry_price: Decimal
    quantity: Decimal
    entry_fee: Decimal
    #: The pre-slippage reference price (the fill candle's open) - recorded
    #: for slippage-cost reporting only; changes no fee/slippage/fill
    #: calculation.
    entry_reference_price: Decimal
    #: The REALIZED risk/reward plan (`backtest/risk_reward.py::
    #: build_realized_plan`) - None unless this position was opened under
    #: `use_fixed_risk_reward_policy=True`. `target_price` is checked
    #: alongside `_LoopState.stop_price` starting on the SAME candle the
    #: entry filled on (its high/low reflect price action after the open -
    #: see `run_segment`) and on every candle after it; `planned_risk_quote`
    #: is compared against a stop exit's actual realized loss to detect a
    #: gap exceeding the planned risk budget.
    realized_plan: RiskRewardPlan | None = None


@dataclass(frozen=True, slots=True)
class _PortfolioSnapshot:
    """Cash/base/open-trade state at one candle - recorded alongside
    `equity_curve` (same index alignment) purely so a later report can look
    up the EXACT state at any split boundary within a continuous run,
    without re-simulating anything or approximating."""

    cash_quote: Decimal
    base_quantity: Decimal
    open_trade: _OpenTrade | None


@dataclass
class _LoopState:
    portfolio: PortfolioState
    open_trade: _OpenTrade | None
    stop_price: Decimal | None
    trades_today: int
    daily_realized_pnl_pct: float
    daily_start_equity: Decimal
    cooldown_bars_remaining: int
    #: Take-profit price under the fixed risk/reward policy - always None
    #: otherwise. Checked alongside `stop_price`; STOP takes precedence
    #: when both would trigger on the same candle (see `run_segment`).
    target_price: Decimal | None = None


@dataclass
class _DiagBuilder:
    """Mutable diagnostics accumulator for one `run_segment` call - frozen
    into a `RunDiagnostics` once the run completes."""

    buy_signals_generated: int = 0
    exit_signals_generated: int = 0
    executed_entries: int = 0
    rejected_entries_by_reason: dict[str, int] = field(default_factory=dict)
    shutdown_first_activation: dict[str, tuple[int, Decimal, float]] = field(default_factory=dict)
    shutdown_last_active: dict[str, int] = field(default_factory=dict)
    last_approved_buy_time_ms: int | None = None

    #: Populated only when `run_segment` is called with
    #: `use_fixed_risk_reward_policy=True` - see `backtest/risk_reward.py`.
    rr_entries_approved: int = 0
    rr_rejected_net_rr_below_minimum: int = 0
    rr_rejected_exchange_filter: int = 0
    rr_rejected_post_fill_revalidation: int = 0
    rr_gap_losses_exceeding_planned_risk: int = 0
    rr_planned_risk_quote_total: Decimal = field(default_factory=lambda: Decimal(0))
    rr_planned_reward_quote_total: Decimal = field(default_factory=lambda: Decimal(0))
    rr_planned_risk_pct_values: list[float] = field(default_factory=list)
    rr_planned_reward_pct_values: list[float] = field(default_factory=list)
    rr_gross_reward_to_risk_values: list[float] = field(default_factory=list)
    rr_net_reward_to_risk_values: list[float] = field(default_factory=list)
    #: Same per-entry values as `rr_planned_risk_pct_values`/`rr_planned_
    #: reward_pct_values`, but in QUOTE currency (exact Decimal) - see
    #: `RiskRewardDiagnostics.planned_risk_quote_values` for why this exists.
    rr_planned_risk_quote_values: list[Decimal] = field(default_factory=list)
    rr_planned_reward_quote_values: list[Decimal] = field(default_factory=list)

    def record_rejected_entry(self, reason_code: str) -> None:
        self.rejected_entries_by_reason[reason_code] = self.rejected_entries_by_reason.get(reason_code, 0) + 1

    def record_risk_gate_rejection(self, reason_code: str, time_ms: int, equity: Decimal, drawdown_pct: float) -> None:
        self.record_rejected_entry(reason_code)
        if reason_code not in self.shutdown_first_activation:
            self.shutdown_first_activation[reason_code] = (time_ms, equity, drawdown_pct)
        self.shutdown_last_active[reason_code] = time_ms


@dataclass(frozen=True, slots=True)
class SegmentRunResult:
    trades: list[Trade]
    equity_curve: list[EquityPoint]
    ends_with_open_position: bool
    pending_signal_note: str | None
    diagnostics: RunDiagnostics
    #: The still-open position's own entry economics when
    #: `ends_with_open_position` is True - None otherwise. No exit is ever
    #: invented for it; this only carries what already happened (the entry)
    #: so unrealized PnL can be reported without fabricating a close.
    open_position: OpenPositionInfo | None
    #: Per-candle (cash, base, open_trade) snapshots, index-aligned with
    #: `equity_curve` - lets a caller reconstruct the EXACT state at any
    #: split boundary within this run (see `_PortfolioSnapshot`).
    portfolio_snapshots: list[_PortfolioSnapshot]
    #: Populated only when this call used `use_fixed_risk_reward_policy=True`
    #: (candidate evaluation - see `research/blocked_chronological_evaluation.py`)
    #: - None for the unmodified frozen-baseline path.
    risk_reward: RiskRewardDiagnostics | None = None


def run_backtest(
    candles: list[Candle],
    config: AppConfig,
    filters: SymbolFilters,
    journal: Journal | None = None,
) -> BacktestResult:
    interval = config.market.interval
    policy = config.backtest.gap_policy

    if policy == "reject":
        validate_candle_sequence(candles, interval)
        raw_segments: list[list[Candle]] = [candles]
        gaps: list[GapRecord] = []
    else:
        segmentation = partition_into_segments(candles, interval)
        raw_segments = segmentation.segments
        gaps = segmentation.gaps

    strategy = EmaCrossoverTrendStrategy(config.strategy.ema_fast, config.strategy.ema_slow)
    risk_engine = RiskEngine(config.risk)
    broker = BacktestBroker(config.fees)
    min_required = config.strategy.ema_slow + 1
    starting_equity = Decimal(str(config.backtest.starting_equity))

    if not any(len(seg) >= min_required for seg in raw_segments):
        sizes = [len(seg) for seg in raw_segments]
        raise ValueError(
            f"need at least {min_required} candles in some contiguous segment to backtest, "
            f"got segment sizes {sizes}"
        )

    segment_reports: list[SegmentReport] = []
    warnings: list[str] = []
    unexecuted_final_signal: str | None = None
    used_results: list[tuple[int, list[Candle], SegmentRunResult]] = []

    for seg_idx, segment in enumerate(raw_segments):
        is_last_segment = seg_idx == len(raw_segments) - 1

        if len(segment) < min_required:
            segment_reports.append(
                SegmentReport(
                    index=seg_idx,
                    start_time_ms=segment[0].open_time_ms if segment else 0,
                    end_time_ms=segment[-1].close_time_ms if segment else 0,
                    candle_count=len(segment),
                    trade_count=0,
                    ends_with_open_position=False,
                    excluded_from_overall=True,
                    cancelled_pending_signal=None,
                    skipped_insufficient_candles=True,
                )
            )
            warnings.append(
                f"segment {seg_idx} has only {len(segment)} candle(s), fewer than the {min_required} "
                "required for indicator warm-up - skipped entirely, not backtested."
            )
            continue

        result = run_segment(segment, config, filters, strategy, risk_engine, broker, journal, min_required, starting_equity)

        if result.pending_signal_note is not None:
            if is_last_segment:
                unexecuted_final_signal = result.pending_signal_note
                warnings.append(result.pending_signal_note)
            else:
                warnings.append(
                    f"segment {seg_idx}: {result.pending_signal_note} Cancelled at the following "
                    "confirmed gap - never carried into the next segment."
                )

        # A position still open at the true end of the LAST segment is not
        # a gap-related condition (the series simply ran out of data) -
        # that has always been allowed and is never excluded or flagged as
        # "unresolved" here. Only a non-final segment's open position is
        # actually gap-adjacent.
        ends_open_at_gap = result.ends_with_open_position and not is_last_segment
        exclude = ends_open_at_gap and config.backtest.exclude_open_position_segments
        if ends_open_at_gap:
            warnings.append(
                f"segment {seg_idx} ends with an open position that this segment's own data cannot "
                "resolve (the position spans a confirmed gap) - marked as an unresolved research "
                "condition; no exit price was invented for it."
                + (" Excluded from aggregate trade statistics." if exclude else "")
            )

        segment_performance = compute_performance_report(
            result.trades,
            result.equity_curve,
            interval,
            config.backtest.min_trades_for_significance,
            buy_and_hold=compute_buy_and_hold_report(
                segment[min_required - 1 :], starting_equity, config.fees.taker_fee_pct
            ),
        )
        segment_extended = _build_extended_diagnostics(
            trades=result.trades,
            equity_curve=result.equity_curve,
            window_end_time_ms=segment[-1].close_time_ms,
            starting_equity=starting_equity,
            final_mark_price=segment[-1].close,
            ending_cash_quote=result.diagnostics.ending_cash_quote,
            ending_base_quantity=result.diagnostics.ending_base_quantity,
            reported_ending_equity=result.diagnostics.ending_equity,
            open_position=result.open_position,
            report=segment_performance,
            run_diagnostics=result.diagnostics,
        )

        segment_reports.append(
            SegmentReport(
                index=seg_idx,
                start_time_ms=segment[0].open_time_ms,
                end_time_ms=segment[-1].close_time_ms,
                candle_count=len(segment),
                trade_count=len(result.trades),
                ends_with_open_position=result.ends_with_open_position,
                excluded_from_overall=exclude,
                cancelled_pending_signal=result.pending_signal_note,
                skipped_insufficient_candles=False,
                performance=segment_performance,
                diagnostics=result.diagnostics,
                open_position=result.open_position,
                extended=segment_extended,
            )
        )

        if not exclude:
            used_results.append((seg_idx, segment, result))

    if gaps:
        warnings.append(
            f"{len(gaps)} confirmed historical gap(s) were detected and preserved as independent "
            "segment boundaries - results across gaps are NOT one continuous tradable equity "
            "history. Each segment starts fresh from the same baseline starting_equity; read its "
            "own PerformanceReport independently (see `segments[i].performance`) rather than "
            "trusting any naive concatenation."
        )

    reports: dict[str, PerformanceReport] = {}
    extended_reports: dict[str, ExtendedDiagnosticsReport] = {}
    diagnostics: RunDiagnostics | None = None
    aggregate_trade_stats: AggregateTradeStats | None = None
    all_trades: list[Trade] = [t for _, _, r in used_results for t in r.trades]
    all_equity: list[EquityPoint] = [p for _, _, r in used_results for p in r.equity_curve]

    if len(used_results) == 1:
        # The common, no-confirmed-gap case: exactly one continuous run.
        # Chronological train/validation/test slices of THAT ONE RUN,
        # computed from local (this segment's own) index fractions -
        # informational labels on one uninterrupted simulation, never
        # independent evaluations (see `run_independent_holdout_evaluation`
        # for that) and never refit per slice.
        _, segment, result = used_results[0]
        n = len(segment)
        train_end = min_required - 1 + int((n - (min_required - 1)) * config.backtest.train_fraction)
        validation_end = train_end + int((n - (min_required - 1)) * config.backtest.validation_fraction)
        bounds = {
            "train": (min_required - 1, train_end),
            "validation": (train_end, validation_end),
            "test": (validation_end, n),
            "overall": (min_required - 1, n),
        }
        offset = min_required - 1
        for split, (start_idx, end_idx) in bounds.items():
            split_trades = [t for t in result.trades if start_idx <= _index_for_time(segment, t.exit_time_ms, offset) < end_idx]
            split_equity = result.equity_curve[max(0, start_idx - offset) : max(0, end_idx - offset)]
            buy_and_hold = compute_buy_and_hold_report(
                segment[start_idx:end_idx], starting_equity, config.fees.taker_fee_pct
            )
            report = compute_performance_report(
                split_trades, split_equity, interval, config.backtest.min_trades_for_significance, buy_and_hold=buy_and_hold
            )
            reports[split] = report
            if report.low_trade_count_warning:
                warnings.append(
                    f"{split}: only {report.trade_count} trade(s), below the configured "
                    f"significance threshold of {config.backtest.min_trades_for_significance} - "
                    "results are not statistically meaningful."
                )

            snapshot_idx = end_idx - offset - 1
            is_whole_segment = start_idx == min_required - 1 and end_idx == n
            snapshot = result.portfolio_snapshots[snapshot_idx] if 0 <= snapshot_idx < len(result.portfolio_snapshots) else None
            extended_reports[split] = _build_extended_diagnostics(
                trades=split_trades,
                equity_curve=split_equity,
                window_end_time_ms=segment[end_idx - 1].close_time_ms if end_idx > start_idx else segment[-1].close_time_ms,
                starting_equity=starting_equity,
                final_mark_price=segment[end_idx - 1].close if end_idx > start_idx else segment[-1].close,
                ending_cash_quote=snapshot.cash_quote if snapshot else result.diagnostics.ending_cash_quote,
                ending_base_quantity=snapshot.base_quantity if snapshot else result.diagnostics.ending_base_quantity,
                reported_ending_equity=split_equity[-1].equity if split_equity else result.diagnostics.ending_equity,
                open_position=_open_trade_to_info(snapshot.open_trade) if snapshot else result.open_position,
                report=report,
                run_diagnostics=result.diagnostics,
                already_consumed=(split == "test"),
                scope_note=None if is_whole_segment else _CONTINUOUS_SPLIT_SCOPE_NOTE,
            )
        diagnostics = result.diagnostics
    elif len(used_results) > 1:
        total_realized = sum((t.pnl_quote for t in all_trades), Decimal(0))
        wins = sum(1 for t in all_trades if t.pnl_quote > 0)
        aggregate_trade_stats = AggregateTradeStats(
            segments_included=len(used_results),
            total_trades=len(all_trades),
            total_realized_pnl_quote=total_realized,
            win_rate=(100.0 * wins / len(all_trades)) if all_trades else None,
            total_strategy_exits=sum(1 for t in all_trades if t.exit_reason == EXIT_REASON_STRATEGY),
            total_stop_loss_exits=sum(1 for t in all_trades if t.exit_reason == EXIT_REASON_STOP_LOSS),
        )
        warnings.append(
            f"{len(used_results)} independent segments contributed trades. No combined 'overall' "
            "return/drawdown/Sharpe/Sortino is reported - see `segments[i].performance` for each "
            "segment's own complete, independent report, and `aggregate_trade_stats` for the only "
            "mathematically valid cross-segment aggregate (trade-level counts and money sums)."
        )

    return BacktestResult(
        trades=all_trades,
        equity_curve=all_equity,
        reports=reports,
        warnings=warnings,
        unexecuted_final_signal=unexecuted_final_signal,
        gaps=gaps,
        segments=segment_reports,
        aggregate_trade_stats=aggregate_trade_stats,
        diagnostics=diagnostics,
        extended_reports=extended_reports,
    )


def run_independent_holdout_evaluation(
    candles: list[Candle],
    config: AppConfig,
    filters: SymbolFilters,
    journal: Journal | None = None,
) -> HoldoutEvaluationResult:
    """Run the INDEPENDENT FIXED-PARAMETER HOLDOUT EVALUATION described in
    the module docstring - see `HOLDOUT_EVALUATION_LABEL`."""
    interval = config.market.interval
    policy = config.backtest.gap_policy

    if policy == "reject":
        validate_candle_sequence(candles, interval)
        raw_segments: list[list[Candle]] = [candles]
    else:
        raw_segments = partition_into_segments(candles, interval).segments

    strategy = EmaCrossoverTrendStrategy(config.strategy.ema_fast, config.strategy.ema_slow)
    risk_engine = RiskEngine(config.risk)
    broker = BacktestBroker(config.fees)
    min_required = config.strategy.ema_slow + 1
    starting_equity = Decimal(str(config.backtest.starting_equity))

    windows: list[HoldoutWindowReport] = []
    warnings: list[str] = []

    for seg_idx, segment in enumerate(raw_segments):
        n = len(segment)
        if n < min_required:
            warnings.append(
                f"segment {seg_idx}: only {n} candle(s), fewer than the {min_required} required for "
                "indicator warm-up - skipped entirely for holdout evaluation."
            )
            continue

        tradable_n = n - (min_required - 1)
        train_end = (min_required - 1) + int(tradable_n * config.backtest.train_fraction)
        validation_end = train_end + int(tradable_n * config.backtest.validation_fraction)
        if train_end <= min_required - 1:
            warnings.append(
                f"segment {seg_idx}: not enough candles to form a non-empty train window - skipped "
                "entirely for holdout evaluation."
            )
            continue

        for label, start_idx, end_idx in (
            ("train", min_required - 1, train_end),
            ("validation", train_end, validation_end),
            ("test", validation_end, n),
        ):
            if end_idx <= start_idx:
                warnings.append(f"segment {seg_idx} {label}: window would be empty - skipped.")
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
            if report.low_trade_count_warning:
                warnings.append(
                    f"segment {seg_idx} {label}: only {report.trade_count} trade(s), below the "
                    f"configured significance threshold of {config.backtest.min_trades_for_significance}."
                )
            if result.pending_signal_note is not None:
                warnings.append(f"segment {seg_idx} {label}: {result.pending_signal_note}")

            window_end_time_ms = segment[end_idx - 1].close_time_ms
            extended = _build_extended_diagnostics(
                trades=result.trades,
                equity_curve=result.equity_curve,
                window_end_time_ms=window_end_time_ms,
                starting_equity=starting_equity,
                final_mark_price=segment[end_idx - 1].close,
                ending_cash_quote=result.diagnostics.ending_cash_quote,
                ending_base_quantity=result.diagnostics.ending_base_quantity,
                reported_ending_equity=result.diagnostics.ending_equity,
                open_position=result.open_position,
                report=report,
                run_diagnostics=result.diagnostics,
                already_consumed=(label == "test"),
            )

            windows.append(
                HoldoutWindowReport(
                    segment_index=seg_idx,
                    label=label,
                    warm_up_start_time_ms=segment[warm_up_start_idx].open_time_ms,
                    warm_up_candle_count=min_required - 1,
                    window_start_time_ms=segment[start_idx].open_time_ms,
                    window_end_time_ms=window_end_time_ms,
                    candle_count=end_idx - start_idx,
                    performance=report,
                    diagnostics=result.diagnostics,
                    ends_with_open_position=result.ends_with_open_position,
                    unresolved_pending_signal=result.pending_signal_note,
                    open_position=result.open_position,
                    extended=extended,
                )
            )

    return HoldoutEvaluationResult(label=HOLDOUT_EVALUATION_LABEL, windows=windows, warnings=warnings)


def _index_for_time(segment: list[Candle], time_ms: int, offset: int) -> int:
    for i, candle in enumerate(segment):
        if candle.open_time_ms == time_ms:
            return i
    return offset  # pragma: no cover - defensive; every trade time originates from this segment


def run_segment(
    segment: list[Candle],
    config: AppConfig,
    filters: SymbolFilters,
    strategy: SignalGenerator,
    risk_engine: RiskEngine,
    broker: BacktestBroker,
    journal: Journal | None,
    min_required: int,
    starting_equity: Decimal,
    use_fixed_risk_reward_policy: bool = False,
) -> SegmentRunResult:
    """Run one fully independent backtest over a single contiguous segment
    (or, from `run_independent_holdout_evaluation`, over one window's own
    warm-up-prefixed candle slice).

    Fresh portfolio (from `starting_equity`), fresh indicator warm-up (via
    `segment[: i + 1]`, never reaching back into a previous segment or
    window), fresh day/cooldown state - nothing from a previous segment (or
    a gap before this one) is ever visible here.

    `use_fixed_risk_reward_policy` (default False, preserving the
    unmodified frozen-baseline path exactly): when True, every BUY is
    sized and gated by the fixed 1:2 planned reward/risk policy
    (`backtest/risk_reward.py`) instead of the plain fixed-percentage
    stop-only sizing, and each open position also carries a take-profit
    target checked alongside its stop. Protection begins IMMEDIATELY,
    starting with the SAME candle the entry fills on: a pending signal
    decided from the previous candle's close fills at this candle's open,
    and this candle's own high/low reflect price movement that occurs
    AFTER that open, so a stop or target genuinely can be (and is)
    evaluated against it - never delayed to the following candle. This is
    not same-close execution: the entry decision still came from the
    prior completed candle, the fill still happens no earlier than this
    candle's open, and the strategy's own signal generation still only
    ever sees completed candles - only the engine's post-fill protective
    exit check reads this candle's OHLC, and only after the fill. Set to
    True ONLY by `research/blocked_chronological_evaluation.py` (every
    research candidate) - `run_backtest`/`run_independent_holdout_evaluation`
    (the frozen v0.1 baseline) never pass it, so that baseline continues to
    reproduce EXACTLY as before this policy existed.
    """
    state = _LoopState(
        portfolio=PortfolioState.initial(starting_equity),
        open_trade=None,
        stop_price=None,
        trades_today=0,
        daily_realized_pnl_pct=0.0,
        daily_start_equity=starting_equity,
        cooldown_bars_remaining=0,
    )
    peak_equity = state.portfolio.equity(segment[min_required - 1].close)
    diag = _DiagBuilder()

    pending_signal: SignalType | None = None
    trades: list[Trade] = []
    equity_curve: list[EquityPoint] = []
    portfolio_snapshots: list[_PortfolioSnapshot] = []
    current_day: int | None = None
    n = len(segment)

    for i in range(min_required - 1, n):
        candle = segment[i]

        # Round 2 finding #5: detect/initialize a new UTC day FIRST (using
        # this candle's OPEN, since that is when any pending signal from the
        # previous candle actually fills), THEN resolve that pending signal -
        # so its trade count and realized PnL are attributed to the day it
        # actually executes in, never to the day it merely was decided on.
        day = candle.open_time_ms // _MS_PER_DAY
        if current_day is None or day != current_day:
            current_day = day
            state.daily_start_equity = state.portfolio.equity(candle.open)
            state.daily_realized_pnl_pct = 0.0
            state.trades_today = 0
        state.cooldown_bars_remaining = max(0, state.cooldown_bars_remaining - 1)

        if pending_signal is not None:
            state, trades, peak_equity = _resolve_pending_signal(
                pending_signal, candle, state, trades, config, filters, risk_engine, broker, peak_equity, journal, diag,
                use_fixed_risk_reward_policy,
            )
            pending_signal = None

        # Protection begins IMMEDIATELY after a fill, on the SAME candle
        # that fill happened on - a pending signal decided from the
        # PREVIOUS candle's close fills at THIS candle's open, and this
        # candle's own high/low represent price movement that occurs
        # AFTER that open, so a stop or target genuinely can (and must be
        # allowed to) be hit within it. This is not same-close execution:
        # the entry decision still came from the prior completed candle,
        # the fill still happens no earlier than this candle's open, and
        # the strategy's own signal generation below still only ever sees
        # completed, already-elapsed candles - only the ENGINE's post-fill
        # protective-exit check is evaluated against this candle's OHLC.
        if (
            state.portfolio.position_side == PositionSide.LONG
            and state.stop_price is not None
        ):
            if candle.low <= state.stop_price:
                # Conservative same-candle ambiguity rule: if both the stop
                # and (under the R/R policy) the take-profit are touched
                # within one candle, STOP is assumed to occur first.
                state, trades = _execute_stop_exit(candle, state, trades, broker, config, journal, diag)
            elif use_fixed_risk_reward_policy and state.target_price is not None and candle.high >= state.target_price:
                state, trades = _execute_take_profit_exit(candle, state, trades, broker, config, journal, diag)

        equity_now = state.portfolio.equity(candle.close)
        peak_equity = max(peak_equity, equity_now)

        signal = strategy.generate_signal(segment[: i + 1], state.portfolio.position_side)
        if journal is not None:
            journal.record(
                "SIGNAL",
                {"type": signal.type.value, "reason_code": signal.reason_code, **signal.inputs},
                candle.close_time_ms,
            )
        if signal.type == SignalType.BUY:
            diag.buy_signals_generated += 1
            pending_signal = signal.type
        elif signal.type == SignalType.EXIT:
            diag.exit_signals_generated += 1
            pending_signal = signal.type

        equity_curve.append(
            EquityPoint(
                timestamp_ms=candle.close_time_ms,
                equity=state.portfolio.equity(candle.close),
                in_position=state.portfolio.position_side == PositionSide.LONG,
            )
        )
        portfolio_snapshots.append(
            _PortfolioSnapshot(
                cash_quote=state.portfolio.quote_balance,
                base_quantity=state.portfolio.base_balance,
                open_trade=state.open_trade,
            )
        )

    pending_signal_note = None
    if pending_signal is not None:
        pending_signal_note = (
            f"a {pending_signal.value.upper()} signal was generated on the final candle of this segment "
            "and is unexecuted - execution requires a following candle's open, which does not exist "
            "within this segment."
        )

    strategy_exits = sum(1 for t in trades if t.exit_reason == EXIT_REASON_STRATEGY)
    stop_exits = sum(1 for t in trades if t.exit_reason == EXIT_REASON_STOP_LOSS)
    take_profit_exits = sum(1 for t in trades if t.exit_reason == EXIT_REASON_TAKE_PROFIT)
    max_dd_pct, max_dd_time_ms = _max_drawdown_with_time(equity_curve)
    segment_end_time_ms = segment[-1].close_time_ms

    shutdown_activations: dict[str, ShutdownActivation] = {}
    for reason_code, (first_ms, eq_at_activation, dd_at_activation) in diag.shutdown_first_activation.items():
        last_active_ms = diag.shutdown_last_active[reason_code]
        remained_latched = (
            diag.last_approved_buy_time_ms is None or diag.last_approved_buy_time_ms < first_ms
        )
        duration_ms = (segment_end_time_ms - first_ms) if remained_latched else (last_active_ms - first_ms)
        shutdown_activations[reason_code] = ShutdownActivation(
            reason_code=reason_code,
            first_activated_time_ms=first_ms,
            equity_at_activation=eq_at_activation,
            drawdown_pct_at_activation=dd_at_activation,
            last_active_time_ms=last_active_ms,
            blocked_buy_count=diag.rejected_entries_by_reason.get(reason_code, 0),
            remained_latched_to_end=remained_latched,
            duration_ms=duration_ms,
        )

    diagnostics = RunDiagnostics(
        buy_signals_generated=diag.buy_signals_generated,
        exit_signals_generated=diag.exit_signals_generated,
        unexecuted_signals=1 if pending_signal_note is not None else 0,
        executed_entries=diag.executed_entries,
        executed_strategy_exits=strategy_exits,
        executed_stop_loss_exits=stop_exits,
        executed_take_profit_exits=take_profit_exits,
        rejected_entries_by_reason=dict(diag.rejected_entries_by_reason),
        first_executed_trade_time_ms=trades[0].entry_time_ms if trades else None,
        last_executed_trade_time_ms=trades[-1].exit_time_ms if trades else None,
        max_drawdown_pct=max_dd_pct,
        max_drawdown_time_ms=max_dd_time_ms,
        shutdown_activations=shutdown_activations,
        starting_equity=starting_equity,
        ending_equity=state.portfolio.equity(segment[-1].close),
        ending_cash_quote=state.portfolio.quote_balance,
        ending_base_quantity=state.portfolio.base_balance,
        ends_with_open_position=state.portfolio.position_side == PositionSide.LONG,
    )

    risk_reward_diagnostics = None
    if use_fixed_risk_reward_policy:
        risk_reward_diagnostics = RiskRewardDiagnostics(
            entries_approved=diag.rr_entries_approved,
            entries_rejected_net_rr_below_minimum=diag.rr_rejected_net_rr_below_minimum,
            entries_rejected_exchange_filter_within_risk_budget=diag.rr_rejected_exchange_filter,
            entries_rejected_post_fill_revalidation=diag.rr_rejected_post_fill_revalidation,
            stop_loss_exits=stop_exits,
            take_profit_exits=take_profit_exits,
            gap_losses_exceeding_planned_risk=diag.rr_gap_losses_exceeding_planned_risk,
            planned_risk_quote_total=diag.rr_planned_risk_quote_total,
            planned_reward_quote_total=diag.rr_planned_reward_quote_total,
            planned_risk_pct_values=tuple(diag.rr_planned_risk_pct_values),
            planned_reward_pct_values=tuple(diag.rr_planned_reward_pct_values),
            gross_reward_to_risk_values=tuple(diag.rr_gross_reward_to_risk_values),
            net_reward_to_risk_values=tuple(diag.rr_net_reward_to_risk_values),
            planned_risk_quote_values=tuple(diag.rr_planned_risk_quote_values),
            planned_reward_quote_values=tuple(diag.rr_planned_reward_quote_values),
        )

    return SegmentRunResult(
        trades=trades,
        equity_curve=equity_curve,
        ends_with_open_position=state.portfolio.position_side == PositionSide.LONG,
        pending_signal_note=pending_signal_note,
        diagnostics=diagnostics,
        open_position=_open_trade_to_info(state.open_trade),
        portfolio_snapshots=portfolio_snapshots,
        risk_reward=risk_reward_diagnostics,
    )


def _open_trade_to_info(open_trade: _OpenTrade | None) -> OpenPositionInfo | None:
    if open_trade is None:
        return None
    return OpenPositionInfo(
        entry_time_ms=open_trade.entry_time_ms,
        entry_price=open_trade.entry_price,
        entry_reference_price=open_trade.entry_reference_price,
        quantity=open_trade.quantity,
        entry_fee_quote=open_trade.entry_fee,
    )


def _resolve_pending_signal(
    signal_type: SignalType,
    candle: Candle,
    state: _LoopState,
    trades: list[Trade],
    config: AppConfig,
    filters: SymbolFilters,
    risk_engine: RiskEngine,
    broker: BacktestBroker,
    peak_equity: Decimal,
    journal: Journal | None,
    diag: _DiagBuilder,
    use_fixed_risk_reward_policy: bool = False,
) -> tuple[_LoopState, list[Trade], Decimal]:
    reference_price = candle.open
    equity = state.portfolio.equity(reference_price)
    peak_equity = max(peak_equity, equity)
    drawdown_pct = float((peak_equity - equity) / peak_equity) if peak_equity > 0 else 0.0

    risk_reward_plan: RiskRewardPlan | None = None
    if signal_type == SignalType.BUY:
        if use_fixed_risk_reward_policy:
            risk_reward_plan = plan_risk_reward_entry(
                equity, state.portfolio.quote_balance, reference_price,
                config.stop_loss.stop_distance_pct, config.fees.taker_fee_pct, config.fees.slippage_pct, filters,
            )
            sizing = SizingDecision(risk_reward_plan.approved, risk_reward_plan.quantity, risk_reward_plan.reason_code)
        else:
            stop_price = reference_price * (1 - Decimal(str(config.stop_loss.stop_distance_pct)))
            sizing = compute_risk_based_buy_quantity(
                equity, reference_price, stop_price,
                config.risk.max_risk_per_trade_pct, config.risk.max_position_pct,
                config.fees.taker_fee_pct, config.fees.slippage_pct, filters,
            )
    else:
        sizing = compute_sell_quantity(state.portfolio.base_balance, reference_price, filters)

    if journal is not None:
        journal.record(
            "SIZING",
            {"signal_type": signal_type.value, "approved": sizing.approved, "reason_code": sizing.reason_code},
            candle.open_time_ms,
        )
    if not sizing.approved or sizing.quantity is None:
        if signal_type == SignalType.BUY:
            diag.record_rejected_entry(sizing.reason_code)
            if risk_reward_plan is not None:
                if risk_reward_plan.reason_code in EXCHANGE_FILTER_REJECTION_REASONS:
                    diag.rr_rejected_exchange_filter += 1
                elif risk_reward_plan.reason_code == RR_REJECTED_NET_REWARD_TO_RISK_BELOW_MINIMUM:
                    diag.rr_rejected_net_rr_below_minimum += 1
        return state, trades, peak_equity

    intent = TradeIntent(signal_type, candle.symbol, sizing.quantity, reference_price)
    context = RiskContext(
        equity=equity,
        quote_balance=state.portfolio.quote_balance,
        trades_today=state.trades_today,
        cooldown_bars_remaining=state.cooldown_bars_remaining,
        daily_realized_pnl_pct=state.daily_realized_pnl_pct,
        current_drawdown_pct=drawdown_pct,
        data_age_seconds=0.0,
        consecutive_api_errors=0,
        kill_switch_engaged=False,
        is_duplicate_order=False,
    )
    risk_decision = risk_engine.evaluate(intent, context)
    if journal is not None:
        journal.record(
            "RISK_DECISION",
            {"signal_type": signal_type.value, "approved": risk_decision.approved, "reason_code": risk_decision.reason_code},
            candle.open_time_ms,
        )
    if not risk_decision.approved:
        if signal_type == SignalType.BUY:
            diag.record_risk_gate_rejection(risk_decision.reason_code, candle.open_time_ms, equity, drawdown_pct)
        return state, trades, peak_equity

    validation = validate_order(intent, filters)
    if journal is not None:
        journal.record(
            "ORDER_VALIDATION",
            {"signal_type": signal_type.value, "approved": validation.approved, "reason_code": validation.reason_code},
            candle.open_time_ms,
        )
    if not validation.approved or validation.validated_quantity is None:
        if signal_type == SignalType.BUY:
            diag.record_rejected_entry(validation.reason_code)
        return state, trades, peak_equity

    quantity = validation.validated_quantity

    if signal_type == SignalType.BUY:
        # `broker.simulate_buy` is a pure computation (no portfolio
        # mutation) - calling it here, BEFORE `apply_buy`, lets the fixed
        # risk/reward policy re-validate the plan against the REAL fill
        # price and FAIL CLOSED (never create the position at all) if it
        # no longer holds, rather than having to unwind an already-applied
        # buy.
        fill = broker.simulate_buy(quantity, reference_price)
        realized_plan: RiskRewardPlan | None = None
        if use_fixed_risk_reward_policy and risk_reward_plan is not None:
            # Persist the FINAL plan computed from the REAL fill price
            # (never the earlier reference price or signal close), anchored
            # to the actual quantity that filled.
            plan_for_fill = (
                risk_reward_plan if quantity == risk_reward_plan.quantity else replace(risk_reward_plan, quantity=quantity)
            )
            realized_plan = build_realized_plan(
                plan_for_fill, fill.fill_price, config.stop_loss.stop_distance_pct,
                config.fees.taker_fee_pct, config.fees.slippage_pct, equity, filters,
            )
            if not realized_plan.approved:
                diag.record_rejected_entry(realized_plan.reason_code)
                diag.rr_rejected_post_fill_revalidation += 1
                return state, trades, peak_equity

        state.portfolio = apply_buy(state.portfolio, quantity, fill.fill_price, fill.fee_quote)
        if use_fixed_risk_reward_policy and realized_plan is not None:
            state.stop_price = realized_plan.stop_price
            state.target_price = realized_plan.target_price
            state.open_trade = _OpenTrade(
                candle.open_time_ms, fill.fill_price, quantity, fill.fee_quote, reference_price,
                realized_plan=realized_plan,
            )
            diag.rr_entries_approved += 1
            if realized_plan.planned_risk_quote is not None:
                diag.rr_planned_risk_quote_total += realized_plan.planned_risk_quote
            if realized_plan.planned_reward_quote is not None:
                diag.rr_planned_reward_quote_total += realized_plan.planned_reward_quote
            if realized_plan.planned_risk_pct is not None:
                diag.rr_planned_risk_pct_values.append(realized_plan.planned_risk_pct)
            if realized_plan.planned_reward_pct is not None:
                diag.rr_planned_reward_pct_values.append(realized_plan.planned_reward_pct)
            if realized_plan.planned_risk_quote is not None:
                diag.rr_planned_risk_quote_values.append(realized_plan.planned_risk_quote)
            if realized_plan.planned_reward_quote is not None:
                diag.rr_planned_reward_quote_values.append(realized_plan.planned_reward_quote)
            if realized_plan.gross_reward_to_risk is not None:
                diag.rr_gross_reward_to_risk_values.append(realized_plan.gross_reward_to_risk)
            if realized_plan.net_reward_to_risk is not None:
                diag.rr_net_reward_to_risk_values.append(realized_plan.net_reward_to_risk)
        else:
            state.stop_price = fill.fill_price * (1 - Decimal(str(config.stop_loss.stop_distance_pct)))
            state.open_trade = _OpenTrade(candle.open_time_ms, fill.fill_price, quantity, fill.fee_quote, reference_price)
        state.trades_today += 1
        diag.executed_entries += 1
        diag.last_approved_buy_time_ms = candle.open_time_ms
    else:
        fill = broker.simulate_sell(quantity, reference_price)
        state = _apply_exit_fill(
            state, candle.open_time_ms, quantity, fill, trades, config, EXIT_REASON_STRATEGY, reference_price, diag
        )

    return state, trades, peak_equity


def _execute_stop_exit(
    candle: Candle,
    state: _LoopState,
    trades: list[Trade],
    broker: BacktestBroker,
    config: AppConfig,
    journal: Journal | None,
    diag: _DiagBuilder,
) -> tuple[_LoopState, list[Trade]]:
    if state.stop_price is None:  # pragma: no cover - guarded by caller
        raise ValueError("_execute_stop_exit called without an active stop price")
    # A gap through the stop fills at the worse (lower) of the stop price
    # or the candle's open - never assume a fill better than the market allowed.
    fill_reference_price = min(state.stop_price, candle.open)
    quantity = state.portfolio.base_balance
    fill = broker.simulate_sell(quantity, fill_reference_price)
    state = _apply_exit_fill(
        state, candle.open_time_ms, quantity, fill, trades, config, EXIT_REASON_STOP_LOSS, fill_reference_price, diag
    )
    if journal is not None:
        journal.record(
            "STOP_LOSS_TRIGGERED",
            {"stop_price": str(fill_reference_price), "quantity": str(quantity)},
            candle.open_time_ms,
        )
    return state, trades


def _execute_take_profit_exit(
    candle: Candle,
    state: _LoopState,
    trades: list[Trade],
    broker: BacktestBroker,
    config: AppConfig,
    journal: Journal | None,
    diag: _DiagBuilder,
) -> tuple[_LoopState, list[Trade]]:
    if state.target_price is None:  # pragma: no cover - guarded by caller
        raise ValueError("_execute_take_profit_exit called without an active target price")
    # Fill AT the planned target - a gap beyond it (favourable for a LONG
    # exit) is never credited as an improvement; `broker.simulate_sell`
    # then applies its own (adverse) slippage on top, exactly like every
    # other exit in this project, so the realized fill is never better
    # than what that shared fill model would give any other sell.
    fill_reference_price = state.target_price
    quantity = state.portfolio.base_balance
    fill = broker.simulate_sell(quantity, fill_reference_price)
    state = _apply_exit_fill(
        state, candle.open_time_ms, quantity, fill, trades, config, EXIT_REASON_TAKE_PROFIT, fill_reference_price, diag
    )
    if journal is not None:
        journal.record(
            "TAKE_PROFIT_TRIGGERED",
            {"target_price": str(fill_reference_price), "quantity": str(quantity)},
            candle.open_time_ms,
        )
    return state, trades


def _apply_exit_fill(
    state: _LoopState,
    exit_time_ms: int,
    quantity: Decimal,
    fill: SimulatedFill,
    trades: list[Trade],
    config: AppConfig,
    exit_reason: str,
    exit_reference_price: Decimal,
    diag: _DiagBuilder,
) -> _LoopState:
    pnl_before = state.portfolio.realized_pnl_quote
    open_trade = state.open_trade
    state.portfolio = apply_sell(state.portfolio, quantity, fill.fill_price, fill.fee_quote)
    realized = state.portfolio.realized_pnl_quote - pnl_before

    if open_trade is not None:
        trades.append(
            Trade(
                entry_time_ms=open_trade.entry_time_ms,
                exit_time_ms=exit_time_ms,
                entry_price=open_trade.entry_price,
                exit_price=fill.fill_price,
                quantity=quantity,
                fees_paid=open_trade.entry_fee + fill.fee_quote,
                pnl_quote=realized,
                exit_reason=exit_reason,
                entry_fee_quote=open_trade.entry_fee,
                exit_fee_quote=fill.fee_quote,
                entry_reference_price=open_trade.entry_reference_price,
                exit_reference_price=exit_reference_price,
            )
        )
        # A gap through the stop can still exceed the planned risk budget -
        # a disclosed, expected limitation of a fixed-percentage stop (see
        # backtest/risk_reward.py), tracked here for reporting rather than
        # silently absorbed into the realized PnL figure alone.
        if (
            exit_reason == EXIT_REASON_STOP_LOSS
            and open_trade.realized_plan is not None
            and open_trade.realized_plan.planned_risk_quote is not None
            and -realized > open_trade.realized_plan.planned_risk_quote
        ):
            diag.rr_gap_losses_exceeding_planned_risk += 1

    if state.daily_start_equity > 0:
        state.daily_realized_pnl_pct += float(realized / state.daily_start_equity)
    if realized < 0:
        state.cooldown_bars_remaining = config.risk.cooldown_bars_after_loss
    state.trades_today += 1
    state.stop_price = None
    state.target_price = None
    state.open_trade = None
    return state


def _max_drawdown_with_time(equity_curve: list[EquityPoint]) -> tuple[float, int | None]:
    if not equity_curve:
        return 0.0, None
    peak = equity_curve[0].equity
    max_dd = 0.0
    max_dd_time_ms: int | None = None
    for point in equity_curve:
        peak = max(peak, point.equity)
        if peak > 0:
            drawdown = float((peak - point.equity) / peak)
            if drawdown > max_dd:
                max_dd = drawdown
                max_dd_time_ms = point.timestamp_ms
    return max_dd * 100, max_dd_time_ms


#: Attached to a continuous-mode train/validation/test split's extended
#: diagnostics: the signal/rejection/shutdown evidence shown there belongs
#: to the WHOLE continuous segment this split is only a chronological label
#: within (see the module docstring) - never scoped to the split alone.
_CONTINUOUS_SPLIT_SCOPE_NOTE = (
    "This split is a chronological label on ONE continuous run (see the module docstring) - the "
    "signal/rejection/shutdown-activation counts above belong to the ENTIRE continuous segment, not "
    "only to this split's own date range. For counts genuinely scoped to an independent window, use "
    "run_independent_holdout_evaluation instead."
)


def _build_extended_diagnostics(
    trades: list[Trade],
    equity_curve: list[EquityPoint],
    window_end_time_ms: int,
    starting_equity: Decimal,
    final_mark_price: Decimal,
    ending_cash_quote: Decimal,
    ending_base_quantity: Decimal,
    reported_ending_equity: Decimal,
    open_position: OpenPositionInfo | None,
    report: PerformanceReport,
    run_diagnostics: RunDiagnostics,
    already_consumed: bool = False,
    scope_note: str | None = None,
) -> ExtendedDiagnosticsReport:
    return compute_extended_diagnostics(
        trades=trades,
        equity_curve=equity_curve,
        window_end_time_ms=window_end_time_ms,
        starting_equity=starting_equity,
        ending_cash_quote=ending_cash_quote,
        ending_base_quantity=ending_base_quantity,
        final_mark_price=final_mark_price,
        reported_ending_equity=reported_ending_equity,
        open_position=open_position,
        executed_entries=run_diagnostics.executed_entries,
        buy_signals_generated=run_diagnostics.buy_signals_generated,
        exposure_pct=report.exposure_pct,
        total_return_pct=report.total_return_pct,
        annualized_return_pct=report.annualized_return_pct,
        max_drawdown_pct=report.max_drawdown_pct,
        shutdown_activations=run_diagnostics.shutdown_activations,
        rejected_entries_by_reason=run_diagnostics.rejected_entries_by_reason,
        already_consumed=already_consumed,
        scope_note=scope_note,
    )
