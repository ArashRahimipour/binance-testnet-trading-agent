"""Computes what happened NEW to an order since the last time its
cumulative execution was applied - a pure function with no I/O, safe to
call from inside a single atomic database transaction
(persistence/execution_store.py).

Review Finding 3: deltas are computed directly from Binance's own
cumulative `executed_qty`/`cumulative_quote_qty` fields (never from a
caller-recomputed running total, and never via proportional estimation
across fills at different prices - the cumulative fields are exact).
Binance's cumulative fields must never decrease between observations;
if they do, that is an inconsistent report and must be rejected, not
silently applied.

Review Finding 1: the requested/intended quantity is never substituted
for a missing or zero `executed_qty` - only Binance's own reported
numbers are ever applied, and only the delta beyond what was already
applied, so re-observing the same order never double-counts a fill.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from trading_agent.execution.fees import compute_commission_buckets
from trading_agent.execution.testnet_adapter import OrderResult
from trading_agent.portfolio.state import PortfolioState, apply_fill_delta

TERMINAL_STATUSES = {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "NEVER_SUBMITTED"}
OPEN_STATUSES = {"NEW", "PARTIALLY_FILLED"}

# NEVER_SUBMITTED is not a real Binance status - it's used internally by
# startup_reconciliation.py to close out a pending order that a NOT_FOUND
# (-2013) reconciliation confirmed never reached the exchange at all.
_JOURNAL_EVENT_BY_STATUS = {
    "NEW": "ORDER_OPEN",
    "PARTIALLY_FILLED": "ORDER_PARTIALLY_FILLED",
    "FILLED": "ORDER_FILLED",
    "CANCELED": "ORDER_CANCELED",
    "REJECTED": "ORDER_REJECTED",
    "EXPIRED": "ORDER_EXPIRED",
    "NEVER_SUBMITTED": "ORDER_NEVER_SUBMITTED",
}


class InconsistentExecutionReportError(Exception):
    """Raised when Binance's cumulative fields would decrease, or are
    otherwise internally inconsistent, relative to what was already
    applied. Must never be silently accepted."""


@dataclass(frozen=True, slots=True)
class OrderApplicationResult:
    portfolio: PortfolioState
    is_terminal: bool
    new_applied_executed_qty: Decimal
    new_applied_cumulative_quote_qty: Decimal
    new_applied_commission_quote: Decimal
    new_applied_commission_base: Decimal
    new_applied_commission_other: dict[str, Decimal]
    journal_event_type: str
    journal_payload: dict
    realized_pnl_delta: Decimal | None


def compute_order_application(
    order: OrderResult,
    side: Literal["BUY", "SELL"],
    quote_asset: str,
    base_asset: str,
    portfolio: PortfolioState,
    applied_executed_qty: Decimal,
    applied_cumulative_quote_qty: Decimal,
    applied_commission_quote: Decimal,
    applied_commission_base: Decimal,
    applied_commission_other: dict[str, Decimal],
    fallback_fee_pct: Decimal,
) -> OrderApplicationResult:
    status = order.status
    is_terminal = status in TERMINAL_STATUSES
    event_type = _JOURNAL_EVENT_BY_STATUS.get(status, "ORDER_UNKNOWN_STATUS")

    if order.executed_qty < applied_executed_qty:
        raise InconsistentExecutionReportError(
            f"executed_qty {order.executed_qty} is less than already-applied {applied_executed_qty} "
            f"for order {order.client_order_id}"
        )
    if order.cumulative_quote_qty < applied_cumulative_quote_qty:
        raise InconsistentExecutionReportError(
            f"cumulative_quote_qty {order.cumulative_quote_qty} is less than already-applied "
            f"{applied_cumulative_quote_qty} for order {order.client_order_id}"
        )
    if (order.executed_qty == 0) != (order.cumulative_quote_qty == 0):
        raise InconsistentExecutionReportError(
            f"executed_qty={order.executed_qty} and cumulative_quote_qty={order.cumulative_quote_qty} "
            f"are inconsistent with each other for order {order.client_order_id}"
        )

    if status == "NEW":
        return OrderApplicationResult(
            portfolio, False, applied_executed_qty, applied_cumulative_quote_qty,
            applied_commission_quote, applied_commission_base, applied_commission_other,
            event_type, {"status": status, "order_id": order.order_id}, None,
        )

    delta_base_qty = order.executed_qty - applied_executed_qty
    delta_quote_qty = order.cumulative_quote_qty - applied_cumulative_quote_qty

    if delta_base_qty <= 0:
        return OrderApplicationResult(
            portfolio, is_terminal, applied_executed_qty, applied_cumulative_quote_qty,
            applied_commission_quote, applied_commission_base, applied_commission_other,
            event_type, {"status": status, "order_id": order.order_id, "executed_qty": str(order.executed_qty)}, None,
        )

    commission_quote_delta, commission_base_delta, commission_other_delta, is_estimated = compute_commission_buckets(
        order.fills, quote_asset, base_asset, fallback_notional=delta_quote_qty, fallback_fee_pct=fallback_fee_pct
    )

    realized_pnl_delta: Decimal | None = None
    if side == "BUY":
        new_portfolio = apply_fill_delta(
            portfolio, "BUY", delta_base_qty, delta_quote_qty,
            commission_quote_delta, commission_base_delta, is_estimated,
        )
    else:
        pnl_before = portfolio.realized_pnl_quote
        new_portfolio = apply_fill_delta(
            portfolio, "SELL", delta_base_qty, delta_quote_qty,
            commission_quote_delta, commission_base_delta, is_estimated,
        )
        realized_pnl_delta = new_portfolio.realized_pnl_quote - pnl_before

    merged_other = dict(applied_commission_other)
    for asset, amount in commission_other_delta.items():
        merged_other[asset] = merged_other.get(asset, Decimal(0)) + amount

    return OrderApplicationResult(
        portfolio=new_portfolio,
        is_terminal=is_terminal,
        new_applied_executed_qty=order.executed_qty,
        new_applied_cumulative_quote_qty=order.cumulative_quote_qty,
        new_applied_commission_quote=applied_commission_quote + commission_quote_delta,
        new_applied_commission_base=applied_commission_base + commission_base_delta,
        new_applied_commission_other=merged_other,
        journal_event_type=event_type,
        journal_payload={
            "status": status,
            "order_id": order.order_id,
            "applied_qty": str(delta_base_qty),
            "applied_quote": str(delta_quote_qty),
            "commission_quote": str(commission_quote_delta),
            "commission_base": str(commission_base_delta),
            "commission_other": {k: str(v) for k, v in commission_other_delta.items()},
            "fee_is_estimated": is_estimated,
        },
        realized_pnl_delta=realized_pnl_delta,
    )
