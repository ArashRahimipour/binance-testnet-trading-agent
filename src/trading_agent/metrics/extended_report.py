"""Extended diagnostic reporting for one already-computed backtest run
(a continuous-mode split/segment, or one independent holdout window).

This module NEVER re-simulates anything and NEVER changes a single fee,
slippage, stop-loss, sizing, or risk-gate calculation - it only computes
additional read-only statistics from the `Trade`/`EquityPoint` lists a run
already produced, plus the ending portfolio state that run already ended
in. Nothing here selects, ranks, or optimizes a configuration; it exists
purely to explain a result that has already happened.

Sections (see `compute_extended_diagnostics`):
  1. `AccountingIdentity` - proves `ending_equity = ending_cash +
     ending_base_quantity * final_mark_price` for this specific run, with
     the actual numbers, rather than asserting it holds.
  2. `PnlBreakdown` - realized closed-trade PnL, unrealized PnL on an
     ending open position (marked at the final available price, no exit
     ever invented), total mark-to-market PnL, entry/exit fees, a fee
     estimated-vs-exchange-derived note, and total slippage cost.
  3. `WindowExplanation` - plain-language, evidence-backed answers to "why
     does this window end with an open position", "why do executed entries
     exceed closed trades", and "why did trading stop".
  4. `TimeBasedPerformance` - CAGR, a monthly return series, % positive
     months, the longest underwater period, an exposure-adjusted return,
     and a Calmar ratio, each with the note explaining when it is/isn't
     mathematically defined.
  5. `TradeDistribution` - median trade return, average/largest
     winner/loser, the best trade's contribution to total PnL and the
     result excluding it, consecutive win/loss streaks, and the
     holding-period distribution.
  6. `BootstrapConfidenceInterval` - a DETERMINISTIC (fixed-seed) trade
     resampling confidence interval, with a prominent caveat that this
     does not preserve chronological/market-regime ordering and is not
     evidence of future profitability.
  7. `RollingWindowDiagnostics` - fixed-size, non-overlapping, chronological
     groups of trades reported purely for inspection; this NEVER ranks,
     optimizes, or selects among windows/configurations.

All of this is diagnostic only. It does not, and must not be used to,
tune strategy parameters, fees, slippage, stop-loss, sizing, or risk
thresholds - those remain exactly as configured.
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from trading_agent.metrics.diagnostics import OpenPositionInfo, ShutdownActivation
from trading_agent.metrics.performance import EquityPoint, Trade

_MS_PER_HOUR = 60 * 60 * 1000
_MS_PER_DAY = 24 * _MS_PER_HOUR
_MS_PER_YEAR = 365 * _MS_PER_DAY

#: Prominent, always-attached caveat for the bootstrap CI - see the module
#: docstring, section 6.
BOOTSTRAP_CAVEAT = (
    "This is a NAIVE TRADE-LEVEL BOOTSTRAP: it resamples this run's own closed trades WITH "
    "REPLACEMENT, treating them as independent and identically distributed. It does NOT preserve "
    "chronological order, market-regime structure, volatility clustering, or any autocorrelation "
    "between trades - all of that is destroyed by resampling. A confidence interval computed this "
    "way describes sampling variability of the trades actually observed under an independence "
    "assumption this market data does not satisfy; it is NOT evidence that the strategy is or will "
    "be profitable, and must never be used on its own to justify a trading decision."
)

#: Prominent, always-attached caveat for the rolling-window diagnostics -
#: see the module docstring, section 7.
ROLLING_WINDOW_CAVEAT = (
    "Diagnostic evaluation only, using the SAME fixed, already-configured strategy parameters "
    "throughout. These per-window figures are for inspection - they are never ranked, compared to "
    "pick a 'best' window, used to select or optimize any configuration, or fed back into "
    "parameter choice."
)


@dataclass(frozen=True, slots=True)
class AccountingIdentity:
    ending_cash_quote: Decimal
    ending_base_quantity: Decimal
    final_mark_price: Decimal
    computed_ending_equity: Decimal
    reported_ending_equity: Decimal
    difference_quote: Decimal
    identity_holds: bool
    note: str = (
        "ending_equity = ending_cash + ending_base_quantity * final_mark_price. Both sides are "
        "printed and compared exactly (Decimal arithmetic, no floating-point rounding) so this is "
        "verified for this specific run, not merely assumed."
    )


def compute_accounting_identity(
    ending_cash_quote: Decimal,
    ending_base_quantity: Decimal,
    final_mark_price: Decimal,
    reported_ending_equity: Decimal,
) -> AccountingIdentity:
    computed = ending_cash_quote + ending_base_quantity * final_mark_price
    difference = computed - reported_ending_equity
    return AccountingIdentity(
        ending_cash_quote=ending_cash_quote,
        ending_base_quantity=ending_base_quantity,
        final_mark_price=final_mark_price,
        computed_ending_equity=computed,
        reported_ending_equity=reported_ending_equity,
        difference_quote=difference,
        identity_holds=(difference == 0),
    )


@dataclass(frozen=True, slots=True)
class PnlBreakdown:
    realized_closed_trade_pnl_quote: Decimal
    unrealized_open_position_pnl_quote: Decimal | None
    total_marked_to_market_pnl_quote: Decimal
    entry_fees_total_quote: Decimal
    exit_fees_total_quote: Decimal
    fees_are_estimated: bool
    fees_note: str
    slippage_cost_total_quote: Decimal
    open_position_note: str | None


def compute_pnl_breakdown(
    trades: list[Trade],
    open_position: OpenPositionInfo | None,
    final_mark_price: Decimal,
) -> PnlBreakdown:
    realized = sum((t.pnl_quote for t in trades), Decimal(0))
    entry_fees = sum((t.entry_fee_quote for t in trades), Decimal(0))
    exit_fees = sum((t.exit_fee_quote for t in trades), Decimal(0))
    slippage = sum(
        (
            abs(t.entry_price - t.entry_reference_price) * t.quantity
            + abs(t.exit_price - t.exit_reference_price) * t.quantity
            for t in trades
        ),
        Decimal(0),
    )

    unrealized: Decimal | None = None
    open_position_note: str | None = None
    if open_position is not None:
        # Marked at the final available price only - no exit (and
        # therefore no exit fee or exit slippage) is ever invented for a
        # position this run's own data cannot resolve.
        unrealized = (
            final_mark_price - open_position.entry_price
        ) * open_position.quantity - open_position.entry_fee_quote
        entry_fees += open_position.entry_fee_quote
        slippage += abs(open_position.entry_price - open_position.entry_reference_price) * open_position.quantity
        open_position_note = (
            f"Position opened at entry_time_ms={open_position.entry_time_ms}, "
            f"entry_price={open_position.entry_price}, quantity={open_position.quantity}, "
            f"remains open at this window's end. Marked to market at the final available close "
            f"({final_mark_price}) - NOT liquidated or closed. unrealized_open_position_pnl_quote "
            "already deducts the entry fee actually paid, but deliberately does NOT deduct a "
            "hypothetical exit fee or exit slippage, since no exit was invented for it."
        )

    total_mtm = realized + (unrealized or Decimal(0))

    return PnlBreakdown(
        realized_closed_trade_pnl_quote=realized,
        unrealized_open_position_pnl_quote=unrealized,
        total_marked_to_market_pnl_quote=total_mtm,
        entry_fees_total_quote=entry_fees,
        exit_fees_total_quote=exit_fees,
        fees_are_estimated=True,
        fees_note=(
            "All fees in a backtest are SIMULATED ESTIMATES computed from config.fees against the "
            "configured percentage - this code path never talks to a real exchange, so there is no "
            "exchange-derived fee to compare against. This is a distinct concept from "
            "PortfolioState.pnl_is_estimated (used only in LIVE/Testnet execution, when a real "
            "commission's asset could not be reliably converted to quote-currency terms) - that "
            "flag never applies to a backtest at all."
        ),
        slippage_cost_total_quote=slippage,
        open_position_note=open_position_note,
    )


@dataclass(frozen=True, slots=True)
class WindowExplanation:
    open_position_reason: str | None
    entries_vs_closed_trades_note: str
    trading_stopped_reason: str | None


def explain_window(
    trades: list[Trade],
    executed_entries: int,
    open_position: OpenPositionInfo | None,
    shutdown_activations: dict[str, ShutdownActivation],
    rejected_entries_by_reason: dict[str, int],
    window_end_time_ms: int,
    buy_signals_generated: int,
) -> WindowExplanation:
    open_position_reason = None
    if open_position is not None:
        open_position_reason = (
            f"A BUY was approved and filled at entry_time_ms={open_position.entry_time_ms}. No EXIT "
            "crossover signal and no stop-loss touch occurred on any subsequent candle before this "
            "window's data ended, so the position is still open at the window's last candle. No exit "
            "price is invented for it - see the PnL breakdown's unrealized figure instead."
        )

    closed_count = len(trades)
    if executed_entries > closed_count:
        entries_vs_closed_trades_note = (
            f"executed_entries ({executed_entries}) exceeds closed_trade_count ({closed_count}) "
            f"because {executed_entries - closed_count} entry/entries opened a position that was "
            "still open when this window's data ended - a closed Trade record requires both an "
            "entry AND an exit fill, and the still-open entry has only the former. This is "
            "consistent with `ends_with_open_position` above, not a counting defect."
        )
    else:
        entries_vs_closed_trades_note = (
            f"executed_entries ({executed_entries}) equals closed_trade_count ({closed_count}) - "
            "every entry this window executed was also closed within it."
        )

    trading_stopped_reason = None
    latched = [a for a in shutdown_activations.values() if a.remained_latched_to_end]
    if latched:
        # Report the earliest-activating latch - it is the one that has
        # been blocking entries the longest.
        earliest = min(latched, key=lambda a: a.first_activated_time_ms)
        trading_stopped_reason = (
            f"No further entries executed after {earliest.first_activated_time_ms} because "
            f"{earliest.reason_code} activated at that time (equity={earliest.equity_at_activation}, "
            f"drawdown_pct={earliest.drawdown_pct_at_activation * 100:.2f}) and remained "
            f"continuously latched for the rest of this window, rejecting {earliest.blocked_buy_count} "
            "further otherwise-valid BUY signal(s) - see `shutdown_activations`. This is a risk-gate "
            "block, not the strategy going silent."
        )
    elif closed_count > 0 or open_position is not None:
        last_trade_time = trades[-1].exit_time_ms if trades else (open_position.entry_time_ms if open_position else None)
        if last_trade_time is not None and last_trade_time < window_end_time_ms and not rejected_entries_by_reason:
            trading_stopped_reason = (
                f"The last execution in this window was at time_ms={last_trade_time}, well before the "
                f"window's own end ({window_end_time_ms}). No risk-gate rejection was ever recorded "
                f"(rejected_entries_by_reason is empty) - the strategy itself simply generated no "
                f"further BUY signal that crossed into an entry for the remainder of this window "
                f"(buy_signals_generated={buy_signals_generated} total across the whole window). "
                "This is a strategy-signal characteristic, not a risk-engine block."
            )

    return WindowExplanation(
        open_position_reason=open_position_reason,
        entries_vs_closed_trades_note=entries_vs_closed_trades_note,
        trading_stopped_reason=trading_stopped_reason,
    )


@dataclass(frozen=True, slots=True)
class MonthlyReturn:
    year: int
    month: int
    return_pct: float


@dataclass(frozen=True, slots=True)
class TimeBasedPerformance:
    cagr_pct: float | None
    cagr_note: str
    monthly_returns: list[MonthlyReturn]
    positive_months_pct: float | None
    longest_underwater_days: float | None
    longest_underwater_start_ms: int | None
    longest_underwater_end_ms: int | None
    exposure_pct: float
    exposure_adjusted_return_pct: float | None
    exposure_adjusted_return_note: str
    calmar_ratio: float | None
    calmar_note: str


def compute_time_based_performance(
    equity_curve: list[EquityPoint],
    total_return_pct: float,
    annualized_return_pct: float | None,
    max_drawdown_pct: float,
    exposure_pct: float,
) -> TimeBasedPerformance:
    cagr_note = (
        "CAGR is the same figure as PerformanceReport.annualized_return_pct: "
        "(ending_equity / starting_equity) ** (365 days / actual elapsed days) - 1. Undefined "
        "(None) when the window spans zero time or either equity endpoint is non-positive."
    )

    monthly_returns: list[MonthlyReturn] = []
    if equity_curve:
        buckets: dict[tuple[int, int], list[EquityPoint]] = {}
        for point in equity_curve:
            dt = datetime.fromtimestamp(point.timestamp_ms / 1000, tz=UTC)
            buckets.setdefault((dt.year, dt.month), []).append(point)
        ordered_keys = sorted(buckets.keys())
        # A month's return needs the equity level entering it - the last
        # point of the PRECEDING month if one exists, else this window's
        # own first point (the month it started mid-way through).
        prior_equity = equity_curve[0].equity
        for key in ordered_keys:
            points = buckets[key]
            start_equity = prior_equity
            end_equity = points[-1].equity
            if start_equity > 0:
                monthly_returns.append(
                    MonthlyReturn(year=key[0], month=key[1], return_pct=float((end_equity / start_equity - 1) * 100))
                )
            prior_equity = end_equity

    positive_months_pct = (
        100.0 * sum(1 for m in monthly_returns if m.return_pct > 0) / len(monthly_returns)
        if monthly_returns
        else None
    )

    longest_underwater_days = None
    longest_underwater_start_ms = None
    longest_underwater_end_ms = None
    if equity_curve:
        peak = equity_curve[0].equity
        peak_time_ms = equity_curve[0].timestamp_ms
        underwater_start_ms: int | None = None
        best_duration_ms = 0
        best_start_ms = None
        best_end_ms = None
        for point in equity_curve:
            if point.equity >= peak:
                if underwater_start_ms is not None:
                    duration = point.timestamp_ms - underwater_start_ms
                    if duration > best_duration_ms:
                        best_duration_ms = duration
                        best_start_ms = underwater_start_ms
                        best_end_ms = point.timestamp_ms
                peak = point.equity
                peak_time_ms = point.timestamp_ms
                underwater_start_ms = None
            else:
                if underwater_start_ms is None:
                    underwater_start_ms = peak_time_ms
        if underwater_start_ms is not None:
            # Still underwater at the window's end - the drought is
            # measured through to the last available point, never assumed
            # to recover beyond the data this window actually has.
            duration = equity_curve[-1].timestamp_ms - underwater_start_ms
            if duration > best_duration_ms:
                best_duration_ms = duration
                best_start_ms = underwater_start_ms
                best_end_ms = equity_curve[-1].timestamp_ms
        if best_start_ms is not None:
            longest_underwater_days = best_duration_ms / _MS_PER_DAY
            longest_underwater_start_ms = best_start_ms
            longest_underwater_end_ms = best_end_ms

    exposure_adjusted_return_note = (
        "total_return_pct / (exposure_pct / 100) - a simple normalization of the raw return by the "
        "fraction of this window's own time actually spent holding a position, so a strategy that is "
        "flat most of the time isn't penalized in this figure the way max_drawdown_pct/Sharpe are "
        "not adjusted for exposure. Undefined (None) when exposure_pct is 0 (never in a position)."
    )
    exposure_adjusted_return_pct = (total_return_pct / (exposure_pct / 100)) if exposure_pct > 0 else None

    calmar_note = (
        "annualized_return_pct / max_drawdown_pct (both already in percent). Undefined (None) when "
        "annualized_return_pct is unavailable or max_drawdown_pct is exactly 0 (division by zero)."
    )
    calmar_ratio = (
        (annualized_return_pct / max_drawdown_pct) if annualized_return_pct is not None and max_drawdown_pct > 0 else None
    )

    return TimeBasedPerformance(
        cagr_pct=annualized_return_pct,
        cagr_note=cagr_note,
        monthly_returns=monthly_returns,
        positive_months_pct=positive_months_pct,
        longest_underwater_days=longest_underwater_days,
        longest_underwater_start_ms=longest_underwater_start_ms,
        longest_underwater_end_ms=longest_underwater_end_ms,
        exposure_pct=exposure_pct,
        exposure_adjusted_return_pct=exposure_adjusted_return_pct,
        exposure_adjusted_return_note=exposure_adjusted_return_note,
        calmar_ratio=calmar_ratio,
        calmar_note=calmar_note,
    )


@dataclass(frozen=True, slots=True)
class HoldingPeriodStats:
    min_hours: float | None
    median_hours: float | None
    mean_hours: float | None
    max_hours: float | None


@dataclass(frozen=True, slots=True)
class TradeDistribution:
    trade_count: int
    median_trade_return_pct: float | None
    average_winner_quote: float | None
    average_loser_quote: float | None
    largest_winner_quote: float | None
    largest_loser_quote: float | None
    best_trade_pnl_quote: Decimal | None
    best_trade_contribution_pct: float | None
    best_trade_contribution_note: str
    total_pnl_excluding_best_trade_quote: Decimal | None
    return_pct_excluding_best_trade: float | None
    max_consecutive_wins: int
    max_consecutive_losses: int
    holding_period: HoldingPeriodStats


def compute_trade_distribution(trades: list[Trade], starting_equity: Decimal) -> TradeDistribution | None:
    if not trades:
        return None

    def _trade_return_pct(t: Trade) -> float:
        notional = t.entry_price * t.quantity
        return float(t.pnl_quote / notional * 100) if notional > 0 else 0.0

    returns_pct = [_trade_return_pct(t) for t in trades]
    winners = [float(t.pnl_quote) for t in trades if t.pnl_quote > 0]
    losers = [float(t.pnl_quote) for t in trades if t.pnl_quote < 0]

    best_trade = max(trades, key=lambda t: t.pnl_quote)
    total_realized = sum((t.pnl_quote for t in trades), Decimal(0))
    best_contribution_pct = None
    best_contribution_note = (
        "best_trade_pnl_quote / total_realized_closed_trade_pnl_quote * 100. Undefined (None) when "
        "total realized PnL is exactly 0 (division by zero)."
    )
    if total_realized != 0:
        best_contribution_pct = float(best_trade.pnl_quote / total_realized * 100)

    total_excl_best = total_realized - best_trade.pnl_quote
    return_excl_best_pct = float(total_excl_best / starting_equity * 100) if starting_equity > 0 else None

    max_wins = 0
    max_losses = 0
    cur_wins = 0
    cur_losses = 0
    for t in trades:
        if t.pnl_quote > 0:
            cur_wins += 1
            cur_losses = 0
        elif t.pnl_quote < 0:
            cur_losses += 1
            cur_wins = 0
        else:
            cur_wins = 0
            cur_losses = 0
        max_wins = max(max_wins, cur_wins)
        max_losses = max(max_losses, cur_losses)

    holding_hours = [(t.exit_time_ms - t.entry_time_ms) / _MS_PER_HOUR for t in trades]

    return TradeDistribution(
        trade_count=len(trades),
        median_trade_return_pct=statistics.median(returns_pct) if returns_pct else None,
        average_winner_quote=(sum(winners) / len(winners)) if winners else None,
        average_loser_quote=(sum(losers) / len(losers)) if losers else None,
        largest_winner_quote=max(winners) if winners else None,
        largest_loser_quote=min(losers) if losers else None,
        best_trade_pnl_quote=best_trade.pnl_quote,
        best_trade_contribution_pct=best_contribution_pct,
        best_trade_contribution_note=best_contribution_note,
        total_pnl_excluding_best_trade_quote=total_excl_best,
        return_pct_excluding_best_trade=return_excl_best_pct,
        max_consecutive_wins=max_wins,
        max_consecutive_losses=max_losses,
        holding_period=HoldingPeriodStats(
            min_hours=min(holding_hours),
            median_hours=statistics.median(holding_hours),
            mean_hours=sum(holding_hours) / len(holding_hours),
            max_hours=max(holding_hours),
        ),
    )


@dataclass(frozen=True, slots=True)
class BootstrapConfidenceInterval:
    method: str
    n_trades: int
    n_resamples: int
    seed: int
    confidence_level: float
    mean_total_return_pct: float | None
    ci_low_pct: float | None
    ci_high_pct: float | None
    caveat: str = BOOTSTRAP_CAVEAT


#: Fixed so the same trades always produce the exact same interval on every
#: run - "deterministic" per the requirement, never a fresh random draw.
_BOOTSTRAP_SEED = 1337
_BOOTSTRAP_RESAMPLES = 2000
_BOOTSTRAP_CONFIDENCE_LEVEL = 0.95


def compute_bootstrap_confidence_interval(
    trades: list[Trade],
    starting_equity: Decimal,
    n_resamples: int = _BOOTSTRAP_RESAMPLES,
    seed: int = _BOOTSTRAP_SEED,
    confidence_level: float = _BOOTSTRAP_CONFIDENCE_LEVEL,
) -> BootstrapConfidenceInterval:
    if len(trades) < 2 or starting_equity <= 0:
        return BootstrapConfidenceInterval(
            method="trade-level resampling with replacement (deterministic, fixed seed)",
            n_trades=len(trades),
            n_resamples=n_resamples,
            seed=seed,
            confidence_level=confidence_level,
            mean_total_return_pct=None,
            ci_low_pct=None,
            ci_high_pct=None,
        )

    pnls = [float(t.pnl_quote) for t in trades]
    rng = random.Random(seed)
    n = len(pnls)
    resample_returns_pct: list[float] = []
    for _ in range(n_resamples):
        resampled_total = sum(pnls[rng.randrange(n)] for _ in range(n))
        resample_returns_pct.append(resampled_total / float(starting_equity) * 100)
    resample_returns_pct.sort()

    alpha = 1 - confidence_level
    low_idx = max(0, int((alpha / 2) * n_resamples))
    high_idx = min(n_resamples - 1, int((1 - alpha / 2) * n_resamples) - 1)

    return BootstrapConfidenceInterval(
        method="trade-level resampling with replacement (deterministic, fixed seed)",
        n_trades=len(trades),
        n_resamples=n_resamples,
        seed=seed,
        confidence_level=confidence_level,
        mean_total_return_pct=sum(resample_returns_pct) / len(resample_returns_pct),
        ci_low_pct=resample_returns_pct[low_idx],
        ci_high_pct=resample_returns_pct[high_idx],
    )


@dataclass(frozen=True, slots=True)
class RollingWindowPoint:
    window_index: int
    trade_count: int
    start_time_ms: int
    end_time_ms: int
    return_pct: float | None
    max_drawdown_pct: float | None


@dataclass(frozen=True, slots=True)
class RollingWindowDiagnostics:
    trades_per_window: int
    windows: list[RollingWindowPoint]
    caveat: str = ROLLING_WINDOW_CAVEAT


_ROLLING_WINDOW_TRADE_COUNT = 10


def compute_rolling_window_diagnostics(
    trades: list[Trade],
    starting_equity: Decimal,
    trades_per_window: int = _ROLLING_WINDOW_TRADE_COUNT,
) -> RollingWindowDiagnostics:
    windows: list[RollingWindowPoint] = []
    if not trades or starting_equity <= 0:
        return RollingWindowDiagnostics(trades_per_window=trades_per_window, windows=windows)

    for window_index, i in enumerate(range(0, len(trades), trades_per_window)):
        chunk = trades[i : i + trades_per_window]
        if not chunk:
            continue
        equity_now = starting_equity
        peak = starting_equity
        max_dd = 0.0
        for t in chunk:
            equity_now += t.pnl_quote
            peak = max(peak, equity_now)
            if peak > 0:
                max_dd = max(max_dd, float((peak - equity_now) / peak))
        return_pct = float((equity_now - starting_equity) / starting_equity * 100)
        windows.append(
            RollingWindowPoint(
                window_index=window_index,
                trade_count=len(chunk),
                start_time_ms=chunk[0].entry_time_ms,
                end_time_ms=chunk[-1].exit_time_ms,
                return_pct=return_pct,
                max_drawdown_pct=max_dd * 100,
            )
        )

    return RollingWindowDiagnostics(trades_per_window=trades_per_window, windows=windows)


@dataclass(frozen=True, slots=True)
class ExtendedDiagnosticsReport:
    accounting: AccountingIdentity
    pnl_breakdown: PnlBreakdown
    explanation: WindowExplanation
    time_based: TimeBasedPerformance
    trade_distribution: TradeDistribution | None
    bootstrap: BootstrapConfidenceInterval
    rolling_windows: RollingWindowDiagnostics
    already_consumed_warning: str | None = None
    #: Set only when the signal/rejection/shutdown evidence fed into
    #: `explanation` belongs to a LARGER continuous run this report is
    #: just a chronological label within (the continuous-mode
    #: train/validation/test split labels) - see backtest/engine.py. None
    #: when the evidence is exactly scoped to this report's own trades
    #: (every segment and every independent holdout window).
    scope_note: str | None = None


#: Attached to any window/report explicitly designated as an
#: already-observed test window - see `explain_window`'s caller and
#: requirement 8 of the corrected-reporting round.
ALREADY_CONSUMED_TEST_WINDOW_WARNING = (
    "This TEST window has already been evaluated and its results observed in this report. It must "
    "NOT be treated as an untouched final holdout for any future strategy selection, parameter "
    "change, or go/no-go decision - doing so would silently reintroduce exactly the kind of "
    "test-set leakage a holdout is meant to prevent. A genuinely unseen test window requires new, "
    "not-yet-observed data collected after this evaluation."
)


def compute_extended_diagnostics(
    trades: list[Trade],
    equity_curve: list[EquityPoint],
    window_end_time_ms: int,
    starting_equity: Decimal,
    ending_cash_quote: Decimal,
    ending_base_quantity: Decimal,
    final_mark_price: Decimal,
    reported_ending_equity: Decimal,
    open_position: OpenPositionInfo | None,
    executed_entries: int,
    buy_signals_generated: int,
    exposure_pct: float,
    total_return_pct: float,
    annualized_return_pct: float | None,
    max_drawdown_pct: float,
    shutdown_activations: dict[str, ShutdownActivation],
    rejected_entries_by_reason: dict[str, int],
    already_consumed: bool = False,
    scope_note: str | None = None,
) -> ExtendedDiagnosticsReport:
    accounting = compute_accounting_identity(
        ending_cash_quote, ending_base_quantity, final_mark_price, reported_ending_equity
    )
    pnl_breakdown = compute_pnl_breakdown(trades, open_position, final_mark_price)
    explanation = explain_window(
        trades, executed_entries, open_position, shutdown_activations, rejected_entries_by_reason,
        window_end_time_ms, buy_signals_generated,
    )
    time_based = compute_time_based_performance(
        equity_curve, total_return_pct, annualized_return_pct, max_drawdown_pct, exposure_pct
    )
    trade_distribution = compute_trade_distribution(trades, starting_equity)
    bootstrap = compute_bootstrap_confidence_interval(trades, starting_equity)
    rolling_windows = compute_rolling_window_diagnostics(trades, starting_equity)

    return ExtendedDiagnosticsReport(
        accounting=accounting,
        pnl_breakdown=pnl_breakdown,
        explanation=explanation,
        time_based=time_based,
        trade_distribution=trade_distribution,
        bootstrap=bootstrap,
        rolling_windows=rolling_windows,
        already_consumed_warning=ALREADY_CONSUMED_TEST_WINDOW_WARNING if already_consumed else None,
        scope_note=scope_note,
    )
