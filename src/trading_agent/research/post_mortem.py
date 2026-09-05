"""Read-only candidate POST-MORTEM report over already-completed,
already-authorized pre-cutoff blocked chronological evaluation results
(`research/blocked_chronological_evaluation.py`).

This module runs NO new simulation, fetches NO candles, and touches NO
database - it is pure, deterministic aggregation math over the `Trade` and
`RiskRewardDiagnostics` data a `CandidateBlockedChronologicalResult` already
carries. It changes no strategy, parameter, threshold, risk/reward rule,
fee, slippage, sizing, or execution behavior; it exists purely to explain
results that have already happened, on data that was already authorized
for pre-cutoff development use (`research/cutoff.py`).

NEVER RANKS, NEVER SELECTS: candidates are reported in whatever order they
are given (normally `candidate_registry.CANDIDATE_REGISTRY`'s own declared
order), never re-sorted by any performance figure. There is no "best
candidate" output anywhere in this module - see
`research/scorecard.py::multiple_testing_warning` for why that matters.

Per-candidate diagnosis (`EvidenceDiagnosis`) is always exactly one of four
fixed labels, assigned by a single DECLARED, FIXED rule (`_diagnose`)
applied identically to every candidate - never a subjective read of the
numbers. It is evidence-only language: never "profitable", "approved", or
"rejected" (those remain `research/scorecard.py`'s vocabulary for its own,
separate, threshold-based robustness test).

Per-trade R-multiples and planned-vs-realized R/R figures require knowing
each closed trade's own PLANNED risk in quote currency. This is recovered
by correlating `BlockResult.trades` (chronological) with
`BlockResult.risk_reward`'s per-approved-entry value tuples (also
chronological, one entry per approved BUY) - see `_correlate_trades_with_plans`.
This works because the engine enforces one open position at a time: entries
and trades are always in the same relative order, and at most one trailing
approved entry per block (the one still open at the block's end) has no
matching closed trade.

CRITICAL DISTINCTION (requirement 17): every block is an INDEPENDENTLY
RESTARTED $50 equity - summing PnL across blocks is a SUM OF INDEPENDENT
OUTCOMES, never a continuous compounding equity curve. Every aggregate PnL
figure this module reports is labelled accordingly; `EQUITY_ACCOUNTING_NOTE`
is attached, verbatim, to every candidate's report so this is never lost in
downstream consumption.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum

from trading_agent.metrics.performance import (
    EXIT_REASON_STOP_LOSS,
    EXIT_REASON_STRATEGY,
    EXIT_REASON_TAKE_PROFIT,
    Trade,
)
from trading_agent.research.blocked_chronological_evaluation import (
    BlockResult,
    CandidateBlockedChronologicalResult,
)
from trading_agent.research.candidates.volatility_breakout import FAMILY as BREAKOUT_FAMILY
from trading_agent.research.scorecard import (
    MIN_BLOCKS_WITH_A_TRADE,
    MIN_POSITIVE_REALIZED_PNL_BLOCK_FRACTION,
    MIN_TOTAL_TRADES_FOR_EVIDENCE,
)

_MS_PER_DAY = 24 * 60 * 60 * 1000

#: Declared, fixed for every candidate - never tuned from a result. The top
#: 3 winning trades supplying more than this share of gross winning PnL is
#: one of the four disclosed conditions that disqualifies an otherwise
#: positive result from "broad" (see `_diagnose`).
TOP3_CONCENTRATION_FRAGILE_THRESHOLD_PCT = 75.0

EQUITY_ACCOUNTING_NOTE = (
    "aggregate_realized_pnl_quote and every other candidate-level PnL total in this report is the "
    "SUM of PnL across INDEPENDENTLY RESTARTED $50 blocks (research/blocked_chronological_evaluation.py) "
    "- it is NOT a continuous compounding equity curve. No account ever existed that grew from $50 to "
    "$50 + this sum; each block began its own fresh $50 and its outcome never carried into the next. "
    "Read per-block figures (chronological_stability.per_block) for each block's own, independently- "
    "realized result."
)

MULTIPLE_TESTING_NOTE = (
    "This report describes every declared candidate independently, in the SAME fixed order they were "
    "declared - it never ranks, sorts by performance, or recommends a 'best' candidate. See "
    "research/scorecard.py's own multiple-testing warning: picking a winner after the fact from "
    "several candidates evaluated on the same data is a selection-bias trap this report deliberately "
    "does not participate in."
)


class EvidenceDiagnosis(str, Enum):
    BROAD_POSITIVE_EXPECTANCY = "broad positive expectancy"
    CONCENTRATED_FRAGILE_POSITIVE_EXPECTANCY = "concentrated/fragile positive expectancy"
    NEGATIVE_EXPECTANCY = "negative expectancy"
    INSUFFICIENT_EVIDENCE = "insufficient evidence"


@dataclass(frozen=True, slots=True)
class TradeRecord:
    """One closed trade, correlated (best-effort, see module docstring)
    with the PLANNED risk/reward it was actually approved and sized under."""

    trade: Trade
    segment_index: int
    block_index: int
    planned_risk_quote: Decimal | None
    planned_reward_quote: Decimal | None
    planned_net_reward_to_risk: float | None
    #: realized PnL expressed as a multiple of this trade's OWN planned
    #: quote risk - None when the planned risk could not be correlated.
    r_multiple: float | None
    #: True/False only for a STOP_LOSS exit with a known planned risk
    #: (whether the realized loss exceeded what was planned, i.e. a gap);
    #: None for any other exit reason, or when the planned risk is unknown.
    is_gap_through_stop: bool | None


def _correlate_trades_with_plans(block: BlockResult) -> list[TradeRecord]:
    trades = block.trades
    rr = block.risk_reward
    records: list[TradeRecord] = []
    for i, trade in enumerate(trades):
        planned_risk = rr.planned_risk_quote_values[i] if rr is not None and i < len(rr.planned_risk_quote_values) else None
        planned_reward = (
            rr.planned_reward_quote_values[i] if rr is not None and i < len(rr.planned_reward_quote_values) else None
        )
        planned_net_rr = (
            rr.net_reward_to_risk_values[i] if rr is not None and i < len(rr.net_reward_to_risk_values) else None
        )
        r_multiple = float(trade.pnl_quote / planned_risk) if planned_risk is not None and planned_risk > 0 else None
        is_gap_through_stop = None
        if trade.exit_reason == EXIT_REASON_STOP_LOSS and planned_risk is not None:
            is_gap_through_stop = (-trade.pnl_quote) > planned_risk
        records.append(
            TradeRecord(
                trade=trade,
                segment_index=block.block.segment_index,
                block_index=block.block.block_index,
                planned_risk_quote=planned_risk,
                planned_reward_quote=planned_reward,
                planned_net_reward_to_risk=planned_net_rr,
                r_multiple=r_multiple,
                is_gap_through_stop=is_gap_through_stop,
            )
        )
    return records


@dataclass(frozen=True, slots=True)
class TradeCounts:
    total_entries_approved: int
    closed_trades: int
    wins: int
    losses: int
    breakeven: int
    win_rate_pct: float | None


def _trade_counts(entries_approved: int, trades: list[Trade]) -> TradeCounts:
    wins = sum(1 for t in trades if t.pnl_quote > 0)
    losses = sum(1 for t in trades if t.pnl_quote < 0)
    breakeven = sum(1 for t in trades if t.pnl_quote == 0)
    win_rate = 100.0 * wins / len(trades) if trades else None
    return TradeCounts(entries_approved, len(trades), wins, losses, breakeven, win_rate)


@dataclass(frozen=True, slots=True)
class PnlStats:
    average_net_pnl_quote: float | None
    median_net_pnl_quote: float | None
    average_winner_quote: float | None
    average_loser_quote: float | None
    realized_payoff_ratio: float | None
    expected_value_quote: float | None
    expected_value_pct_of_starting_equity: float | None
    expected_value_r_multiple: float | None
    profit_factor: float | None


def _pnl_stats(trades: list[Trade], records: list[TradeRecord], starting_equity: Decimal | None) -> PnlStats:
    if not trades:
        return PnlStats(None, None, None, None, None, None, None, None, None)
    pnls = [float(t.pnl_quote) for t in trades]
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p < 0]
    avg_winner = sum(winners) / len(winners) if winners else None
    avg_loser = sum(losers) / len(losers) if losers else None
    payoff = (avg_winner / abs(avg_loser)) if avg_winner is not None and avg_loser not in (None, 0) else None
    ev_quote = sum(pnls) / len(pnls)
    ev_pct = (ev_quote / float(starting_equity) * 100) if starting_equity else None
    known_r = [r.r_multiple for r in records if r.r_multiple is not None]
    ev_r = sum(known_r) / len(known_r) if known_r else None
    gross_profit = sum(winners)
    gross_loss = abs(sum(losers))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None
    return PnlStats(
        average_net_pnl_quote=ev_quote,
        median_net_pnl_quote=statistics.median(pnls),
        average_winner_quote=avg_winner,
        average_loser_quote=avg_loser,
        realized_payoff_ratio=payoff,
        expected_value_quote=ev_quote,
        expected_value_pct_of_starting_equity=ev_pct,
        expected_value_r_multiple=ev_r,
        profit_factor=profit_factor,
    )


@dataclass(frozen=True, slots=True)
class ExitReasonBreakdown:
    exit_reason: str
    trade_count: int
    win_rate_pct: float | None
    expectancy_quote: float | None


_EXIT_REASONS = (EXIT_REASON_TAKE_PROFIT, EXIT_REASON_STOP_LOSS, EXIT_REASON_STRATEGY)


def _exit_reason_breakdown(trades: list[Trade]) -> list[ExitReasonBreakdown]:
    breakdown = []
    for reason in _EXIT_REASONS:
        group = [t for t in trades if t.exit_reason == reason]
        if not group:
            breakdown.append(ExitReasonBreakdown(reason, 0, None, None))
            continue
        wins = sum(1 for t in group if t.pnl_quote > 0)
        pnls = [float(t.pnl_quote) for t in group]
        breakdown.append(
            ExitReasonBreakdown(
                exit_reason=reason,
                trade_count=len(group),
                win_rate_pct=100.0 * wins / len(group),
                expectancy_quote=sum(pnls) / len(pnls),
            )
        )
    return breakdown


def _gap_through_stop_breakdown(records: list[TradeRecord]) -> ExitReasonBreakdown:
    gap_records = [r for r in records if r.is_gap_through_stop is True]
    if not gap_records:
        return ExitReasonBreakdown("GAP_THROUGH_STOP", 0, None, None)
    wins = sum(1 for r in gap_records if r.trade.pnl_quote > 0)
    pnls = [float(r.trade.pnl_quote) for r in gap_records]
    return ExitReasonBreakdown(
        exit_reason="GAP_THROUGH_STOP",
        trade_count=len(gap_records),
        win_rate_pct=100.0 * wins / len(gap_records),
        expectancy_quote=sum(pnls) / len(pnls),
    )


@dataclass(frozen=True, slots=True)
class RealizedRDistribution:
    trades_with_known_r: int
    trades_excluded_unknown_r: int
    min_r: float | None
    median_r: float | None
    mean_r: float | None
    max_r: float | None
    pct_at_least_plus_2r: float | None
    pct_losing_more_than_minus_1r: float | None
    note: str = (
        "R = a trade's realized net PnL divided by that SAME trade's own planned quote risk "
        "(backtest/risk_reward.py). Trades whose planned risk could not be correlated (should not "
        "occur under the fixed risk/reward policy, but excluded rather than assumed if it ever does) "
        "are counted in trades_excluded_unknown_r and never included in any statistic here."
    )


def _realized_r_distribution(records: list[TradeRecord]) -> RealizedRDistribution:
    known = [r.r_multiple for r in records if r.r_multiple is not None]
    excluded = len(records) - len(known)
    if not known:
        return RealizedRDistribution(0, excluded, None, None, None, None, None, None)
    at_least_2r = sum(1 for r in known if r >= 2.0)
    below_minus_1r = sum(1 for r in known if r < -1.0)
    return RealizedRDistribution(
        trades_with_known_r=len(known),
        trades_excluded_unknown_r=excluded,
        min_r=min(known),
        median_r=statistics.median(known),
        mean_r=sum(known) / len(known),
        max_r=max(known),
        pct_at_least_plus_2r=100.0 * at_least_2r / len(known),
        pct_losing_more_than_minus_1r=100.0 * below_minus_1r / len(known),
    )


@dataclass(frozen=True, slots=True)
class PlannedVsRealizedRR:
    average_planned_net_reward_to_risk: float | None
    average_realized_r_multiple_on_winners: float | None
    total_planned_risk_quote: Decimal
    total_planned_reward_quote: Decimal
    total_realized_pnl_quote: Decimal


def _planned_vs_realized(
    records: list[TradeRecord], total_planned_risk: Decimal, total_planned_reward: Decimal, total_realized: Decimal
) -> PlannedVsRealizedRR:
    planned_values = [r.planned_net_reward_to_risk for r in records if r.planned_net_reward_to_risk is not None]
    avg_planned = sum(planned_values) / len(planned_values) if planned_values else None
    winner_r = [r.r_multiple for r in records if r.r_multiple is not None and r.trade.pnl_quote > 0]
    avg_realized_winners = sum(winner_r) / len(winner_r) if winner_r else None
    return PlannedVsRealizedRR(avg_planned, avg_realized_winners, total_planned_risk, total_planned_reward, total_realized)


@dataclass(frozen=True, slots=True)
class CostTotals:
    total_fees_quote: Decimal
    total_slippage_quote: Decimal
    note: str = (
        "total_slippage_quote is the sum of |fill_price - reference_price| * quantity across both the "
        "entry and exit leg of every closed trade - the same modeled slippage cost concept as "
        "metrics/extended_report.py::PnlBreakdown.slippage_cost_total_quote, recomputed here across "
        "every block of this candidate."
    )


def _cost_totals(trades: list[Trade]) -> CostTotals:
    fees = sum((t.fees_paid for t in trades), Decimal(0))
    slippage = sum(
        (
            abs(t.entry_price - t.entry_reference_price) * t.quantity
            + abs(t.exit_price - t.exit_reference_price) * t.quantity
            for t in trades
        ),
        Decimal(0),
    )
    return CostTotals(fees, slippage)


@dataclass(frozen=True, slots=True)
class ExclusionResult:
    label: str
    trades_excluded: int
    net_pnl_quote: Decimal
    remains_positive: bool


def _exclusion_results(trades: list[Trade]) -> list[ExclusionResult]:
    if not trades:
        return [
            ExclusionResult("excluding_best_trade", 0, Decimal(0), False),
            ExclusionResult("excluding_best_3_trades", 0, Decimal(0), False),
            ExclusionResult("excluding_best_5_pct_of_trades", 0, Decimal(0), False),
        ]
    total = sum((t.pnl_quote for t in trades), Decimal(0))
    ranked = sorted(trades, key=lambda t: t.pnl_quote, reverse=True)
    n = len(ranked)
    best5pct_count = max(1, -(-n * 5 // 100))  # ceil(5% of n), minimum 1
    results = []
    for label, k in (
        ("excluding_best_trade", 1),
        ("excluding_best_3_trades", 3),
        ("excluding_best_5_pct_of_trades", best5pct_count),
    ):
        k = min(k, n)
        excluded_sum = sum((t.pnl_quote for t in ranked[:k]), Decimal(0))
        remaining = total - excluded_sum
        results.append(ExclusionResult(label, k, remaining, remaining > 0))
    return results


@dataclass(frozen=True, slots=True)
class ConcentrationStats:
    top1_pct_of_positive_pnl: float | None
    top3_pct_of_positive_pnl: float | None
    top5_pct_of_positive_pnl: float | None
    trades_to_reach_50_pct_of_net_profit: int | None
    trades_to_reach_100_pct_of_net_profit: int | None
    note: str = (
        "top-N figures are the summed PnL of the N single largest-WINNING trades divided by the "
        "candidate's total gross winning PnL (never total net PnL - a losing trade cannot supply "
        "'positive PnL'). trades_to_reach_50/100_pct_of_net_profit walk ALL trades sorted largest-PnL- "
        "first and count how many are needed for their running sum to reach 50%/100% of the "
        "candidate's aggregate NET realized PnL - both are None when that aggregate is not positive."
    )


def _concentration_stats(trades: list[Trade]) -> ConcentrationStats:
    winners = sorted((t.pnl_quote for t in trades if t.pnl_quote > 0), reverse=True)
    gross_positive = sum(winners, Decimal(0))
    top1_pct = float(sum(winners[:1], Decimal(0)) / gross_positive * 100) if gross_positive > 0 else None
    top3_pct = float(sum(winners[:3], Decimal(0)) / gross_positive * 100) if gross_positive > 0 else None
    top5_pct = float(sum(winners[:5], Decimal(0)) / gross_positive * 100) if gross_positive > 0 else None

    net_profit = sum((t.pnl_quote for t in trades), Decimal(0))
    trades_to_50 = None
    trades_to_100 = None
    if net_profit > 0:
        ranked = sorted((t.pnl_quote for t in trades), reverse=True)
        cumulative = Decimal(0)
        for i, pnl in enumerate(ranked, start=1):
            cumulative += pnl
            if trades_to_50 is None and cumulative >= net_profit * Decimal("0.5"):
                trades_to_50 = i
            if trades_to_100 is None and cumulative >= net_profit:
                trades_to_100 = i
                break

    return ConcentrationStats(top1_pct, top3_pct, top5_pct, trades_to_50, trades_to_100)


@dataclass(frozen=True, slots=True)
class BlockChronoStat:
    segment_index: int
    block_index: int
    window_start_time_ms: int | None
    window_end_time_ms: int | None
    trade_count: int
    net_pnl_quote: Decimal
    positive: bool


@dataclass(frozen=True, slots=True)
class YearlyStat:
    year: int
    trade_count: int
    net_pnl_quote: Decimal
    win_rate_pct: float | None


@dataclass(frozen=True, slots=True)
class HalfSplitStat:
    label: str
    trade_count: int
    net_pnl_quote: Decimal
    win_rate_pct: float | None


@dataclass(frozen=True, slots=True)
class ChronologicalStability:
    per_block: list[BlockChronoStat]
    per_calendar_year: list[YearlyStat]
    longest_losing_streak_trades: int
    longest_underwater_period_days: float | None
    first_half: HalfSplitStat
    second_half: HalfSplitStat
    cumulative_curve_note: str = (
        "longest_underwater_period_days is measured on a CUMULATIVE SUM OF CLOSED-TRADE PnL ordered "
        "by exit time across every block, sorted purely for this one diagnostic - it is NOT an equity "
        "curve (see EQUITY_ACCOUNTING_NOTE) and implies no compounding or continuous account. It "
        "measures only how long, in this ordering, realized results stayed below a prior high-water "
        "mark of cumulative realized PnL."
    )


def _half_split_stat(label: str, trades: list[Trade]) -> HalfSplitStat:
    if not trades:
        return HalfSplitStat(label, 0, Decimal(0), None)
    wins = sum(1 for t in trades if t.pnl_quote > 0)
    return HalfSplitStat(
        label, len(trades), sum((t.pnl_quote for t in trades), Decimal(0)), 100.0 * wins / len(trades)
    )


def _chronological_stability(blocks: list[BlockResult], all_records: list[TradeRecord]) -> ChronologicalStability:
    per_block = []
    for block in blocks:
        if block.block.skipped_reason is not None:
            continue
        trades = block.trades
        net = sum((t.pnl_quote for t in trades), Decimal(0))
        per_block.append(
            BlockChronoStat(
                segment_index=block.block.segment_index,
                block_index=block.block.block_index,
                window_start_time_ms=block.block.window_start_time_ms,
                window_end_time_ms=block.block.window_end_time_ms,
                trade_count=len(trades),
                net_pnl_quote=net,
                positive=net > 0,
            )
        )

    ordered = sorted(all_records, key=lambda r: r.trade.entry_time_ms)
    ordered_trades = [r.trade for r in ordered]

    yearly: dict[int, list[Trade]] = {}
    for t in ordered_trades:
        year = datetime.fromtimestamp(t.entry_time_ms / 1000, tz=UTC).year
        yearly.setdefault(year, []).append(t)
    per_year = []
    for year in sorted(yearly):
        group = yearly[year]
        wins = sum(1 for t in group if t.pnl_quote > 0)
        per_year.append(
            YearlyStat(
                year=year,
                trade_count=len(group),
                net_pnl_quote=sum((t.pnl_quote for t in group), Decimal(0)),
                win_rate_pct=100.0 * wins / len(group) if group else None,
            )
        )

    longest_losing_streak = 0
    current_streak = 0
    for t in ordered_trades:
        if t.pnl_quote < 0:
            current_streak += 1
            longest_losing_streak = max(longest_losing_streak, current_streak)
        else:
            current_streak = 0

    longest_underwater_days = None
    if ordered_trades:
        cumulative = Decimal(0)
        peak = Decimal(0)
        peak_time_ms = ordered_trades[0].exit_time_ms
        underwater_start_ms: int | None = None
        best_duration_ms = 0
        for t in ordered_trades:
            cumulative += t.pnl_quote
            if cumulative >= peak:
                if underwater_start_ms is not None:
                    best_duration_ms = max(best_duration_ms, t.exit_time_ms - underwater_start_ms)
                    underwater_start_ms = None
                peak = cumulative
                peak_time_ms = t.exit_time_ms
            elif underwater_start_ms is None:
                underwater_start_ms = peak_time_ms
        if underwater_start_ms is not None:
            best_duration_ms = max(best_duration_ms, ordered_trades[-1].exit_time_ms - underwater_start_ms)
        if best_duration_ms > 0:
            longest_underwater_days = best_duration_ms / _MS_PER_DAY

    mid = len(ordered_trades) // 2
    first_half = _half_split_stat("first_half", ordered_trades[:mid])
    second_half = _half_split_stat("second_half", ordered_trades[mid:])

    return ChronologicalStability(
        per_block=per_block,
        per_calendar_year=per_year,
        longest_losing_streak_trades=longest_losing_streak,
        longest_underwater_period_days=longest_underwater_days,
        first_half=first_half,
        second_half=second_half,
    )


@dataclass(frozen=True, slots=True)
class RiskRewardPolicyDiagnostics:
    entries_rejected_net_rr_below_minimum: int
    entries_rejected_exchange_filter: int
    entries_rejected_post_fill_revalidation: int
    stop_loss_trades_total: int
    stop_loss_trades_within_planned_risk: int
    stop_loss_trades_gap_exceeding_planned_risk: int
    planned_1pct_risk_compliance_pct: float | None
    note: str = (
        "planned_1pct_risk_compliance_pct is the share of STOP_LOSS-exit trades whose realized loss "
        "stayed within this candidate's own planned risk budget (backtest/risk_reward.py, fixed 1% of "
        "equity at decision time) - the complement is stop_loss_trades_gap_exceeding_planned_risk, "
        "always a price-gap event, never a sizing or policy failure. None when no STOP_LOSS trade with "
        "a known planned risk occurred."
    )


def _risk_reward_policy_diagnostics(blocks: list[BlockResult], records: list[TradeRecord]) -> RiskRewardPolicyDiagnostics:
    rr_blocks = [b.risk_reward for b in blocks if b.risk_reward is not None]
    stop_records = [r for r in records if r.trade.exit_reason == EXIT_REASON_STOP_LOSS]
    known_gap = [r for r in stop_records if r.is_gap_through_stop is not None]
    within = sum(1 for r in known_gap if r.is_gap_through_stop is False)
    gap = sum(1 for r in known_gap if r.is_gap_through_stop is True)
    compliance = 100.0 * within / len(known_gap) if known_gap else None
    return RiskRewardPolicyDiagnostics(
        entries_rejected_net_rr_below_minimum=sum(rr.entries_rejected_net_rr_below_minimum for rr in rr_blocks),
        entries_rejected_exchange_filter=sum(rr.entries_rejected_exchange_filter_within_risk_budget for rr in rr_blocks),
        entries_rejected_post_fill_revalidation=sum(rr.entries_rejected_post_fill_revalidation for rr in rr_blocks),
        stop_loss_trades_total=len(stop_records),
        stop_loss_trades_within_planned_risk=within,
        stop_loss_trades_gap_exceeding_planned_risk=gap,
        planned_1pct_risk_compliance_pct=compliance,
    )


@dataclass(frozen=True, slots=True)
class BreakoutExclusionCheck:
    applicable: bool
    remains_positive_excluding_best_trade: bool | None
    remains_positive_excluding_best_3_trades: bool | None
    remains_positive_excluding_best_5_pct: bool | None
    note: str = (
        "Requirement: for breakout-family candidates specifically, report whether aggregate "
        "profitability survives removing the best trade, the best 3 trades, and the best 5% of "
        "trades. `applicable=False` (all three fields None) for any non-breakout-family candidate."
    )


def _breakout_exclusion_check(family: str, exclusions: list[ExclusionResult]) -> BreakoutExclusionCheck:
    if family != BREAKOUT_FAMILY:
        return BreakoutExclusionCheck(False, None, None, None)
    by_label = {e.label: e for e in exclusions}
    return BreakoutExclusionCheck(
        True,
        by_label["excluding_best_trade"].remains_positive,
        by_label["excluding_best_3_trades"].remains_positive,
        by_label["excluding_best_5_pct_of_trades"].remains_positive,
    )


def _diagnose(
    total_closed_trades: int,
    blocks_with_a_trade: int,
    aggregate_pnl: Decimal,
    exclusions: list[ExclusionResult],
    top3_positive_pct: float | None,
    positive_block_fraction: float | None,
) -> tuple[EvidenceDiagnosis, str]:
    """Declared, fixed rule - see module docstring. Applied identically to
    every candidate, never adjusted after seeing a result."""
    if total_closed_trades < MIN_TOTAL_TRADES_FOR_EVIDENCE or blocks_with_a_trade < MIN_BLOCKS_WITH_A_TRADE:
        return (
            EvidenceDiagnosis.INSUFFICIENT_EVIDENCE,
            (
                f"total_closed_trades={total_closed_trades} (need >= {MIN_TOTAL_TRADES_FOR_EVIDENCE}), "
                f"blocks_with_a_trade={blocks_with_a_trade} (need >= {MIN_BLOCKS_WITH_A_TRADE})."
            ),
        )
    if aggregate_pnl <= 0:
        return EvidenceDiagnosis.NEGATIVE_EXPECTANCY, f"aggregate_realized_pnl_quote={aggregate_pnl} (<= 0)."

    by_label = {e.label: e for e in exclusions}
    reasons_against_broad = []
    if not by_label["excluding_best_trade"].remains_positive:
        reasons_against_broad.append("aggregate PnL is not positive once the single best trade is excluded")
    if not by_label["excluding_best_3_trades"].remains_positive:
        reasons_against_broad.append("aggregate PnL is not positive once the best 3 trades are excluded")
    if top3_positive_pct is not None and top3_positive_pct > TOP3_CONCENTRATION_FRAGILE_THRESHOLD_PCT:
        reasons_against_broad.append(
            f"top 3 winning trades supply {top3_positive_pct:.1f}% of gross winning PnL "
            f"(> {TOP3_CONCENTRATION_FRAGILE_THRESHOLD_PCT}%)"
        )
    if positive_block_fraction is None or positive_block_fraction < MIN_POSITIVE_REALIZED_PNL_BLOCK_FRACTION:
        reasons_against_broad.append(
            f"positive_block_fraction={positive_block_fraction} "
            f"(need >= {MIN_POSITIVE_REALIZED_PNL_BLOCK_FRACTION})"
        )
    if reasons_against_broad:
        return EvidenceDiagnosis.CONCENTRATED_FRAGILE_POSITIVE_EXPECTANCY, "; ".join(reasons_against_broad)
    return (
        EvidenceDiagnosis.BROAD_POSITIVE_EXPECTANCY,
        (
            "aggregate PnL positive, sufficient evidence, survives excluding the best trade and best 3 "
            "trades, top-3 concentration and per-block positivity both within the declared bar."
        ),
    )


@dataclass(frozen=True, slots=True)
class CandidatePostMortem:
    candidate_id: str
    family: str
    params: dict
    trade_counts: TradeCounts
    pnl_stats: PnlStats
    exit_reason_breakdown: list[ExitReasonBreakdown]
    realized_r_distribution: RealizedRDistribution
    planned_vs_realized: PlannedVsRealizedRR
    costs: CostTotals
    exclusions: list[ExclusionResult]
    concentration: ConcentrationStats
    chronological_stability: ChronologicalStability
    risk_reward_diagnostics: RiskRewardPolicyDiagnostics
    breakout_exclusion_check: BreakoutExclusionCheck
    diagnosis: EvidenceDiagnosis
    diagnosis_reason: str
    warnings: list[str]
    equity_accounting_note: str = EQUITY_ACCOUNTING_NOTE


def build_candidate_post_mortem(result: CandidateBlockedChronologicalResult) -> CandidatePostMortem:
    evaluated_blocks = [b for b in result.blocks if b.block.skipped_reason is None]
    all_trades: list[Trade] = [t for b in evaluated_blocks for t in b.trades]
    all_records: list[TradeRecord] = [r for b in evaluated_blocks for r in _correlate_trades_with_plans(b)]

    entries_approved_total = sum(b.risk_reward.entries_approved for b in evaluated_blocks if b.risk_reward is not None)
    blocks_with_a_trade = sum(1 for b in evaluated_blocks if len(b.trades) > 0)

    starting_equities = {b.performance.starting_equity for b in evaluated_blocks if b.performance is not None}
    warnings: list[str] = []
    starting_equity: Decimal | None = None
    if len(starting_equities) == 1:
        starting_equity = next(iter(starting_equities))
    elif len(starting_equities) > 1:
        warnings.append(
            f"blocks used inconsistent starting_equity values {sorted(starting_equities)} - "
            "expected_value_pct_of_starting_equity is left undefined (None)."
        )

    trade_counts = _trade_counts(entries_approved_total, all_trades)
    pnl_stats = _pnl_stats(all_trades, all_records, starting_equity)
    exit_breakdown = _exit_reason_breakdown(all_trades)
    exit_breakdown.append(_gap_through_stop_breakdown(all_records))
    r_distribution = _realized_r_distribution(all_records)

    total_planned_risk = sum(
        (b.risk_reward.planned_risk_quote_total for b in evaluated_blocks if b.risk_reward is not None), Decimal(0)
    )
    total_planned_reward = sum(
        (b.risk_reward.planned_reward_quote_total for b in evaluated_blocks if b.risk_reward is not None), Decimal(0)
    )
    aggregate_pnl = sum((t.pnl_quote for t in all_trades), Decimal(0))
    planned_vs_realized = _planned_vs_realized(all_records, total_planned_risk, total_planned_reward, aggregate_pnl)

    costs = _cost_totals(all_trades)
    exclusions = _exclusion_results(all_trades)
    concentration = _concentration_stats(all_trades)
    chronological = _chronological_stability(evaluated_blocks, all_records)
    rr_diagnostics = _risk_reward_policy_diagnostics(evaluated_blocks, all_records)
    breakout_check = _breakout_exclusion_check(result.family, exclusions)

    positive_blocks = sum(1 for b in chronological.per_block if b.positive)
    positive_block_fraction = positive_blocks / len(chronological.per_block) if chronological.per_block else None

    diagnosis, diagnosis_reason = _diagnose(
        total_closed_trades=len(all_trades),
        blocks_with_a_trade=blocks_with_a_trade,
        aggregate_pnl=aggregate_pnl,
        exclusions=exclusions,
        top3_positive_pct=concentration.top3_pct_of_positive_pnl,
        positive_block_fraction=positive_block_fraction,
    )

    return CandidatePostMortem(
        candidate_id=result.candidate_id,
        family=result.family,
        params=result.params,
        trade_counts=trade_counts,
        pnl_stats=pnl_stats,
        exit_reason_breakdown=exit_breakdown,
        realized_r_distribution=r_distribution,
        planned_vs_realized=planned_vs_realized,
        costs=costs,
        exclusions=exclusions,
        concentration=concentration,
        chronological_stability=chronological,
        risk_reward_diagnostics=rr_diagnostics,
        breakout_exclusion_check=breakout_check,
        diagnosis=diagnosis,
        diagnosis_reason=diagnosis_reason,
        warnings=warnings,
    )


@dataclass(frozen=True, slots=True)
class PostMortemReport:
    candidates: list[CandidatePostMortem] = field(default_factory=list)
    equity_accounting_note: str = EQUITY_ACCOUNTING_NOTE
    multiple_testing_note: str = MULTIPLE_TESTING_NOTE


def build_post_mortem_report(results: list[CandidateBlockedChronologicalResult]) -> PostMortemReport:
    """Build the full, ordered (never ranked) post-mortem for every given
    candidate result, in the SAME order they are given."""
    return PostMortemReport(candidates=[build_candidate_post_mortem(r) for r in results])
