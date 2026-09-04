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
RISK_POLICY.md).

Every proposed trade goes through the same RiskEngine and OrderValidator
that a testnet run would use; only the broker is swapped for
`BacktestBroker`'s simulated fills. Chronological train/validation/test
splits are CHRONOLOGICAL HOLDOUT REPORTING ONLY, not model selection - the
strategy's parameters are fixed by config and never fit to any split
(review Finding 10: this is not genuine rolling walk-forward re-fitting).

UTC-day boundary ordering (review round 2, Finding 5): a signal queued on
one candle can fill on the next, which may be the first candle of a new
UTC day. Each iteration therefore detects/initializes a new day (using
that candle's OPEN price for the day's starting equity, since a queued
fill or stop-loss executes against that open, not some later, already-
moved price) and decrements cooldown BEFORE resolving any pending signal
or checking the stop-loss - so a trade that executes on the first candle
of a new day has its count and realized PnL attributed to that new day,
never discarded into the day that merely queued it or a fresh cooldown
immediately eaten into by that same candle's decrement.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trading_agent.config.models import AppConfig
from trading_agent.data.models import Candle
from trading_agent.data.validation import validate_candle_sequence
from trading_agent.execution.backtest_broker import BacktestBroker, SimulatedFill
from trading_agent.execution.order_validator import validate_order
from trading_agent.journal.journal import Journal
from trading_agent.metrics.performance import (
    EquityPoint,
    PerformanceReport,
    Trade,
    compute_performance_report,
)
from trading_agent.portfolio.state import PortfolioState, apply_buy, apply_sell
from trading_agent.risk.engine import RiskEngine
from trading_agent.risk.limits import RiskContext, TradeIntent
from trading_agent.sizing.exchange_filters import SymbolFilters
from trading_agent.sizing.position_sizer import (
    compute_risk_based_buy_quantity,
    compute_sell_quantity,
)
from trading_agent.strategy.base import PositionSide, SignalType
from trading_agent.strategy.trend_baseline import EmaCrossoverTrendStrategy

_MS_PER_DAY = 24 * 60 * 60 * 1000

SPLIT_NAMES = ("train", "validation", "test")


@dataclass(frozen=True, slots=True)
class BacktestResult:
    trades: list[Trade]
    equity_curve: list[EquityPoint]
    reports: dict[str, PerformanceReport]
    warnings: list[str]
    unexecuted_final_signal: str | None  # reason a queued signal on the last candle went unfilled


@dataclass
class _OpenTrade:
    entry_time_ms: int
    entry_price: Decimal
    quantity: Decimal
    entry_fee: Decimal


@dataclass
class _LoopState:
    portfolio: PortfolioState
    open_trade: _OpenTrade | None
    stop_price: Decimal | None
    trades_today: int
    daily_realized_pnl_pct: float
    daily_start_equity: Decimal
    cooldown_bars_remaining: int


def run_backtest(
    candles: list[Candle],
    config: AppConfig,
    filters: SymbolFilters,
    journal: Journal | None = None,
) -> BacktestResult:
    interval = config.market.interval
    validate_candle_sequence(candles, interval)

    strategy = EmaCrossoverTrendStrategy(config.strategy.ema_fast, config.strategy.ema_slow)
    risk_engine = RiskEngine(config.risk)
    broker = BacktestBroker(config.fees)

    min_required = config.strategy.ema_slow + 1
    if len(candles) < min_required:
        raise ValueError(f"need at least {min_required} candles to backtest, got {len(candles)}")

    n = len(candles)
    train_end_idx = int(n * config.backtest.train_fraction)
    validation_end_idx = train_end_idx + int(n * config.backtest.validation_fraction)

    state = _LoopState(
        portfolio=PortfolioState.initial(Decimal(50)),
        open_trade=None,
        stop_price=None,
        trades_today=0,
        daily_realized_pnl_pct=0.0,
        daily_start_equity=Decimal(50),
        cooldown_bars_remaining=0,
    )
    peak_equity = state.portfolio.equity(candles[min_required - 1].close)

    pending_signal: SignalType | None = None
    trades: list[Trade] = []
    equity_curve: list[EquityPoint] = []
    split_labels: list[str] = []
    current_day: int | None = None

    for i in range(min_required - 1, n):
        candle = candles[i]

        # Round 2 finding #5: detect/initialize a new UTC day FIRST (using
        # this candle's OPEN, since that is when any pending signal from the
        # previous candle actually fills), THEN resolve that pending signal -
        # so its trade count and realized PnL are attributed to the day it
        # actually executes in, never to the day it merely was decided on.
        # Cooldown is decremented here too, before any fill this candle can
        # set a fresh cooldown, so a just-triggered cooldown is never
        # immediately eaten into within the same candle that set it.
        day = candle.open_time_ms // _MS_PER_DAY
        if current_day is None or day != current_day:
            current_day = day
            state.daily_start_equity = state.portfolio.equity(candle.open)
            state.daily_realized_pnl_pct = 0.0
            state.trades_today = 0
        state.cooldown_bars_remaining = max(0, state.cooldown_bars_remaining - 1)

        if pending_signal is not None:
            state, trades, peak_equity = _resolve_pending_signal(
                pending_signal, candle, state, trades, config, filters, risk_engine, broker, peak_equity, journal
            )
            pending_signal = None

        if (
            state.portfolio.position_side == PositionSide.LONG
            and state.stop_price is not None
            and candle.low <= state.stop_price
        ):
            state, trades = _execute_stop_exit(candle, state, trades, broker, config, journal)

        equity_now = state.portfolio.equity(candle.close)
        peak_equity = max(peak_equity, equity_now)

        signal = strategy.generate_signal(candles[: i + 1], state.portfolio.position_side)
        if journal is not None:
            journal.record(
                "SIGNAL",
                {"type": signal.type.value, "reason_code": signal.reason_code, **signal.inputs},
                candle.close_time_ms,
            )
        if signal.type in (SignalType.BUY, SignalType.EXIT):
            pending_signal = signal.type

        equity_curve.append(
            EquityPoint(
                timestamp_ms=candle.close_time_ms,
                equity=state.portfolio.equity(candle.close),
                in_position=state.portfolio.position_side == PositionSide.LONG,
            )
        )
        split_labels.append(_split_name(i, train_end_idx, validation_end_idx))

    unexecuted_final_signal = None
    if pending_signal is not None:
        unexecuted_final_signal = (
            f"a {pending_signal.value.upper()} signal was generated on the final candle and is unexecuted - "
            "execution requires a following candle's open, which does not exist at the end of the series."
        )

    reports: dict[str, PerformanceReport] = {}
    warnings: list[str] = []
    if unexecuted_final_signal:
        warnings.append(unexecuted_final_signal)

    for split in (*SPLIT_NAMES, "overall"):
        split_equity = [p for p, label in zip(equity_curve, split_labels) if split == "overall" or label == split]
        split_trades = [
            t for t in trades
            if split == "overall" or _split_name_for_time(t.exit_time_ms, candles, train_end_idx, validation_end_idx) == split
        ]
        buy_and_hold_pct = _buy_and_hold_return_pct(split_equity, candles)
        report = compute_performance_report(
            split_trades, split_equity, interval, config.backtest.min_trades_for_significance, buy_and_hold_pct
        )
        reports[split] = report
        if report.low_trade_count_warning:
            warnings.append(
                f"{split}: only {report.trade_count} trade(s), below the configured "
                f"significance threshold of {config.backtest.min_trades_for_significance} - "
                "results are not statistically meaningful."
            )

    return BacktestResult(
        trades=trades, equity_curve=equity_curve, reports=reports, warnings=warnings,
        unexecuted_final_signal=unexecuted_final_signal,
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
) -> tuple[_LoopState, list[Trade], Decimal]:
    reference_price = candle.open
    equity = state.portfolio.equity(reference_price)
    peak_equity = max(peak_equity, equity)
    drawdown_pct = float((peak_equity - equity) / peak_equity) if peak_equity > 0 else 0.0

    if signal_type == SignalType.BUY:
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
        return state, trades, peak_equity

    validation = validate_order(intent, filters)
    if journal is not None:
        journal.record(
            "ORDER_VALIDATION",
            {"signal_type": signal_type.value, "approved": validation.approved, "reason_code": validation.reason_code},
            candle.open_time_ms,
        )
    if not validation.approved or validation.validated_quantity is None:
        return state, trades, peak_equity

    quantity = validation.validated_quantity

    if signal_type == SignalType.BUY:
        fill = broker.simulate_buy(quantity, reference_price)
        state.portfolio = apply_buy(state.portfolio, quantity, fill.fill_price, fill.fee_quote)
        state.stop_price = fill.fill_price * (1 - Decimal(str(config.stop_loss.stop_distance_pct)))
        state.open_trade = _OpenTrade(candle.open_time_ms, fill.fill_price, quantity, fill.fee_quote)
        state.trades_today += 1
    else:
        fill = broker.simulate_sell(quantity, reference_price)
        state = _apply_exit_fill(state, candle.open_time_ms, quantity, fill, trades, config)

    return state, trades, peak_equity


def _execute_stop_exit(
    candle: Candle,
    state: _LoopState,
    trades: list[Trade],
    broker: BacktestBroker,
    config: AppConfig,
    journal: Journal | None,
) -> tuple[_LoopState, list[Trade]]:
    if state.stop_price is None:  # pragma: no cover - guarded by caller
        raise ValueError("_execute_stop_exit called without an active stop price")
    # A gap through the stop fills at the worse (lower) of the stop price
    # or the candle's open - never assume a fill better than the market allowed.
    fill_reference_price = min(state.stop_price, candle.open)
    quantity = state.portfolio.base_balance
    fill = broker.simulate_sell(quantity, fill_reference_price)
    state = _apply_exit_fill(state, candle.open_time_ms, quantity, fill, trades, config)
    if journal is not None:
        journal.record(
            "STOP_LOSS_TRIGGERED",
            {"stop_price": str(fill_reference_price), "quantity": str(quantity)},
            candle.open_time_ms,
        )
    return state, trades


def _apply_exit_fill(
    state: _LoopState, exit_time_ms: int, quantity: Decimal, fill: SimulatedFill, trades: list[Trade], config: AppConfig
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
            )
        )

    if state.daily_start_equity > 0:
        state.daily_realized_pnl_pct += float(realized / state.daily_start_equity)
    if realized < 0:
        state.cooldown_bars_remaining = config.risk.cooldown_bars_after_loss
    state.trades_today += 1
    state.stop_price = None
    state.open_trade = None
    return state


def _split_name(index: int, train_end_idx: int, validation_end_idx: int) -> str:
    if index < train_end_idx:
        return "train"
    if index < validation_end_idx:
        return "validation"
    return "test"


def _split_name_for_time(time_ms: int, candles: list[Candle], train_end_idx: int, validation_end_idx: int) -> str:
    for i, candle in enumerate(candles):
        if candle.open_time_ms == time_ms:
            return _split_name(i, train_end_idx, validation_end_idx)
    return "test"


def _buy_and_hold_return_pct(equity_points: list[EquityPoint], candles: list[Candle]) -> float | None:
    if not equity_points:
        return None
    start_time = equity_points[0].timestamp_ms
    end_time = equity_points[-1].timestamp_ms
    start_close = next((c.close for c in candles if c.close_time_ms == start_time), None)
    end_close = next((c.close for c in candles if c.close_time_ms == end_time), None)
    if start_close is None or end_close is None or start_close == 0:
        return None
    return float((end_close / start_close - 1) * 100)
