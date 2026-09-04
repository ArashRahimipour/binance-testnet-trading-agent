"""Position sizing: fixed-fractional of available quote balance.

Two hard rules enforced here:
  - A computed size that fails the exchange's minimum notional/lot-size
    filters after rounding down is REJECTED, never bumped up to meet the
    minimum (bumping up would silently exceed the configured risk budget).
  - A sell can never be sized above the quantity actually held - rounding
    is always downward, so the returned quantity is always <= the input
    held quantity.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trading_agent.sizing.exchange_filters import (
    SymbolFilters,
    meets_lot_size,
    meets_min_notional,
    round_quantity,
)


@dataclass(frozen=True, slots=True)
class SizingDecision:
    approved: bool
    quantity: Decimal | None
    reason_code: str


def compute_buy_quantity(
    available_quote_balance: Decimal,
    price: Decimal,
    max_allocation_pct: float,
    min_quote_buffer: Decimal,
    filters: SymbolFilters,
) -> SizingDecision:
    if price <= 0:
        return SizingDecision(False, None, "INVALID_PRICE")

    spendable = available_quote_balance - min_quote_buffer
    if spendable <= 0:
        return SizingDecision(False, None, "BELOW_MIN_QUOTE_BUFFER")

    budget = spendable * Decimal(str(max_allocation_pct))
    raw_quantity = budget / price
    quantity = round_quantity(raw_quantity, filters)

    if not meets_lot_size(quantity, filters):
        return SizingDecision(False, None, "BELOW_MIN_LOT_SIZE")
    if not meets_min_notional(price, quantity, filters):
        return SizingDecision(False, None, "BELOW_MIN_NOTIONAL")

    return SizingDecision(True, quantity, "SIZED_OK")


def compute_risk_based_buy_quantity(
    equity: Decimal,
    entry_price: Decimal,
    stop_price: Decimal,
    max_risk_per_trade_pct: float,
    max_position_pct: float,
    filters: SymbolFilters,
) -> SizingDecision:
    """Size a position from a real stop-loss distance, not a notional guess.

    quantity = risk_budget / stop_distance, where risk_budget = equity *
    max_risk_per_trade_pct and stop_distance = entry_price - stop_price.
    That is the actual amount lost, per unit, if the stop is hit - this is
    what makes `max_risk_per_trade_pct` a genuine risk figure rather than a
    proxy for position size (contrast with the old notional-cap approach
    documented as a limitation in earlier revisions - see RISK_POLICY.md).

    `max_position_pct` is enforced as a SEPARATE notional ceiling on top of
    the risk-based size - a tight stop can legitimately justify a large
    notional for a small risk budget, but the position can still never
    exceed the configured maximum share of equity.

    As with all sizing in this project: rounds DOWN only, and REJECTS
    (never enlarges) a trade that fails an exchange minimum after rounding
    - including when satisfying the minimum would require risking more
    than the budget allows.
    """
    if entry_price <= 0 or stop_price <= 0:
        return SizingDecision(False, None, "INVALID_PRICE")
    stop_distance = entry_price - stop_price
    if stop_distance <= 0:
        return SizingDecision(False, None, "INVALID_STOP_DISTANCE")
    if equity <= 0:
        return SizingDecision(False, None, "INVALID_EQUITY")

    risk_budget = equity * Decimal(str(max_risk_per_trade_pct))
    risk_based_quantity = risk_budget / stop_distance

    max_notional = equity * Decimal(str(max_position_pct))
    position_cap_quantity = max_notional / entry_price

    quantity = round_quantity(min(risk_based_quantity, position_cap_quantity), filters)

    if not meets_lot_size(quantity, filters):
        return SizingDecision(False, None, "BELOW_MIN_LOT_SIZE")
    if not meets_min_notional(entry_price, quantity, filters):
        return SizingDecision(False, None, "BELOW_MIN_NOTIONAL")

    return SizingDecision(True, quantity, "SIZED_OK")


def compute_sell_quantity(held_quantity: Decimal, price: Decimal, filters: SymbolFilters) -> SizingDecision:
    quantity = round_quantity(held_quantity, filters)
    if quantity <= 0 or quantity > held_quantity:
        return SizingDecision(False, None, "NO_SELLABLE_QUANTITY")
    if not meets_lot_size(quantity, filters):
        return SizingDecision(False, None, "BELOW_MIN_LOT_SIZE")
    if not meets_min_notional(price, quantity, filters):
        return SizingDecision(False, None, "BELOW_MIN_NOTIONAL")
    return SizingDecision(True, quantity, "SIZED_OK")
