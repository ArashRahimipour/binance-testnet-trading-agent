"""Pure message-text builders for shadow-mode Telegram notifications - no
I/O, no side effects, never touches a network or a database. Every number
here is either read directly from an already-persisted/already-computed
shadow value, or - for the ENTRY message's planned stop-loss/take-profit
prices - EXACTLY re-derived by calling `backtest/risk_reward.py::
build_realized_plan` (public, UNMODIFIED) with the identical inputs
`backtest/engine.py::run_segment` used internally, never approximated and
never a second implementation of that math (see
`compute_realized_plan_for_display`).

`NO_REAL_ORDER_NOTE` is appended to every entry/exit message - see the
mandate this package was built under: shadow mode never places a real or
Testnet order, and every notification says so prominently.
"""

from __future__ import annotations

from decimal import Decimal

from trading_agent.backtest.risk_reward import RR_APPROVED, RiskRewardPlan, build_realized_plan
from trading_agent.config.models import AppConfig
from trading_agent.metrics.performance import (
    EXIT_REASON_STOP_LOSS,
    EXIT_REASON_TAKE_PROFIT,
    Trade,
)
from trading_agent.shadow.report import ShadowReport
from trading_agent.shadow.store import ShadowTradeRecord
from trading_agent.sizing.exchange_filters import SymbolFilters

NO_REAL_ORDER_NOTE = "⚠️ NO REAL OR TESTNET ORDER WAS PLACED - this is a simulated shadow-mode event only."

_EXIT_REASON_LABELS = {
    EXIT_REASON_TAKE_PROFIT: "TAKE-PROFIT",
    EXIT_REASON_STOP_LOSS: "STOP-LOSS",
}
_STRATEGY_EXIT_LABEL = "STRATEGY EXIT"

_HOUR_MS = 3_600_000


def compute_realized_plan_for_display(
    entry_price: Decimal,
    quantity: Decimal,
    equity_before_entry: Decimal,
    config: AppConfig,
    filters: SymbolFilters,
) -> RiskRewardPlan:
    """Exactly reproduce the stop-loss/take-profit prices `run_segment`
    computed internally for this entry - a pure, deterministic recompute
    using the SAME public `build_realized_plan` function, fed the SAME
    fill price, config-derived stop distance/fee/slippage rates, exchange
    filters, and pre-entry equity that produced the real result. Never
    approximated, never a duplicate implementation of the algebra."""
    placeholder = RiskRewardPlan(
        approved=True, reason_code=RR_APPROVED, quantity=quantity,
        stop_price=None, target_price=None, planned_risk_quote=None, planned_risk_pct=None,
        planned_reward_quote=None, planned_reward_pct=None, gross_reward_to_risk=None, net_reward_to_risk=None,
    )
    return build_realized_plan(
        placeholder, entry_price, config.stop_loss.stop_distance_pct,
        config.fees.taker_fee_pct, config.fees.slippage_pct, equity_before_entry, filters,
    )


def _fmt_money(value: Decimal | float | None) -> str:
    if value is None:
        return "n/a"
    return f"${float(value):,.4f}"


def _fmt_pct(value: float | None, *, from_fraction: bool = True) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.2f}%" if from_fraction else f"{value:.2f}%"


def build_entry_message(
    *,
    event_id: str,
    symbol: str,
    signal_time_ms: int,
    entry_time_ms: int,
    entry_price: Decimal,
    entry_reference_price: Decimal,
    quantity: Decimal,
    entry_fee_quote: Decimal,
    equity_before_entry: Decimal,
    realized_plan: RiskRewardPlan,
    signal_reason_code: str,
    signal_inputs: dict,
    config: AppConfig,
) -> str:
    notional = quantity * entry_price
    entry_slippage_quote = (entry_price - entry_reference_price) * quantity
    breakout_level = signal_inputs.get("governing_breakout_level")
    four_h_reason = (
        f"4h Donchian breakout confirmed above {breakout_level}" if breakout_level is not None
        else "4h Donchian breakout + rising 4h EMA200 regime confirmed"
    )
    # Estimated (not yet incurred) exit-leg cost, computed with the SAME
    # fee/slippage formulas `execution/backtest_broker.py::simulate_sell`
    # applies to a real exit fill - never reused as a decision input, pure
    # display arithmetic against the planned take-profit price.
    taker_fee_pct = Decimal(str(config.fees.taker_fee_pct))
    slippage_pct = Decimal(str(config.fees.slippage_pct))
    estimated_exit_fee_quote = None
    estimated_exit_slippage_quote = None
    if realized_plan.target_price is not None:
        estimated_exit_fee_quote = quantity * realized_plan.target_price * (1 - slippage_pct) * taker_fee_pct
        estimated_exit_slippage_quote = quantity * realized_plan.target_price * slippage_pct

    lines = [
        "\U0001f7e2 SHADOW ENTRY",
        f"Event ID: {event_id}",
        f"Symbol: {symbol}",
        "Side: BUY (simulated long)",
        f"Signal timestamp (ms): {signal_time_ms}",
        f"Hypothetical fill timestamp (ms): {entry_time_ms}",
        f"Entry price: {_fmt_money(entry_price)}",
        f"Quantity: {quantity}",
        f"Position notional: {_fmt_money(notional)}",
        f"Stop-loss price: {_fmt_money(realized_plan.stop_price)}",
        f"Take-profit price: {_fmt_money(realized_plan.target_price)}",
        (
            f"Maximum planned loss: {_fmt_money(realized_plan.planned_risk_quote)} "
            f"({_fmt_pct(realized_plan.planned_risk_pct)})"
        ),
        f"Planned net profit (after costs): {_fmt_money(realized_plan.planned_reward_quote)}",
        f"Net reward/risk after costs: {realized_plan.net_reward_to_risk:.2f}" if realized_plan.net_reward_to_risk is not None else "Net reward/risk after costs: n/a",
        f"Entry fee (actual): {_fmt_money(entry_fee_quote)}",
        f"Exit fee (estimated at take-profit): {_fmt_money(estimated_exit_fee_quote)}",
        f"Entry slippage (actual): {_fmt_money(entry_slippage_quote)}",
        f"Exit slippage (estimated at take-profit): {_fmt_money(estimated_exit_slippage_quote)}",
        "Weekly regime: BULLISH (close above rising 40-period weekly EMA - required for any entry)",
        f"4h setup: {four_h_reason}",
        f"1h confirmation: {signal_reason_code}",
        f"Simulated equity before this entry: {_fmt_money(equity_before_entry)}",
        "",
        NO_REAL_ORDER_NOTE,
    ]
    return "\n".join(lines)


def compute_trade_stats_to_date(
    trade_records: list[ShadowTradeRecord],
) -> tuple[int, float | None, float | None, float | None]:
    """`(closed_trade_count, win_rate_pct, expectancy_quote, expectancy_r)` -
    the exact same win-rate/expectancy formulas `shadow/report.py::
    build_shadow_report` applies to the full stored trade history, reused
    here unmodified so an exit notification's "to date" figures always
    match what `shadow-report` shows once the enclosing cycle commits.
    Callers pass already-persisted trades plus this cycle's own new trades
    up to and including the one being reported, in chronological order."""
    trades = [r.trade for r in trade_records]
    if not trades:
        return 0, None, None, None
    wins = [t for t in trades if t.pnl_quote > 0]
    win_rate_pct = 100.0 * len(wins) / len(trades)
    expectancy_quote = float(sum(t.pnl_quote for t in trades) / len(trades))
    r_multiples = [
        float(r.trade.pnl_quote / r.planned_risk_quote)
        for r in trade_records
        if r.planned_risk_quote is not None and r.planned_risk_quote > 0
    ]
    expectancy_r = sum(r_multiples) / len(r_multiples) if r_multiples else None
    return len(trades), win_rate_pct, expectancy_quote, expectancy_r


def build_exit_message(
    *,
    event_id: str,
    symbol: str,
    trade: Trade,
    planned_risk_quote: Decimal | None,
    updated_equity: Decimal,
    closed_trade_count: int,
    win_rate_pct: float | None,
    expectancy_quote: float | None,
    expectancy_r: float | None,
) -> str:
    exit_reason_label = _EXIT_REASON_LABELS.get(trade.exit_reason, _STRATEGY_EXIT_LABEL)
    gross_pnl = trade.pnl_quote + trade.fees_paid
    slippage_cost = (
        (trade.entry_price - trade.entry_reference_price) * trade.quantity
        + (trade.exit_reference_price - trade.exit_price) * trade.quantity
    )
    entry_notional = trade.quantity * trade.entry_price
    net_pnl_pct = float(trade.pnl_quote / entry_notional) * 100 if entry_notional > 0 else None
    holding_hours = (trade.exit_time_ms - trade.entry_time_ms) / _HOUR_MS
    realized_r = (
        float(trade.pnl_quote / planned_risk_quote) if planned_risk_quote is not None and planned_risk_quote > 0
        else None
    )

    lines = [
        "\U0001f534 SHADOW EXIT",
        f"Event ID: {event_id}",
        f"Symbol: {symbol}",
        f"Exit reason: {exit_reason_label}",
        f"Entry timestamp (ms): {trade.entry_time_ms}  price: {_fmt_money(trade.entry_price)}",
        f"Exit timestamp (ms): {trade.exit_time_ms}  price: {_fmt_money(trade.exit_price)}",
        f"Quantity: {trade.quantity}",
        f"Holding duration: {holding_hours:.1f} hour(s)",
        f"Gross PnL: {_fmt_money(gross_pnl)}",
        f"Fees paid: {_fmt_money(trade.fees_paid)}",
        f"Slippage cost: {_fmt_money(slippage_cost)}",
        f"Net PnL: {_fmt_money(trade.pnl_quote)} ({net_pnl_pct:.2f}% of entry notional)" if net_pnl_pct is not None else f"Net PnL: {_fmt_money(trade.pnl_quote)}",
        f"Realized R-multiple: {realized_r:.2f}R" if realized_r is not None else "Realized R-multiple: n/a",
        f"Updated simulated equity: {_fmt_money(updated_equity)}",
        f"Closed trade count (to date): {closed_trade_count}",
        f"Win rate (to date): {_fmt_pct(win_rate_pct, from_fraction=False)}" if win_rate_pct is not None else "Win rate (to date): n/a",
        f"Expectancy (to date): {_fmt_money(expectancy_quote)}" + (f" ({expectancy_r:.2f}R)" if expectancy_r is not None else ""),
        "",
        NO_REAL_ORDER_NOTE,
    ]
    return "\n".join(lines)


def build_daily_summary_message(melbourne_date_iso: str, report: ShadowReport) -> str:
    perf = report.performance
    state = report.run_state
    open_position_line = "none"
    if report.open_position is not None:
        pos = report.open_position
        open_position_line = (
            f"entry_price={pos.entry_price} quantity={pos.quantity} "
            f"unrealized_pnl={_fmt_money(pos.unrealized_pnl_quote)}"
        )
    lines = [
        f"\U0001f4c5 SHADOW DAILY SUMMARY - {melbourne_date_iso} (Australia/Melbourne)",
        f"Last cycle status: {state.last_cycle_status}",
        f"Simulated equity: {_fmt_money(Decimal(str(perf.ending_equity)))}",
        f"Open position: {open_position_line}",
        f"Closed trades: {perf.trade_count}",
        f"Win rate: {_fmt_pct(perf.win_rate, from_fraction=False)}" if perf.win_rate is not None else "Win rate: n/a",
        f"Expectancy: {_fmt_money(report.expectancy_quote)}" + (f" ({report.expectancy_r:.2f}R)" if report.expectancy_r is not None else ""),
        f"Max drawdown: {perf.max_drawdown_pct:.2f}%",
        (
            f"Data gaps: {report.data_gaps.gap_count} confirmed, current segment "
            f"{report.data_gaps.latest_segment_length}/{report.data_gaps.effective_min_required_candles}"
        ),
        f"Last processed candle close (ms): {state.last_processed_close_time_ms}",
        f"Promotion progress: {report.promotion_review_note}",
        "",
        report.not_profitable_note,
    ]
    return "\n".join(lines)


def build_test_message() -> str:
    return (
        "\U0001f9ea SHADOW TEST NOTIFICATION\n"
        "This is a manually triggered test message from shadow mode - it does not "
        "describe any real trading event.\n\n" + NO_REAL_ORDER_NOTE
    )


__all__ = [
    "NO_REAL_ORDER_NOTE",
    "build_daily_summary_message",
    "build_entry_message",
    "build_exit_message",
    "build_test_message",
    "compute_realized_plan_for_display",
    "compute_trade_stats_to_date",
]
