"""A strictly local, read-only shadow-mode report: everything `shadow-report`
prints comes from `data/shadow_agent.db` alone - no Binance call is ever
made to build it.

Reuses `metrics/performance.py::compute_performance_report` UNMODIFIED for
trade count, win rate, max drawdown, and profit factor - the exact same,
already-tested statistic engine every backtest/research report uses. Adds
only what that function does not already compute: expectancy expressed as
a multiple of PLANNED risk (R-multiples, the same correlation convention as
`research/post_mortem.py::_correlate_trades_with_plans`), total simulated
cost (fees + adverse slippage), the longest losing streak, the still-open
position's unrealized PnL (if any), a data-gap summary, and the fixed
30-closed-trade promotion-review gate.

`SHADOW_NOT_PROFITABLE_NOTE` is attached to every report unconditionally -
see the mandate this package was built under: shadow mode never claims
profitability and never permits a Testnet or live order of any kind.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trading_agent.config.models import AppConfig
from trading_agent.data.gap_detection import partition_into_segments
from trading_agent.data.storage import CandleStore
from trading_agent.metrics.performance import PerformanceReport, Trade, compute_performance_report
from trading_agent.research.candidates.multitimeframe_breakout import (
    MultiTimeframeBreakoutStrategy,
)
from trading_agent.shadow.boundary import SHADOW_START_BOUNDARY_MS
from trading_agent.shadow.store import ShadowRunState, ShadowStore

#: A user-mandated, fixed gate - never lowered, never read from config.
#: Deliberately independent of `config.backtest.min_trades_for_significance`
#: (a research-context statistical-significance threshold for a different
#: purpose): this is specifically "at least 30 CLOSED FORWARD trades before
#: any promotion review", per the shadow-mode mandate.
SHADOW_MIN_CLOSED_TRADES_FOR_PROMOTION_REVIEW = 30

SHADOW_NOT_PROFITABLE_NOTE = (
    "This is a forward-only SHADOW SIMULATION of the frozen multitimeframe_breakout_E1_round3 candidate. "
    "No order - real, Testnet, or otherwise - has ever been placed by this tool. Nothing in this report is "
    "a claim of profitability, and nothing in this report is approval for Testnet or live trading. "
    f"A promotion review requires at least {SHADOW_MIN_CLOSED_TRADES_FOR_PROMOTION_REVIEW} closed forward "
    "trades and is, in any case, a separate manual decision this tool does not make."
)


@dataclass(frozen=True, slots=True)
class ShadowDataGapSummary:
    stored_candle_count: int
    gap_count: int
    total_missing_intervals: int
    latest_segment_length: int
    min_required_candles: int


@dataclass(frozen=True, slots=True)
class ShadowOpenPositionSummary:
    entry_time_ms: int
    entry_price: Decimal
    quantity: Decimal
    entry_fee_quote: Decimal
    latest_close_time_ms: int | None
    latest_close_price: Decimal | None
    unrealized_pnl_quote: Decimal | None


@dataclass(frozen=True, slots=True)
class ShadowReport:
    run_state: ShadowRunState
    performance: PerformanceReport
    expectancy_r: float | None
    expectancy_quote: float | None
    total_fees_paid_quote: Decimal
    total_slippage_cost_quote: Decimal
    longest_losing_streak: int
    open_position: ShadowOpenPositionSummary | None
    data_gaps: ShadowDataGapSummary
    promotion_review_eligible: bool
    promotion_review_note: str
    not_profitable_note: str = SHADOW_NOT_PROFITABLE_NOTE


def build_shadow_report(config: AppConfig) -> ShadowReport:
    symbol = config.market.symbol
    interval = config.market.interval
    min_required_candles = MultiTimeframeBreakoutStrategy().min_required_candles

    with (
        ShadowStore(config.paths.db_path) as shadow_store,
        CandleStore(config.paths.db_path) as candle_store,
    ):
        run_state = shadow_store.get_run_state()
        trade_records = shadow_store.get_all_trades()
        equity_curve = shadow_store.get_equity_curve()
        stored_candles = candle_store.get_candles(symbol, interval, start_time_ms=SHADOW_START_BOUNDARY_MS)

    trades: list[Trade] = [r.trade for r in trade_records]
    performance = compute_performance_report(
        trades, equity_curve, interval, SHADOW_MIN_CLOSED_TRADES_FOR_PROMOTION_REVIEW
    )

    r_multiples = [
        float(r.trade.pnl_quote / r.planned_risk_quote)
        for r in trade_records
        if r.planned_risk_quote is not None and r.planned_risk_quote > 0
    ]
    expectancy_r = sum(r_multiples) / len(r_multiples) if r_multiples else None
    expectancy_quote = float(sum(t.pnl_quote for t in trades) / len(trades)) if trades else None

    total_fees_paid_quote = sum((t.fees_paid for t in trades), Decimal(0))
    total_slippage_cost_quote = sum(
        (
            (t.entry_price - t.entry_reference_price) * t.quantity
            + (t.exit_reference_price - t.exit_price) * t.quantity
            for t in trades
        ),
        Decimal(0),
    )

    longest_losing_streak = 0
    current_streak = 0
    for t in trades:
        if t.pnl_quote < 0:
            current_streak += 1
            longest_losing_streak = max(longest_losing_streak, current_streak)
        else:
            current_streak = 0

    open_position = None
    if run_state.open_position is not None:
        pos = run_state.open_position
        latest_candle = stored_candles[-1] if stored_candles else None
        unrealized = None
        if latest_candle is not None:
            unrealized = pos.quantity * (latest_candle.close - pos.entry_price) - pos.entry_fee_quote
        open_position = ShadowOpenPositionSummary(
            entry_time_ms=pos.entry_time_ms,
            entry_price=pos.entry_price,
            quantity=pos.quantity,
            entry_fee_quote=pos.entry_fee_quote,
            latest_close_time_ms=latest_candle.close_time_ms if latest_candle is not None else None,
            latest_close_price=latest_candle.close if latest_candle is not None else None,
            unrealized_pnl_quote=unrealized,
        )

    if stored_candles:
        segmentation = partition_into_segments(stored_candles, interval)
        gap_count = len(segmentation.gaps)
        total_missing_intervals = sum(g.missing_intervals for g in segmentation.gaps)
        latest_segment_length = len(segmentation.segments[-1])
    else:
        gap_count = 0
        total_missing_intervals = 0
        latest_segment_length = 0
    data_gaps = ShadowDataGapSummary(
        stored_candle_count=len(stored_candles),
        gap_count=gap_count,
        total_missing_intervals=total_missing_intervals,
        latest_segment_length=latest_segment_length,
        min_required_candles=min_required_candles,
    )

    closed_trade_count = len(trades)
    promotion_review_eligible = closed_trade_count >= SHADOW_MIN_CLOSED_TRADES_FOR_PROMOTION_REVIEW
    promotion_review_note = (
        f"eligible: {closed_trade_count} closed forward trade(s) observed "
        f"(>= {SHADOW_MIN_CLOSED_TRADES_FOR_PROMOTION_REVIEW} required) - a promotion review is a separate "
        "manual decision this tool does not make."
        if promotion_review_eligible
        else (
            f"NOT yet eligible: {closed_trade_count} of {SHADOW_MIN_CLOSED_TRADES_FOR_PROMOTION_REVIEW} "
            "required closed forward trades observed."
        )
    )

    return ShadowReport(
        run_state=run_state,
        performance=performance,
        expectancy_r=expectancy_r,
        expectancy_quote=expectancy_quote,
        total_fees_paid_quote=total_fees_paid_quote,
        total_slippage_cost_quote=total_slippage_cost_quote,
        longest_losing_streak=longest_losing_streak,
        open_position=open_position,
        data_gaps=data_gaps,
        promotion_review_eligible=promotion_review_eligible,
        promotion_review_note=promotion_review_note,
    )
