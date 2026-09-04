"""Applies a Binance order result to portfolio state, handling every
possible order status distinctly and exactly once.

This is the single place that decides what an order's status means for
local state. It is used both by the main testnet decision cycle (right
after submitting an order) and by startup reconciliation
(startup_reconciliation.py), so a given status is always handled the same
way regardless of when it is observed - today or after a crash and
restart.

Hard rule, per the review finding this module exists to fix: the
requested/intended quantity is NEVER substituted for a missing or zero
`executed_qty`. Only Binance's own reported `executed_qty` and
`cumulative_quote_qty` are ever applied, and only the portion not already
applied in a previous call (`previously_applied_qty`) - so re-observing
the same order (e.g. on a later reconciliation pass) never double-counts
a fill.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from trading_agent.execution.fees import (
    compute_confirmed_fee_for_first_application,
    estimate_fee_for_incremental_fill,
)
from trading_agent.execution.testnet_adapter import OrderResult
from trading_agent.portfolio.state import (
    PortfolioState,
    apply_buy,
    apply_partial_fill_increase,
    apply_sell,
)

TERMINAL_STATUSES = {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}
OPEN_STATUSES = {"NEW", "PARTIALLY_FILLED"}

_JOURNAL_EVENT_BY_STATUS = {
    "NEW": "ORDER_OPEN",
    "PARTIALLY_FILLED": "ORDER_PARTIALLY_FILLED",
    "FILLED": "ORDER_FILLED",
    "CANCELED": "ORDER_CANCELED",
    "REJECTED": "ORDER_REJECTED",
    "EXPIRED": "ORDER_EXPIRED",
}


@dataclass(frozen=True, slots=True)
class OrderOutcome:
    portfolio: PortfolioState
    is_terminal: bool
    newly_applied_qty: Decimal  # the DELTA quantity applied by this call - never the full requested qty
    journal_event_type: str
    journal_payload: dict
    realized_pnl_delta: Decimal | None  # only set when this call applied part of a SELL


def apply_order_result(
    order: OrderResult,
    side: Literal["BUY", "SELL"],
    quote_asset: str,
    portfolio: PortfolioState,
    previously_applied_qty: Decimal,
    fallback_fee_pct: Decimal,
) -> OrderOutcome:
    status = order.status
    is_terminal = status in TERMINAL_STATUSES
    event_type = _JOURNAL_EVENT_BY_STATUS.get(status, "ORDER_UNKNOWN_STATUS")

    if status == "NEW":
        # Accepted, nothing executed yet - the portfolio must not change.
        return OrderOutcome(
            portfolio, False, Decimal(0), event_type,
            {"status": status, "order_id": order.order_id}, None,
        )

    delta_qty = order.executed_qty - previously_applied_qty
    if delta_qty <= 0:
        # No new execution to apply (e.g. CANCELED/REJECTED with zero fill,
        # or a status we've already fully applied).
        return OrderOutcome(
            portfolio, is_terminal, Decimal(0), event_type,
            {"status": status, "order_id": order.order_id, "executed_qty": str(order.executed_qty)}, None,
        )

    fraction_of_order = delta_qty / order.executed_qty if order.executed_qty > 0 else Decimal(1)
    delta_quote = order.cumulative_quote_qty * fraction_of_order
    fill_price = delta_quote / delta_qty

    if previously_applied_qty == 0:
        fee_quote, fee_is_estimated = compute_confirmed_fee_for_first_application(
            order.fills, quote_asset, fallback_notional=delta_quote, fallback_fee_pct=fallback_fee_pct
        )
    else:
        fee_quote, fee_is_estimated = estimate_fee_for_incremental_fill(delta_quote, fallback_fee_pct)

    realized_pnl_delta: Decimal | None = None
    if side == "BUY":
        if previously_applied_qty == 0:
            new_portfolio = apply_buy(portfolio, delta_qty, fill_price, fee_quote, fee_is_estimated)
        else:
            new_portfolio = apply_partial_fill_increase(portfolio, delta_qty, fill_price, fee_quote, fee_is_estimated)
    else:
        pnl_before = portfolio.realized_pnl_quote
        new_portfolio = apply_sell(portfolio, delta_qty, fill_price, fee_quote, fee_is_estimated)
        realized_pnl_delta = new_portfolio.realized_pnl_quote - pnl_before

    return OrderOutcome(
        portfolio=new_portfolio,
        is_terminal=is_terminal,
        newly_applied_qty=delta_qty,
        journal_event_type=event_type,
        journal_payload={
            "status": status,
            "order_id": order.order_id,
            "applied_qty": str(delta_qty),
            "fill_price": str(fill_price),
            "fee_quote": str(fee_quote),
            "fee_is_estimated": fee_is_estimated,
        },
        realized_pnl_delta=realized_pnl_delta,
    )
