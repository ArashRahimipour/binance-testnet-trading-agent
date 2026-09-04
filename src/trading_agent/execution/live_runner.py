"""One testnet decision cycle: fetch data, decide, risk-check, execute.

This is invoked once per completed candle - by a person running the CLI
manually, or by an external scheduler (see README.md) - rather than
running its own polling loop. That keeps the process simple: it starts,
makes at most one trade decision, records everything, and exits.

Known simplifications, documented rather than hidden:
  - Cold start requires the testnet account to be flat (no base asset
    balance beyond dust) - if the account already holds a position, we
    have no way to know its cost basis from a balance query alone, and
    guessing would corrupt PnL accounting. The operator must flatten
    manually via the Testnet UI first.
  - Order fee is approximated as `cumulative_quote_qty * taker_fee_pct`
    rather than parsed from the exact per-fill commission (which can be
    charged in a different asset, e.g. BNB fee discounts). This is
    conservative for a small, fee-dominated V0.1 test account but is not
    exact accounting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import requests

from trading_agent.config.models import AppConfig, Secrets
from trading_agent.data.ingestion import fetch_completed_candles, require_non_empty
from trading_agent.data.market_data_public import TESTNET_HOST, BinancePublicMarketDataClient
from trading_agent.data.validation import validate_candle_sequence, validate_not_stale
from trading_agent.execution.client_order_id import generate_client_order_id
from trading_agent.execution.order_validator import validate_order
from trading_agent.execution.reconciliation import ReconciledStatus, reconcile_order, safe_to_retry
from trading_agent.execution.testnet_adapter import BinanceApiError, TestnetBrokerAdapter
from trading_agent.journal.journal import Journal
from trading_agent.persistence.portfolio_store import PortfolioStore
from trading_agent.persistence.risk_state_store import RiskState, RiskStateStore
from trading_agent.portfolio.state import PortfolioState, apply_buy, apply_sell
from trading_agent.risk.engine import RiskEngine
from trading_agent.risk.kill_switch import KillSwitch
from trading_agent.risk.limits import RiskContext, TradeIntent
from trading_agent.sizing.exchange_filters import SymbolFilters
from trading_agent.sizing.position_sizer import compute_buy_quantity, compute_sell_quantity
from trading_agent.strategy.base import SignalType
from trading_agent.strategy.trend_baseline import EmaCrossoverTrendStrategy


class ColdStartReconciliationError(Exception):
    """Raised when the testnet account is not flat and no cost basis can be established."""


@dataclass(frozen=True, slots=True)
class CycleResult:
    action: str  # "HOLD", "BUY", "SELL", "NO_TRADE", "ERROR"
    reason_code: str
    detail: dict


def _quote_asset(symbol: str) -> str:
    for suffix in ("USDT", "USDC", "BUSD", "BTC", "ETH"):
        if symbol.endswith(suffix):
            return suffix
    raise ValueError(f"cannot determine quote asset for symbol {symbol!r}")


def reconcile_initial_portfolio(
    adapter: TestnetBrokerAdapter, symbol: str, filters: SymbolFilters
) -> PortfolioState:
    quote_asset = _quote_asset(symbol)
    base_asset = symbol[: -len(quote_asset)]
    balances = adapter.get_account_balances()
    quote_balance = balances.get(quote_asset, Decimal(0))
    base_balance = balances.get(base_asset, Decimal(0))
    if base_balance >= filters.min_qty:
        raise ColdStartReconciliationError(
            f"testnet account already holds {base_balance} {base_asset}; cannot determine "
            "its cost basis from a balance query. Flatten the position manually via the "
            "Testnet UI before the first run."
        )
    return PortfolioState.initial(quote_balance)


def _utc_day_key(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).strftime("%Y-%m-%d")


def run_testnet_cycle(
    config: AppConfig,
    secrets: Secrets,
    journal: Journal,
    portfolio_store: PortfolioStore,
    risk_state_store: RiskStateStore,
) -> CycleResult:
    symbol = config.market.symbol
    interval = config.market.interval
    kill_switch = KillSwitch(config.paths.data_dir / "KILL_SWITCH")
    public_client = BinancePublicMarketDataClient(TESTNET_HOST)
    adapter = TestnetBrokerAdapter(secrets.testnet_api_key, secrets.testnet_api_secret)

    try:
        server_time_ms = public_client.get_server_time_ms()
        candles = fetch_completed_candles(
            public_client, symbol, interval, limit=config.strategy.ema_slow + 10, reference_time_ms=server_time_ms
        )
        require_non_empty(candles)
        validate_candle_sequence(candles, interval)
        validate_not_stale(candles, server_time_ms, config.risk.stale_data_max_age_seconds)
        filters = SymbolFilters.from_exchange_info(public_client.get_exchange_info(symbol))
    except Exception as exc:  # noqa: BLE001 - any data problem blocks trading this cycle
        journal.record("EXCEPTION", {"stage": "data_fetch", "error": str(exc)}, 0)
        return CycleResult("NO_TRADE", "DATA_UNAVAILABLE_OR_INVALID", {"error": str(exc)})

    last_candle = candles[-1]

    portfolio = portfolio_store.load(symbol)
    if portfolio is None:
        portfolio = reconcile_initial_portfolio(adapter, symbol, filters)
        portfolio_store.save(symbol, portfolio, server_time_ms)

    day_key = _utc_day_key(server_time_ms)
    risk_state = risk_state_store.load(symbol)
    equity_now = portfolio.equity(last_candle.close)
    if risk_state is None:
        risk_state = RiskState.initial(equity_now, day_key)
    elif risk_state.day_key != day_key:
        risk_state = RiskState(
            day_key=day_key,
            daily_start_equity=equity_now,
            daily_realized_pnl_pct=0.0,
            trades_today=0,
            peak_equity=risk_state.peak_equity,
            cooldown_bars_remaining=risk_state.cooldown_bars_remaining,
            consecutive_api_errors=risk_state.consecutive_api_errors,
        )
    risk_state.cooldown_bars_remaining = max(0, risk_state.cooldown_bars_remaining - 1)
    risk_state.peak_equity = max(risk_state.peak_equity, equity_now)

    strategy = EmaCrossoverTrendStrategy(config.strategy.ema_fast, config.strategy.ema_slow)
    signal = strategy.generate_signal(candles, portfolio.position_side)
    journal.record(
        "SIGNAL",
        {"type": signal.type.value, "reason_code": signal.reason_code, **signal.inputs},
        last_candle.close_time_ms,
    )

    if signal.type == SignalType.HOLD:
        risk_state_store.save(symbol, risk_state)
        return CycleResult("HOLD", signal.reason_code, {})

    if signal.type == SignalType.BUY:
        sizing = compute_buy_quantity(
            portfolio.quote_balance,
            last_candle.close,
            config.sizing.max_allocation_pct,
            Decimal(str(config.sizing.min_quote_buffer)),
            filters,
        )
        side = "BUY"
    else:
        sizing = compute_sell_quantity(portfolio.base_balance, last_candle.close, filters)
        side = "SELL"

    journal.record(
        "SIZING", {"approved": sizing.approved, "reason_code": sizing.reason_code}, last_candle.close_time_ms
    )
    if not sizing.approved or sizing.quantity is None:
        risk_state_store.save(symbol, risk_state)
        return CycleResult("NO_TRADE", sizing.reason_code, {})

    client_order_id = generate_client_order_id(symbol, side, last_candle.close_time_ms)
    already_submitted = any(
        e["payload"].get("client_order_id") == client_order_id
        for e in journal.entries_by_type("ORDER_SUBMITTED")
    )

    drawdown_pct = (
        float((risk_state.peak_equity - equity_now) / risk_state.peak_equity)
        if risk_state.peak_equity > 0
        else 0.0
    )
    intent = TradeIntent(signal.type, symbol, sizing.quantity, last_candle.close)
    context = RiskContext(
        equity=equity_now,
        quote_balance=portfolio.quote_balance,
        trades_today=risk_state.trades_today,
        cooldown_bars_remaining=risk_state.cooldown_bars_remaining,
        daily_realized_pnl_pct=risk_state.daily_realized_pnl_pct,
        current_drawdown_pct=drawdown_pct,
        data_age_seconds=(server_time_ms - last_candle.close_time_ms) / 1000,
        consecutive_api_errors=risk_state.consecutive_api_errors,
        kill_switch_engaged=kill_switch.is_engaged(),
        is_duplicate_order=already_submitted,
    )
    risk_decision = RiskEngine(config.risk).evaluate(intent, context)
    journal.record(
        "RISK_DECISION",
        {"approved": risk_decision.approved, "reason_code": risk_decision.reason_code},
        last_candle.close_time_ms,
    )
    if not risk_decision.approved:
        risk_state_store.save(symbol, risk_state)
        return CycleResult("NO_TRADE", risk_decision.reason_code, {})

    validation = validate_order(intent, filters)
    journal.record(
        "ORDER_VALIDATION",
        {"approved": validation.approved, "reason_code": validation.reason_code},
        last_candle.close_time_ms,
    )
    if not validation.approved or validation.validated_quantity is None:
        risk_state_store.save(symbol, risk_state)
        return CycleResult("NO_TRADE", validation.reason_code, {})

    quantity = validation.validated_quantity
    journal.record(
        "ORDER_SUBMITTED",
        {"client_order_id": client_order_id, "side": side, "quantity": str(quantity)},
        last_candle.close_time_ms,
    )

    try:
        order = adapter.place_market_order(symbol, side, quantity, client_order_id)
    except requests.Timeout:
        status, reconciled_order = reconcile_order(adapter, symbol, client_order_id)
        journal.record("RECONCILIATION", {"status": status.value}, last_candle.close_time_ms)
        if safe_to_retry(status):
            try:
                order = adapter.place_market_order(symbol, side, quantity, client_order_id)
            except (requests.Timeout, BinanceApiError) as exc:
                risk_state.consecutive_api_errors += 1
                risk_state_store.save(symbol, risk_state)
                journal.record("EXCEPTION", {"stage": "retry_after_timeout", "error": str(exc)}, last_candle.close_time_ms)
                return CycleResult("ERROR", "RETRY_FAILED", {"error": str(exc)})
        elif status in (ReconciledStatus.CONFIRMED_TERMINAL, ReconciledStatus.CONFIRMED_OPEN) and reconciled_order:
            order = reconciled_order
        else:
            risk_state.consecutive_api_errors += 1
            risk_state_store.save(symbol, risk_state)
            journal.record("EXCEPTION", {"stage": "reconciliation_unknown"}, last_candle.close_time_ms)
            return CycleResult("ERROR", "RECONCILIATION_UNKNOWN", {})
    except BinanceApiError as exc:
        risk_state.consecutive_api_errors += 1
        risk_state_store.save(symbol, risk_state)
        journal.record("EXCEPTION", {"stage": "place_order", "error": str(exc)}, last_candle.close_time_ms)
        return CycleResult("ERROR", "API_ERROR", {"error": str(exc)})

    risk_state.consecutive_api_errors = 0
    journal.record("ORDER_FILLED", order.raw, last_candle.close_time_ms)

    executed_qty = order.executed_qty if order.executed_qty > 0 else quantity
    avg_fill_price = (
        order.cumulative_quote_qty / executed_qty if executed_qty > 0 else last_candle.close
    )
    fee_quote = order.cumulative_quote_qty * Decimal(str(config.fees.taker_fee_pct))

    if side == "BUY":
        portfolio = apply_buy(portfolio, executed_qty, avg_fill_price, fee_quote)
        risk_state.trades_today += 1
    else:
        pnl_before = portfolio.realized_pnl_quote
        portfolio = apply_sell(portfolio, executed_qty, avg_fill_price, fee_quote)
        realized = portfolio.realized_pnl_quote - pnl_before
        risk_state.trades_today += 1
        if risk_state.daily_start_equity > 0:
            risk_state.daily_realized_pnl_pct += float(realized / risk_state.daily_start_equity)
        if realized < 0:
            risk_state.cooldown_bars_remaining = config.risk.cooldown_bars_after_loss

    portfolio_store.save(symbol, portfolio, server_time_ms)
    risk_state_store.save(symbol, risk_state)

    return CycleResult(
        side,
        "FILLED",
        {"quantity": str(executed_qty), "price": str(avg_fill_price), "status": order.status},
    )
