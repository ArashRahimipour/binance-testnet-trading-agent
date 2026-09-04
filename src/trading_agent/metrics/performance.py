"""Backtest performance metrics.

Documented assumptions (see TESTING.md / STRATEGY.md for the full
discussion):
  - Risk-free rate is assumed to be 0 for Sharpe/Sortino.
  - Annualization uses the number of candle periods per year implied by
    the configured interval (e.g. 4h -> 365*24/4 = 2190 periods/year).
  - Sortino's downside deviation is computed against a 0% minimum
    acceptable return (i.e. any negative period return counts).
  - These are research metrics on simulated fills, not a claim about real
    trading performance.

Buy-and-hold comparison (`BuyAndHoldReport`, `compute_buy_and_hold_report`):
computed from the SAME candle range as the report it is attached to (exact
matching start/end timestamps - never a different window), starting from
the same configured `starting_equity`, with one buy-side transaction cost
applied at entry (`buy_fee_pct`, documented on the report itself) and
marked to market at the final available candle's close. It is never
computed across a confirmed gap - callers must pass a single contiguous
candle range.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal

from trading_agent.data.models import Candle, interval_to_ms

_MS_PER_YEAR = 365 * 24 * 60 * 60 * 1000

#: Values `Trade.exit_reason` can take - kept as plain strings (not an enum)
#: so journal/report serialization stays trivial, but these are the only
#: two values ever produced by the backtest engine.
EXIT_REASON_STRATEGY = "STRATEGY_EXIT"
EXIT_REASON_STOP_LOSS = "STOP_LOSS"


@dataclass(frozen=True, slots=True)
class Trade:
    entry_time_ms: int
    exit_time_ms: int
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    fees_paid: Decimal
    pnl_quote: Decimal
    #: Distinguishes a strategy-generated EXIT signal from a protective
    #: stop-loss fill - required so "executed strategy exits" and
    #: "executed stop-loss exits" can be counted separately. Defaults to
    #: the strategy-exit value so existing positional callers (tests that
    #: predate this field) are unaffected.
    exit_reason: str = EXIT_REASON_STRATEGY


@dataclass(frozen=True, slots=True)
class EquityPoint:
    timestamp_ms: int
    equity: Decimal
    in_position: bool


@dataclass(frozen=True, slots=True)
class BuyAndHoldReport:
    """A buy-and-hold comparison over the exact same candle range as the
    report it is attached to - see the module docstring."""

    return_pct: float | None
    max_drawdown_pct: float | None
    start_time_ms: int | None
    end_time_ms: int | None
    buy_fee_pct_applied: float
    note: str


@dataclass(frozen=True, slots=True)
class PerformanceReport:
    trade_count: int
    total_return_pct: float
    annualized_return_pct: float | None
    max_drawdown_pct: float
    volatility_pct: float | None
    sharpe_ratio: float | None
    sortino_ratio: float | None
    win_rate: float | None
    profit_factor: float | None
    avg_win_quote: float | None
    avg_loss_quote: float | None
    exposure_pct: float
    turnover: float
    buy_and_hold_return_pct: float | None
    low_trade_count_warning: bool
    starting_equity: Decimal = Decimal(0)
    ending_equity: Decimal = Decimal(0)
    strategy_exit_count: int = 0
    stop_loss_exit_count: int = 0
    buy_and_hold: BuyAndHoldReport | None = None
    assumptions: dict = field(default_factory=dict)


def periods_per_year(interval: str) -> float:
    return _MS_PER_YEAR / interval_to_ms(interval)


def compute_buy_and_hold_report(
    window_candles: list[Candle],
    starting_equity: Decimal,
    buy_fee_pct: float,
) -> BuyAndHoldReport:
    """Buy-and-hold over exactly `window_candles` (a single contiguous,
    gap-free range - callers must never pass candles spanning a confirmed
    gap). Buys at the first candle's close (net of one buy-side transaction
    cost, `buy_fee_pct`), marks to market at every subsequent candle's
    close for the drawdown calculation, and reports the ending mark at the
    LAST candle's close - the final available price in this range.
    """
    if not window_candles:
        return BuyAndHoldReport(
            return_pct=None,
            max_drawdown_pct=None,
            start_time_ms=None,
            end_time_ms=None,
            buy_fee_pct_applied=buy_fee_pct,
            note="no candles in this window - buy-and-hold is undefined.",
        )

    entry_price = window_candles[0].close
    start_time_ms = window_candles[0].close_time_ms
    end_time_ms = window_candles[-1].close_time_ms

    if entry_price <= 0 or starting_equity <= 0:
        return BuyAndHoldReport(
            return_pct=None,
            max_drawdown_pct=None,
            start_time_ms=start_time_ms,
            end_time_ms=end_time_ms,
            buy_fee_pct_applied=buy_fee_pct,
            note="entry price or starting equity was non-positive - buy-and-hold is undefined.",
        )

    effective_capital = starting_equity * (Decimal(1) - Decimal(str(buy_fee_pct)))
    quantity = effective_capital / entry_price
    curve = [float(quantity * c.close) for c in window_candles]
    ending_equity = curve[-1]
    return_pct = (ending_equity / float(starting_equity) - 1.0) * 100

    peak = curve[0]
    max_dd = 0.0
    for value in curve:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)

    return BuyAndHoldReport(
        return_pct=return_pct,
        max_drawdown_pct=max_dd * 100,
        start_time_ms=start_time_ms,
        end_time_ms=end_time_ms,
        buy_fee_pct_applied=buy_fee_pct,
        note=(
            f"buys at the window's first candle close ({start_time_ms}) net of a "
            f"{buy_fee_pct * 100:.3f}% one-time transaction cost, holds unconditionally, and marks "
            f"to market at the window's last available candle close ({end_time_ms}). This range is "
            "never bridged across a confirmed gap - it always matches the agent report's own "
            "candle range exactly. A comparison across a gap-segmented backtest's separate segments "
            "is only ever the sum/average of independently-computed per-segment buy-and-hold "
            "figures, never one continuous buy-and-hold position spanning the missing candles."
        ),
    )


def compute_performance_report(
    trades: list[Trade],
    equity_curve: list[EquityPoint],
    interval: str,
    min_trades_for_significance: int,
    buy_and_hold: BuyAndHoldReport | None = None,
) -> PerformanceReport:
    assumptions = {
        "risk_free_rate": 0.0,
        "periods_per_year": periods_per_year(interval),
        "sortino_minimum_acceptable_return": 0.0,
    }
    buy_and_hold_return_pct = buy_and_hold.return_pct if buy_and_hold is not None else None

    if not equity_curve:
        return PerformanceReport(
            trade_count=0,
            total_return_pct=0.0,
            annualized_return_pct=None,
            max_drawdown_pct=0.0,
            volatility_pct=None,
            sharpe_ratio=None,
            sortino_ratio=None,
            win_rate=None,
            profit_factor=None,
            avg_win_quote=None,
            avg_loss_quote=None,
            exposure_pct=0.0,
            turnover=0.0,
            buy_and_hold_return_pct=buy_and_hold_return_pct,
            low_trade_count_warning=True,
            starting_equity=Decimal(0),
            ending_equity=Decimal(0),
            buy_and_hold=buy_and_hold,
            assumptions=assumptions,
        )

    equities = [float(p.equity) for p in equity_curve]
    starting_equity = equities[0]
    ending_equity = equities[-1]
    total_return_pct = (ending_equity / starting_equity - 1.0) * 100 if starting_equity > 0 else 0.0

    duration_ms = equity_curve[-1].timestamp_ms - equity_curve[0].timestamp_ms
    annualized_return_pct = None
    if duration_ms > 0 and starting_equity > 0 and ending_equity > 0:
        years = duration_ms / _MS_PER_YEAR
        annualized_return_pct = ((ending_equity / starting_equity) ** (1 / years) - 1) * 100

    period_returns = [
        equities[i] / equities[i - 1] - 1.0
        for i in range(1, len(equities))
        if equities[i - 1] > 0
    ]

    volatility_pct = None
    sharpe_ratio = None
    sortino_ratio = None
    if len(period_returns) >= 2:
        mean_return = sum(period_returns) / len(period_returns)
        variance = sum((r - mean_return) ** 2 for r in period_returns) / (len(period_returns) - 1)
        std_dev = math.sqrt(variance)
        ppy = periods_per_year(interval)
        volatility_pct = std_dev * math.sqrt(ppy) * 100
        if std_dev > 0:
            sharpe_ratio = (mean_return / std_dev) * math.sqrt(ppy)

        downside_returns = [min(r, 0.0) for r in period_returns]
        downside_variance = sum(r**2 for r in downside_returns) / len(downside_returns)
        downside_dev = math.sqrt(downside_variance)
        if downside_dev > 0:
            sortino_ratio = (mean_return / downside_dev) * math.sqrt(ppy)

    max_drawdown_pct = _max_drawdown_pct(equities)

    exposure_pct = (
        100.0 * sum(1 for p in equity_curve if p.in_position) / len(equity_curve)
        if equity_curve
        else 0.0
    )

    win_rate = None
    profit_factor = None
    avg_win_quote = None
    avg_loss_quote = None
    turnover = 0.0
    if trades:
        wins = [float(t.pnl_quote) for t in trades if t.pnl_quote > 0]
        losses = [float(t.pnl_quote) for t in trades if t.pnl_quote < 0]
        win_rate = 100.0 * len(wins) / len(trades)
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None
        avg_win_quote = (sum(wins) / len(wins)) if wins else None
        avg_loss_quote = (sum(losses) / len(losses)) if losses else None
        total_notional = sum(float(t.quantity * t.entry_price) + float(t.quantity * t.exit_price) for t in trades)
        turnover = total_notional / starting_equity if starting_equity > 0 else 0.0

    strategy_exit_count = sum(1 for t in trades if t.exit_reason == EXIT_REASON_STRATEGY)
    stop_loss_exit_count = sum(1 for t in trades if t.exit_reason == EXIT_REASON_STOP_LOSS)

    return PerformanceReport(
        trade_count=len(trades),
        total_return_pct=total_return_pct,
        annualized_return_pct=annualized_return_pct,
        max_drawdown_pct=max_drawdown_pct,
        volatility_pct=volatility_pct,
        sharpe_ratio=sharpe_ratio,
        sortino_ratio=sortino_ratio,
        win_rate=win_rate,
        profit_factor=profit_factor,
        avg_win_quote=avg_win_quote,
        avg_loss_quote=avg_loss_quote,
        exposure_pct=exposure_pct,
        turnover=turnover,
        buy_and_hold_return_pct=buy_and_hold_return_pct,
        low_trade_count_warning=len(trades) < min_trades_for_significance,
        starting_equity=equity_curve[0].equity,
        ending_equity=equity_curve[-1].equity,
        strategy_exit_count=strategy_exit_count,
        stop_loss_exit_count=stop_loss_exit_count,
        buy_and_hold=buy_and_hold,
        assumptions=assumptions,
    )


def _max_drawdown_pct(equities: list[float]) -> float:
    peak = equities[0]
    max_dd = 0.0
    for value in equities:
        peak = max(peak, value)
        if peak > 0:
            drawdown = (peak - value) / peak
            max_dd = max(max_dd, drawdown)
    return max_dd * 100
