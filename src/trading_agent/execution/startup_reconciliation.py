"""Reconciliation run at the start of every testnet cycle (not just cold
start): resolve any order left unresolved by a previous - possibly
crashed - run, then compare local balances against the exchange's actual
free+locked balances. Both checks run BEFORE a new trading signal is ever
generated, and both fail closed: an unresolved order or an unexplained
balance mismatch blocks ALL new order submission (see risk/engine.py's
universal `reconciliation_blocked` gate - round 2 finding #2 corrected
this from a buy-only check, since an untrusted local balance is exactly
as unsafe for sizing a SELL as a BUY) until repaired. Nothing here ever
overwrites local state with a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trading_agent.execution.reconciliation import ReconciledStatus, reconcile_order
from trading_agent.execution.testnet_adapter import TestnetBrokerAdapter
from trading_agent.journal.journal import Journal
from trading_agent.persistence.execution_store import ExecutionStateStore

# Small absolute tolerance for Decimal rounding noise accumulated across
# many fills - not a license to ignore a real discrepancy.
BALANCE_RECONCILIATION_TOLERANCE = Decimal("0.00000010")


@dataclass(frozen=True, slots=True)
class PendingOrderReconciliationResult:
    blocked: bool
    blocked_reason: str | None


def reconcile_pending_orders(
    adapter: TestnetBrokerAdapter,
    symbol: str,
    quote_asset: str,
    base_asset: str,
    store: ExecutionStateStore,
    fallback_fee_pct: Decimal,
    journal: Journal,
    now_ms: int,
) -> PendingOrderReconciliationResult:
    for pending in store.load_open_pending(symbol):
        status, order = reconcile_order(adapter, symbol, pending.client_order_id)

        if status == ReconciledStatus.NOT_FOUND:
            # This order never reached Binance - nothing to apply. There is
            # no fill to run through the atomic path; mark it resolved via
            # a synthetic zero-execution terminal report so the SAME code
            # path (and its consistency checks) closes it out.
            from trading_agent.execution.testnet_adapter import OrderResult

            synthetic = OrderResult(
                order_id=0, client_order_id=pending.client_order_id, status="NEVER_SUBMITTED",
                executed_qty=pending.applied_executed_qty, cumulative_quote_qty=pending.applied_cumulative_quote_qty,
                transact_time_ms=now_ms, fills=[], raw={},
            )
            store.apply_order_result_atomically(symbol, quote_asset, base_asset, synthetic, fallback_fee_pct, now_ms)
            journal.record(
                "RECONCILIATION_STARTUP",
                {"client_order_id": pending.client_order_id, "outcome": "NOT_FOUND"},
                now_ms,
            )
            continue

        if status == ReconciledStatus.UNKNOWN or order is None:
            journal.record(
                "RECONCILIATION_STARTUP",
                {"client_order_id": pending.client_order_id, "outcome": "UNKNOWN"},
                now_ms,
            )
            return PendingOrderReconciliationResult(
                True, f"unresolved order {pending.client_order_id}: reconciliation returned an unrecognized outcome"
            )

        result = store.apply_order_result_atomically(symbol, quote_asset, base_asset, order, fallback_fee_pct, now_ms)
        journal.record(
            result.journal_event_type,
            {"client_order_id": pending.client_order_id, **result.journal_payload},
            now_ms,
        )

        if not result.is_terminal:
            return PendingOrderReconciliationResult(
                True, f"order {pending.client_order_id} is still open ({order.status})"
            )

    return PendingOrderReconciliationResult(False, None)


def reconcile_balances(
    adapter: TestnetBrokerAdapter,
    symbol: str,
    quote_asset: str,
    base_asset: str,
    store: ExecutionStateStore,
) -> tuple[bool, str | None]:
    """Compare local (quote_balance, base_balance) against exchange free+locked.

    Returns (ok, reason). Never overwrites local state - only reports
    whether it is trustworthy enough to submit ANY new order. Reads the
    portfolio fresh from the store rather than trusting a caller-held
    copy, since pending-order reconciliation may just have changed it.
    """
    portfolio = store.load_portfolio(symbol)
    if portfolio is None:
        return False, f"no portfolio state for {symbol!r} to reconcile"

    balances = adapter.get_account_balances()
    exchange_quote_free, exchange_quote_locked = balances.get(quote_asset, (Decimal(0), Decimal(0)))
    exchange_base_free, exchange_base_locked = balances.get(base_asset, (Decimal(0), Decimal(0)))
    exchange_quote_total = exchange_quote_free + exchange_quote_locked
    exchange_base_total = exchange_base_free + exchange_base_locked

    quote_diff = abs(portfolio.quote_balance - exchange_quote_total)
    base_diff = abs(portfolio.base_balance - exchange_base_total)
    if quote_diff > BALANCE_RECONCILIATION_TOLERANCE or base_diff > BALANCE_RECONCILIATION_TOLERANCE:
        return False, (
            f"balance mismatch: local quote={portfolio.quote_balance} vs exchange={exchange_quote_total} "
            f"(diff {quote_diff}); local base={portfolio.base_balance} vs exchange={exchange_base_total} "
            f"(diff {base_diff})"
        )
    return True, None
