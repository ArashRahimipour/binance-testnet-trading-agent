"""Reconciliation run at the start of every testnet cycle (not just cold
start): resolve any order left unresolved by a previous - possibly
crashed - run, then compare local balances against the exchange's actual
free+locked balances. Both checks run BEFORE a new trading signal is ever
generated, and both fail closed: an unresolved order or an unexplained
balance mismatch blocks new entries (never exits, and never overwrites
local state with a guess) until repaired.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from trading_agent.execution.order_outcome import apply_order_result
from trading_agent.execution.reconciliation import ReconciledStatus, reconcile_order
from trading_agent.execution.testnet_adapter import TestnetBrokerAdapter
from trading_agent.journal.journal import Journal
from trading_agent.persistence.pending_orders_store import PendingOrdersStore
from trading_agent.portfolio.state import PortfolioState

# Small absolute tolerance for Decimal rounding noise accumulated across
# many fills - not a license to ignore a real discrepancy.
BALANCE_RECONCILIATION_TOLERANCE = Decimal("0.00000010")


@dataclass(frozen=True, slots=True)
class PendingOrderReconciliationResult:
    portfolio: PortfolioState
    blocked: bool
    blocked_reason: str | None


def reconcile_pending_orders(
    adapter: TestnetBrokerAdapter,
    symbol: str,
    quote_asset: str,
    store: PendingOrdersStore,
    portfolio: PortfolioState,
    fallback_fee_pct: Decimal,
    journal: Journal,
    now_ms: int,
) -> PendingOrderReconciliationResult:
    for pending in store.load_open(symbol):
        side: Literal["BUY", "SELL"] = "BUY" if pending.side == "BUY" else "SELL"
        status, order = reconcile_order(adapter, symbol, pending.client_order_id)

        if status == ReconciledStatus.NOT_FOUND:
            # This order never reached Binance - nothing to apply, safe to close out.
            store.mark_resolved(pending.client_order_id, "NEVER_SUBMITTED", now_ms)
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
                portfolio, True,
                f"unresolved order {pending.client_order_id}: reconciliation returned an unrecognized outcome",
            )

        outcome = apply_order_result(
            order, side, quote_asset, portfolio, pending.applied_executed_qty, fallback_fee_pct
        )
        portfolio = outcome.portfolio
        journal.record(
            outcome.journal_event_type,
            {"client_order_id": pending.client_order_id, **outcome.journal_payload},
            now_ms,
        )
        new_applied_qty = pending.applied_executed_qty + outcome.newly_applied_qty

        if outcome.is_terminal:
            store.mark_resolved(pending.client_order_id, order.status, now_ms)
        else:
            store.update_applied_qty(pending.client_order_id, new_applied_qty)
            return PendingOrderReconciliationResult(
                portfolio, True, f"order {pending.client_order_id} is still open ({order.status})"
            )

    return PendingOrderReconciliationResult(portfolio, False, None)


def reconcile_balances(
    adapter: TestnetBrokerAdapter,
    quote_asset: str,
    base_asset: str,
    portfolio: PortfolioState,
) -> tuple[bool, str | None]:
    """Compare local (quote_balance, base_balance) against exchange free+locked.

    Returns (ok, reason). Never overwrites local state - only reports
    whether it is trustworthy enough to open a NEW position; existing
    positions can still be closed regardless (see risk/engine.py).
    """
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
