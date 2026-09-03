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


def compute_sell_quantity(held_quantity: Decimal, price: Decimal, filters: SymbolFilters) -> SizingDecision:
    quantity = round_quantity(held_quantity, filters)
    if quantity <= 0 or quantity > held_quantity:
        return SizingDecision(False, None, "NO_SELLABLE_QUANTITY")
    if not meets_lot_size(quantity, filters):
        return SizingDecision(False, None, "BELOW_MIN_LOT_SIZE")
    if not meets_min_notional(price, quantity, filters):
        return SizingDecision(False, None, "BELOW_MIN_NOTIONAL")
    return SizingDecision(True, quantity, "SIZED_OK")
