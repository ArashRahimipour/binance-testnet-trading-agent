"""End-to-end backtest engine.

Walks completed candles forward one at a time - the strategy only ever
sees `candles[:i+1]` at step `i`, never anything later, so there is no
look-ahead. Every proposed trade goes through the same RiskEngine and
OrderValidator that a testnet run would use; only the broker is swapped
for `BacktestBroker`'s simulated fills. Chronological train/validation/test
splits are for REPORTING ONLY - the strategy's parameters are fixed by
config and are not fit to any split (see STRATEGY.md: this project does
not optimize parameters to chase historical results).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trading_agent.config.models import AppConfig
from trading_agent.data.models import Candle
from trading_agent.data.validation import validate_candle_sequence
from trading_agent.execution.backtest_broker import BacktestBroker
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
from trading_agent.sizing.position_sizer import compute_buy_quantity, compute_sell_quantity
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


@dataclass
class _OpenTrade:
    entry_time_ms: int
    entry_price: Decimal
    quantity: Decimal
    entry_fee: Decimal


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

    portfolio = PortfolioState.initial(Decimal(50))
    peak_equity = portfolio.equity(candles[min_required - 1].close)

    open_trade: _OpenTrade | None = None
    trades: list[Trade] = []
    equity_curve: list[EquityPoint] = []
    split_index_by_position: list[str] = []

    current_day: int | None = None
    daily_start_equity = portfolio.quote_balance
    daily_realized_pnl_pct = 0.0
    trades_today = 0
    cooldown_bars_remaining = 0

    for i in range(min_required - 1, n):
        candle = candles[i]
        history = candles[: i + 1]

        day = candle.close_time_ms // _MS_PER_DAY
        if current_day is None or day != current_day:
            current_day = day
            daily_start_equity = portfolio.equity(candle.close)
            daily_realized_pnl_pct = 0.0
            trades_today = 0

        cooldown_bars_remaining = max(0, cooldown_bars_remaining - 1)

        equity_before_signal = portfolio.equity(candle.close)
        peak_equity = max(peak_equity, equity_before_signal)
        drawdown_pct = (
            float((peak_equity - equity_before_signal) / peak_equity) if peak_equity > 0 else 0.0
        )

        signal = strategy.generate_signal(history, portfolio.position_side)
        if journal is not None:
            journal.record(
                "SIGNAL",
                {"type": signal.type.value, "reason_code": signal.reason_code, **signal.inputs},
                candle.close_time_ms,
            )

        if signal.type in (SignalType.BUY, SignalType.EXIT):
            portfolio, open_trade, trades_today, daily_realized_pnl_pct, cooldown_bars_remaining = _handle_trade_signal(
                signal_type=signal.type,
                candle=candle,
                config=config,
                filters=filters,
                risk_engine=risk_engine,
                broker=broker,
                portfolio=portfolio,
                open_trade=open_trade,
                trades=trades,
                equity=equity_before_signal,
                drawdown_pct=drawdown_pct,
                trades_today=trades_today,
                daily_realized_pnl_pct=daily_realized_pnl_pct,
                daily_start_equity=daily_start_equity,
                cooldown_bars_remaining=cooldown_bars_remaining,
                journal=journal,
            )

        equity_curve.append(
            EquityPoint(
                timestamp_ms=candle.close_time_ms,
                equity=portfolio.equity(candle.close),
                in_position=portfolio.position_side == PositionSide.LONG,
            )
        )
        split_index_by_position.append(_split_name(i, train_end_idx, validation_end_idx))

    reports: dict[str, PerformanceReport] = {}
    warnings: list[str] = []
    for split in (*SPLIT_NAMES, "overall"):
        split_equity = [
            p for p, label in zip(equity_curve, split_index_by_position) if split == "overall" or label == split
        ]
        split_trades = [
            t for t in trades if split == "overall" or _split_name_for_time(t.exit_time_ms, candles, train_end_idx, validation_end_idx) == split
        ]
        buy_and_hold_pct = _buy_and_hold_return_pct(split_equity, candles)
        report = compute_performance_report(
            split_trades,
            split_equity,
            interval,
            config.backtest.min_trades_for_significance,
            buy_and_hold_pct,
        )
        reports[split] = report
        if report.low_trade_count_warning:
            warnings.append(
                f"{split}: only {report.trade_count} trade(s), below the configured "
                f"significance threshold of {config.backtest.min_trades_for_significance} - "
                "results are not statistically meaningful."
            )

    return BacktestResult(trades=trades, equity_curve=equity_curve, reports=reports, warnings=warnings)


def _handle_trade_signal(
    *,
    signal_type: SignalType,
    candle: Candle,
    config: AppConfig,
    filters: SymbolFilters,
    risk_engine: RiskEngine,
    broker: BacktestBroker,
    portfolio: PortfolioState,
    open_trade: _OpenTrade | None,
    trades: list[Trade],
    equity: Decimal,
    drawdown_pct: float,
    trades_today: int,
    daily_realized_pnl_pct: float,
    daily_start_equity: Decimal,
    cooldown_bars_remaining: int,
    journal: Journal | None,
) -> tuple[PortfolioState, _OpenTrade | None, int, float, int]:
    if signal_type == SignalType.BUY:
        sizing = compute_buy_quantity(
            portfolio.quote_balance,
            candle.close,
            config.sizing.max_allocation_pct,
            Decimal(str(config.sizing.min_quote_buffer)),
            filters,
        )
    else:
        sizing = compute_sell_quantity(portfolio.base_balance, candle.close, filters)

    if journal is not None:
        journal.record(
            "SIZING",
            {"signal_type": signal_type.value, "approved": sizing.approved, "reason_code": sizing.reason_code},
            candle.close_time_ms,
        )
    if not sizing.approved or sizing.quantity is None:
        return portfolio, open_trade, trades_today, daily_realized_pnl_pct, cooldown_bars_remaining

    intent = TradeIntent(signal_type, candle.symbol, sizing.quantity, candle.close)
    context = RiskContext(
        equity=equity,
        quote_balance=portfolio.quote_balance,
        trades_today=trades_today,
        cooldown_bars_remaining=cooldown_bars_remaining,
        daily_realized_pnl_pct=daily_realized_pnl_pct,
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
            candle.close_time_ms,
        )
    if not risk_decision.approved:
        return portfolio, open_trade, trades_today, daily_realized_pnl_pct, cooldown_bars_remaining

    validation = validate_order(intent, filters)
    if journal is not None:
        journal.record(
            "ORDER_VALIDATION",
            {"signal_type": signal_type.value, "approved": validation.approved, "reason_code": validation.reason_code},
            candle.close_time_ms,
        )
    if not validation.approved or validation.validated_quantity is None:
        return portfolio, open_trade, trades_today, daily_realized_pnl_pct, cooldown_bars_remaining

    quantity = validation.validated_quantity
    trades_today += 1

    if signal_type == SignalType.BUY:
        fill = broker.simulate_buy(quantity, candle.close)
        portfolio = apply_buy(portfolio, quantity, fill.fill_price, fill.fee_quote)
        open_trade = _OpenTrade(candle.close_time_ms, fill.fill_price, quantity, fill.fee_quote)
    else:
        fill = broker.simulate_sell(quantity, candle.close)
        pnl_before = portfolio.realized_pnl_quote
        portfolio = apply_sell(portfolio, quantity, fill.fill_price, fill.fee_quote)
        realized = portfolio.realized_pnl_quote - pnl_before
        if open_trade is not None:
            trades.append(
                Trade(
                    entry_time_ms=open_trade.entry_time_ms,
                    exit_time_ms=candle.close_time_ms,
                    entry_price=open_trade.entry_price,
                    exit_price=fill.fill_price,
                    quantity=quantity,
                    fees_paid=open_trade.entry_fee + fill.fee_quote,
                    pnl_quote=realized,
                )
            )
        if daily_start_equity > 0:
            daily_realized_pnl_pct += float(realized / daily_start_equity)
        if realized < 0:
            cooldown_bars_remaining = config.risk.cooldown_bars_after_loss
        open_trade = None

    return portfolio, open_trade, trades_today, daily_realized_pnl_pct, cooldown_bars_remaining


def _split_name(index: int, train_end_idx: int, validation_end_idx: int) -> str:
    if index < train_end_idx:
        return "train"
    if index < validation_end_idx:
        return "validation"
    return "test"


def _split_name_for_time(time_ms: int, candles: list[Candle], train_end_idx: int, validation_end_idx: int) -> str:
    for i, candle in enumerate(candles):
        if candle.close_time_ms == time_ms:
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
