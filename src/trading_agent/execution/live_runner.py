"""One testnet decision cycle: reconcile, fetch data, decide, risk-check, execute.

This is invoked once per completed candle - by a person running the CLI
manually, or by an external scheduler (see README.md) - rather than
running its own polling loop. That keeps the process simple: it starts,
resolves anything left unresolved by a previous run, makes at most one
trade decision, records everything, and exits.

Testnet operation is OBSERVATIONAL in this revision (round 2 finding #7):
every cycle evaluates HOLD normally, but a BUY signal is always suppressed
- the agent cannot initiate a position on Testnet at all. SELL exists only
to close (or help recover) a position that already exists and has been
fully reconciled against the exchange - it is not a general trading path.
See RISK_POLICY.md's "Protective exits" section for why: this baseline
strategy has no verified exchange-resident stop-loss order on Testnet yet,
and shipping unexercised order-signing code for the one feature whose job
is capital protection is not a risk this project is willing to take
without first testing it against a live Testnet from an environment with
real network access.

Round 2 finding #2 correction: a SELL is never sized from a local base
balance that reconciliation has flagged as untrustworthy. This is
enforced twice, deliberately redundantly: the risk engine's universal
`reconciliation_blocked` gate (risk/engine.py) would reject the resulting
intent regardless, but this module does not even compute a sizing
decision from local state while blocked - it short-circuits before ever
calling `compute_sell_quantity`.

Known simplifications, documented rather than hidden:
  - Cold start requires the testnet account to be flat (no base asset
    balance beyond dust) - if the account already holds a position, we
    have no way to know its cost basis from a balance query alone, and
    guessing would corrupt PnL accounting. The operator must flatten
    manually via the Testnet UI first.
  - Fee accounting parses Binance's actual per-fill commission, bucketed
    by asset, when the order response includes `fills` (only the direct
    response to placing an order does - a reconciliation query never
    returns fills); otherwise it falls back to
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
from trading_agent.persistence.execution_store import ExecutionStateStore
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
RECONCILIATION_BLOCKS_SELL_REASON = "RECONCILIATION_DISCREPANCY_BLOCKS_ALL_ORDERS"


class ColdStartReconciliationError(Exception):
    """Raised when the testnet account is not flat and no cost basis can be established."""


@dataclass(frozen=True, slots=True)
class CycleResult:
    action: str  # "HOLD", "SELL", "NO_TRADE", "ERROR"
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
    execution_store: ExecutionStateStore,
    risk_state_store: RiskStateStore,
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

    portfolio = execution_store.load_portfolio(symbol)
    if portfolio is None:
        portfolio = reconcile_initial_portfolio(adapter, symbol, filters)
        execution_store.save_portfolio(symbol, portfolio, server_time_ms)

    # --- Finding 2 (round 2): resolve anything left unresolved by a previous
    # (possibly crashed) run BEFORE ever generating a new signal, atomically. ---
    pending_result = reconcile_pending_orders(
        adapter, symbol, quote_asset, base_asset, execution_store,
        Decimal(str(config.fees.taker_fee_pct)), journal, server_time_ms,
    )
    if pending_result.blocked:
        return CycleResult("NO_TRADE", "UNRESOLVED_ORDER_BLOCKS_NEW_SIGNAL", {"reason": pending_result.blocked_reason})

    portfolio = execution_store.load_portfolio(symbol)  # reload - reconciliation may have changed it
    assert portfolio is not None, "portfolio was seeded above and reconciliation never deletes it"

    # --- Finding 5 (round 2): continuous balance reconciliation, every cycle. ---
    balances_ok, balances_reason = reconcile_balances(adapter, symbol, quote_asset, base_asset, execution_store)
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
        # Observational mode (Finding 7): automatic entry is disabled entirely.
        journal.record(
            "SIGNAL_SUPPRESSED",
            {"reason": TESTNET_ENTRY_DISABLED_REASON},
            last_candle.close_time_ms,
        )
        risk_state_store.save(symbol, risk_state)
        return CycleResult("NO_TRADE", TESTNET_ENTRY_DISABLED_REASON, {})

    # --- Finding 2 (round 2): never size a SELL from an untrusted local balance. ---
    if reconciliation_blocked:
        journal.record(
            "SIGNAL_SUPPRESSED",
            {"reason": RECONCILIATION_BLOCKS_SELL_REASON, "detail": balances_reason},
            last_candle.close_time_ms,
        )
        risk_state_store.save(symbol, risk_state)
        return CycleResult("NO_TRADE", RECONCILIATION_BLOCKS_SELL_REASON, {"reason": balances_reason})

    side: Literal["SELL"] = "SELL"
    sizing = compute_sell_quantity(portfolio.base_balance, last_candle.close, filters)

    journal.record(
        "SIZING", {"approved": sizing.approved, "reason_code": sizing.reason_code}, last_candle.close_time_ms
    )
    if not sizing.approved or sizing.quantity is None:
        risk_state_store.save(symbol, risk_state)
        return CycleResult("NO_TRADE", sizing.reason_code, {})

    client_order_id = generate_client_order_id(symbol, side, last_candle.close_time_ms)
    already_submitted = execution_store.get_pending(client_order_id) is not None or any(
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

    # Round 2 finding #1: durably record the intent to submit BEFORE calling
    # the exchange, in the SAME database that will atomically apply the fill.
    execution_store.create_pending(client_order_id, symbol, side, quantity, last_candle.close_time_ms, server_time_ms)
    journal.record(
        "ORDER_SUBMITTED",
        {"client_order_id": client_order_id, "side": side, "quantity": str(quantity)},
        last_candle.close_time_ms,
    )

    try:
        order = adapter.place_market_order(symbol, side, quantity, client_order_id)
    except requests.exceptions.RequestException:
        # Any ambiguous network failure (not just Timeout) - reconcile before retrying.
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

    # Round 2 findings #1/#3/#4: one atomic transaction applies exactly the
    # new execution delta (from Binance's cumulative fields, commission
    # bucketed by asset) to portfolio state and the pending order's applied
    # fields together, or not at all.
    result = execution_store.apply_order_result_atomically(
        symbol, quote_asset, base_asset, order, Decimal(str(config.fees.taker_fee_pct)), server_time_ms
    )
    journal.record(result.journal_event_type, result.journal_payload, last_candle.close_time_ms)

    newly_applied_qty = result.new_applied_executed_qty  # this pending order started at applied=0
    if newly_applied_qty > 0 or result.is_terminal:
        risk_state.trades_today += 1
    if result.realized_pnl_delta is not None:
        if risk_state.daily_start_equity > 0:
            risk_state.daily_realized_pnl_pct += float(result.realized_pnl_delta / risk_state.daily_start_equity)
        if result.realized_pnl_delta < 0:
            risk_state.cooldown_bars_remaining = config.risk.cooldown_bars_after_loss

    risk_state_store.save(symbol, risk_state)

    if not result.is_terminal:
        return CycleResult("NO_TRADE", "ORDER_STILL_OPEN", {"status": order.status, "client_order_id": client_order_id})
    if newly_applied_qty == 0:
        return CycleResult("NO_TRADE", order.status, {"client_order_id": client_order_id})

    return CycleResult(
        side,
        order.status,
        {"quantity": str(newly_applied_qty), "client_order_id": client_order_id},
    )
