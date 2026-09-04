"""One testnet decision cycle: reconcile, fetch data, decide, risk-check, execute.

This is invoked once per completed candle - by a person running the CLI
manually, or by an external scheduler (see README.md) - rather than
running its own polling loop. That keeps the process simple: it starts,
resolves anything left unresolved by a previous run, makes at most one
trade decision, records everything, and exits.

Automatic entry (BUY) is disabled on Testnet in this revision - see
RISK_POLICY.md's "Protective exits" section for why: this baseline
strategy has no verified exchange-resident stop-loss order on Testnet yet,
and shipping unexercised order-signing code for the one feature whose job
is capital protection is not a risk this project is willing to take
without first testing it against a live Testnet from an environment with
real network access. EXIT (closing an existing position) remains enabled,
consistent with the project-wide rule that de-risking is never blocked.

Known simplifications, documented rather than hidden:
  - Cold start requires the testnet account to be flat (no base asset
    balance beyond dust) - if the account already holds a position, we
    have no way to know its cost basis from a balance query alone, and
    guessing would corrupt PnL accounting. The operator must flatten
    manually via the Testnet UI first.
  - Fee accounting parses Binance's actual per-fill commission when the
    order response includes `fills` and every fill's commission is
    quote-denominated; otherwise it falls back to
    `cumulative_quote_qty * taker_fee_pct` and marks the result as
    estimated (see execution/fees.py and portfolio/state.py's
    `pnl_is_estimated`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

import requests

from trading_agent.config.models import AppConfig, Secrets
from trading_agent.data.ingestion import fetch_completed_candles, require_non_empty
from trading_agent.data.market_data_public import TESTNET_HOST, BinancePublicMarketDataClient
from trading_agent.data.validation import validate_candle_sequence, validate_not_stale
from trading_agent.execution.client_order_id import generate_client_order_id
from trading_agent.execution.order_outcome import apply_order_result
from trading_agent.execution.order_validator import validate_order
from trading_agent.execution.reconciliation import ReconciledStatus, reconcile_order, safe_to_retry
from trading_agent.execution.startup_reconciliation import (
    reconcile_balances,
    reconcile_pending_orders,
)
from trading_agent.execution.testnet_adapter import (
    BinanceApiError,
    ClockDriftError,
    TestnetBrokerAdapter,
)
from trading_agent.journal.journal import Journal
from trading_agent.persistence.pending_orders_store import PendingOrdersStore
from trading_agent.persistence.portfolio_store import PortfolioStore
from trading_agent.persistence.risk_state_store import RiskState, RiskStateStore
from trading_agent.portfolio.state import PortfolioState
from trading_agent.risk.engine import RiskEngine
from trading_agent.risk.kill_switch import KillSwitch
from trading_agent.risk.limits import RiskContext, TradeIntent
from trading_agent.sizing.exchange_filters import SymbolFilters
from trading_agent.sizing.position_sizer import compute_sell_quantity
from trading_agent.strategy.base import SignalType
from trading_agent.strategy.trend_baseline import EmaCrossoverTrendStrategy

TESTNET_ENTRY_DISABLED_REASON = "TESTNET_AUTO_ENTRY_DISABLED_PENDING_PROTECTIVE_ORDER"


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


def _base_asset(symbol: str) -> str:
    return symbol[: -len(_quote_asset(symbol))]


def reconcile_initial_portfolio(
    adapter: TestnetBrokerAdapter, symbol: str, filters: SymbolFilters
) -> PortfolioState:
    quote_asset = _quote_asset(symbol)
    base_asset = _base_asset(symbol)
    balances = adapter.get_account_balances()
    quote_free, quote_locked = balances.get(quote_asset, (Decimal(0), Decimal(0)))
    base_free, base_locked = balances.get(base_asset, (Decimal(0), Decimal(0)))
    base_total = base_free + base_locked
    if base_total >= filters.min_qty:
        raise ColdStartReconciliationError(
            f"testnet account already holds {base_total} {base_asset}; cannot determine "
            "its cost basis from a balance query. Flatten the position manually via the "
            "Testnet UI before the first run."
        )
    return PortfolioState.initial(quote_free + quote_locked)


def _utc_day_key(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).strftime("%Y-%m-%d")


def run_testnet_cycle(
    config: AppConfig,
    secrets: Secrets,
    journal: Journal,
    portfolio_store: PortfolioStore,
    risk_state_store: RiskStateStore,
    pending_orders_store: PendingOrdersStore,
) -> CycleResult:
    symbol = config.market.symbol
    interval = config.market.interval
    quote_asset = _quote_asset(symbol)
    base_asset = _base_asset(symbol)
    kill_switch = KillSwitch(config.paths.data_dir / "KILL_SWITCH")
    public_client = BinancePublicMarketDataClient(TESTNET_HOST)
    adapter = TestnetBrokerAdapter(secrets.testnet_api_key, secrets.testnet_api_secret)

    try:
        server_time_ms = public_client.get_server_time_ms()
        adapter.sync_time(server_time_ms)
        candles = fetch_completed_candles(
            public_client, symbol, interval, limit=config.strategy.ema_slow + 10, reference_time_ms=server_time_ms
        )
        require_non_empty(candles)
        validate_candle_sequence(candles, interval)
        validate_not_stale(candles, server_time_ms, config.risk.stale_data_max_age_seconds)
        filters = SymbolFilters.from_exchange_info(public_client.get_exchange_info(symbol))
    except ClockDriftError as exc:
        journal.record("EXCEPTION", {"stage": "clock_sync", "error": str(exc)}, 0)
        return CycleResult("ERROR", "CLOCK_DRIFT_EXCEEDS_TOLERANCE", {"error": str(exc)})
    except Exception as exc:  # noqa: BLE001 - any data problem blocks trading this cycle
        journal.record("EXCEPTION", {"stage": "data_fetch", "error": str(exc)}, 0)
        return CycleResult("NO_TRADE", "DATA_UNAVAILABLE_OR_INVALID", {"error": str(exc)})

    last_candle = candles[-1]

    portfolio = portfolio_store.load(symbol)
    if portfolio is None:
        portfolio = reconcile_initial_portfolio(adapter, symbol, filters)
        portfolio_store.save(symbol, portfolio, server_time_ms)

    # --- Finding 2: resolve anything left unresolved by a previous (possibly
    # crashed) run BEFORE ever generating a new signal. ---
    pending_result = reconcile_pending_orders(
        adapter, symbol, quote_asset, pending_orders_store, portfolio,
        Decimal(str(config.fees.taker_fee_pct)), journal, server_time_ms,
    )
    portfolio = pending_result.portfolio
    portfolio_store.save(symbol, portfolio, server_time_ms)
    if pending_result.blocked:
        return CycleResult("NO_TRADE", "UNRESOLVED_ORDER_BLOCKS_NEW_SIGNAL", {"reason": pending_result.blocked_reason})

    # --- Finding 5: continuous balance reconciliation, every cycle. ---
    balances_ok, balances_reason = reconcile_balances(adapter, quote_asset, base_asset, portfolio)
    if not balances_ok:
        journal.record("RECONCILIATION_DISCREPANCY", {"reason": balances_reason}, server_time_ms)
    reconciliation_blocked = not balances_ok

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
        # Finding 4: automatic entry is disabled on Testnet in this revision.
        journal.record(
            "SIGNAL_SUPPRESSED",
            {"reason": TESTNET_ENTRY_DISABLED_REASON},
            last_candle.close_time_ms,
        )
        risk_state_store.save(symbol, risk_state)
        return CycleResult("NO_TRADE", TESTNET_ENTRY_DISABLED_REASON, {})

    side: Literal["SELL"] = "SELL"
    sizing = compute_sell_quantity(portfolio.base_balance, last_candle.close, filters)

    journal.record(
        "SIZING", {"approved": sizing.approved, "reason_code": sizing.reason_code}, last_candle.close_time_ms
    )
    if not sizing.approved or sizing.quantity is None:
        risk_state_store.save(symbol, risk_state)
        return CycleResult("NO_TRADE", sizing.reason_code, {})

    client_order_id = generate_client_order_id(symbol, side, last_candle.close_time_ms)
    already_submitted = pending_orders_store.get(client_order_id) is not None or any(
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
        reconciliation_blocked=reconciliation_blocked,
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

    # Finding 2: durably record the intent to submit BEFORE calling the
    # exchange, so a crash at any point after this line is recoverable.
    pending_orders_store.create(client_order_id, symbol, side, quantity, last_candle.close_time_ms, server_time_ms)
    journal.record(
        "ORDER_SUBMITTED",
        {"client_order_id": client_order_id, "side": side, "quantity": str(quantity)},
        last_candle.close_time_ms,
    )

    try:
        order = adapter.place_market_order(symbol, side, quantity, client_order_id)
    except requests.exceptions.RequestException:
        # Finding 9: ANY ambiguous network failure (timeout, connection
        # reset, DNS failure, ...) - not just Timeout - means we don't know
        # whether Binance received this order. Reconcile before retrying.
        status, reconciled_order = reconcile_order(adapter, symbol, client_order_id)
        journal.record("RECONCILIATION", {"status": status.value}, last_candle.close_time_ms)
        if safe_to_retry(status):
            try:
                order = adapter.place_market_order(symbol, side, quantity, client_order_id)
            except (requests.exceptions.RequestException, BinanceApiError) as exc:
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

    # Finding 1: dispatch on the order's ACTUAL status. Never substitute the
    # requested quantity for a missing/zero executed_qty.
    outcome = apply_order_result(order, side, quote_asset, portfolio, Decimal(0), Decimal(str(config.fees.taker_fee_pct)))
    portfolio = outcome.portfolio
    journal.record(outcome.journal_event_type, outcome.journal_payload, last_candle.close_time_ms)

    if outcome.is_terminal:
        pending_orders_store.mark_resolved(client_order_id, order.status, server_time_ms)
    else:
        pending_orders_store.update_applied_qty(client_order_id, outcome.newly_applied_qty)

    if outcome.newly_applied_qty > 0 or outcome.is_terminal:
        risk_state.trades_today += 1
    if outcome.realized_pnl_delta is not None:
        if risk_state.daily_start_equity > 0:
            risk_state.daily_realized_pnl_pct += float(outcome.realized_pnl_delta / risk_state.daily_start_equity)
        if outcome.realized_pnl_delta < 0:
            risk_state.cooldown_bars_remaining = config.risk.cooldown_bars_after_loss

    portfolio_store.save(symbol, portfolio, server_time_ms)
    risk_state_store.save(symbol, risk_state)

    if not outcome.is_terminal:
        return CycleResult("NO_TRADE", "ORDER_STILL_OPEN", {"status": order.status, "client_order_id": client_order_id})
    if outcome.newly_applied_qty == 0:
        return CycleResult("NO_TRADE", order.status, {"client_order_id": client_order_id})

    return CycleResult(
        side,
        order.status,
        {"quantity": str(outcome.newly_applied_qty), "client_order_id": client_order_id},
    )
